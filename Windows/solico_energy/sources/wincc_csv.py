from __future__ import annotations
import csv, re
from datetime import datetime
from pathlib import Path

ALIASES={
 'varname':['varname','tag','tagname','variablename','name'],
 'timestring':['timestring','timestamp','datetime','time','date/time'],
 'varvalue':['varvalue','value','tagvalue'],
 'validity':['validity','quality','status'],
 'time_ms':['time_ms','timems','utc','timestamp_ms','time ms']
}

def detect_encoding(path:Path):
    raw=path.read_bytes()[:8192]
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'): return 'utf-16'
    if raw.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
    for enc in ('utf-8-sig','utf-8','cp1256','cp1252'):
        try: raw.decode(enc); return enc
        except Exception: pass
    return 'utf-8'

def sniff(path):
    p=Path(path); enc=detect_encoding(p)
    sample=p.read_text(encoding=enc, errors='replace')[:10000]
    try: dialect=csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except Exception:
        class D(csv.excel): delimiter=';'
        dialect=D
    return enc,dialect

def _norm(s): return re.sub(r'[^a-z0-9_]','',str(s).strip().lower().replace(' ','_'))

def _fieldmap(headers):
    nh={_norm(h):h for h in headers}
    out={}
    for k,als in ALIASES.items():
        for a in als:
            if _norm(a) in nh: out[k]=nh[_norm(a)]; break
    if 'varname' not in out or 'varvalue' not in out:
        raise ValueError(f'ستون‌های VarName و VarValue پیدا نشدند. ستون‌های موجود: {headers}')
    if 'timestring' not in out and 'time_ms' not in out:
        raise ValueError('هیچ ستون زمان (TimeString/Time_ms) پیدا نشد.')
    return out

def parse_dt(value):
    s = str(value or '').strip().strip('"')
    if not s:
        return None
    fmts = [
        '%m/%d/%Y %I:%M:%S %p',
        '%m/%d/%Y %I:%M:%S.%f %p',
        '%m/%d/%Y %I:%M %p',
        '%m/%d/%Y %H:%M:%S.%f',
        '%m/%d/%Y %H:%M:%S',
        '%m/%d/%Y %H:%M',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%d.%m.%Y %H:%M:%S.%f',
        '%d.%m.%Y %H:%M:%S',
        '%d.%m.%Y %H:%M',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M'
    ]
    for f in fmts:
        try: return datetime.strptime(s,f)
        except ValueError: pass
    try:return datetime.fromisoformat(s.replace('Z','+00:00')).replace(tzinfo=None)
    except Exception:return None

def parse_num(value):
    s=str(value or '').strip().replace(' ','')
    if not s:return None
    if ',' in s and '.' not in s: s=s.replace(',','.')
    else: s=s.replace(',','')
    try:return float(s)
    except Exception:return None

def iter_rows(path):
    p=Path(path); enc,dialect=sniff(p)
    with p.open('r',encoding=enc,errors='replace',newline='') as f:
        reader=csv.DictReader(f,dialect=dialect)
        fm=_fieldmap(reader.fieldnames or [])
        for row in reader:
            tag=str(row.get(fm['varname'],'')).strip().strip('"')
            if not tag: continue
            dt=parse_dt(row.get(fm.get('timestring','')))
            tms=str(row.get(fm.get('time_ms',''),'') or '').strip()
            if dt is None and tms:
                try:
                    n=float(tms)
                    if n>1e12:n/=1000.0
                    if n>1e9:dt=datetime.fromtimestamp(n)
                except Exception:pass
            if dt is None: continue
            rawv=str(row.get(fm['varvalue'],'') or '').strip().strip('"')
            yield {'tag_name':tag,'dt':dt,'value_num':parse_num(rawv),'value_text':rawv,'validity':str(row.get(fm.get('validity',''),'') or '').strip(),'time_ms':tms}

def scan_tags(path, limit=None):
    tags=set(); n=0
    for r in iter_rows(path):
        tags.add(r['tag_name']); n+=1
        if limit and n>=limit:break
    return sorted(tags, key=str.lower)
