"""Verify portable update in a disposable copy; never touches production data."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('verify', ROOT/'MathChecker_Distribution/scripts/verify_portable.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)
archive = sorted((ROOT/'MathChecker_Distribution/previous_builds').glob('*/MathChecker_Portable.zip'))[0]
work = Path(tempfile.mkdtemp(prefix='Math Checker Update isolated '))
with zipfile.ZipFile(archive) as z: z.extractall(work)
package = work/'MathChecker_Portable'
port = v.free_port()
report = v.Report(ROOT/'docs/logs/FINAL_UPDATE_VERIFY.txt')
instance = v.Instance(package, port, work/'launcher.log')
try:
    instance.start()
    assert v.wait_health(port), 'old package not ready'
    session = v.sign_in(port, package/'Data')
    assert session, 'old package sign-in failed'
    assert instance.stop_with_launcher(), 'old package stop failed'
    with sqlite3.connect(package/'Data/app.db') as c:
        c.execute("INSERT INTO students(id,name,fetched_at) VALUES('release-update-student','Synthetic update student',0)")
        c.commit()
    shutil.copytree(package/'Data', work/'Data-backup')
    with zipfile.ZipFile(ROOT/'MathChecker_Distribution/release/MathChecker_Portable.zip') as z:
        for info in z.infolist():
            rel = Path(info.filename).parts
            if len(rel)>1 and rel[1]=='Data': continue
            z.extract(info,work)
    instance.start()
    assert v.wait_health(port), 'updated package not ready'
    session = v.sign_in(port,package/'Data')
    report.check('same host account signs in after update',bool(session),'existing synthetic account')
    with sqlite3.connect(package/'Data/app.db') as c:
        report.check('synthetic student survives update',c.execute("SELECT COUNT(*) FROM students WHERE id='release-update-student'").fetchone()[0]==1)
        report.check('updated database integrity',c.execute('PRAGMA quick_check').fetchone()[0]=='ok')
    status,_,raw=v.http('GET',f'http://127.0.0.1:{port}/api/dashboard',headers={'Cookie':session[0]})
    report.check('updated code serves new build',status==200 and json.loads(raw).get('build_number')=='20261013-rc1')
    report.check('updated package clean stop',instance.stop_with_launcher())
finally:
    instance.stop_with_launcher()
report.note('Portable update only; Windows installer installation/update not exercised.')
raise SystemExit(report.summary())
