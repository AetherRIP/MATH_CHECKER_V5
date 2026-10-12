"""Definitive probe: can this machine talk to Google Classroom right now?

Reports only booleans / status strings. Never prints token or key values.
Run from the backend directory:  python scripts/auth_probe.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402

cfg = config.get_config(reload=True)
secrets = config.get_secrets()

client_path = config.resolve_oauth_client_path()
print("oauth client json present :", client_path.exists(), "->", client_path.name)
print("oauth client id present   :", bool(secrets.oauth_client_id()))
print("oauth refresh token stored:", bool(secrets.oauth_refresh_token()))
print("classroom.enabled         :", cfg.classroom.enabled)
print("classroom.write_draft_grades:", cfg.classroom.write_draft_grades)
print("automation.enabled        :", cfg.automation.enabled)

if not client_path.exists():
    print("VERDICT: no OAuth client file -> Classroom unreachable")
    raise SystemExit(0)

try:
    from app.classroom import auth as cauth

    print("connected helper           :", cauth.is_connected())
    tok = cauth.get_access_token()
    print("access token acquired      :", bool(tok), "len", len(tok or ""))
except Exception as exc:  # noqa: BLE001
    print("access token FAILED        :", type(exc).__name__, str(exc)[:200])
    print("VERDICT: Classroom API not reachable from this environment")
    raise SystemExit(0)

try:
    from app.classroom import client as cclient

    courses = cclient.list_courses()
    print("courses fetched            :", len(courses))
    for c in courses[:5]:
        print("   -", c.get("id"), c.get("name"))
except Exception as exc:  # noqa: BLE001
    print("course list FAILED         :", type(exc).__name__, str(exc)[:300])
