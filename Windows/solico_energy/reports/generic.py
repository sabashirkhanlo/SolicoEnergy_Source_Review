from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
import math
import re

from .common import base_css, logo_src
from .html_pdf import html_to_pdf, img_data
from ..core import db, settings as settings_mod
from ..core.jalali import format_jalali, jalali_month_bounds
from ..services.importer import import_source


ENERGY_ROLES = {
    'energy_active', 'energy_reactive', 'max_power', 'max_power_time',
    'max_reactive_power', 'max_reactive_power_time', 'running_hours',
    'breaker_status', 'temp_min', 'temp_min_time', 'temp_max', 'temp_max_time',
    'humidity_min', 'humidity_min_time', 'humidity_max', 'humidity_max_time',
}

ROLE_LABELS = {
    'ignore': 'نادیده گرفته شود',
    'energy_active': 'انرژی اکتیو تجمعی',
    'energy_reactive': 'انرژی راکتیو تجمعی',
    'max_power': 'حداکثر توان اکتیو',
    'max_power_time': 'زمان حداکثر توان اکتیو',
    'max_reactive_power': 'حداکثر توان راکتیو',
    'max_reactive_power_time': 'زمان حداکثر توان راکتیو',
    'running_hours': 'ساعت کارکرد تجمعی',
    'breaker_status': 'وضعیت بریکر',
    'temp_min': 'حداقل دما',
    'temp_min_time': 'زمان حداقل دما',
    'temp_max': 'حداکثر دما',
    'temp_max_time': 'زمان حداکثر دما',
    'humidity_min': 'حداقل رطوبت',
    'humidity_min_time': 'زمان حداقل رطوبت',
    'humidity_max': 'حداکثر رطوبت',
    'humidity_max_time': 'زمان حداکثر رطوبت',
}


def is_energy_source(source_id=None, source_name=None):
    """Keep the legacy energy report untouched; route other mapped sources to generic reports."""
    name = str(source_name or '').strip()
    if name in {'گزارش', 'انرژی', 'گزارش انرژی'}:
        return True
    if source_id is None:
        return False
    try:
        roles = {str(m.get('role') or '').strip() for m in db.mappings(source_id, enabled_only=True)}
    except Exception:
        return False
    # Energy sources have at least one of the core energy roles. Generic sensor sources normally
    # use user-defined roles such as دما، فشار، توان، لرزش, etc.
    return bool(roles & {'energy_active', 'energy_reactive', 'max_power', 'max_reactive_power', 'breaker_status'})


def _asset(name):
    return str(Path(__file__).resolve().parents[1] / 'assets' / name)


def _safe_source_name(name):
    v = re.sub(r'[^0-9A-Za-z_\-\u0600-\u06FF]+', '_', str(name or '').strip()).strip('_')
    return v[:60] or 'Source'


def _selected_source_name(source_id, source_name=None):
    name = str(source_name or '').strip()
    if not name and source_id is not None:
        try:
            src = next((x for x in db.list_sources() if x.get('id') == source_id), None)
            name = str((src or {}).get('name') or '').strip()
        except Exception:
            pass
    return name or f'Source_{source_id}'


def _period_epochs(start: datetime, end_exclusive: datetime):
    return float(start.timestamp()), float(end_exclusive.timestamp())


def _fmt_num(v, dec=2):
    if v is None:
        return '—'
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return '—'
        # Integers stay compact; measurements keep two decimals by default.
        if abs(x - round(x)) < 1e-9:
            return f'{x:,.0f}'
        return f'{x:,.{dec}f}'
    except Exception:
        return escape(str(v))


def _fmt_dt(epoch, include_date=False):
    if epoch is None:
        return '—'
    try:
        d = datetime.fromtimestamp(float(epoch))
        if include_date:
            return f'{format_jalali(d.date())} {d:%H:%M}'
        return d.strftime('%H:%M')
    except Exception:
        return '—'


def _role_label(role):
    r = str(role or '').strip()
    return ROLE_LABELS.get(r, r or 'بدون نقش')


def _display_name(mapping):
    return str(mapping.get('display_name') or '').strip() or str(mapping.get('tag_name') or '').strip()


def _equipment_name(mapping):
    return str(mapping.get('entity_name') or '').strip() or 'بدون نام تجهیز'


def _tag_stats(con, source_id, mapping, start_epoch, end_epoch, include_date=False):
    tag = mapping['tag_name']
    args = (source_id, tag, start_epoch, end_epoch)
    base = 'source_id=? AND tag_name=? AND ts_epoch>=? AND ts_epoch<?'
    agg = con.execute(
        f'''SELECT COUNT(*) AS record_count,
                   COUNT(value_num) AS numeric_count,
                   AVG(value_num) AS avg_v,
                   MIN(value_num) AS min_v,
                   MAX(value_num) AS max_v
            FROM records WHERE {base}''', args
    ).fetchone()
    latest = con.execute(
        f'''SELECT ts_epoch,value_num,value_text FROM records WHERE {base}
            ORDER BY ts_epoch DESC LIMIT 1''', args
    ).fetchone()
    min_row = max_row = None
    if agg and int(agg['numeric_count'] or 0) > 0:
        min_row = con.execute(
            f'''SELECT ts_epoch,value_num FROM records WHERE {base} AND value_num IS NOT NULL
                ORDER BY value_num ASC, ts_epoch ASC LIMIT 1''', args
        ).fetchone()
        max_row = con.execute(
            f'''SELECT ts_epoch,value_num FROM records WHERE {base} AND value_num IS NOT NULL
                ORDER BY value_num DESC, ts_epoch ASC LIMIT 1''', args
        ).fetchone()

    # Uniformly sampled trend (max ~140 points), using SQLite window functions so very large
    # month-long CSV archives are not loaded into memory.
    trend = []
    try:
        trend_rows = con.execute(
            f'''WITH ordered AS (
                    SELECT ts_epoch,value_num,
                           ROW_NUMBER() OVER (ORDER BY ts_epoch) AS rn,
                           COUNT(*) OVER () AS cnt
                    FROM records
                    WHERE {base} AND value_num IS NOT NULL
                )
                SELECT ts_epoch,value_num FROM ordered
                WHERE cnt<=140 OR rn=1 OR rn=cnt OR (rn % CAST((cnt+139)/140 AS INTEGER))=1
                ORDER BY ts_epoch LIMIT 160''', args
        ).fetchall()
        trend = [(float(r['ts_epoch']), float(r['value_num'])) for r in trend_rows if r['value_num'] is not None]
    except Exception:
        # Conservative fallback for older SQLite builds: the first 140 ordered points.
        trend_rows = con.execute(
            f'''SELECT ts_epoch,value_num FROM records WHERE {base} AND value_num IS NOT NULL
                ORDER BY ts_epoch LIMIT 140''', args
        ).fetchall()
        trend = [(float(r['ts_epoch']), float(r['value_num'])) for r in trend_rows if r['value_num'] is not None]

    latest_value = None
    if latest:
        latest_value = latest['value_num'] if latest['value_num'] is not None else latest['value_text']

    return {
        'mapping': mapping,
        'record_count': int((agg or {})['record_count'] or 0) if agg else 0,
        'numeric_count': int((agg or {})['numeric_count'] or 0) if agg else 0,
        'avg': agg['avg_v'] if agg else None,
        'min': agg['min_v'] if agg else None,
        'max': agg['max_v'] if agg else None,
        'min_time': min_row['ts_epoch'] if min_row else None,
        'max_time': max_row['ts_epoch'] if max_row else None,
        'latest': latest_value,
        'latest_time': latest['ts_epoch'] if latest else None,
        'trend': trend,
        'include_date': include_date,
    }


def _collect(source_id, start, end_exclusive, include_date=False):
    mappings = [m for m in db.mappings(source_id, enabled_only=True)
                if str(m.get('role') or '').strip() != 'ignore']
    start_epoch, end_epoch = _period_epochs(start, end_exclusive)
    stats = []
    with db.connect() as con:
        for m in mappings:
            stats.append(_tag_stats(con, source_id, m, start_epoch, end_epoch, include_date))
    return stats


def _header(st, title, subtitle, period_text):
    primary = logo_src(st, _asset('kalleh_logo.png'))
    group = img_data(_asset('solico_group_logo.png'))
    return f'''<div class="header">
      <div class="header-logos"><img class="logo-primary" src="{primary}"><img class="logo-group" src="{group}"></div>
      <div class="headtxt"><h1>نیروگاه 25 مگاواتی کاله</h1><h2>{escape(title)}</h2><div class="date">{escape(subtitle)} &nbsp; | &nbsp; {escape(period_text)}</div></div>
      <div style="min-width:145px"></div>
    </div>'''


def _trend_svg(points, unit=''):
    if len(points) < 2:
        return '<div class="nodata">داده کافی برای نمودار وجود ندارد</div>'
    vals = [v for _, v in points]
    mn, mx = min(vals), max(vals)
    span = (mx - mn) or 1.0
    w, h = 620, 165
    left, right, top, bottom = 48, 14, 14, 30
    pw, ph = w-left-right, h-top-bottom
    t0, t1 = points[0][0], points[-1][0]
    tsp = (t1-t0) or 1.0
    xy = []
    for t, v in points:
        x = left + (t-t0)/tsp*pw
        y = top + ph - (v-mn)/span*ph
        xy.append(f'{x:.1f},{y:.1f}')
    first_label = datetime.fromtimestamp(points[0][0]).strftime('%m/%d %H:%M')
    last_label = datetime.fromtimestamp(points[-1][0]).strftime('%m/%d %H:%M')
    return f'''<svg viewBox="0 0 {w} {h}" class="generic-chart">
      <line x1="{left}" y1="{top+ph}" x2="{left+pw}" y2="{top+ph}" stroke="#aebdca"/>
      <line x1="{left}" y1="{top}" x2="{left}" y2="{top+ph}" stroke="#aebdca"/>
      <polyline fill="none" stroke="#2E6DB4" stroke-width="2.5" points="{' '.join(xy)}"/>
      <text x="{left-7}" y="{top+8}" text-anchor="end">{escape(_fmt_num(mx))}</text>
      <text x="{left-7}" y="{top+ph}" text-anchor="end">{escape(_fmt_num(mn))}</text>
      <text x="{left}" y="{h-7}" text-anchor="start">{escape(first_label)}</text>
      <text x="{left+pw}" y="{h-7}" text-anchor="end">{escape(last_label)}</text>
      <text x="{left+pw/2}" y="12" text-anchor="middle">{escape(unit)}</text>
    </svg>'''


def _generic_css():
    return base_css(False) + r'''
    .generic-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0 14px}
    .generic-summary .card{min-height:82px}.generic-summary .value{font-size:20px}
    .source-banner{background:#eef4f9;border:1px solid #d6e2ee;border-radius:8px;padding:9px 12px;margin:8px 0 12px;font-size:11px;display:flex;justify-content:space-between;gap:20px}
    .group-block{margin-top:14px;page-break-inside:auto}.group-title{background:#1B3A6B;color:#fff;border-radius:6px;padding:7px 10px;font-weight:800;font-size:13px}
    .group-sub{font-size:10px;color:#607386;margin:5px 2px}.stats-table{font-size:8.6px}.stats-table th,.stats-table td{padding:5px 4px}
    .chart-card{border:1px solid #dbe5ef;border-radius:9px;padding:8px;margin:9px 0;page-break-inside:avoid}
    .chart-head{display:flex;justify-content:space-between;gap:12px;font-size:11px;font-weight:700;color:#123f68;margin-bottom:3px}
    .chart-meta{font-size:9px;color:#6a7e90;font-weight:400}.generic-chart{width:100%;height:165px}.generic-chart text{font-size:8px;fill:#617789}
    .empty{border:1px dashed #cbd8e4;border-radius:8px;padding:30px;text-align:center;color:#71869a;margin-top:20px}
    @media print{thead{display:table-header-group}.chart-card,.group-title{break-inside:avoid}}
    '''


def _build_html(source_id, source_name, title, subtitle, period_text, stats, report_no, st):
    equipment = sorted({_equipment_name(s['mapping']) for s in stats})
    roles = sorted({_role_label(s['mapping'].get('role')) for s in stats})
    total_records = sum(s['record_count'] for s in stats)
    header = _header(st, title, subtitle, period_text)

    cards = f'''<div class="generic-summary">
      <div class="card"><div class="label">تگ‌های فعال</div><div class="value">{len(stats)}</div></div>
      <div class="card"><div class="label">تجهیزها</div><div class="value">{len(equipment)}</div></div>
      <div class="card"><div class="label">نقش‌ها</div><div class="value">{len(roles)}</div></div>
      <div class="card"><div class="label">رکوردهای دوره</div><div class="value">{total_records:,}</div></div>
    </div>'''
    banner = f'''<div class="source-banner"><span><b>منبع داده:</b> {escape(source_name)}</span><span><b>مبنای گزارش:</b> Tagهای فعال و Mapping تعریف‌شده برای همین منبع</span></div>'''

    if not stats:
        body = '<div class="empty">برای این منبع هیچ Tag فعالی با نقش قابل گزارش تعریف نشده است. ابتدا در «تعریف تگ‌ها» Mapping را ذخیره کنید.</div>'
    else:
        groups = defaultdict(list)
        for s in stats:
            m = s['mapping']
            groups[(_equipment_name(m), _role_label(m.get('role')))].append(s)
        chunks = []
        for (equipment_name, role_name), items in groups.items():
            rows = []
            charts = []
            for s in items:
                m = s['mapping']; unit = str(m.get('unit') or '').strip()
                include_date = bool(s.get('include_date'))
                rows.append(f'''<tr>
                  <td>{escape(_display_name(m))}</td><td dir="ltr">{escape(str(m.get('tag_name') or ''))}</td><td>{escape(unit or '—')}</td>
                  <td>{_fmt_num(s['min'])}</td><td>{_fmt_dt(s['min_time'], include_date)}</td><td>{_fmt_num(s['avg'])}</td>
                  <td>{_fmt_num(s['max'])}</td><td>{_fmt_dt(s['max_time'], include_date)}</td><td>{escape(_fmt_num(s['latest']) if isinstance(s['latest'], (int,float)) else str(s['latest'] if s['latest'] not in (None,'') else '—'))}</td>
                  <td>{_fmt_dt(s['latest_time'], include_date)}</td><td>{s['record_count']}</td>
                </tr>''')
                charts.append(f'''<div class="chart-card"><div class="chart-head"><span>{escape(_display_name(m))}</span><span class="chart-meta">{escape(equipment_name)} | {escape(role_name)} | {escape(unit or 'بدون واحد')}</span></div>{_trend_svg(s['trend'], unit)}</div>''')
            chunks.append(f'''<div class="group-block">
              <div class="group-title">{escape(equipment_name)} — {escape(role_name)}</div>
              <div class="group-sub">هر ردیف دقیقاً از Tag فعال و Mapping همین منبع ساخته شده است.</div>
              <table class="stats-table"><thead><tr><th>نام نمایشی</th><th>Tag در CSV</th><th>واحد</th><th>کمینه</th><th>زمان کمینه</th><th>میانگین</th><th>بیشینه</th><th>زمان بیشینه</th><th>آخرین مقدار</th><th>زمان آخرین</th><th>تعداد داده</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
              {''.join(charts)}
            </div>''')
        body = ''.join(chunks)

    prepared_title = escape(st.get('prepared_title', 'تهیه‌کننده گزارش'))
    prepared_name = escape(st.get('prepared_name', ''))
    manager_title = escape(st.get('manager_title', 'مدیر/مسئول نیروگاه'))
    manager_name = escape(st.get('manager_name', ''))
    footer = f'''<div class="note">این گزارش برای منابع غیرانرژی به‌صورت پویا ساخته می‌شود؛ فقط Tagهای فعال همان منبع و نام تجهیز، نقش، نام نمایشی و واحدی که در Mapping تعریف شده‌اند وارد گزارش می‌شوند.</div>
      <div class="signatures"><div class="sig"><b>{prepared_title}</b><br>{prepared_name}</div><div class="sig"><b>{manager_title}</b><br>{manager_name}</div></div>
      <div class="footer"><span>شماره گزارش: <span dir="ltr">{escape(report_no)}</span></span><span>{escape(st.get('department',''))}</span></div>'''
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{_generic_css()}</style></head><body><div class="page">{header}<div class="section-title">{escape(title)}</div>{banner}{cards}{body}{footer}</div></body></html>'''


def generate_daily(d: date, regenerate=False, source_id=None, source_name=None):
    if source_id is None:
        raise ValueError('برای گزارش پارامتری، منبع داده باید مشخص باشد.')
    import_source(source_id)
    source_name = _selected_source_name(source_id, source_name)
    st = settings_mod.load()
    start = datetime.combine(d, time.min)
    end = start + timedelta(days=1)
    stats = _collect(source_id, start, end, include_date=False)
    safe = _safe_source_name(source_name)
    outroot = settings_mod.ensure_output_dir() / 'Daily' / safe / f'Parameters_{d:%Y-%m}'
    outroot.mkdir(parents=True, exist_ok=True)
    out = outroot / f'Parameter_Report_{d:%Y-%m-%d}_{safe}.pdf'
    report_no = f'PR-{d:%Y%m%d}-S{source_id}'
    html = _build_html(source_id, source_name, f'گزارش روزانه {source_name}', 'گزارش پارامترهای تجهیزات', f'{format_jalali(d)} | {d.isoformat()}', stats, report_no, st)
    html_to_pdf(html, out)
    db.mark_report(f'generic_daily_s{source_id}', d.isoformat(), str(out))
    return str(out)


def generate_monthly(jy: int, jm: int, regenerate=False, source_id=None, source_name=None):
    if source_id is None:
        raise ValueError('برای گزارش پارامتری، منبع داده باید مشخص باشد.')
    import_source(source_id)
    source_name = _selected_source_name(source_id, source_name)
    st = settings_mod.load()
    start_date, end_date = jalali_month_bounds(jy, jm)
    start = datetime.combine(start_date, time.min)
    end = datetime.combine(end_date + timedelta(days=1), time.min)
    stats = _collect(source_id, start, end, include_date=True)
    safe = _safe_source_name(source_name)
    outroot = settings_mod.ensure_output_dir() / 'Monthly' / safe / 'Parameters'
    outroot.mkdir(parents=True, exist_ok=True)
    out = outroot / f'Parameter_Monthly_Report_{jy:04d}-{jm:02d}_{safe}.pdf'
    report_no = f'PMR-{jy:04d}{jm:02d}-S{source_id}'
    html = _build_html(source_id, source_name, f'گزارش ماهانه {source_name}', 'گزارش پارامترهای تجهیزات', f'{jy:04d}/{jm:02d}', stats, report_no, st)
    html_to_pdf(html, out)
    db.mark_report(f'generic_monthly_s{source_id}', f'{jy:04d}-{jm:02d}', str(out))
    return str(out)
