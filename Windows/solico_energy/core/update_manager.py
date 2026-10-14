from __future__ import annotations
import json, shutil, zipfile, hashlib, os
from pathlib import Path
from .paths import updates_dir

PRODUCT='SolicoEnergy'

def _sha256(p:Path):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def stage_update(path):
    p=Path(path)
    if not p.exists(): raise FileNotFoundError(str(p))
    with zipfile.ZipFile(p,'r') as z:
        if 'manifest.json' not in z.namelist(): raise ValueError('manifest.json در بسته Update وجود ندارد.')
        m=json.loads(z.read('manifest.json').decode('utf-8'))
        if m.get('product')!=PRODUCT: raise ValueError('این بسته برای SolicoEnergy نیست.')
        v=str(m.get('version','')).strip()
        if not v: raise ValueError('Version نامعتبر است.')
        prefix=f'versions/{v}/'
        if not any(n.startswith(prefix) and not n.endswith('/') for n in z.namelist()):
            raise ValueError(f'فایل‌های نسخه {v} در بسته وجود ندارند.')
    dst=updates_dir()/'pending_update.seupdate'; shutil.copy2(p,dst)
    meta={'version':v,'path':str(dst),'sha256':_sha256(dst)}
    (updates_dir()/'pending.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    return v

def request_rollback():
    (updates_dir()/'rollback.request').write_text('1',encoding='utf-8')
