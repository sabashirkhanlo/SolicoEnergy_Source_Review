from __future__ import annotations
import logging
from pathlib import Path
from ..core import db
from ..sources import wincc_csv
log=logging.getLogger(__name__)

# WinCC writes a snapshot and then writes a reset/set-clear copy a few seconds later.
# For all snapshot-style tags we keep the FIRST record in a short burst. Breaker
# events are exempt because two close changes can be genuine events.
BURST_SECONDS = 30.0

# A legacy energy source is snapshot/reset based.  Generic sensor sources (custom roles such as
# دما، فشار، لرزش، توان...) are continuous logs and must not lose samples merely because they are
# closer than 30 seconds.  This keeps the established energy behavior intact while allowing new
# parameter CSVs to be reported faithfully.
ENERGY_SOURCE_ROLES = {'energy_active','energy_reactive','max_power','max_power_time','max_reactive_power','max_reactive_power_time','running_hours','breaker_status'}
SNAPSHOT_ROLES = ENERGY_SOURCE_ROLES | {'temp_min','temp_min_time','temp_max','temp_max_time','humidity_min','humidity_min_time','humidity_max','humidity_max_time'}

def import_source(source_id:int, progress=None):
    src=next((s for s in db.list_sources() if s['id']==source_id),None)
    if not src: raise ValueError('Data source not found')
    p=Path(src['path'])
    if not p.exists(): raise FileNotFoundError(str(p))
    maps={m['tag_name']:m for m in db.mappings(source_id, enabled_only=True) if m['role']!='ignore'}
    if not maps: return {'read':0,'inserted':0,'ignored':0,'message':'هیچ تگ فعالی برای این منبع تعریف نشده است.'}
    roles={str(m.get('role') or '').strip() for m in maps.values()}
    legacy_energy_source=bool(roles & {'energy_active','energy_reactive','max_power','max_reactive_power','breaker_status'})
    buf=[]; read=ins=ign=burst_ignored=0
    last_kept={}
    for r in wincc_csv.iter_rows(p):
        read+=1
        m=maps.get(r['tag_name'])
        if not m: continue
        dt=r.pop('dt'); epoch=dt.timestamp()
        role=str(m.get('role') or '').strip()
        # Preserve the exact legacy snapshot/reset rule for energy sources. For generic sensor
        # sources, only explicit built-in snapshot roles are de-bursted; custom roles retain all
        # distinct timestamped samples.
        should_dedup=(legacy_energy_source and role!='breaker_status') or (not legacy_energy_source and role in SNAPSHOT_ROLES and role!='breaker_status')
        if should_dedup:
            prev=last_kept.get(r['tag_name'])
            if prev is not None and 0 <= epoch-prev <= BURST_SECONDS:
                ign+=1; burst_ignored+=1
                continue
            last_kept[r['tag_name']]=epoch
        row={'source_id':source_id,'ts':dt.isoformat(sep=' '),'ts_epoch':epoch,**r}
        buf.append(row)
        if len(buf)>=2000:
            added=db.insert_records(buf); ins+=added; ign+=len(buf)-added; buf=[]
            if progress: progress(read,ins)
    if buf:
        added=db.insert_records(buf); ins+=added; ign+=len(buf)-added
    log.info('import source=%s read=%s inserted=%s ignored=%s burst_ignored=%s',source_id,read,ins,ign,burst_ignored)
    return {'read':read,'inserted':ins,'ignored':ign,'burst_ignored':burst_ignored,'message':'OK'}

def import_all(progress=None):
    out=[]
    for s in db.list_sources():
        if s['enabled']:
            try: out.append((s['name'],import_source(s['id'],progress)))
            except Exception as e: out.append((s['name'],{'error':str(e)}))
    return out
