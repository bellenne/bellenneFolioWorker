"""Fixed successful prompt plus pose sentence; isolated paid comparison."""
import shutil
import sqlite3
from pathlib import Path
from folio_worker import app

root = Path('/data/vision-fixed-pose-20261013')
folder = root / 'sample'
folder.mkdir(parents=True, exist_ok=True)
shutil.copyfile('/data/575c400a-5152-54ac-bbb7-5e92af88db08/input.png', folder / 'input.png')
app.DATA = root
app.DB = root / 'check.sqlite3'
with sqlite3.connect(app.DB) as con:
    con.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, provider TEXT, aitunnel_request_started_at TEXT)')
    con.execute("INSERT OR IGNORE INTO jobs VALUES ('sample', 'aitunnel', NULL)")
prompt = Path('/data/prompt-planner-20261013-dns-recovered/planned/generation-prompt.txt').read_text().strip()
prompt += '\nСохрани исходную позу, поворот и наклон головы и ракурс камеры, если клиент явно не просит их изменить.'
(folder / 'generation-prompt.txt').write_text(prompt, encoding='utf-8')
row = dict(id='sample', prompt=prompt, width_cm=40, height_cm=60, input_count=1, model='hy-image-v3.5-preview')
print('RESULT:', app.aitunnel_result(row), flush=True)
