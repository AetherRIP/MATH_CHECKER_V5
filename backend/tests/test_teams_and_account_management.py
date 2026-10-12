"""Assistant teams (§1), account management (§2/§3) and the audit trail (§4).

Everything runs against the REAL FastAPI app and the real database. No mocking
of permissions: every allow/deny below is decided by the backend.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.distributed import accounts, audit, org, teams
from app.main import app


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    from app.distributed import schema
    schema.ensure_tables()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def dev(fresh_state):
    return accounts.create_account(username="dev", display_name="Dev",
                                   password="sup3rsafe", role="DEVELOPER",
                                   permissions=["*"])


def _auth(account: dict) -> dict:
    return {"Authorization": f"Bearer {accounts.issue_token(account['id'])}"}


def _result(school_id, class_id, submission="s1", version="v1"):
    return {"student_id": "stu1", "submission_id": submission,
            "submission_version": version, "raw_marks": 7, "max_marks": 9,
            "percentage": 77.8, "final_grade": 7, "correction_status": "done",
            "school_id": school_id, "class_id": class_id}


# ---------------------------------------------------------------------------
# §1: team sharing between authorized assistants
# ---------------------------------------------------------------------------

def test_two_assistants_in_the_same_team_share_reports(client, dev):
    school = org.create_school("School A")
    cls = org.create_class(school["id"], "10A")
    a = accounts.create_account(username="assist_a", display_name="Assist A",
                                password="Ass!stant1", role="ASSISTANT")
    b = accounts.create_account(username="assist_b", display_name="Assist B",
                                password="Ass!stant1", role="ASSISTANT")
    accounts.set_scope(a["id"], [{"school_id": school["id"], "class_id": None}])
    accounts.set_scope(b["id"], [{"school_id": school["id"], "class_id": None}])

    team = teams.create_team("School A Correction Team", school["id"],
                             created_by=dev["id"])
    teams.add_member(team["id"], a["id"], by=dev["id"])
    teams.add_member(team["id"], b["id"], by=dev["id"])

    # A corrects a submission on their own laptop.
    r = client.post("/api/distributed/results", json=_result(school["id"], cls["id"]),
                    headers=_auth(a))
    assert r.status_code == 200 and r.json()["duplicate"] is False

    # B sees it through the team, with A still named as the corrector.
    got = client.get("/api/distributed/results", headers=_auth(b)).json()["results"]
    assert len(got) == 1
    assert got[0]["account_id"] == a["id"]
    assert got[0]["payload"]["submission_id"] == "s1"

    # And the explicit team view agrees.
    tv = client.get(f"/api/distributed/teams/{team['id']}/results",
                    headers=_auth(b)).json()
    assert tv["member_count"] == 2 and len(tv["results"]) == 1


def test_assistants_in_unrelated_schools_cannot_see_each_other(client, dev):
    a_school = org.create_school("School A")
    b_school = org.create_school("School B")
    a_cls = org.create_class(a_school["id"], "10A")
    b_cls = org.create_class(b_school["id"], "10B")
    a = accounts.create_account(username="a1", display_name="A1",
                                password="Ass!stant1", role="ASSISTANT")
    b = accounts.create_account(username="b1", display_name="B1",
                                password="Ass!stant1", role="ASSISTANT")
    accounts.set_scope(a["id"], [{"school_id": a_school["id"]}])
    accounts.set_scope(b["id"], [{"school_id": b_school["id"]}])
    # Each is in their OWN school's team — never a shared one.
    team_a = teams.create_team("A Team", a_school["id"], created_by=dev["id"])
    team_b = teams.create_team("B Team", b_school["id"], created_by=dev["id"])
    teams.add_member(team_a["id"], a["id"], by=dev["id"])
    teams.add_member(team_b["id"], b["id"], by=dev["id"])

    client.post("/api/distributed/results", json=_result(a_school["id"], a_cls["id"]),
                headers=_auth(a))
    b_view = client.get("/api/distributed/results", headers=_auth(b)).json()["results"]
    assert b_view == []
    # And B cannot read A's team either.
    assert client.get(f"/api/distributed/teams/{team_a['id']}/results",
                      headers=_auth(b)).status_code == 403


def test_team_sharing_stops_immediately_when_a_member_is_removed(client, dev):
    school = org.create_school("School A")
    cls = org.create_class(school["id"], "10A")
    a = accounts.create_account(username="ra", display_name="RA",
                                password="Ass!stant1", role="ASSISTANT")
    b = accounts.create_account(username="rb", display_name="RB",
                                password="Ass!stant1", role="ASSISTANT")
    for acc in (a, b):
        accounts.set_scope(acc["id"], [{"school_id": school["id"]}])
    team = teams.create_team("T", school["id"], created_by=dev["id"])
    teams.add_member(team["id"], a["id"], by=dev["id"])
    teams.add_member(team["id"], b["id"], by=dev["id"])
    client.post("/api/distributed/results", json=_result(school["id"], cls["id"]),
                headers=_auth(a))
    assert len(client.get("/api/distributed/results",
                          headers=_auth(b)).json()["results"]) == 1
    # Remove B → access is gone at once, but A's record still exists.
    teams.remove_member(team["id"], b["id"], by=dev["id"])
    assert client.get("/api/distributed/results", headers=_auth(b)).json()["results"] == []
    assert len(client.get("/api/distributed/results",
                          headers=_auth(a)).json()["results"]) == 1


def test_a_new_install_discovers_its_team_after_linking(client, dev):
    school = org.create_school("School A")
    assistant = accounts.create_account(username="newbie", display_name="Newbie",
                                        password="Ass!stant1", role="ASSISTANT")
    accounts.set_scope(assistant["id"], [{"school_id": school["id"]}])
    team = teams.create_team("School A Team", school["id"], created_by=dev["id"])
    teams.add_member(team["id"], assistant["id"], by=dev["id"])

    # Freshly installed machine: unlinked, so it learns no team names.
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "newbie-device-01"}).json()
    assert reg["discovery"]["teams"] == []
    DEV = "newbie-device-01"
    H = {"X-Device-Id": DEV, "X-Device-Credential": reg["credential"]}

    # After the assistant's account links the device, the team appears.
    client.post(f"/api/distributed/devices/{DEV}/link",
                json={"account_id": assistant["id"]}, headers=_auth(dev))
    disc = client.get("/api/distributed/device/discovery", headers=H).json()
    assert [t["name"] for t in disc["teams"]] == ["School A Team"]
    assert disc["teams"][0]["member"] is True


def test_offline_results_sync_later_without_duplication(client, dev):
    school = org.create_school("School A")
    cls = org.create_class(school["id"], "10A")
    a = accounts.create_account(username="off_a", display_name="Off A",
                                password="Ass!stant1", role="ASSISTANT")
    accounts.set_scope(a["id"], [{"school_id": school["id"]}])
    # The same submission/version is posted twice (an offline replay).
    r1 = client.post("/api/distributed/results", json=_result(school["id"], cls["id"]),
                     headers=_auth(a)).json()
    r2 = client.post("/api/distributed/results", json=_result(school["id"], cls["id"]),
                     headers=_auth(a)).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    assert r1["record_id"] == r2["record_id"]
    rows = db.query("SELECT COUNT(*) n FROM correction_results_sync")[0]
    assert rows["n"] == 1          # exactly one record, no duplicate grade


# ---------------------------------------------------------------------------
# §2: account management permissions
# ---------------------------------------------------------------------------

def test_main_teacher_can_manage_co_teacher_and_assistant_accounts(client, dev):
    teacher = accounts.create_account(username="main", display_name="Main",
                                      password="T3ach!ng1", role="TEACHER",
                                      created_by=dev["id"])
    asst = accounts.create_account(username="helper", display_name="Helper",
                                   password="Ass!stant1", role="ASSISTANT")
    tok = _auth(teacher)
    # rename
    r = client.post(f"/api/distributed/accounts/{asst['id']}/username",
                    json={"username": "helper_renamed"}, headers=tok)
    assert r.status_code == 200 and r.json()["username"] == "helper_renamed"
    # profile
    assert client.post(f"/api/distributed/accounts/{asst['id']}/display-name",
                       json={"display_name": "Helper Two"}, headers=tok).status_code == 200
    # password reset (never viewing the old one)
    r = client.post(f"/api/distributed/accounts/{asst['id']}/password-reset",
                    json={"new_password": "N3wPass!ng"}, headers=tok)
    assert r.status_code == 200
    assert accounts.authenticate("helper_renamed", "N3wPass!ng") is not None
    assert accounts.authenticate("helper_renamed", "Ass!stant1") is None
    # the teacher can also create it in the first place
    r = client.post("/api/distributed/accounts",
                    json={"username": "another", "display_name": "Another",
                          "password": "Ass!stant2", "role": "ASSISTANT"},
                    headers=tok)
    assert r.status_code == 200


def test_main_teacher_cannot_modify_developer_or_another_main_teacher(client, dev):
    teacher = accounts.create_account(username="main", display_name="Main",
                                      password="T3ach!ng1", role="TEACHER")
    teacher2 = accounts.create_account(username="main2", display_name="Main2",
                                       password="T3ach!ng1", role="TEACHER")
    tok = _auth(teacher)
    for target in (dev, teacher2):
        r = client.post(f"/api/distributed/accounts/{target['id']}/password-reset",
                        json={"new_password": "H!jacked9"}, headers=tok)
        assert r.status_code == 403
    # The Developer account's password is untouched.
    assert accounts.authenticate("dev", "sup3rsafe") is not None
    # A teacher cannot mint a peer or a developer.
    for role in ("TEACHER", "DEVELOPER"):
        r = client.post("/api/distributed/accounts",
                        json={"username": f"x{role}", "display_name": "X",
                              "password": "Secret!123", "role": role},
                        headers=tok)
        assert r.status_code == 403


def test_a_developer_can_manage_every_account_type(client, dev):
    tok = _auth(dev)
    teacher = accounts.create_account(username="t", display_name="T",
                                      password="T3ach!ng1", role="TEACHER")
    co = accounts.create_account(username="c", display_name="C",
                                 password="C0!teacher", role="CO_TEACHER")
    asst = accounts.create_account(username="a", display_name="A",
                                   password="Ass!stant1", role="ASSISTANT")
    for target in (teacher, co, asst):
        r = client.post(f"/api/distributed/accounts/{target['id']}/display-name",
                        json={"display_name": "Renamed By Dev"}, headers=tok)
        assert r.status_code == 200
        r = client.post(f"/api/distributed/accounts/{target['id']}/password-reset",
                        json={"new_password": "Reset!Pass9"}, headers=tok)
        assert r.status_code == 200
    # role change is also allowed for the developer
    r = client.post(f"/api/distributed/accounts/{asst['id']}/roles",
                    json={"roles": ["ASSISTANT", "DEVELOPER"]}, headers=tok)
    assert r.status_code == 200
    assert set(r.json()["roles"]) == {"ASSISTANT", "DEVELOPER"}


def test_assistant_cannot_manage_other_accounts(client, dev):
    asst = accounts.create_account(username="asst", display_name="Asst",
                                   password="Ass!stant1", role="ASSISTANT")
    other = accounts.create_account(username="other", display_name="Other",
                                    password="Ass!stant1", role="ASSISTANT")
    tok = _auth(asst)
    for path, body in (
        (f"/api/distributed/accounts/{other['id']}/display-name", {"display_name": "X"}),
        (f"/api/distributed/accounts/{other['id']}/password-reset", {"new_password": "W0rm!ng9"}),
        (f"/api/distributed/accounts/{other['id']}/username", {"username": "z"}),
    ):
        assert client.post(path, json=body, headers=tok).status_code == 403
    # ...and cannot promote itself.
    r = client.post(f"/api/distributed/accounts/{asst['id']}/roles",
                    json={"roles": ["DEVELOPER"]}, headers=tok)
    assert r.status_code == 403
    assert "DEVELOPER" not in accounts.account_roles(accounts.get_account(asst["id"]))


# ---------------------------------------------------------------------------
# §3: username / password security
# ---------------------------------------------------------------------------

def test_username_change_validates_and_keeps_uniqueness(client, dev):
    a = accounts.create_account(username="uniquename", display_name="U",
                                password="Ass!stant1", role="ASSISTANT")
    other = accounts.create_account(username="taken", display_name="T",
                                    password="Ass!stant1", role="ASSISTANT")
    tok = _auth(dev)
    # Duplicate rejected.
    r = client.post(f"/api/distributed/accounts/{other['id']}/username",
                    json={"username": "uniquename"}, headers=tok)
    assert r.status_code == 400
    # Invalid rejected.
    r = client.post(f"/api/distributed/accounts/{other['id']}/username",
                    json={"username": "no spaces!"}, headers=tok)
    assert r.status_code == 400
    # Valid + unique accepted.
    r = client.post(f"/api/distributed/accounts/{other['id']}/username",
                    json={"username": "Valid_Name.1"}, headers=tok)
    assert r.status_code == 200 and r.json()["username"] == "valid_name.1"


def test_passwords_are_never_exposed_in_api_or_audit(client, dev):
    tok = _auth(dev)
    created = client.post("/api/distributed/accounts",
                          json={"username": "sec", "display_name": "Sec",
                                "password": "Sup3rS3cret!", "role": "ASSISTANT"},
                          headers=tok).json()
    aid = created["account_id"]
    client.post(f"/api/distributed/accounts/{aid}/password-reset",
                json={"new_password": "An0therS3cret!"}, headers=tok)

    # The accounts listing carries no hash/salt.
    listing = json.dumps(client.get("/api/distributed/accounts", headers=tok).json())
    assert "Sup3rS3cret" not in listing and "password_hash" not in listing
    # The audit trail contains no secret either.
    trail = json.dumps(client.get("/api/distributed/audit", headers=tok).json())
    assert "Sup3rS3cret" not in trail and "An0therS3cret" not in trail
    # And not in the database plaintext columns.
    row = db.query_one("SELECT password_hash, password_salt FROM accounts WHERE id = ?",
                       (aid,))
    assert "An0therS3cret" not in row["password_hash"] + row["password_salt"]


def test_password_reset_revokes_the_targets_existing_sessions(client, dev):
    asst = accounts.create_account(username="sessioned", display_name="S",
                                   password="Ass!stant1", role="ASSISTANT")
    victim_token = accounts.issue_token(asst["id"])
    assert accounts.resolve_token(victim_token) is not None
    r = client.post(f"/api/distributed/accounts/{asst['id']}/password-reset",
                    json={"new_password": "Fr3sh!Pass9"}, headers=_auth(dev))
    assert r.status_code == 200 and r.json()["sessions_revoked"] >= 1
    assert accounts.resolve_token(victim_token) is None


# ---------------------------------------------------------------------------
# §4: the Developer-visible audit trail
# ---------------------------------------------------------------------------

def test_every_account_change_writes_a_developer_visible_audit_event(client, dev):
    tok = _auth(dev)
    teacher = accounts.create_account(username="audited", display_name="Audited",
                                      password="T3ach!ng1", role="TEACHER",
                                      created_by=dev["id"])
    school = org.create_school("Audit School")
    client.post(f"/api/distributed/accounts/{teacher['id']}/display-name",
                json={"display_name": "Audited Two"}, headers=tok)
    client.post(f"/api/distributed/accounts/{teacher['id']}/password-reset",
                json={"new_password": "R3set!Pass9"}, headers=tok)
    team = teams.create_team("Audit Team", school["id"], created_by=dev["id"])
    teams.add_member(team["id"], teacher["id"], by=dev["id"])
    teams.remove_member(team["id"], teacher["id"], by=dev["id"])
    client.post(f"/api/distributed/accounts/{teacher['id']}/disable",
                json={"active": False}, headers=tok)

    trail = client.get("/api/distributed/audit", headers=tok).json()["audit"]
    actions = {e["action"] for e in trail}
    for expected in ("account.created", "account.profile_changed",
                     "account.password_reset", "team.member_added",
                     "team.member_removed", "account.deactivated"):
        assert expected in actions, f"missing audit action {expected}"
    # Structured fields the business requires.
    created = next(e for e in trail if e["action"] == "account.created")
    assert created["event_id"] and created["actor_account"] == dev["id"]
    assert created["actor_role"] == "DEVELOPER"
    assert created["affected_account"] == teacher["id"]
    assert created["outcome"] == "success"


def test_denied_attempts_are_recorded_and_can_be_filtered(client, dev):
    teacher = accounts.create_account(username="denier", display_name="D",
                                      password="T3ach!ng1", role="TEACHER")
    # A Teacher tries to reset the Developer's password → denied + logged.
    r = client.post(f"/api/distributed/accounts/{dev['id']}/password-reset",
                    json={"new_password": "N0pe!Pass12"}, headers=_auth(teacher))
    assert r.status_code == 403
    trail = client.get("/api/distributed/audit", headers=_auth(dev),
                       params={"outcome": "denied"}).json()["audit"]
    assert any(e["action"] == "account.password_reset_denied"
               and e["actor_account"] == teacher["id"] for e in trail)


def test_audit_search_and_filters(client, dev):
    school = org.create_school("Filter School")
    team = teams.create_team("Filter Team", school["id"], created_by=dev["id"])
    trail = client.get("/api/distributed/audit", headers=_auth(dev),
                       params={"q": "Filter Team"}).json()["audit"]
    assert any(e["team_id"] == team["id"] for e in trail)
    by_school = client.get("/api/distributed/audit", headers=_auth(dev),
                           params={"school_id": school["id"]}).json()["audit"]
    assert by_school and all(e["school_id"] == school["id"] for e in by_school)


def test_assistants_cannot_read_the_audit_trail(client, dev):
    asst = accounts.create_account(username="nosey", display_name="N",
                                   password="Ass!stant1", role="ASSISTANT")
    assert client.get("/api/distributed/audit", headers=_auth(asst)).status_code == 403


def test_audit_events_are_written_server_side_only(client, dev):
    """A client cannot insert, edit or delete its own audit history."""
    before = db.query_one("SELECT COUNT(*) n FROM audit_log")["n"]
    # No write method exists on the audit endpoint.
    forged = client.post("/api/distributed/audit", json={"action": "forged"},
                         headers=_auth(dev))
    assert forged.status_code in (404, 405)
    assert db.query_one("SELECT COUNT(*) n FROM audit_log")["n"] == before
    # Deleting/editing is likewise unavailable.
    assert client.delete("/api/distributed/audit",
                         headers=_auth(dev)).status_code in (404, 405)
    assert db.query_one("SELECT COUNT(*) n FROM audit_log")["n"] == before
