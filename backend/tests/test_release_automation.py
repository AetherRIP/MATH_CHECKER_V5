import time

from fastapi.testclient import TestClient
from app import db
from app.main import app
from app.automation import store
from app.distributed import accounts
from test_scheme_audit import _seed_assignment, _seed_result


def test_clear_activity_hides_only_visible_feed_and_preserves_records(fresh_state):
    store.log_event_entry('info', 'synthetic', 'Synthetic release event')
    assert store.recent_log()
    before = db.query_one('SELECT COUNT(*) AS n FROM automation_log')['n']
    with TestClient(app) as client:
        assert client.post('/api/automation/log/clear', json={}).status_code == 400
        response = client.post('/api/automation/log/clear', json={'confirm': True})
        assert response.status_code == 200
        assert response.json()['audit_preserved'] is True
        assert client.get('/api/automation/log').json()['entries'] == []
        assert db.query_one('SELECT COUNT(*) AS n FROM automation_log')['n'] >= before
        store.log_event_entry('info', 'new_event', 'New synthetic event')
        assert len(client.get('/api/automation/log').json()['entries']) == 1


def test_review_mode_hold_is_present_in_real_review_queue(fresh_state):
    _seed_assignment()
    _seed_result(result_id='r1', submission_id='s1', student_id='s1',
                 student_name='Synthetic', awarded=2, max_marks=3)
    now = time.time()
    db.execute("INSERT INTO automation_jobs(id, course_id, coursework_id, submission_id, "
               "submission_version, status, result_id, error_code, created_at, updated_at) "
               "VALUES('a1','c1','cw1','s1','v1','ATTENTION_REQUIRED','r1',"
               "'review_mode_await_teacher',?,?)", (now, now))
    with TestClient(app) as client:
        rows = client.get('/api/review').json()['items']
        assert len(rows) == 1 and rows[0]['automation_hold'] == 1


def test_unassigned_assistant_does_not_get_global_class_access(fresh_state):
    from app.distributed.schema import ensure_tables
    ensure_tables()
    assistant = accounts.create_account(username='isolated', display_name='Synthetic',
                                        password='test-only-safe-123', role='ASSISTANT', created_by=None)
    assert accounts.can_see_class(assistant, 'school', 'class') is False
