from __future__ import annotations
import math
from datetime import datetime, date, time, timedelta
from collections import defaultdict
from ..core import db
from ..core.jalali import gregorian_to_jalali, jalali_month_bounds

CUM_ROLES={'energy_active','energy_reactive','running_hours'}
VALUE_ROLES={'max_power','max_reactive_power','temp_min','temp_max','humidity_min','humidity_max'}
TIME_ROLES={'max_power_time','max_reactive_power_time','temp_min_time','temp_max_time','humidity_min_time','humidity_max_time'}
BURST_SECONDS=30.0
BOUNDARY_RESOLUTION_SECONDS=1.0


def _maps(source_id=None):
    return db.mappings(source_id=source_id, enabled_only=True)


def _entity_key(m):
    return f"{m['entity_type']}::{m['entity_name']}"


def _parse_value_time(v):
    from ..sources.wincc_csv import parse_dt
    return parse_dt(v)


def _rolemap(source_id=None):
    by=defaultdict(dict)
    for m in _maps(source_id):
        if m['role']!='ignore':
            by[_entity_key(m)][m['role']]=m
    return by


def _pf(a,q):
    if a in (None,0) or q is None:
        return None
    return abs(a)/math.sqrt(a*a+q*q)


def _rows(con,m,start_epoch,end_epoch):
    return list(con.execute(
        '''SELECT id,ts,ts_epoch,value_num,value_text FROM records
           WHERE source_id=? AND tag_name=? AND ts_epoch>=? AND ts_epoch<=?
           ORDER BY ts_epoch ASC,id ASC''',
        (m['source_id'],m['tag_name'],start_epoch,end_epoch)
    ))


def _dedup_bursts(rows):
    """Keep the first record in each WinCC set/reset burst."""
    out=[]; last=None
    for r in rows:
        ep=float(r['ts_epoch'])
        if last is not None and 0 <= ep-last <= BURST_SECONDS:
            continue
        out.append(r); last=ep
    return out


def _boundary_value(con,m,boundary:datetime):
    """Use the first midnight snapshot, allowing WinCC logging latency.

    A snapshot at 23:59:59 or up to 120 seconds after midnight belongs
    to that boundary. Never look back to an older day or use daytime data.
    The same boundary record is shared by adjacent days.
    """
    # The stored WinCC wall clock is authoritative. Legacy Windows DST
    # conversion produced inconsistent epochs; do not rewrite the database.
    lo=(boundary-timedelta(seconds=3900)).isoformat(sep=' ')
    hi=(boundary+timedelta(seconds=3900)).isoformat(sep=' ')
    rows=[]
    for raw in con.execute(
        "SELECT id,ts,ts_epoch,value_num,value_text FROM records WHERE source_id=? AND tag_name=? AND replace(ts,'T',' ')>=? AND replace(ts,'T',' ')<=? ORDER BY replace(ts,'T',' '),id",
        (m['source_id'],m['tag_name'],lo,hi)):
        row=dict(raw)
        stamp=datetime.fromisoformat(row['ts']).replace(tzinfo=None)
        row['ts_epoch']=(stamp-datetime(1970,1,1)).total_seconds()
        rows.append(row)
    b=(boundary-datetime(1970,1,1)).total_seconds()
    normal=[row for row in rows if b-1 <= row['ts_epoch'] <= b+120]
    if normal:return normal[0]
    candidates=_dedup_bursts(rows)
    return min(candidates,key=lambda row:abs(row['ts_epoch']-b)) if candidates else None



def _counter_day(con,m,d:date):
    start_boundary=datetime.combine(d,time.min)
    end_boundary=datetime.combine(d+timedelta(days=1),time.min)
    start=_boundary_value(con,m,start_boundary)
    end=_boundary_value(con,m,end_boundary)
    info={'value':None,'gap_days':0,'partial_day':True,
          'expected_from':start_boundary.isoformat(sep=' '),
          'expected_to':end_boundary.isoformat(sep=' '),
          'from':start['ts'] if start else None,'to':end['ts'] if end else None}
    if not start or start['value_num'] is None:
        info['missing_start_snapshot']=True
    if not end or end['value_num'] is None:
        info['missing_end_snapshot']=True
    if info.get('missing_start_snapshot') or info.get('missing_end_snapshot'):
        return info
    delta=end['value_num']-start['value_num']
    info.update(current=end['value_num'],previous=start['value_num'])
    if delta<0:
        info['counter_reset']=True
        return info
    # This is a calendar-day report, not a last-reading-to-current report.
    actual_hours=(end['ts_epoch']-start['ts_epoch'])/3600.0
    irregular=(abs(start['ts_epoch']-(start_boundary-datetime(1970,1,1)).total_seconds())>120 or
               abs(end['ts_epoch']-(end_boundary-datetime(1970,1,1)).total_seconds())>120)
    info.update(value=delta,hours=actual_hours if irregular else 24.0,
                actual_hours=actual_hours, partial_day=irregular)
    return info


def _latest_row_until(con,m,start_dt:datetime,end_dt:datetime):
    """Return the latest stored WinCC reading inside a real wall-clock window."""
    lo=start_dt.isoformat(sep=' ')
    hi=end_dt.isoformat(sep=' ')
    raw=con.execute(
        """SELECT id,ts,ts_epoch,value_num,value_text FROM records
           WHERE source_id=? AND tag_name=?
             AND replace(ts,'T',' ')>=? AND replace(ts,'T',' ')<=?
           ORDER BY replace(ts,'T',' ') DESC,id DESC LIMIT 1""",
        (m['source_id'],m['tag_name'],lo,hi)
    ).fetchone()
    if not raw:
        return None
    row=dict(raw)
    try:
        stamp=datetime.fromisoformat(str(row['ts']).replace('Z','')).replace(tzinfo=None)
        row['ts_epoch']=(stamp-datetime(1970,1,1)).total_seconds()
    except Exception:
        pass
    return row


def _wallclock_numeric_rows(con,m,start_dt:datetime,end_dt:datetime):
    """Read numeric samples in wall-clock order for a partial-day counter.

    Using the stored ISO wall clock here avoids old Windows/DST epoch drift and,
    unlike the full-day midnight rule, lets an in-progress report use the first
    real sample after 00:00 when an exact midnight snapshot is unavailable.
    """
    lo=start_dt.isoformat(sep=' ')
    hi=end_dt.isoformat(sep=' ')
    out=[]
    for raw in con.execute(
        """SELECT id,ts,ts_epoch,value_num,value_text FROM records
           WHERE source_id=? AND tag_name=? AND value_num IS NOT NULL
             AND replace(ts,'T',' ')>=? AND replace(ts,'T',' ')<=?
           ORDER BY replace(ts,'T',' ') ASC,id ASC""",
        (m['source_id'],m['tag_name'],lo,hi)):
        row=dict(raw)
        try:
            stamp=datetime.fromisoformat(str(row['ts']).replace('Z','')).replace(tzinfo=None)
            row['ts_epoch']=(stamp-datetime(1970,1,1)).total_seconds()
        except Exception:
            pass
        out.append(row)
    return _dedup_bursts(out)


def _counter_partial_day(con,m,d:date,cutoff:datetime):
    """Counter energy from 00:00 to the newest sample available by *cutoff*.

    For a live report we prefer real samples inside the current day.  This is more
    robust than requiring the next 24-hour boundary and also handles counters that
    reset once during the day by summing positive increments after the reset.
    """
    start_boundary=datetime.combine(d,time.min)
    rows=_wallclock_numeric_rows(con,m,start_boundary,cutoff)

    # Prefer an exact/near-midnight boundary only when it is genuinely close;
    # otherwise use the first actual sample after 00:00 so a midday report is not
    # forced to zero merely because the midnight snapshot is absent.
    boundary=_boundary_value(con,m,start_boundary)
    start=None
    if boundary and boundary.get('value_num') is not None:
        try:
            bsec=(start_boundary-datetime(1970,1,1)).total_seconds()
            if abs(float(boundary['ts_epoch'])-bsec) <= 120:
                start=boundary
        except Exception:
            pass
    if start is None and rows:
        start=rows[0]
    if start is None:
        start=boundary

    end=rows[-1] if rows else _latest_row_until(con,m,start_boundary,cutoff)
    info={'value':None,'gap_days':0,'partial_day':True,
          'expected_from':start_boundary.isoformat(sep=' '),
          'expected_to':cutoff.isoformat(sep=' '),
          'from':start['ts'] if start else None,'to':end['ts'] if end else None}
    if not start or start.get('value_num') is None:
        info['missing_start_snapshot']=True
    if not end or end.get('value_num') is None:
        info['missing_end_snapshot']=True
    if info.get('missing_start_snapshot') or info.get('missing_end_snapshot'):
        return info

    # Build a chronological series starting from the chosen start row.  Summing
    # positive increments makes the live report resilient to a daily counter reset.
    series=[]
    sid=start.get('id')
    if start is not None:
        series.append(start)
    for r in rows:
        if sid is not None and r.get('id')==sid:
            continue
        if float(r.get('ts_epoch') or 0) < float(start.get('ts_epoch') or 0):
            continue
        series.append(r)
    if not series or series[-1].get('id')!=end.get('id'):
        series.append(end)

    total=0.0
    reset=False
    prev=float(series[0]['value_num'])
    for r in series[1:]:
        cur=float(r['value_num'])
        dv=cur-prev
        if dv >= 0:
            total += dv
        else:
            # Counter reset: the new counter value is the energy accumulated after reset.
            reset=True
            if cur >= 0:
                total += cur
        prev=cur

    # With exactly one sample there is no measurable interval yet; don't present a
    # misleading 0.0 MWh as a completed calculation.
    if len(series) < 2:
        info['missing_end_snapshot']=True
        return info

    info.update(current=end['value_num'],previous=start['value_num'])
    if reset:
        info['counter_reset']=True
    hours=max((float(end['ts_epoch'])-float(start['ts_epoch']))/3600.0,0.0)
    info.update(value=total,hours=hours,actual_hours=hours,partial_day=True)
    return info


def _endpoint_day(con,m,d:date,as_of:datetime|None=None):
    if as_of is not None and as_of.date()==d:
        return _latest_row_until(con,m,datetime.combine(d,time.min),as_of)
    return _boundary_value(con,m,datetime.combine(d+timedelta(days=1),time.min))


def entity_stats_for_day(d:date, source_id=None, as_of:datetime|None=None):
    by=_rolemap(source_id); result={}
    with db.connect() as con:
        for key,roles in by.items():
            etype,ename=key.split('::',1)
            st={'entity_type':etype,'entity_name':ename,'roles':{},'notes':[],'has_data':False}
            for role,m in roles.items():
                if role in CUM_ROLES:
                    cutoff=as_of if (as_of is not None and as_of.date()==d) else (datetime.now() if d==date.today() else None)
                    info=_counter_partial_day(con,m,d,cutoff) if cutoff is not None else _counter_day(con,m,d)
                    st['roles'][role]=info
                    if info.get('to'): st['has_data']=True
                    if info.get('missing_start_snapshot'): st['notes'].append(f'{role}: ثبت مرز شروع روز موجود نیست')
                    if info.get('missing_end_snapshot'): st['notes'].append(f'{role}: ثبت مرز پایان روز موجود نیست')
                    if info.get('counter_reset'): st['notes'].append(f'{role}: کاهش شمارنده مشاهده شد')
                    if info.get('gap_days',0)>0: st['notes'].append(f'{role}: فاصله ثبت روزانه وجود دارد')
                    if info.get('partial_day'): st['notes'].append(f'{role}: بازه ثبت کامل ۲۴ ساعت نیست')
                elif role in VALUE_ROLES:
                    cutoff=as_of if (as_of is not None and as_of.date()==d) else (datetime.now() if d==date.today() else None)
                    r=_endpoint_day(con,m,d,cutoff)
                    if r: st['has_data']=True
                    st['roles'][role]={'value':r['value_num'] if r else None,'to':r['ts'] if r else None}
                elif role in TIME_ROLES:
                    cutoff=as_of if (as_of is not None and as_of.date()==d) else (datetime.now() if d==date.today() else None)
                    r=_endpoint_day(con,m,d,cutoff)
                    if r: st['has_data']=True
                    raw=r['value_text'] if r else ''
                    t=_parse_value_time(raw)
                    st['roles'][role]={'value':t.isoformat(sep=' ') if t else raw or None,'to':r['ts'] if r else None}
            a=st['roles'].get('energy_active',{}).get('value')
            q=st['roles'].get('energy_reactive',{}).get('value')
            hrs=st['roles'].get('energy_active',{}).get('hours')
            st['active_kwh']=a
            st['reactive_kvarh']=q
            st['avg_kw']=a/hrs if a is not None and hrs and hrs>0 else None
            st['pf']=_pf(a,q)
            st['max_kw']=st['roles'].get('max_power',{}).get('value')
            st['max_time']=st['roles'].get('max_power_time',{}).get('value')
            st['max_kvar']=st['roles'].get('max_reactive_power',{}).get('value')
            st['max_reactive_time']=st['roles'].get('max_reactive_power_time',{}).get('value')
            st['running_hours']=st['roles'].get('running_hours',{}).get('value')
            gapinfo=st['roles'].get('energy_active',{})
            st['gap_days']=gapinfo.get('gap_days',0)
            st['partial_day']=gapinfo.get('partial_day',False)
            st['interval_hours']=gapinfo.get('hours')
            st['interval_from']=gapinfo.get('from')
            st['interval_to']=gapinfo.get('to')
            result[key]=st
    return result


def breaker_stats_for_day(d:date, entity_type='grid', source_id=None, as_of:datetime|None=None):
    maps=[m for m in _maps(source_id) if m['role']=='breaker_status' and m['entity_type']==entity_type]
    start_dt=datetime.combine(d,time.min)
    end_dt=datetime.combine(d+timedelta(days=1),time.min)
    if as_of is not None and as_of.date()==d:
        end_dt=min(end_dt,as_of)
    elif d==date.today():
        end_dt=min(end_dt,datetime.now())
    start=start_dt.timestamp(); end=end_dt.timestamp()
    out=[]
    with db.connect() as con:
        for m in maps:
            before=con.execute(
                '''SELECT ts_epoch,value_num,value_text FROM records
                   WHERE source_id=? AND tag_name=? AND ts_epoch<?
                   ORDER BY ts_epoch DESC,id ASC LIMIT 1''',
                (m['source_id'],m['tag_name'],start)
            ).fetchone()
            rows=list(con.execute(
                '''SELECT ts,ts_epoch,value_num,value_text FROM records
                   WHERE source_id=? AND tag_name=? AND ts_epoch>=? AND ts_epoch<?
                   ORDER BY ts_epoch,id ASC''',
                (m['source_id'],m['tag_name'],start,end)
            ))
            state=int(before['value_num']) if before and before['value_num'] is not None else None
            events=[]; open_start=start if state==0 else None; count=0; duration=0.0
            for r in rows:
                v=int(r['value_num']) if r['value_num'] is not None else None
                if v not in (0,1): continue
                if v==0 and state!=0:
                    count+=1; open_start=max(r['ts_epoch'],start); events.append({'type':'open','ts':r['ts']})
                elif v==1 and state==0:
                    if open_start is not None: duration += max(0,r['ts_epoch']-open_start)
                    open_start=None; events.append({'type':'close','ts':r['ts']})
                state=v
            if state==0 and open_start is not None: duration += max(0,end-open_start)
            out.append({'entity_name':m['entity_name'],'count':count,'duration_minutes':round(duration/60,1),'events':events,'known_start_state':before is not None})
    return out


def day_summary(d:date, source_id=None, as_of:datetime|None=None):
    cutoff=as_of if (as_of is not None and as_of.date()==d) else (datetime.now() if d==date.today() else None)
    ents=entity_stats_for_day(d, source_id, as_of=cutoff)
    gens=[v for v in ents.values() if v['entity_type']=='generator']
    factory=next((v for v in ents.values() if v['entity_type']=='factory'),None)
    grid=next((v for v in ents.values() if v['entity_type']=='grid'),None)

    def all_or_none(field):
        if not gens: return None
        vals=[g.get(field) for g in gens]
        return sum(vals) if all(v is not None for v in vals) else None

    plant_energy=all_or_none('active_kwh')
    plant_reactive=all_or_none('reactive_kvarh')
    plant_avg=all_or_none('avg_kw')
    plant_pf=_pf(plant_energy,plant_reactive)
    outages=breaker_stats_for_day(d,'grid',source_id,as_of=cutoff)
    outage_count=sum(x['count'] for x in outages)
    outage_min=sum(x['duration_minutes'] for x in outages)
    supply_total=(plant_energy or 0)+(grid.get('active_kwh') if grid and grid.get('active_kwh') is not None else 0)
    share=(plant_energy/supply_total*100) if plant_energy is not None and grid and grid.get('active_kwh') is not None and supply_total>0 else None
    env=next((v for v in ents.values() if v['entity_type']=='environment'),None)
    gaps=[]
    for x in [factory,grid,*gens]:
        if x and (x.get('gap_days',0)>0 or x.get('partial_day')):
            gaps.append({'entity_name':x['entity_name'],'gap_days':x.get('gap_days',0),'partial_day':x.get('partial_day',False),'hours':x.get('interval_hours'),'from':x.get('interval_from'),'to':x.get('interval_to')})
    return {
        'date':d,'entities':ents,'generators':gens,'factory':factory,'grid':grid,
        'plant_energy':plant_energy,'plant_reactive':plant_reactive,'plant_avg':plant_avg,'plant_pf':plant_pf,
        'plant_share':share,'outage_count':outage_count,'outage_minutes':outage_min,'outages':outages,
        'environment':env,'gaps':gaps,
        'has_daily_snapshot':bool(factory and factory.get('active_kwh') is not None),
        'is_temporary':bool(cutoff is not None),
        'as_of':cutoff.isoformat(sep=' ',timespec='seconds') if cutoff is not None else None,
        'last_data_time':factory.get('interval_to') if factory else None,
        'report_hours':factory.get('interval_hours') if factory else None
    }


def _counter_delta_for_period(con,m,start:date,end:date):
    start_r=_boundary_value(con,m,datetime.combine(start,time.min))
    end_r=_boundary_value(con,m,datetime.combine(end+timedelta(days=1),time.min))
    if not start_r or not end_r or start_r['value_num'] is None or end_r['value_num'] is None:
        return None
    delta=end_r['value_num']-start_r['value_num']
    if delta<0:return None
    return {'value':delta,'from':start_r['ts'],'to':end_r['ts'],'hours':max((end_r['ts_epoch']-start_r['ts_epoch'])/3600,0.0001)}


def entity_stats_for_period(start:date,end:date, source_id=None):
    by=_rolemap(source_id); out={}
    with db.connect() as con:
        for key,roles in by.items():
            etype,ename=key.split('::',1)
            st={'entity_type':etype,'entity_name':ename,'roles':{}}
            for role in CUM_ROLES:
                m=roles.get(role)
                if m: st['roles'][role]=_counter_delta_for_period(con,m,start,end) or {'value':None}
            a=st['roles'].get('energy_active',{}).get('value')
            q=st['roles'].get('energy_reactive',{}).get('value')
            h=st['roles'].get('energy_active',{}).get('hours')
            st['active_kwh']=a; st['reactive_kvarh']=q; st['avg_kw']=a/h if a is not None and h else None
            st['pf']=_pf(a,q); st['running_hours']=st['roles'].get('running_hours',{}).get('value')
            out[key]=st
    return out


def monthly_summary(start:date,end:date, source_id=None):
    """Aggregate monthly KPIs from the daily rows that are actually valid."""
    days=[]; cur=start
    while cur<=end:
        days.append(day_summary(cur,source_id)); cur+=timedelta(days=1)

    # Retained for compatibility with callers that inspect detailed period entities.
    period_entities=entity_stats_for_period(start,end,source_id)
    gens=[v for v in period_entities.values() if v['entity_type']=='generator']
    factory=next((v for v in period_entities.values() if v['entity_type']=='factory'),None)
    grid=next((v for v in period_entities.values() if v['entity_type']=='grid'),None)

    def _sum_valid(values):
        vals=[float(v) for v in values if v is not None]
        return sum(vals) if vals else None

    factory_energy=_sum_valid([(d.get('factory') or {}).get('active_kwh') for d in days])
    plant_energy=_sum_valid([d.get('plant_energy') for d in days])
    grid_energy=_sum_valid([(d.get('grid') or {}).get('active_kwh') for d in days])

    valid_hours=[]
    for d in days:
        f=d.get('factory') or {}
        if f.get('active_kwh') is not None:
            h=f.get('interval_hours')
            if h is not None and float(h)>0:
                valid_hours.append(float(h))
    factory_hours=sum(valid_hours) if valid_hours else 0.0
    factory_avg=(factory_energy/factory_hours) if factory_energy is not None and factory_hours>0 else None

    plant_reactive=_sum_valid([d.get('plant_reactive') for d in days])
    plant_pf=_pf(plant_energy,plant_reactive)
    factory_vals=[(d['date'],(d.get('factory') or {}).get('active_kwh')) for d in days if (d.get('factory') or {}).get('active_kwh') is not None]
    valid_peaks=[((d.get('factory') or {}).get('max_kw'),d['date'],(d.get('factory') or {}).get('max_time')) for d in days if (d.get('factory') or {}).get('max_kw') is not None]
    missing_days=[d['date'] for d in days if not d.get('factory') or (d.get('factory') or {}).get('active_kwh') is None]

    env_min_t=[]; env_max_t=[]; env_min_h=[]; env_max_h=[]
    for ds in days:
        env=ds.get('environment') or {}; roles=env.get('roles',{})
        for bucket,role,trole in [
            (env_min_t,'temp_min','temp_min_time'),(env_max_t,'temp_max','temp_max_time'),
            (env_min_h,'humidity_min','humidity_min_time'),(env_max_h,'humidity_max','humidity_max_time')]:
            v=roles.get(role,{}).get('value')
            if v is not None: bucket.append((v,roles.get(trole,{}).get('value'),ds['date']))
    env_month={
        'temp_min':min(env_min_t,key=lambda x:x[0]) if env_min_t else None,
        'temp_max':max(env_max_t,key=lambda x:x[0]) if env_max_t else None,
        'humidity_min':min(env_min_h,key=lambda x:x[0]) if env_min_h else None,
        'humidity_max':max(env_max_h,key=lambda x:x[0]) if env_max_h else None,
    }

    supply_total=(plant_energy or 0)+(grid_energy or 0)
    share=(plant_energy/supply_total*100) if plant_energy is not None and grid_energy is not None and supply_total>0 else None
    return {
        'start':start,'end':end,'days':days,'period_entities':period_entities,'generators':gens,
        'factory':factory,'grid':grid,'factory_energy':factory_energy,'plant_energy':plant_energy,'grid_energy':grid_energy,
        'factory_avg':factory_avg,'factory_valid_hours':factory_hours,
        'plant_reactive':plant_reactive,'plant_pf':plant_pf,'plant_share':share,
        'highest_day':max(factory_vals,key=lambda x:x[1]) if factory_vals else None,
        'lowest_day':min(factory_vals,key=lambda x:x[1]) if factory_vals else None,
        'peak':max(valid_peaks,key=lambda x:x[0]) if valid_peaks else None,
        'missing_days':missing_days,'valid_day_count':len(days)-len(missing_days),'environment':env_month
    }


# ---- comparison / automatic-report validation ----
def period_summary(start:date,end:date, source_id=None):
    """Compact technically-valid energy summary for an arbitrary closed period.

    Period energy comes from cumulative counter boundaries, not from summing daily
    peaks or inventing values for missing days.
    """
    ents=entity_stats_for_period(start,end,source_id)
    gens=[v for v in ents.values() if v['entity_type']=='generator']
    factory=next((v for v in ents.values() if v['entity_type']=='factory'),None)
    grid=next((v for v in ents.values() if v['entity_type']=='grid'),None)
    plant_energy=sum(g['active_kwh'] for g in gens) if gens and all(g.get('active_kwh') is not None for g in gens) else None
    plant_reactive=sum(g['reactive_kvarh'] for g in gens) if gens and all(g.get('reactive_kvarh') is not None for g in gens) else None
    plant_pf=_pf(plant_energy,plant_reactive)
    plant_avg=sum(g['avg_kw'] for g in gens) if gens and all(g.get('avg_kw') is not None for g in gens) else None
    factory_energy=factory.get('active_kwh') if factory else None
    grid_energy=grid.get('active_kwh') if grid else None
    supply=(plant_energy or 0)+(grid_energy or 0)
    share=(plant_energy/supply*100) if plant_energy is not None and grid_energy is not None and supply>0 else None
    return {
        'start':start,'end':end,'factory':factory,'grid':grid,'generators':gens,
        'factory_energy':factory_energy,'plant_energy':plant_energy,'grid_energy':grid_energy,
        'plant_reactive':plant_reactive,'plant_avg':plant_avg,'plant_pf':plant_pf,'plant_share':share,
        'factory_avg':factory.get('avg_kw') if factory else None,
        'factory_pf':factory.get('pf') if factory else None,
        'grid_pf':grid.get('pf') if grid else None,
    }


def comparison_summary(base_day:date, mode='day', source_id=None):
    """Compare a selected day, rolling 7-day week, or complete Jalali month."""
    if mode=='month':
        jy,jm,_=gregorian_to_jalali(base_day.year,base_day.month,base_day.day)
        current_start,current_end=jalali_month_bounds(jy,jm)
        py,pm=(jy-1,12) if jm==1 else (jy,jm-1)
        previous_start,previous_end=jalali_month_bounds(py,pm)
        current=period_summary(current_start,current_end,source_id)
        previous=period_summary(previous_start,previous_end,source_id)
    elif mode=='week':
        current_start=base_day-timedelta(days=6); current_end=base_day
        previous_start=base_day-timedelta(days=13); previous_end=base_day-timedelta(days=7)
        current=period_summary(current_start,current_end,source_id)
        previous=period_summary(previous_start,previous_end,source_id)
    else:
        current_start=current_end=base_day
        previous_start=previous_end=base_day-timedelta(days=1)
        ds=day_summary(base_day,source_id); ps=day_summary(base_day-timedelta(days=1),source_id)
        def compact(x):
            fac=x.get('factory'); grd=x.get('grid')
            return {
                'factory_energy':fac.get('active_kwh') if fac else None,
                'plant_energy':x.get('plant_energy'),'grid_energy':grd.get('active_kwh') if grd else None,
                'plant_share':x.get('plant_share'),'factory_avg':fac.get('avg_kw') if fac else None,
                'factory_pf':fac.get('pf') if fac else None,'plant_pf':x.get('plant_pf'),
                'grid_pf':grd.get('pf') if grd else None,
            }
        current=compact(ds); previous=compact(ps)
    return {
        'mode':mode,'current_start':current_start,'current_end':current_end,
        'previous_start':previous_start,'previous_end':previous_end,
        'current':current,'previous':previous
    }


def daily_completeness(d:date):
    """Validate that mapped report-critical snapshot fields are actually available.

    Only roles that are mapped are required. Breaker/running-hours/environment fields
    remain optional. This prevents an automatic report from being produced from an
    incomplete daily Snapshot/Reset cycle while avoiding fabricated values.
    """
    summary=day_summary(d)
    entities=summary.get('entities') or {}
    role_maps=defaultdict(set)
    for m in _maps():
        if m.get('entity_type') in {'generator','factory','grid'} and m.get('role') in {
            'energy_active','energy_reactive','max_power','max_power_time',
            'max_reactive_power','max_reactive_power_time'
        }:
            role_maps[_entity_key(m)].add(m['role'])
    reasons=[]
    for key,required in role_maps.items():
        st=entities.get(key)
        label=key.split('::',1)[1] or key.split('::',1)[0]
        if not st:
            reasons.append(f'{label}: داده تجهیز پیدا نشد')
            continue
        for role in sorted(required):
            info=st.get('roles',{}).get(role,{})
            if info.get('value') is None:
                fa={
                    'energy_active':'انرژی اکتیو','energy_reactive':'انرژی راکتیو',
                    'max_power':'پیک اکتیو','max_power_time':'زمان پیک اکتیو',
                    'max_reactive_power':'پیک راکتیو','max_reactive_power_time':'زمان پیک راکتیو'
                }.get(role,role)
                reasons.append(f'{label}: {fa} موجود نیست')
    if not summary.get('has_daily_snapshot'):
        reasons.append('Snapshot روزانه کارخانه کامل نیست')
    # de-duplicate while preserving order
    seen=set(); reasons=[x for x in reasons if not (x in seen or seen.add(x))]
    return {'complete':not reasons,'reasons':reasons,'summary':summary}
