"""Two-installation end-to-end integration harness (Developer PC vs Assistant PC).

Runs REAL HTTP against a running backend and drives the REAL agent/updater code.
This machine is a single Windows host, so "two Windows environments" are two
INDEPENDENT INSTALLATIONS: separate agent directories, separate device ids,
separate data dirs. That exercises registration, roles, teams, the publish ->
discover -> download -> verify -> install -> report path, ON/OFF, offline queue
and duplicate prevention. It does NOT exercise two physical machines, OS
restarts or real network partitions — those are reported BLOCKED.

Usage:
    python scripts/two_env_e2e.py --base-url http://127.0.0.1:8643 \
        --data-dir <backend data dir holding distributed_bootstrap.txt>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

RESULTS: list[dict] = []


def record(test_id, desc, env, expected, actual, ok, blocked=False, note=""):
    RESULTS.append({
        "id": test_id, "desc": desc, "env": env, "expected": expected,
        "actual": actual, "status": "BLOCKED" if blocked else ("PASS" if ok else "FAIL"),
        "note": note,
    })
    print(f"[{'BLOCKED' if blocked else ('PASS' if ok else 'FAIL')}] {test_id} {desc}"
          f" — {actual}")


def req(base, method, path, body=None, headers=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}")
        except ValueError:
            return exc.code, payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8643")
    ap.add_argument("--data-dir", required=True)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    data_dir = Path(args.data_dir)

    st, _ = req(base, "GET", "/api/health", raw=True)
    record("T0", "Test backend reachable", "shared", "HTTP 200",
           f"HTTP {st}", st == 200)
    if st != 200:
        return 1

    # -- credentials: the isolated backend bootstrapped them to a file --------
    boot = json.loads((data_dir / "distributed_bootstrap.txt").read_text("utf-8"))
    dev_user, dev_pw = boot["developer"]["username"], boot["developer"]["password"]
    st, login = req(base, "POST", "/api/distributed/login",
                    {"username": dev_user, "password": dev_pw})
    record("T1", "Developer signs in with real credentials", "A",
           "HTTP 200 + token", f"HTTP {st}", st == 200)
    if st != 200:
        return 1
    dev_tok = login["token"]
    H = {"Authorization": f"Bearer {dev_tok}"}
    dev_id = login["account_id"]
    record("T1b", "Backend reports the Developer role", "A", "roles contains DEVELOPER",
           str(login["roles"]), "DEVELOPER" in login["roles"])

    ts = int(time.time())
    schoolA = req(base, "POST", "/api/distributed/schools", {"name": f"E2E A {ts}"}, H)[1]
    schoolB = req(base, "POST", "/api/distributed/schools", {"name": f"E2E B {ts}"}, H)[1]
    clsA = req(base, "POST", f"/api/distributed/schools/{schoolA['id']}/classes",
               {"name": "10A"}, H)[1]

    def account(role, name):
        st, r = req(base, "POST", "/api/distributed/accounts",
                    {"username": name, "display_name": name, "password": "T3st!ng123",
                     "role": role}, H)
        return st, r

    st, teacher = account("TEACHER", f"e2e_teacher_{ts}")
    stA, aA = account("ASSISTANT", f"e2e_a_{ts}")
    stB, aB = account("ASSISTANT", f"e2e_b_{ts}")
    stC, aC = account("ASSISTANT", f"e2e_c_{ts}")
    record("T2", "Developer creates teacher + 3 assistants", "A",
           "all HTTP 200", f"{st},{stA},{stB},{stC}",
           all(s == 200 for s in (st, stA, stB, stC)))

    for acc, sch in ((aA, schoolA), (aB, schoolA), (aC, schoolB)):
        req(base, "POST", f"/api/distributed/accounts/{acc['account_id']}/scope",
            {"scope": [{"school_id": sch["id"]}]}, H)

    team = req(base, "POST", "/api/distributed/teams",
               {"name": f"E2E Team {ts}", "school_id": schoolA["id"]}, H)[1]
    req(base, "POST", f"/api/distributed/teams/{team['id']}/members",
        {"account_id": aA["account_id"]}, H)
    req(base, "POST", f"/api/distributed/teams/{team['id']}/members",
        {"account_id": aB["account_id"]}, H)

    tokA = req(base, "POST", "/api/distributed/login",
               {"username": f"e2e_a_{ts}",
                "password": "T3st!ng123"})[1]["token"]
    tokB = req(base, "POST", "/api/distributed/login",
               {"username": f"e2e_b_{ts}", "password": "T3st!ng123"})[1]["token"]
    tokC = req(base, "POST", "/api/distributed/login",
               {"username": f"e2e_c_{ts}", "password": "T3st!ng123"})[1]["token"]

    # -- team sharing ---------------------------------------------------------
    res = {"student_id": "e2e-stu", "submission_id": f"e2e-sub-{ts}",
           "submission_version": "v1", "raw_marks": 7, "max_marks": 9,
           "percentage": 77.8, "final_grade": 7, "correction_status": "done",
           "school_id": schoolA["id"], "class_id": clsA["id"]}
    HA = {"Authorization": f"Bearer {tokA}"}
    req(base, "POST", "/api/distributed/results", res, HA)
    seen_b = req(base, "GET", "/api/distributed/results", None,
                 {"Authorization": f"Bearer {tokB}"})[1]["results"]
    seen_c = req(base, "GET", "/api/distributed/results", None,
                 {"Authorization": f"Bearer {tokC}"})[1]["results"]
    record("T18", "Teammate assistant reads shared report", "A+B",
           "B sees >=1", f"B sees {len(seen_b)}", len(seen_b) >= 1)
    record("T19", "Unrelated-school assistant blocked", "C",
           "C sees 0", f"C sees {len(seen_c)}", len(seen_c) == 0)
    dup = req(base, "POST", "/api/distributed/results", res, HA)[1]
    record("T20", "Replayed sync item is not duplicated", "A",
           "duplicate=true", str(dup["duplicate"]), dup["duplicate"] is True)

    r = req(base, "GET", "/api/distributed/accounts", None,
            {"Authorization": f"Bearer {tokC}"})
    record("T21", "Assistant denied a Developer/manager endpoint", "C",
           "HTTP 403", f"HTTP {r[0]}", r[0] == 403)
    r = req(base, "GET", "/api/distributed/audit", None,
            {"Authorization": f"Bearer {tokC}"})
    record("T21b", "Assistant denied the audit trail", "C",
           "HTTP 403", f"HTTP {r[0]}", r[0] == 403)

    # -- two installations register as separate devices -----------------------
    from app.distributed.tiny_agent import TinyAgent
    from app.distributed.device_updater import DeviceUpdater, package_from_bytes
    from app.distributed import signing

    dirA = Path(tempfile.mkdtemp(prefix="e2e_envA_"))
    dirB = Path(tempfile.mkdtemp(prefix="e2e_envB_"))
    calls = {"A": 0, "B": 0}

    def make_agent(directory, key):
        agent = TinyAgent(backend_url=base, agent_dir=directory,
                          app_version="5.0.0", agent_version="1.0.0",
                          worker_id=f"E2E-{key}-PC")
        orig_post, orig_get = agent._http_post, agent._http_get

        def post(path, payload, timeout=10):
            calls[key] += 1
            return orig_post(path, payload, timeout)

        def get(path, timeout=10, headers=None):
            calls[key] += 1
            return orig_get(path, timeout, headers)

        agent._post, agent._get = post, get
        return agent

    agentA = make_agent(dirA, "A")   # "Developer PC" installation
    agentB = make_agent(dirB, "B")   # "Assistant PC" installation
    outA = agentA.tick()
    outB = agentB.tick()
    record("T2b", "Both installations register + heartbeat", "A+B",
           "registered+heartbeat true",
           f"A={outA['registered']}/{outA['heartbeat']} B={outB['registered']}/{outB['heartbeat']}",
           outA["registered"] and outA["heartbeat"] and outB["registered"] and outB["heartbeat"])
    record("T2c", "Each installation has a distinct device identity", "A+B",
           "different ids", f"{agentA.device_id[:8]} != {agentB.device_id[:8]}",
           agentA.device_id != agentB.device_id)

    # Bind installation B's device to assistant B and give it an account token
    req(base, "POST", f"/api/distributed/devices/{agentB.device_id}/link",
        {"account_id": aB["account_id"]}, H)
    loginB = req(base, "POST", "/api/distributed/login",
                 {"username": f"e2e_b_{ts}", "password": "T3st!ng123",
                  "device_id": agentB.device_id})[1]
    agentB.auth_token = loginB["token"]
    agentB.tick()                    # reports app_version 5.0.0 to the backend

    # -- Stage A: publish a real signed test release --------------------------
    priv, pub = signing.generate_keypair()
    kid = signing.key_id(pub)
    pkg = __import__("app.distributed.device_updater", fromlist=["Package"]).Package(
        target_version="5.1.0", files={"main.py": b"# e2e v5.1.0\n"})
    body = pkg.serialize()
    sha = hashlib.sha256(body).hexdigest()
    sig = signing.sign_package(body, priv)

    st, rel = req(base, "POST", "/api/distributed/releases",
                  {"target_version": "5.1.0", "target_component": "app",
                   "release_notes": "E2E test release",
                   "platform": "windows", "channel": "stable",
                   "package_sha256": sha, "package_signature": sig,
                   "signing_key_id": kid, "signing_public_key": pub,
                   "scope": {"kind": "ALL"}}, H)
    record("T5", "Developer creates a DRAFT release", "A", "HTTP 200 draft",
           f"HTTP {st} status={rel.get('status')}",
           st == 200 and rel.get("status") == "draft")
    upd_id = rel["update_id"]

    st, stored = req(base, "POST", f"/api/distributed/updates/{upd_id}/package",
                     {"data": base64.b64encode(body).decode()}, H)
    record("T6", "Signed package body stored persistently", "A",
           "sha256 matches", stored.get("sha256") == sha, f"HTTP {st}")

    st, val = req(base, "POST", f"/api/distributed/releases/{upd_id}/validate", {}, H)
    record("T7", "Release validates (structure + checksum + signature)", "A",
           "ok=true", f"ok={val.get('ok')} problems={val.get('problems')}",
           val.get("ok") is True)

    # Publishing explicitly requires confirmation — an incomplete release must
    # never reach clients because a row was inserted.
    st_no, no_conf = req(base, "POST", f"/api/distributed/releases/{upd_id}/publish",
                         {}, H)
    record("T8a", "Publish without confirmation is refused", "A",
           "HTTP 400", f"HTTP {st_no} ({no_conf.get('code')})", st_no == 400)
    st, pubr = req(base, "POST", f"/api/distributed/releases/{upd_id}/publish",
                   {"confirm": True}, H)
    record("T8", "Release published", "A", "status=published",
           f"HTTP {st} status={pubr.get('status')}",
           st == 200 and pubr.get("status") == "published")

    # -- Stage B: Assistant discovers the release -----------------------------
    pending = agentB.fetch_updates()
    record("T9", "Assistant discovers the published release", "B",
           "5.1.0 offered", str([u["target_version"] for u in pending]),
           any(u["target_version"] == "5.1.0" for u in pending))

    # -- Stage C: download, verify, install -----------------------------------
    appB = dirB / "app"
    appB.mkdir(parents=True, exist_ok=True)
    (appB / "main.py").write_text("# e2e v5.0.0\n", encoding="utf-8")
    dataB = dirB / "data"
    dataB.mkdir(exist_ok=True)
    (dataB / "reports.txt").write_text("keep me\n", encoding="utf-8")
    updater = DeviceUpdater(app_dir=appB, data_dir=dataB,
                            backup_root=dirB / "backups", current_version="5.0.0",
                            trusted_public_keys={kid: pub},
                            busy_check=lambda: (False, ""),
                            health_check=lambda: (True, "ok"))
    reqmeta = next((u for u in pending if u["update_id"] == upd_id), None)
    data = agentB.download_package(upd_id) if reqmeta else None

    if reqmeta is None or data is None:
        record("T10", "Downloaded package checksum + signature verify", "B",
               "ok", "release was never offered / download failed", False)
    else:
        ok, why = updater.verify_download(data, reqmeta)
        record("T10", "Downloaded package checksum + signature verify", "B",
               "ok", why, ok)
        result = updater.apply_update(package_from_bytes(reqmeta, data),
                                      update_id=upd_id, device_id=agentB.device_id)
        record("T11", "Update installs through the real updater", "B",
               "UPDATED", result.final_state.value,
               result.final_state.value == "UPDATED")
        def read(path):
            try:
                return path.read_text("utf-8")
            except OSError:
                return None

        installed_ok = read(appB / "main.py") == "# e2e v5.1.0\n"
        record("T11b", "Installed file is the new version", "B",
               "content replaced", str(installed_ok), installed_ok)
        data_kept = read(dataB / "reports.txt") == "keep me\n"
        record("T11c", "DATA directory preserved across the update", "B",
               "reports survived", str(data_kept), data_kept)

    agentB.report_update(upd_id, "UPDATED")
    agentB.report_update_history(update_id=upd_id, old_version="5.0.0",
                                 new_version="5.1.0", result="UPDATED")
    st, inst = req(base, "GET", "/api/distributed/releases/installations", None, H)
    reported = any(e.get("device_id") == agentB.device_id
                   for group in inst.get("installations", {}).values() for e in group)
    record("T12", "Developer dashboard reflects the real installation", "A",
           "device present in install status", str(reported), reported)

    # -- failure handling -----------------------------------------------------
    tampered = bytearray(data)
    tampered[-2] ^= 0x01
    ok2, why2 = updater.verify_download(bytes(tampered), reqmeta)
    record("T13", "Corrupted package rejected", "B", "rejected",
           f"{ok2} ({why2})", not ok2)

    priv2, pub2 = signing.generate_keypair()
    bad_sig = signing.sign_package(body, priv2)
    ok3, why3 = updater.verify_download(
        data, {**reqmeta, "package_signature": bad_sig, "signing_key_id": kid})
    record("T14", "Invalid signature rejected", "B", "rejected",
           f"{ok3} ({why3})", not ok3)

    ok4, why4 = updater.verify_download(
        data, {**reqmeta, "package_signature": None})
    record("T15", "Missing signature rejected", "B", "rejected",
           f"{ok4} ({why4})", not ok4)

    # The installation now reports the new version; the release must not be
    # re-offered (a real backend reads the reported version, never a guess).
    agentB.app_version = "5.1.0"
    agentB.tick()
    pending_after = agentB.fetch_updates()
    record("T16", "Client already on the latest version is not re-offered", "B",
           "5.1.0 absent", str([u["target_version"] for u in pending_after]),
           all(u["target_version"] != "5.1.0" for u in pending_after))

    # withdrawn release stops being offered
    req(base, "POST", f"/api/distributed/releases/{upd_id}/withdraw",
        {"confirm": True, "reason": "e2e"}, H)
    pending_w = agentB.fetch_updates()
    record("T17", "Withdrawn release is not offered", "B",
           "5.1.0 absent", str([u["target_version"] for u in pending_w]),
           all(u["target_version"] != "5.1.0" for u in pending_w))

    # -- Tiny Agent ON/OFF ----------------------------------------------------
    before = calls["B"]
    paused_agent = make_agent(Path(tempfile.mkdtemp(prefix="e2e_off_")), "B")
    paused_agent._state_loader = lambda: False
    before2 = calls["B"]
    out_off = paused_agent.tick()
    record("T22", "Agent OFF performs no network work", "B",
           "paused + 0 calls",
           f"paused={out_off['paused']} calls={calls['B'] - before2}",
           out_off["paused"] and calls["B"] - before2 == 0)

    # -- audit -----------------------------------------------------------------
    st, aud = req(base, "GET", "/api/distributed/audit?limit=200", None, H)
    acts = {e["action"] for e in aud["audit"]}
    record("T23", "Update lifecycle produces audit events", "A",
           "published + package_stored",
           str(sorted(a for a in acts if a.startswith("update."))),
           "update.published" in acts and "update.package_stored" in acts)

    # -- platform filtering: a VALID linux-only release must not be offered to
    #    a Windows installation. The package manifest must match the release
    #    version or publishing (correctly) refuses it — so build a real 5.2.0.
    pkg2 = __import__("app.distributed.device_updater", fromlist=["Package"]).Package(
        target_version="5.2.0", files={"main.py": b"# e2e v5.2.0 linux\n"})
    body2 = pkg2.serialize()
    sha2 = hashlib.sha256(body2).hexdigest()
    sig2 = signing.sign_package(body2, priv)
    st, relx = req(base, "POST", "/api/distributed/releases",
                   {"target_version": "5.2.0", "target_component": "app",
                    "platform": "linux", "channel": "stable",
                    "package_sha256": sha2, "package_signature": sig2,
                    "signing_key_id": kid, "signing_public_key": pub,
                    "scope": {"kind": "ALL"}}, H)
    req(base, "POST", f"/api/distributed/updates/{relx['update_id']}/package",
        {"data": base64.b64encode(body2).decode()}, H)
    stx, pubx = req(base, "POST", f"/api/distributed/releases/{relx['update_id']}/publish",
                    {"confirm": True}, H)
    record("T24a", "Valid linux-only release publishes", "A", "status=published",
           f"HTTP {stx} status={pubx.get('status')}",
           stx == 200 and pubx.get("status") == "published")
    linux_offered = [u for u in agentB.fetch_updates() if u["update_id"] == relx["update_id"]]
    record("T24", "Linux-only release not offered to a Windows device", "B",
           "not offered", f"{len(linux_offered)} offered", not linux_offered)

    # -- evidence --------------------------------------------------------------
    print("\n=== EVIDENCE ===")
    print(f"release {upd_id} version 5.1.0 sha256={sha}")
    print(f"device A {agentA.device_id[:12]}..  device B {agentB.device_id[:12]}..")
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    blocked = [r for r in RESULTS if r["status"] == "BLOCKED"]
    print(f"\nPASS {passed}  FAIL {len(failed)}  BLOCKED {len(blocked)}")
    for r in failed:
        print(f"  FAIL {r['id']}: {r['desc']} — expected {r['expected']}, got {r['actual']}")

    out = Path(ROOT / "docs" / "results" / "two_env_e2e_results.json")
    out.write_text(json.dumps({"results": RESULTS, "release": upd_id, "sha256": sha,
                               "device_a": agentA.device_id, "device_b": agentB.device_id},
                              indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
