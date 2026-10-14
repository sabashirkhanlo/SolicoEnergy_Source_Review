from __future__ import annotations
from pathlib import Path
from datetime import date
import re
from html import escape
from .common import *
from .html_pdf import html_to_pdf, merge_pdfs
from ..core import settings as settings_mod, db
from ..core.jalali import format_jalali, jalali_month_bounds
from ..services.calculations import monthly_summary
from ..services.importer import import_all, import_source
from .daily import _asset_logo, _header, _mwh, _mw


def _line_svg(days):
    """Three daily energy series with real gaps kept as gaps, never bridged."""
    vals=[]
    for d in days:
        vals.append((d['date'],(d.get('factory') or {}).get('active_kwh'),d.get('plant_energy'),(d.get('grid') or {}).get('active_kwh')))
    nums=[float(v) for r in vals for v in r[1:] if v is not None]
    mx=max(nums) if nums else 1.0
    w,h=760,275; left,top=42,18; pw,ph=690,205
    n=max(len(vals)-1,1)

    def xy(i,v):
        x=left+pw*i/n
        y=top+ph-(float(v)/mx)*ph
        return x,y

    def segments(ix,color):
        groups=[]; cur=[]
        for i,r in enumerate(vals):
            v=r[ix]
            if v is None:
                if cur: groups.append(cur); cur=[]
                continue
            cur.append(xy(i,v))
        if cur: groups.append(cur)
        out=[]
        for g in groups:
            pts=' '.join(f'{x:.1f},{y:.1f}' for x,y in g)
            if len(g)==1:
                x,y=g[0]; out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{color}"/>')
            else:
                out.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{pts}"/>')
        return ''.join(out)

    labels=[]
    for i,r in enumerate(vals):
        if i%2==0 or i==len(vals)-1:
            x=left+pw*i/n
            day=format_jalali(r[0]).split('/')[-1]
            labels.append(f'<text x="{x:.1f}" y="{top+ph+18}" font-size="7" text-anchor="middle" fill="#60798c">{day}</text>')
    return f'''<svg viewBox="0 0 {w} {h}" style="width:100%;height:275px">
      <line x1="{left}" y1="{top+ph}" x2="{left+pw}" y2="{top+ph}" stroke="#9fb0c0"/>
      {segments(1,'#1b79c9')}{segments(2,'#2f9e44')}{segments(3,'#7b8794')}
      {''.join(labels)}
      <g font-size="10" font-family="Tahoma,Arial">
        <circle cx="130" cy="260" r="4" fill="#1b79c9"/><text x="142" y="264">مصرف کارخانه</text>
        <circle cx="315" cy="260" r="4" fill="#2f9e44"/><text x="327" y="264">تولید نیروگاه</text>
        <circle cx="500" cy="260" r="4" fill="#7b8794"/><text x="512" y="264">دریافت از شبکه</text>
      </g>
    </svg>'''


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


def generate(jy:int,jm:int,regenerate=False,source_id=None,source_name=None):
    if source_id is None: import_all()
    else: import_source(source_id)
    source_name=_selected_source_name(source_id,source_name)
    st=settings_mod.load(); start,end=jalali_month_bounds(jy,jm); s=monthly_summary(start,end,source_id)
    temporary=bool(start <= date.today() <= end)
    title=st.get('monthly_title','گزارش ماهانه') + (' — موقت تا امروز' if temporary else '')
    outroot=settings_mod.ensure_output_dir()/'Monthly'
    if source_id is None:
        outroot.mkdir(parents=True,exist_ok=True); suffix='_TEMP' if temporary else ''; out=outroot/f'Monthly_Report_{jy:04d}-{jm:02d}{suffix}.pdf'; report_no=f'MR-{jy:04d}-{jm:02d}' + ('-TEMP' if temporary else '')
    else:
        safe=_safe_source_name(source_name); outroot=outroot/safe; outroot.mkdir(parents=True,exist_ok=True); suffix='_TEMP' if temporary else ''; out=outroot/f'Monthly_Report_{jy:04d}-{jm:02d}_{safe}{suffix}.pdf'; report_no=f'MR-{jy:04d}-{jm:02d}-S{source_id}' + ('-TEMP' if temporary else '')

    peak=s.get('peak'); hi=s.get('highest_day'); lo=s.get('lowest_day')
    peak_kw=peak[0] if peak else None
    valid_days=int(s.get('valid_day_count') or 0)
    total_days=len(s.get('days') or [])
    p1=f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{base_css(False)}</style></head><body><div class="page">{_header(st,end,title)}
    <div class="section-title">خلاصه عملکرد ماه {jy:04d}/{jm:02d}{' — موقت تا امروز' if temporary else ''}</div><div class="cards">
    <div class="card"><div class="label">مصرف ماهانه کارخانه</div><div class="value">{_mwh(s.get('factory_energy'))}<span class="unit">MWh</span></div></div>
    <div class="card"><div class="label">تولید ماهانه نیروگاه</div><div class="value">{_mwh(s.get('plant_energy'))}<span class="unit">MWh</span></div></div>
    <div class="card"><div class="label">دریافت ماهانه از شبکه</div><div class="value">{_mwh(s.get('grid_energy'))}<span class="unit">MWh</span></div></div>
    <div class="card"><div class="label">سهم نیروگاه در ماه</div><div class="value">{fmt(s.get('plant_share'),1)}<span class="unit">%</span></div></div>
    <div class="card"><div class="label">پیک مصرف کارخانه در ماه</div><div class="value">{_mw(peak_kw,2)}<span class="unit">MW</span></div></div>
    <div class="card"><div class="label">متوسط توان مصرف کارخانه</div><div class="value">{_mw(s.get('factory_avg'),2)}<span class="unit">MW</span></div></div></div>
    <div class="panel" style="margin-top:10px"><h3>روند روزانه انرژی</h3>{_line_svg(s['days'])}</div>
    <div class="summary"><b>خلاصه ماه:</b> مجموع‌ها از روزهای دارای داده معتبر محاسبه شده‌اند ({valid_days} روز از {total_days} روز ماه). متوسط توان بر اساس مجموع انرژی و مجموع ساعات واقعی معتبر محاسبه شده است. بیشترین مصرف روزانه: {_mwh(hi[1] if hi else None)} MWh در {format_jalali(hi[0]) if hi else '—'}؛ کمترین مصرف روزانه: {_mwh(lo[1] if lo else None)} MWh در {format_jalali(lo[0]) if lo else '—'}.</div>
    <div class="footer"><span>شماره گزارش: {report_no}</span><span>مدیریت نیروگاه و انرژی</span></div></div></body></html>'''

    rows=''
    for d in s['days']:
        f=d.get('factory') or {}; g=d.get('grid') or {}
        rows+=f'<tr><td>{format_jalali(d["date"])}</td><td>{_mwh(f.get("active_kwh"))}</td><td>{_mwh(d.get("plant_energy"))}</td><td>{_mwh(g.get("active_kwh"))}</td><td>{fmt(d.get("plant_share"),1)}</td><td>{_mw(f.get("max_kw"),2)}</td><td>{_mw(f.get("avg_kw"),2)}</td></tr>'
    p2=f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{base_css(True)}</style></head><body><div class="page">{_header(st,end,'جدول روزانه ماه')}
    <table><thead><tr><th>تاریخ</th><th>مصرف کارخانه MWh</th><th>تولید نیروگاه MWh</th><th>شبکه MWh</th><th>سهم نیروگاه %</th><th>پیک کارخانه MW</th><th>متوسط توان MW</th></tr></thead><tbody>{rows}</tbody></table>
    <div class="footer"><span>شماره گزارش: {report_no}</span><span>مدیریت نیروگاه و انرژی</span></div></div></body></html>'''
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        a=Path(td)/'a.pdf'; b=Path(td)/'b.pdf'; html_to_pdf(p1,a); html_to_pdf(p2,b); merge_pdfs([a,b],out)
    db.mark_report('monthly_partial' if temporary else 'monthly',f'{jy:04d}-{jm:02d}',str(out)); return str(out)
