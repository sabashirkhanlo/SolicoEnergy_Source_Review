from __future__ import annotations
from pathlib import Path
import re
from html import escape
from datetime import date, timedelta

from ..core import settings as settings_mod, db
from ..core.jalali import format_jalali, gregorian_to_jalali
from ..services.calculations import comparison_summary, day_summary
from .html_pdf import html_to_pdf, img_data
from .common import base_css, logo_src, bars_svg


def _asset_logo():
    return str(Path(__file__).resolve().parents[1]/'assets'/'solico_s_mark.png')


def _asset_group_logo():
    return str(Path(__file__).resolve().parents[1]/'assets'/'solico_s_mark.png')


def _fmt(value, kind):
    if value is None:
        return '—'
    if kind == 'pf':
        return f'{value:.3f}'
    if kind == 'share':
        return f'{value:.1f}%'
    if kind == 'kw':
        return f'{value:,.0f} kW'
    return f'{value:,.0f} kWh'


def _change(a, b, kind):
    if a is None or b is None:
        return '—', 'neutral'
    if kind == 'pf':
        d = a - b
        arrow = '↑' if d > 0 else ('↓' if d < 0 else '→')
        return f'{arrow} {d:+.3f}', ('up' if d > 0 else ('down' if d < 0 else 'neutral'))
    if kind == 'share':
        d = a - b
        arrow = '↑' if d > 0 else ('↓' if d < 0 else '→')
        return f'{arrow} {d:+.1f} واحد درصد', ('up' if d > 0 else ('down' if d < 0 else 'neutral'))
    if b == 0:
        return ('→ 0.0%' if a == 0 else '—'), 'neutral'
    p = (a - b) / abs(b) * 100.0
    arrow = '↑' if p > 0 else ('↓' if p < 0 else '→')
    return f'{arrow} {p:+.1f}%', ('up' if p > 0 else ('down' if p < 0 else 'neutral'))




def _mwh(value, digits=1):
    if value is None:
        return '—'
    try:
        return f'{float(value)/1000.0:,.{digits}f}'
    except Exception:
        return '—'


def _mw(value, digits=2):
    if value is None:
        return '—'
    try:
        return f'{float(value)/1000.0:,.{digits}f}'
    except Exception:
        return '—'


def _weekly_factory_peak(start, end, source_id=None):
    peak=None
    cur=start
    while cur<=end:
        try:
            ds=day_summary(cur, source_id)
            fac=ds.get('factory') or {}
            v=fac.get('max_kw')
            if v is not None and (peak is None or float(v)>float(peak)):
                peak=float(v)
        except Exception:
            pass
        cur+=timedelta(days=1)
    return peak


def _weekly_kpi_html(data, source_id=None):
    if data.get('mode')!='week':
        return ''
    cur=data.get('current') or {}
    peak=_weekly_factory_peak(data['current_start'], data['current_end'], source_id)
    vals=[
        ('🏭','مصرف کارخانه',_mwh(cur.get('factory_energy'),1),'MWh'),
        ('⚡','تولید نیروگاه',_mwh(cur.get('plant_energy'),1),'MWh'),
        ('⌁','دریافت از شبکه',_mwh(cur.get('grid_energy'),1),'MWh'),
        ('📊','سهم نیروگاه',('—' if cur.get('plant_share') is None else f"{float(cur.get('plant_share')):.1f}"),'%'),
        ('📈','پیک مصرف کارخانه',_mw(peak,2),'MW'),
        ('∑','متوسط توان مصرفی',_mw(cur.get('factory_avg'),2),'MW'),
    ]
    cards=''.join(
        f'<div class="weekly-kpi-card"><div class="weekly-kpi-icon">{icon}</div>'
        f'<div class="weekly-kpi-label">{escape(label)}</div>'
        f'<div class="weekly-kpi-value">{value}</div><div class="weekly-kpi-unit">{unit}</div></div>'
        for icon,label,value,unit in vals
    )
    return '<div class="weekly-kpi-wrap"><div class="weekly-kpi-title">شاخص‌های کلیدی هفته جاری</div><div class="weekly-kpi-grid">'+cards+'</div></div>'

def _period_label(start, end, mode):
    if mode == 'day':
        return format_jalali(end)
    if mode == 'month':
        jy, jm, _ = gregorian_to_jalali(end.year, end.month, end.day)
        return f'{jy:04d}/{jm:02d}'
    return f'{format_jalali(start)} تا {format_jalali(end)}'


def _filename(data):
    mode = data['mode']
    if mode == 'month':
        cy, cm, _ = gregorian_to_jalali(data['current_end'].year, data['current_end'].month, data['current_end'].day)
        py, pm, _ = gregorian_to_jalali(data['previous_end'].year, data['previous_end'].month, data['previous_end'].day)
        return f'Comparison_Month_{cy:04d}-{cm:02d}_vs_{py:04d}-{pm:02d}.pdf'
    c = format_jalali(data['current_end']).replace('/', '-')
    p = format_jalali(data['previous_end']).replace('/', '-')
    prefix = 'Comparison_Week' if mode == 'week' else 'Comparison_Day'
    return f'{prefix}_{c}_vs_{p}.pdf'


def _selected_source_name(source_id, source_name=None):
    name=str(source_name or '').strip()
    if not name and source_id is not None:
        try:
            src=next((x for x in db.list_sources() if x.get('id')==source_id),None)
            name=str((src or {}).get('name') or '').strip()
        except Exception:
            pass
    return name or (f'Source_{source_id}' if source_id is not None else '')

def _safe_source_name(name):
    v=re.sub(r'[^0-9A-Za-z_\-\u0600-\u06FF]+','_',str(name or '').strip()).strip('_')
    return v[:60] or 'Source'

def generate(base_day: date, mode='day', source_id=None, source_name=None):
    st = settings_mod.load()
    source_name=_selected_source_name(source_id,source_name)
    data = comparison_summary(base_day, mode, source_id=source_id)
    cur = data['current']
    prev = data['previous']
    metrics = [
        ('مصرف کارخانه', 'factory_energy', 'energy'),
        ('تولید نیروگاه', 'plant_energy', 'energy'),
        ('دریافت از شبکه', 'grid_energy', 'energy'),
        ('سهم نیروگاه از تأمین', 'plant_share', 'share'),
        ('متوسط توان کارخانه', 'factory_avg', 'kw'),
        ('PF کارخانه', 'factory_pf', 'pf'),
        ('PF نیروگاه', 'plant_pf', 'pf'),
        ('PF شبکه', 'grid_pf', 'pf'),
    ]

    rows = []
    for label, key, kind in metrics:
        a = cur.get(key)
        b = prev.get(key)
        change, cls = _change(a, b, kind)
        rows.append(f'<tr><td>{escape(label)}</td><td>{_fmt(a, kind)}</td><td>{_fmt(b, kind)}</td><td class="{cls}">{change}</td></tr>')

    current_label = _period_label(data['current_start'], data['current_end'], mode)
    previous_label = _period_label(data['previous_start'], data['previous_end'], mode)
    mode_fa = {'day': 'روز با روز', 'week': 'هفته با هفته', 'month': 'ماه با ماه'}.get(mode, mode)
    logo = logo_src(st, _asset_logo())
    group_logo = img_data(_asset_group_logo())
    chart = bars_svg(
        {'factory': cur.get('factory_energy'), 'plant': cur.get('plant_energy'), 'grid': cur.get('grid_energy')},
        {'factory': prev.get('factory_energy'), 'plant': prev.get('plant_energy'), 'grid': prev.get('grid_energy')},
    )
    weekly_kpis = _weekly_kpi_html(data, source_id)

    css = base_css(True) + r'''
      .header{align-items:center}
      .header-logos{width:120px;min-width:120px;justify-content:center}.logo-primary{width:62px;height:58px}.logo-group{display:none}
      .weekly-kpi-wrap{margin:8px 0 10px}
      .weekly-kpi-title{background:#21477a;color:#fff;border-radius:7px;padding:6px 10px;text-align:center;font-size:13px;font-weight:800;margin-bottom:7px}
      .weekly-kpi-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:7px}
      .weekly-kpi-card{background:#eef4fb;border:1px solid #c9d9e8;border-radius:8px;padding:8px 5px 7px;text-align:center;min-height:78px}
      .weekly-kpi-icon{font-size:18px;line-height:1;margin-bottom:3px}
      .weekly-kpi-label{font-size:9px;color:#40576d;font-weight:700;line-height:1.25;min-height:22px}
      .weekly-kpi-value{font-size:17px;font-weight:900;color:#123f68;direction:ltr;unicode-bidi:isolate;line-height:1.15;margin-top:2px}
      .weekly-kpi-unit{font-size:9px;font-weight:700;color:#2e6db4;margin-top:1px}
      .compare-head{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0 10px}
      .period{border:1px solid #cbdbe8;border-radius:9px;padding:9px;background:#f7fbff;text-align:center}
      .period .cap{font-size:9px;color:#6b8091}.period .val{font-size:17px;font-weight:800;color:#123f68;margin-top:2px}
      .cmp-table{font-size:11px;margin-top:8px}.cmp-table th{font-size:11px;padding:7px}.cmp-table td{padding:7px}
      .up,.down,.neutral{color:#26384b;font-weight:700}
      .chart-panel{margin:8px 0 8px}
      .chart-panel .panel{min-height:188px;padding:8px}
      .chart-note{font-size:9px;color:#6b8091;text-align:center;margin-top:2px}
      .signatures{display:grid;grid-template-columns:1fr 1fr;gap:34px;margin-top:12px}
      .sig{border-top:1px solid #cdd9e4;padding-top:6px;min-height:56px;text-align:center;font-size:10px;color:#26384b}
      .sig .role{font-weight:800;color:#123f68;margin-bottom:8px}
      .sig .name{margin-top:22px;font-size:10px;color:#6b7d8e}
    '''

    html = f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{css}</style></head><body><div class="page">
    <div class="header"><div class="header-logos"><img class="logo-primary" src="{logo}"></div><div class="headtxt"><h1>نیروگاه 25 مگاواتی کاله</h1><h2>گزارش مقایسه عملکرد انرژی - {mode_fa}</h2><div class="date">دوره فعلی در برابر دوره قبل</div></div></div>
    {weekly_kpis}
    <div class="compare-head"><div class="period"><div class="cap">دوره فعلی</div><div class="val">{current_label}</div></div><div class="period"><div class="cap">دوره قبل</div><div class="val">{previous_label}</div></div></div>
    <div class="chart-panel"><div class="panel"><h3>نمودار مقایسه انرژی</h3>{chart}<div class="chart-note">آبی: دوره فعلی &nbsp;&nbsp; خاکستری: دوره قبل</div></div></div>
    <table class="cmp-table"><thead><tr><th>شاخص</th><th>دوره فعلی</th><th>دوره قبل</th><th>تغییر</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <div class="signatures"><div class="sig"><div class="role">{escape(st.get('prepared_title','تهیه‌کننده گزارش'))}</div><div>{escape(st.get('prepared_name','')) or '&nbsp;'}</div><div class="name">محل امضا</div></div><div class="sig"><div class="role">مدیر نیروگاه</div><div>{escape(st.get('manager_name','')) or '&nbsp;'}</div><div class="name">محل امضای مدیر نیروگاه</div></div></div>
    <div class="footer"><span>مدیریت نیروگاه و انرژی</span><span>گزارش مقایسه - {mode_fa}</span></div>
    </div></body></html>'''

    folder = settings_mod.ensure_output_dir() / 'Comparison'
    if source_id is not None:
        folder = folder / _safe_source_name(source_name)
    folder.mkdir(parents=True, exist_ok=True)
    filename=_filename(data)
    if source_id is not None:
        stem=Path(filename).stem; filename=f'{stem}_{_safe_source_name(source_name)}.pdf'
    out = folder / filename
    html_to_pdf(html, out, landscape=True)
    return str(out)
