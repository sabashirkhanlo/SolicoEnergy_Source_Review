from __future__ import annotations
import json, shutil, os
from pathlib import Path
from .paths import settings_path, default_output_dir, data_dir, user_root

DEFAULT = {
  'company':'SOLICO FOODSERVICE',
  'plant':'پالایشگاه شیر تهران',
  'department':'مدیریت نیروگاه‌ها و انرژی',
  'daily_title':'گزارش روزانه انرژی و نیروگاه',
  'monthly_title':'گزارش ماهانه انرژی و نیروگاه',
  'smart_system_label':'سامانه هوشمند پایش نیروگاه',
  'output_dir':str(default_output_dir()),
  'daily_time':'08:00',
  'auto_report_enabled':True,
  'monthly_time':'00:10',
  'logo_path':'', 'prepared_name':'', 'prepared_title':'تهیه‌کننده گزارش', 'prepared_signature':'',
  'manager_name':'', 'manager_title':'مدیر/مسئول نیروگاه', 'manager_signature':'',
  'report_numbering':True,
  'theme':'blue',
  'mobile_notify_enabled':True
}


def load():
    p=settings_path()
    if not p.exists(): save(DEFAULT.copy()); return DEFAULT.copy()
    try: obj=json.loads(p.read_text(encoding='utf-8'))
    except Exception: obj={}
    out=DEFAULT.copy(); out.update(obj)
    # One-time migration for installations created before Auto Report existed.
    # The requested automatic daily report time is 08:00.
    if 'auto_report_enabled' not in obj:
        out['auto_report_enabled']=True
        out['daily_time']='08:00'
        try: p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception: pass
    if out.get('_update_1017_applied') is not True:
        out['_previous_logo_path_1017']=out.get('logo_path','')
        out['logo_path']=''
        out['daily_time']='08:00'
        out['_update_1017_applied']=True
        save(out)
    return out


def save(settings):
    p=settings_path(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')


def _writable_dir(path: Path) -> bool:
    """Create *path* when needed and verify that the current Windows user can write to it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f'.solico_write_test_{os.getpid()}.tmp'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def ensure_output_dir() -> Path:
    """Return a real, writable reports directory.

    Older installations could keep an obsolete/unwritable project folder (for example C:\\energy)
    in settings.  Reports must never silently fail because of that.  We keep a valid user choice;
    otherwise we migrate once to Documents\\Solico Energy Reports, with LocalAppData as a final
    offline-safe fallback, and persist the repaired setting.
    """
    s = load()
    raw = str(s.get('output_dir') or '').strip()
    candidates=[]
    if raw:
        candidates.append(Path(os.path.expandvars(raw)).expanduser())
    candidates.append(default_output_dir())
    candidates.append(user_root() / 'reports')

    chosen=None
    for p in candidates:
        if _writable_dir(p):
            chosen=p
            break
    if chosen is None:
        raise OSError('هیچ پوشه قابل نوشتنی برای ذخیره گزارش پیدا نشد.')

    # Keep the selected directory in settings so the UI and generated reports always agree.
    if raw != str(chosen):
        s['output_dir']=str(chosen)
        save(s)
    return chosen


def export_to(path):
    """Export a portable plant profile: settings + sources + tag mappings.
    Raw archive records and reports are intentionally not exported.
    """
    from . import db
    sources=db.list_sources(); maps=db.mappings()
    payload={'format':'SolicoEnergyProfile','version':1,'settings':load(),'sources':[]}
    by_source={s['id']:{'name':s['name'],'path':s['path'],'adapter':s['adapter'],'enabled':bool(s['enabled']),'mappings':[]} for s in sources}
    for m in maps:
        item={k:m[k] for k in ('tag_name','enabled','entity_type','entity_name','role','display_name','unit')}
        by_source.get(m['source_id'],{}).setdefault('mappings',[]).append(item)
    payload['sources']=list(by_source.values())
    Path(path).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')


def import_from(path):
    from . import db
    obj=json.loads(Path(path).read_text(encoding='utf-8'))
    if obj.get('format')!='SolicoEnergyProfile':
        # backward compatibility: settings-only JSON
        save({**DEFAULT,**obj}); return load()
    save({**DEFAULT,**obj.get('settings',{})})
    # Do not delete archived records. Imported source definitions are appended/reused by path+name.
    existing=db.list_sources()
    for src in obj.get('sources',[]):
        hit=next((s for s in existing if s['path']==src.get('path') and s['name']==src.get('name')),None)
        if hit: sid=hit['id']; db.update_source(sid,src.get('name','Source'),src.get('path',''),1 if src.get('enabled',True) else 0)
        else:
            sid=db.add_source(src.get('name','Source'),src.get('path',''),src.get('adapter','wincc_csv'))
            existing=db.list_sources()
        for m in src.get('mappings',[]):
            db.upsert_mapping({'source_id':sid,**m})
    return load()


def copy_managed_asset(src, name):
    if not src: return ''
    src=Path(src); ext=src.suffix.lower() or '.png'; dst=data_dir()/f'{name}{ext}'
    shutil.copy2(src,dst); return str(dst)
