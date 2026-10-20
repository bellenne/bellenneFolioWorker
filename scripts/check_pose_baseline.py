"""One isolated paid integration check; no Core callbacks or buyer messages."""
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path
from folio_worker import app

spec = importlib.util.spec_from_file_location('candidate_planner', '/tmp/candidate_planner.py')
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)
root = Path('/data/vision-baseline-pose-20261013')
folder = root / 'sample'
folder.mkdir(parents=True, exist_ok=True)
shutil.copyfile('/data/575c400a-5152-54ac-bbb7-5e92af88db08/input.png', folder / 'input.png')
app.DATA = root
app.DB = root / 'check.sqlite3'
with sqlite3.connect(app.DB) as con:
    con.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, provider TEXT, aitunnel_request_started_at TEXT)')
    con.execute("INSERT OR IGNORE INTO jobs VALUES ('sample', 'aitunnel', NULL)")
row = dict(id='sample', prompt=Path('/tmp/baseline-task.txt').read_text().strip().replace('{background}', 'Замените фон'),
           width_cm=40, height_cm=60, input_count=1, model='hy-image-v3.5-preview')
prompt = planner.prepare(row, folder, app.aitunnel_references(row), key=app.AITUNNEL_KEY,
    base=app.AITUNNEL_BASE_URL, mirror=app.AITUNNEL_MIRROR_BASE,
    connect_timeout=app.AITUNNEL_CONNECT_TIMEOUT, read_timeout=app.AITUNNEL_READ_TIMEOUT)
print('PLAN:', prompt, flush=True)
row['prompt'] = prompt
print('RESULT:', app.aitunnel_result(row), flush=True)
