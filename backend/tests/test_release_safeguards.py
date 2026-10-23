import json

import pytest
from fastapi.testclient import TestClient

from app import auth_gate, db
from app.config import get_config
from app.engine import scheme_audit as sa
from app.main import app
from test_scheme_audit import _seed_assignment, _seed_result, _wrong_numeric_check


def stage():
    _seed_assignment()
    for n in (1, 2):
        _seed_result(result_id=f'r{n}', submission_id=f's{n}', student_id=f's{n}',
                     student_name=f'Synthetic {n}', awarded=0, max_marks=3,
                     scheme_check=_wrong_numeric_check())
    return sa.build_proposal(sa.list_discrepancies()[0]['id'])


def test_assistant_cannot_read_or_approve_global_bulk(fresh_state):
    proposal = stage()
    get_config().role = 'assistant'
    with TestClient(app) as client:
        assert client.get('/api/scheme/discrepancies').status_code == 403
        assert client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                           json={'confirm': True}).status_code == 403
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")['earned'] == 0


def test_reviewer_identity_cannot_be_spoofed(fresh_state, monkeypatch):
    proposal = stage()
    monkeypatch.setattr(auth_gate, 'identity_of', lambda request: {
        'role': 'TEACHER', 'account_id': 'real-reviewer', 'username': 'teacher'})
    with TestClient(app) as client:
        response = client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                               json={'confirm': True, 'actor': 'spoofed'})
        assert response.status_code == 200
        assert response.json()['decided_by'] == 'real-reviewer'


def test_override_after_staging_is_not_overwritten(fresh_state):
    proposal = stage()
    db.execute("UPDATE results SET manual_override=1 WHERE id='r2'")
    result = sa.apply_approved(proposal['id'], approved_by='teacher')
    assert result['status'] == sa.PROPOSAL_PARTIAL
    assert result['applied_count'] == result['failed_count'] == 1
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")['earned'] == 0


def test_failure_rolls_back_item_and_retry_only_recovers_failed(fresh_state, monkeypatch):
    proposal = stage()
    original = sa._apply_item

    def failure(conn, item, *args):
        answer = original(conn, item, *args)
        if item['result_id'] == 'r2':
            raise RuntimeError('Synthetic post-update failure')
        return answer

    monkeypatch.setattr(sa, '_apply_item', failure)
    result = sa.apply_approved(proposal['id'], approved_by='teacher')
    assert result['status'] == sa.PROPOSAL_PARTIAL
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")['earned'] == 0
    assert not db.query("SELECT * FROM result_versions WHERE result_id='r2'")
    monkeypatch.setattr(sa, '_apply_item', original)
    recovered = sa.apply_approved(proposal['id'], approved_by='recoverer', retry_failed=True)
    assert recovered['status'] == sa.PROPOSAL_APPLIED
    assert recovered['applied_count'] == 2 and recovered['failed_count'] == 0
    assert recovered['decided_by'] == 'teacher'
    assert len(db.query("SELECT * FROM result_versions WHERE result_id='r1'")) == 2
    assert len(db.query("SELECT * FROM result_versions WHERE result_id='r2'")) == 2


def test_changed_marks_after_staging_require_new_review(fresh_state):
    proposal = stage()
    row = db.query_one("SELECT result_json FROM results WHERE id='r2'")
    payload = json.loads(row['result_json'])
    payload['questions'][0]['awarded_marks'] = 1
    db.execute("UPDATE results SET earned=1, result_json=? WHERE id='r2'", (json.dumps(payload),))
    result = sa.apply_approved(proposal['id'], approved_by='teacher')
    assert result['failed_count'] == 1
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")['earned'] == 1


def test_assigned_grade_alone_does_not_prove_return(fresh_state, monkeypatch):
    from app.automation.runner import WorkflowRunner, StepFailure
    runner = WorkflowRunner()
    monkeypatch.setattr(runner, '_read_submission', lambda job: {'state': 'TURNED_IN', 'assignedGrade': 9})
    with pytest.raises(StepFailure, match='did not report'):
        runner._verify_returned({'submission_id': 'synthetic', 'grade': 9})


def test_verified_answer_but_zero_award_is_held_for_review(fresh_state):
    from app.engine.verify import verify_result
    proposal = stage()
    payload = json.loads(db.query_one("SELECT result_json FROM results WHERE id='r1'")['result_json'])
    verified = verify_result(payload)
    assert verified['status'] == 'REVIEW'
    assert '12' in verified['review_questions']


def test_all_failed_or_skipped_publication_is_not_success():
    assert sa.classroom_publish_state({'published': 0, 'failed': 2, 'total': 2}) == 'failed'
    assert sa.classroom_publish_state({'published': 0, 'skipped': 2, 'total': 2}) == 'failed'
