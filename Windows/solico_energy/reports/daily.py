from __future__ import annotations

from datetime import timedelta, datetime, date
from pathlib import Path
from html import escape
import tempfile
import re

from .common import *
from .html_pdf import html_to_pdf, merge_pdfs, img_data
from ..core import settings as settings_mod, db
from ..core.jalali import format_jalali
from ..services.calculations import day_summary
from ..services.importer import import_all, import_source


_FA_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
_JMONTHS = ['', 'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور', 'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند']
_WEEKDAYS_FA = {0:'دوشنبه',1:'سه‌شنبه',2:'چهارشنبه',3:'پنج‌شنبه',4:'جمعه',5:'شنبه',6:'یکشنبه'}
_MONTHS_EN = ['', 'January','February','March','April','May','June','July','August','September','October','November','December']


def _fa(s):
    return str(s).translate(_FA_DIGITS)


def _num(v, dec=0, dash='—'):
    if v is None:
        return dash
    try:
        return _fa(f'{float(v):,.{dec}f}')
    except Exception:
        return escape(str(v))


def _mwh(kwh, dec=1):
    return '—' if kwh is None else _num(float(kwh)/1000.0, dec)


def _mw(kw, dec=2):
    return '—' if kw is None else _num(float(kw)/1000.0, dec)


def _pct(v, dec=1):
    return '—' if v is None else _fa(f'{float(v):.{dec}f}') + '٪'


def _time_fa(v):
    t = time_only(v)
    return _fa(t) if t != '—' else '—'


def _jalali_parts(d):
    return tuple(map(int, format_jalali(d).split('/')))


def _jalali_long(d):
    jy, jm, jd = _jalali_parts(d)
    return f"{_fa(jd)} {_JMONTHS[jm]} {_fa(jy)}"


def _date_line(d):
    return f"{_WEEKDAYS_FA[d.weekday()]} {_jalali_long(d)} &nbsp;|&nbsp; {d.day} {_MONTHS_EN[d.month]} {d.year}"


def _change(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (float(cur)-float(prev))/float(prev)*100.0


def _change_html(cur, prev):
    ch = _change(cur, prev)
    if ch is None:
        return '<div class="kpi-change">—</div><div class="kpi-sub">نسبت به روز قبل</div>'
    arrow = '↑' if ch >= 0 else '↓'
    return f'<div class="kpi-change">{arrow} {_pct(abs(ch))}</div><div class="kpi-sub">نسبت به روز قبل</div>'


def _target_css():
    return r'''
@page { size: A4 portrait; margin: 0; }
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 210mm; min-height: 297mm; }
body {
  font-family: "Vazirmatn", Tahoma, "Segoe UI", Arial, sans-serif;
  background: white; color: #1a2e4a; direction: rtl;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.page { width: 210mm; min-height: 297mm; margin: 0 auto; background: white; padding: 0; }
.header { background:white; padding:18px 28px 14px; border-bottom:3px solid #1B3A6B; display:flex; align-items:flex-start; justify-content:space-between; }
.header-center { text-align:center; flex:1; padding-top:4px; }
.header-center h1 { font-size:26px; font-weight:900; color:#1B3A6B; line-height:1.3; }
.header-center h2 { font-size:17px; font-weight:600; color:#2E6DB4; margin-top:3px; }
.header-center .date-row { font-size:13px; color:#555; margin-top:6px; font-weight:500; }
.header-logos { width:120px; min-width:120px; text-align:center; display:flex; align-items:center; justify-content:center; padding-top:2px; }
.logo-primary { display:none; }
.logo-group { width:62px; height:58px; object-fit:contain; display:block; }
.header-meta { width:120px; font-size:11px; color:#444; text-align:right; line-height:2; }
.header-meta strong { color:#1B3A6B; font-weight:700; }
.content { padding:16px 16px 12px; }
.section-title { background:#1B3A6B; color:white; text-align:center; font-size:16px; font-weight:700; padding:9px; margin:0 0 14px; border-radius:4px; }
.kpi-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:16px; }
.kpi-card { background:#E8F0FB; border-radius:8px; padding:12px 8px 10px; text-align:center; border:1px solid #c5d8f0; min-height:120px; }
.kpi-icon { font-size:22px; margin-bottom:4px; line-height:1; color:#2E6DB4; }
.kpi-label { font-size:10px; color:#444; font-weight:600; margin-bottom:4px; line-height:1.3; }
.kpi-value { font-size:20px; font-weight:900; color:#1B3A6B; line-height:1; direction:ltr; unicode-bidi:isolate; }
.kpi-unit { font-size:11px; font-weight:600; color:#2E6DB4; margin-top:3px; }
.kpi-change { font-size:11px; font-weight:700; margin-top:5px; color:#586b7d; }
.kpi-change.up, .kpi-change.down { color:#586b7d; }
.kpi-sub { font-size:9px; color:#666; margin-top:1px; }
.two-col { display:grid; grid-template-columns:1fr 1.3fr; gap:14px; margin-bottom:16px; }
.card { background:white; border:1.5px solid #c5d8f0; border-radius:8px; padding:14px; min-height:285px; }
.card-title { font-size:14px; font-weight:800; color:#1B3A6B; margin-bottom:12px; text-align:center; border-bottom:1.5px solid #E8F0FB; padding-bottom:8px; }
.donut-wrap { display:flex; flex-direction:column; align-items:center; }
.donut-container { position:relative; width:160px; height:160px; }
.donut-container svg { width:160px; height:160px; }
.donut-center { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); text-align:center; line-height:1.2; }
.donut-center .dc-val { font-size:18px; font-weight:900; color:#1B3A6B; direction:ltr; }
.donut-center .dc-label { font-size:9px; color:#555; font-weight:600; }
.donut-legend { margin-top:12px; display:flex; flex-direction:column; gap:8px; width:100%; }
.legend-item { display:flex; align-items:center; gap:8px; font-size:11px; }
.legend-dot { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
.legend-item strong { font-weight:800; font-size:13px; color:#1B3A6B; direction:ltr; }
.bar-chart { width:100%; }
.chart-bars { display:flex; align-items:flex-end; gap:18px; height:140px; padding:0 8px; border-bottom:2px solid #ddd; border-right:2px solid #ddd; margin-bottom:8px; direction:ltr; }
.bar-group { display:flex; gap:5px; align-items:flex-end; flex:1; height:100%; }
.bar { flex:1; border-radius:3px 3px 0 0; position:relative; min-width:18px; }
.bar-today { background:#2E6DB4; }
.bar-yesterday { background:#aaa; }
.bar-val { position:absolute; top:-16px; left:50%; transform:translateX(-50%); font-size:8px; font-weight:700; color:#333; white-space:nowrap; direction:ltr; }
.bar-labels { display:flex; gap:18px; padding:0 8px; direction:ltr; }
.bar-label { flex:1; text-align:center; font-size:9.5px; font-weight:600; color:#444; direction:rtl; }
.chart-legend { display:flex; gap:14px; justify-content:center; margin-top:8px; font-size:10px; }
.chart-legend span { display:flex; align-items:center; gap:4px; }
.cl-dot { width:10px; height:10px; border-radius:2px; display:inline-block; }
.summary-card { background:white; border:1.5px solid #c5d8f0; border-radius:8px; padding:14px 18px; margin-bottom:14px; }
.summary-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; border-bottom:1.5px solid #E8F0FB; padding-bottom:8px; }
.summary-icon { width:36px; height:36px; background:#E8F0FB; border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:18px; }
.summary-header h3 { font-size:16px; font-weight:800; color:#1B3A6B; }
.summary-list { list-style:none; }
.summary-list li { font-size:11.5px; color:#333; padding:4px 14px 4px 0; border-bottom:1px dashed #e5edf5; line-height:1.6; position:relative; }
.summary-list li:last-child { border-bottom:none; }
.summary-list li::before { content:'•'; position:absolute; right:0; color:#2E6DB4; font-size:14px; line-height:1.5; }
.smart-box { background:linear-gradient(135deg,#1B3A6B 0%,#2E6DB4 100%); border-radius:8px; padding:12px 10px; color:white; text-align:center; border:1px solid #d7e4f5; box-shadow:inset 0 1px 0 rgba(255,255,255,.10); }
.smart-icon { font-size:24px; margin-bottom:4px; opacity:.95; }
.smart-box h4 { font-size:11.5px; font-weight:800; margin-bottom:6px; line-height:1.45; }
.smart-box p { font-size:8.8px; opacity:.92; line-height:1.65; margin-top:6px; }
.smart-tag { font-size:9px; line-height:1.2; padding:4px 9px; border-radius:999px; background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.18); margin:3px 0; }
.bottom-row { display:grid; grid-template-columns:2.08fr .42fr; gap:10px; margin-bottom:14px; direction:ltr; align-items:start; }
.bottom-row > * { direction:rtl; }
.summary-card { min-height:220px; }
.smart-box { min-height:156px; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.footer { background:#1B3A6B; color:white; padding:12px 24px; display:flex; justify-content:space-between; align-items:center; }
.footer-item { text-align:center; font-size:10px; line-height:1.7; }
.footer-item strong { font-size:12px; display:block; }
.footer-item .fi-icon { font-size:18px; margin-bottom:3px; }
'''


def _header_target(st, d, report_no, temporary=False):
    real_logo = logo_src(st, _asset_logo())
    group_logo = img_data(_asset_group_logo())
    return f'''
<div class="header">
  <div class="header-logos">
    <img class="logo-group" src="{group_logo}">
  </div>
  <div class="header-center">
    <h1>نیروگاه 25 مگاواتی کاله</h1>
    <h2>{escape(st.get('daily_title','گزارش روزانه انرژی و نیروگاه'))}</h2>
  </div>
  <div class="header-meta">
    <div>شماره گزارش:</div>
    <div><strong><span dir="ltr">{report_no}</span></strong></div>
    <div style="margin-top:4px">نوع گزارش:</div><div><strong>{'روزانه موقت' if temporary else 'روزانه'}</strong></div>
  </div>
</div>'''


def _bar_pair(cur, prev, maxv):
    def h(v):
        if v is None or maxv <= 0:
            return 0
        return max(0, min(130, float(v)/maxv*130))
    return h(cur), h(prev)


def _management_bullets(s, prev):
    bullets = []
    f = s.get('factory') or {}
    pf = (prev.get('factory') or {}) if prev else {}
    g = s.get('grid') or {}
    pg = (prev.get('grid') or {}) if prev else {}
    plant = s.get('plant_energy')
    pplant = prev.get('plant_energy') if prev else None

    if f.get('active_kwh') is not None:
        ch = _change(f.get('active_kwh'), pf.get('active_kwh'))
        if ch is None:
            bullets.append(f"مصرف انرژی کارخانه {_mwh(f.get('active_kwh'))} MWh ثبت شد.")
        else:
            bullets.append(f"مصرف انرژی کارخانه نسبت به روز قبل {_pct(abs(ch))} {'افزایش یافت' if ch >= 0 else 'کاهش یافت'} و به {_mwh(f.get('active_kwh'))} MWh رسید.")

    if plant is not None:
        ch = _change(plant, pplant)
        tail = '' if ch is None else f" که نسبت به روز قبل {_pct(abs(ch))} {'افزایش' if ch >= 0 else 'کاهش'} دارد"
        bullets.append(f"نیروگاه {_pct(s.get('plant_share'))} از انرژی مصرفی را تأمین کرد ({_mwh(plant)} MWh){tail}.")

    if g.get('active_kwh') is not None:
        ch = _change(g.get('active_kwh'), pg.get('active_kwh'))
        if ch is None:
            bullets.append(f"دریافت از شبکه {_mwh(g.get('active_kwh'))} MWh بود.")
        else:
            bullets.append(f"دریافت از شبکه {_mwh(g.get('active_kwh'))} MWh بود که {_pct(abs(ch))} {'بیشتر' if ch >= 0 else 'کمتر'} از روز قبل است.")

    if f.get('max_kw') is not None:
        bullets.append(f"پیک مصرف کارخانه {_mw(f.get('max_kw'),1)} MW در ساعت {_time_fa(f.get('max_time'))} ثبت شد.")

    for entity in s.get('entities',{}).values():
        energy=entity.get('roles',{}).get('energy_active',{})
        if energy.get('partial_day') and energy.get('value') is not None:
            bullets.insert(0,f"{entity['entity_name']}: گزارش بر مبنای بازه واقعی {energy.get('from')} تا {energy.get('to')}، معادل {energy.get('hours',0):.2f} ساعت است؛ متوسط توان بر همین مدت محاسبه شده است.")
        if energy.get('missing_start_snapshot') or energy.get('missing_end_snapshot'):
            which='شروع و پایان' if energy.get('missing_start_snapshot') and energy.get('missing_end_snapshot') else ('شروع' if energy.get('missing_start_snapshot') else 'پایان')
            bullets.insert(0,f"{entity['entity_name']}: داده مرز {which} روز موجود نیست؛ انرژی روزانه و متوسط توان قابل محاسبه نیست و به روز بعد منتقل نمی‌شود.")
    if not bullets:
        bullets.append('داده کافی برای خلاصه مدیریتی وجود ندارد.')
    return bullets


def _page1(st, d, s, prev, report_no):
    temporary=bool(s.get('is_temporary'))
    end_stamp=s.get('last_data_time') or s.get('as_of')
    end_clock=_time_fa(end_stamp) if temporary else '۲۳:۵۹'
    section_title=(f'شاخص‌های کلیدی روز — موقت تا {end_clock}' if temporary else 'شاخص‌های کلیدی روز')
    interval_text=(f'۰۰:۰۰ تا {end_clock}' if temporary else '۰۰:۰۰ تا ۲۳:۵۹')
    report_type_text=('روزانه موقت' if temporary else 'روزانه')
    f = s.get('factory') or {}
    pf = (prev.get('factory') or {}) if prev else {}
    g = s.get('grid') or {}
    pg = (prev.get('grid') or {}) if prev else {}
    plant = s.get('plant_energy')
    pplant = prev.get('plant_energy') if prev else None

    supply_total = (plant or 0) + (g.get('active_kwh') or 0)
    if supply_total > 0:
        mix_plant = (plant or 0) / supply_total * 100.0
        mix_grid = 100.0 - mix_plant
    else:
        mix_plant = mix_grid = 0.0

    circumference = 376.99
    plant_dash = circumference * mix_plant / 100.0
    grid_dash = circumference * mix_grid / 100.0

    today_vals = [(f.get('active_kwh') or 0)/1000.0, (plant or 0)/1000.0, (g.get('active_kwh') or 0)/1000.0]
    prev_vals = [(pf.get('active_kwh') or 0)/1000.0, (pplant or 0)/1000.0, (pg.get('active_kwh') or 0)/1000.0]
    maxv = max(today_vals + prev_vals + [1])
    heights = [_bar_pair(c, p, maxv) for c, p in zip(today_vals, prev_vals)]
    bullets = ''.join(f'<li>{escape(x)}</li>' for x in _management_bullets(s, prev))

    return f'''<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"><title>گزارش روزانه انرژی و نیروگاه</title><style>{_target_css()}</style></head>
<body><div class="page">
{_header_target(st,d,report_no,temporary)}
<div class="content">
  <div class="section-title">{section_title}</div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-icon">🏭</div><div class="kpi-label">مصرف کل کارخانه</div><div class="kpi-value">{_mwh(f.get('active_kwh'))}</div><div class="kpi-unit">MWh</div>{_change_html(f.get('active_kwh'),pf.get('active_kwh'))}</div>
    <div class="kpi-card"><div class="kpi-icon">⚡</div><div class="kpi-label">تولید کل نیروگاه</div><div class="kpi-value">{_mwh(plant)}</div><div class="kpi-unit">MWh</div>{_change_html(plant,pplant)}</div>
    <div class="kpi-card"><div class="kpi-icon"><svg viewBox="0 0 64 64" width="24" height="24" aria-hidden="true"><g fill="none" stroke="#2E6DB4" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M32 5L21 21h22L32 5z"/><path d="M24 21l-8 14h32l-8-14"/><path d="M20 35l-8 11h40l-8-11"/><path d="M26 21L20 58M38 21l6 37M18 58h28"/><path d="M4 27h16M44 27h16M6 43h13M45 43h13"/></g></svg></div><div class="kpi-label">دریافت از شبکه</div><div class="kpi-value">{_mwh(g.get('active_kwh'))}</div><div class="kpi-unit">MWh</div>{_change_html(g.get('active_kwh'),pg.get('active_kwh'))}</div>
    <div class="kpi-card"><div class="kpi-icon">📊</div><div class="kpi-label">سهم نیروگاه از مصرف</div><div class="kpi-value" style="font-size:22px">{_pct(s.get('plant_share'))}</div><div class="kpi-unit">&nbsp;</div>{_change_html(s.get('plant_share'),prev.get('plant_share') if prev else None)}</div>
    <div class="kpi-card"><div class="kpi-icon">📈</div><div class="kpi-label">پیک مصرف کارخانه</div><div class="kpi-value">{_mw(f.get('max_kw'),1)}</div><div class="kpi-unit">MW</div><div class="kpi-change" style="color:#555">در ساعت {_time_fa(f.get('max_time'))}</div><div class="kpi-sub">&nbsp;</div></div>
    <div class="kpi-card"><div class="kpi-icon">∑</div><div class="kpi-label">متوسط توان مصرفی</div><div class="kpi-value">{_mw(f.get('avg_kw'),2)}</div><div class="kpi-unit">MW</div>{_change_html(f.get('avg_kw'),pf.get('avg_kw'))}</div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">ترکیب تأمین انرژی کارخانه</div>
      <div class="donut-wrap">
        <div class="donut-container">
          <svg viewBox="0 0 160 160">
            <circle cx="80" cy="80" r="60" fill="none" stroke="#eee" stroke-width="22"/>
            <circle cx="80" cy="80" r="60" fill="none" stroke="#1B3A6B" stroke-width="22"
              stroke-dasharray="{plant_dash:.1f} {circumference-plant_dash:.1f}"
              stroke-dashoffset="0" transform="rotate(-90 80 80)"/>
            <circle cx="80" cy="80" r="60" fill="none" stroke="#7FB3E8" stroke-width="22"
              stroke-dasharray="{grid_dash:.1f} {circumference-grid_dash:.1f}"
              stroke-dashoffset="{-plant_dash:.1f}" transform="rotate(-90 80 80)"/>
          </svg>
          <div class="donut-center"><div class="dc-val">{_mwh(f.get('active_kwh'))}</div><div class="dc-label">MWh</div><div class="dc-label">مصرف کل</div></div>
        </div>
        <div class="donut-legend">
          <div class="legend-item"><div class="legend-dot" style="background:#1B3A6B"></div><div><div style="font-size:10px;color:#555">تولید نیروگاه</div><strong>{_mwh(plant)} MWh</strong></div><div style="margin-right:auto;font-size:13px;font-weight:800;color:#1B3A6B">{_pct(mix_plant)}</div></div>
          <div class="legend-item"><div class="legend-dot" style="background:#7FB3E8"></div><div><div style="font-size:10px;color:#555">دریافت از شبکه</div><strong>{_mwh(g.get('active_kwh'))} MWh</strong></div><div style="margin-right:auto;font-size:13px;font-weight:800;color:#2E6DB4">{_pct(mix_grid)}</div></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">مقایسه با روز قبل</div>
      <div class="chart-legend" style="margin-bottom:10px"><span><span class="cl-dot" style="background:#2E6DB4"></span> امروز</span><span><span class="cl-dot" style="background:#aaa"></span> روز قبل</span></div>
      <div class="bar-chart">
        <div class="chart-bars">
          <div class="bar-group"><div class="bar bar-yesterday" style="height:{heights[0][1]:.1f}px"><div class="bar-val">{_num(prev_vals[0],1)}</div></div><div class="bar bar-today" style="height:{heights[0][0]:.1f}px"><div class="bar-val">{_num(today_vals[0],1)}</div></div></div>
          <div class="bar-group"><div class="bar bar-yesterday" style="height:{heights[1][1]:.1f}px"><div class="bar-val">{_num(prev_vals[1],1)}</div></div><div class="bar bar-today" style="height:{heights[1][0]:.1f}px"><div class="bar-val">{_num(today_vals[1],1)}</div></div></div>
          <div class="bar-group"><div class="bar bar-yesterday" style="height:{heights[2][1]:.1f}px"><div class="bar-val">{_num(prev_vals[2],1)}</div></div><div class="bar bar-today" style="height:{heights[2][0]:.1f}px"><div class="bar-val">{_num(today_vals[2],1)}</div></div></div>
        </div>
        <div class="bar-labels"><div class="bar-label">مصرف کارخانه</div><div class="bar-label">تولید نیروگاه</div><div class="bar-label">دریافت از شبکه</div></div>
      </div>
    </div>
  </div>

  <div class="bottom-row">
    <div class="summary-card" style="margin-bottom:0"><div class="summary-header"><div class="summary-icon">📋</div><h3>خلاصه مدیریتی</h3></div><ul class="summary-list">{bullets}</ul></div>
    <div class="smart-box">
      <div class="smart-icon">
        <svg viewBox="0 0 64 64" width="34" height="34" aria-hidden="true">
          <g fill="none" stroke="white" stroke-width="3" stroke-linecap="round">
            <path d="M22 42l20-20"/>
            <path d="M18 46l10-4 4-10"/>
            <path d="M12 52h40"/>
            <path d="M35 18c6 1 10 5 11 11"/>
            <path d="M39 10c10 2 17 9 19 19"/>
            <circle cx="31" cy="33" r="3" fill="white" stroke="none"/>
          </g>
        </svg>
      </div>
      <h4>{escape(st.get('smart_system_label','سامانه هوشمند پایش نیروگاه'))}</h4>
      <div class="smart-tag">پایش پیوسته</div>
      <div class="smart-tag">تحلیل هوشمند</div>
      <div class="smart-tag">گزارش‌گیری خودکار</div>
      <p>مدیریت نیروگاه و انرژی</p>
    </div>
  </div>
</div>

<div class="footer">
  <div class="footer-item">
    <div class="fi-icon"><svg viewBox="0 0 24 24" width="18" height="18"><g fill="none" stroke="white" stroke-width="1.8"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 9h18"/></g></svg></div>
    <div>تاریخ گزارش:</div><strong>{_jalali_long(d)}</strong>
  </div>
  <div class="footer-item">
    <div class="fi-icon"><svg viewBox="0 0 24 24" width="18" height="18"><g fill="none" stroke="white" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></g></svg></div>
    <div>بازه زمانی:</div><strong>{interval_text}</strong>
  </div>
  <div class="footer-item">
    <div class="fi-icon"><svg viewBox="0 0 24 24" width="18" height="18"><g fill="none" stroke="white" stroke-width="1.8"><path d="M6 3h9l4 4v14H6z"/><path d="M15 3v5h5M9 12h6M9 16h6"/></g></svg></div>
    <div>نوع گزارش:</div><strong>{report_type_text}</strong>
  </div>
  <div class="footer-item">
    <div class="fi-icon"><svg viewBox="0 0 24 24" width="18" height="18"><g fill="none" stroke="white" stroke-width="1.8"><circle cx="12" cy="8" r="4"/><path d="M4 21c1-5 4-7 8-7s7 2 8 7"/></g></svg></div>
    <div>تهیه شده توسط:</div><strong>مدیریت نیروگاه و انرژی</strong>
  </div>
</div>
</div></body></html>'''


def _asset_logo():
    return str(Path(__file__).resolve().parents[1]/'assets'/'kalleh_logo.png')


def _asset_group_logo():
    return str(Path(__file__).resolve().parents[1]/'assets'/'solico_s_mark.png')


def _header(st,d,title):
    logo=logo_src(st,_asset_logo())
    group_logo=img_data(_asset_group_logo())
    return f'''<div class="header"><div class="header-logos"><img class="logo-group" src="{group_logo}"></div><div class="headtxt"><h1>نیروگاه 25 مگاواتی کاله</h1><h2>{escape(title)}</h2></div></div>'''


def _env_rows(env):
    if not env:return ''
    r=env.get('roles',{})
    vals=[('حداقل دما',r.get('temp_min',{}).get('value'),'°C',r.get('temp_min_time',{}).get('value')),('حداکثر دما',r.get('temp_max',{}).get('value'),'°C',r.get('temp_max_time',{}).get('value')),('حداقل رطوبت',r.get('humidity_min',{}).get('value'),'%',r.get('humidity_min_time',{}).get('value')),('حداکثر رطوبت',r.get('humidity_max',{}).get('value'),'%',r.get('humidity_max_time',{}).get('value'))]
    return ''.join(f'<tr><td>{a}</td><td>{fmt(v,1)}</td><td>{u}</td><td>{time_only(t)}</td></tr>' for a,v,u,t in vals)


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

def generate(d, regenerate=False, source_id=None, source_name=None, as_of=None):
    if source_id is None: import_all()
    else: import_source(source_id)
    source_name=_selected_source_name(source_id,source_name)
    if as_of is None and d==date.today(): as_of=datetime.now()
    st=settings_mod.load(); s=day_summary(d,source_id,as_of=as_of); prev=day_summary(d-timedelta(days=1),source_id)
    temporary=bool(s.get('is_temporary'))
    outroot=settings_mod.ensure_output_dir(); jy,jm,jd=_jalali_parts(d)
    if source_id is None:
        folder=outroot/'Daily'/f'{jy:04d}-{jm:02d}'
        suffix='_TEMP' if temporary else ''
        outname=f'Daily_Report_{jy:04d}-{jm:02d}-{jd:02d}{suffix}.pdf'
        report_no=f'DR-{jy:04d}-{jm:02d}-{jd:02d}' + ('-TEMP' if temporary else '')
    else:
        safe=_safe_source_name(source_name)
        folder=outroot/'Daily'/safe/f'{jy:04d}-{jm:02d}'
        suffix='_TEMP' if temporary else ''
        outname=f'Daily_Report_{jy:04d}-{jm:02d}-{jd:02d}_{safe}{suffix}.pdf'
        report_no=f'DR-{jy:04d}-{jm:02d}-{jd:02d}-S{source_id}' + ('-TEMP' if temporary else '')
    folder.mkdir(parents=True,exist_ok=True)
    out=folder/outname

    p1=_page1(st,d,s,prev,report_no)

    f=s.get('factory') or {}; g=s.get('grid') or {}; gens=s.get('generators') or []
    rows=''.join(f'''<tr><td>{escape(x['entity_name'])}</td><td>{fmt(x.get('active_kwh'))}</td><td>{fmt(x.get('reactive_kvarh'))}</td><td>{fmt(x.get('avg_kw'))}</td><td>{fmt_pf(x.get('pf'))}</td><td>{fmt(x.get('max_kw'))}</td><td>{time_only(x.get('max_time'))}</td><td>{fmt(x.get('max_kvar'))}</td><td>{time_only(x.get('max_reactive_time'))}</td><td>{fmt(x.get('running_hours'))}</td></tr>''' for x in gens)
    rows += f'''<tr><td><b>مجموع نیروگاه</b></td><td><b>{fmt(s.get('plant_energy'))}</b></td><td>{fmt(s.get('plant_reactive'))}</td><td>{fmt(s.get('plant_avg'))}</td><td>{fmt_pf(s.get('plant_pf'))}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'''
    if g: rows+=f'''<tr><td>شبکه</td><td>{fmt(g.get('active_kwh'))}</td><td>{fmt(g.get('reactive_kvarh'))}</td><td>{fmt(g.get('avg_kw'))}</td><td>{fmt_pf(g.get('pf'))}</td><td>{fmt(g.get('max_kw'))}</td><td>{time_only(g.get('max_time'))}</td><td>{fmt(g.get('max_kvar'))}</td><td>{time_only(g.get('max_reactive_time'))}</td><td>—</td></tr>'''
    if f: rows+=f'''<tr><td>مصرف کارخانه</td><td>{fmt(f.get('active_kwh'))}</td><td>{fmt(f.get('reactive_kvarh'))}</td><td>{fmt(f.get('avg_kw'))}</td><td>{fmt_pf(f.get('pf'))}</td><td>{fmt(f.get('max_kw'))}</td><td>{time_only(f.get('max_time'))}</td><td>{fmt(f.get('max_kvar'))}</td><td>{time_only(f.get('max_reactive_time'))}</td><td>—</td></tr>'''

    if temporary:
        end_stamp=s.get('last_data_time') or s.get('as_of')
        interval_note=f'این گزارش موقت است و از ۰۰:۰۰ روز انتخابی تا آخرین داده موجود در {_time_fa(end_stamp)} محاسبه شده است. متوسط توان بر اساس ساعات واقعی همین بازه محاسبه می‌شود. گزارش کامل پس از پایان روز ساخته خواهد شد.'
        detail_title='جزئیات فنی عملکرد روزانه — موقت تا این لحظه'
    else:
        interval_note='بازه گزارش روزانه از ۰۰:۰۰ روز انتخابی تا ۰۰:۰۰ روز بعد است. ثبت ۲۳:۵۹:۵۹ و اولین ثبت تا ۱۲۰ ثانیه پس از نیمه‌شب برای مرز روز پذیرفته می‌شود؛ این مهلت برای تأخیر ثبت WinCC است. اگر ثبت دقیق مرز موجود نباشد، نزدیک‌ترین ثبت تا ۶۵ دقیقه فاصله پذیرفته و بازه واقعی گزارش می‌شود. اختلاف چندروزه به یک روز نسبت داده نمی‌شود.'
        detail_title='جزئیات فنی عملکرد روزانه'
    p2=f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{base_css(True)}</style></head><body><div class="page">{_header(st,d,detail_title)}
    <div class="technical-title">عملکرد ژنراتورها، شبکه و کارخانه</div><table><thead><tr><th>بخش</th><th>انرژی اکتیو kWh</th><th>انرژی راکتیو kVArh</th><th>متوسط توان kW</th><th>PF</th><th>پیک اکتیو kW</th><th>زمان اکتیو</th><th>پیک راکتیو kVAr</th><th>زمان راکتیو</th><th>ساعت کارکرد</th></tr></thead><tbody>{rows}</tbody></table>
    <div class="technical-title">قطعی‌های شبکه</div><table><thead><tr><th>تعداد قطعی</th><th>مدت کل قطعی (دقیقه)</th></tr></thead><tbody><tr><td>&nbsp;</td><td>&nbsp;</td></tr></tbody></table>
    <div class="signatures"><div class="sig"><b>{escape(st.get('prepared_title','تهیه‌کننده گزارش'))}</b><br>{escape(st.get('prepared_name',''))}</div><div class="sig"><b>مدیر نیروگاه</b><br>{escape(st.get('manager_name',''))}</div></div>
    <div class="footer"><span>شماره گزارش: <span dir="ltr">{report_no}</span></span><span>مدیریت نیروگاه و انرژی</span></div></div></body></html>'''

    with tempfile.TemporaryDirectory() as td:
        a=Path(td)/'a.pdf'; b=Path(td)/'b.pdf'
        html_to_pdf(p1,a); html_to_pdf(p2,b); merge_pdfs([a,b],out)
    db.mark_report('daily_partial' if temporary else 'daily',d.isoformat(),str(out))
    return str(out)
