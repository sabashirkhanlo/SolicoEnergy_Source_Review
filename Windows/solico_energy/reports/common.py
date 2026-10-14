from __future__ import annotations
from datetime import datetime
from html import escape
from ..core.jalali import format_jalali
from .html_pdf import img_data

def fmt(v,dec=0,dash='—'):
    if v is None:return dash
    try:return f'{v:,.{dec}f}'
    except:return escape(str(v))

def fmt_pf(v): return fmt(v,3)
def fmt_pct(v): return '—' if v is None else f'{v:.1f}%'
def time_only(v):
    if not v:return '—'
    try:return datetime.fromisoformat(str(v)).strftime('%H:%M')
    except:return escape(str(v))
def date_fa(d): return format_jalali(d)
def logo_src(settings, default_logo): return img_data(settings.get('logo_path') or default_logo)

def donut_svg(plant,grid,total):
    if total in (None,0): return '<div class="nodata">داده کافی نیست</div>'
    p=max(0,min(100,(plant or 0)/total*100)); g=100-p
    return f'''<svg viewBox="0 0 220 150" class="donut"><g transform="translate(110 72) rotate(-90)">
    <circle r="48" cx="0" cy="0" fill="none" stroke="#e7edf5" stroke-width="20"/>
    <circle r="48" cx="0" cy="0" fill="none" stroke="#1687e6" stroke-width="20" pathLength="100" stroke-dasharray="{p:.1f} {100-p:.1f}"/>
    </g><text x="110" y="78" text-anchor="middle" class="big">{p:.1f}%</text><text x="110" y="98" text-anchor="middle" class="small">سهم نیروگاه</text>
    <text x="45" y="140" class="legend">نیروگاه {p:.1f}%</text><text x="140" y="140" class="legend">شبکه {g:.1f}%</text></svg>'''

def bars_svg(today,prev):
    pairs=[('مصرف کارخانه',today.get('factory'),prev.get('factory')),('تولید نیروگاه',today.get('plant'),prev.get('plant')),('دریافت شبکه',today.get('grid'),prev.get('grid'))]
    vals=[x for p in pairs for x in p[1:] if x is not None]; mx=max(vals) if vals else 1
    y=20; parts=['<svg viewBox="0 0 420 180" class="bars">']
    for label,a,b in pairs:
        wa=0 if a is None else 240*a/mx; wb=0 if b is None else 240*b/mx
        parts += [f'<text x="410" y="{y+12}" text-anchor="end">{label}</text>',f'<rect x="120" y="{y}" width="{wa:.1f}" height="12" rx="4" fill="#1687e6"/>',f'<rect x="120" y="{y+16}" width="{wb:.1f}" height="12" rx="4" fill="#a9b7c6"/>',f'<text x="112" y="{y+10}" text-anchor="end" class="n">{fmt(a)}</text>',f'<text x="112" y="{y+27}" text-anchor="end" class="n">{fmt(b)}</text>']
        y+=52
    parts += ['<text x="210" y="176" class="small">آبی: دوره فعلی   خاکستری: دوره قبل</text>','</svg>']
    return ''.join(parts)

def base_css(landscape=False):
    orient='landscape' if landscape else 'portrait'
    return f'''@page{{size:A4 {orient};margin:9mm}}*{{box-sizing:border-box}}body{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;margin:0;color:#17263a;background:white;direction:rtl}}.page{{width:100%;min-height:100%;page-break-after:always}}.header{{display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #0e477e;padding:4px 0 10px;margin-bottom:12px}}.header-logos{{display:flex;align-items:center;justify-content:flex-end;gap:10px;min-width:145px}}.logo{{width:145px;height:55px;object-fit:contain;margin-left:auto}}.logo-primary{{width:96px;height:52px;object-fit:contain;display:block}}.logo-group{{width:74px;height:42px;object-fit:contain;display:block}}.headtxt{{flex:1;text-align:center}}h1{{font-size:24px;margin:0;color:#0d3f73}}h2{{font-size:15px;margin:4px 0;color:#33546f;font-weight:600}}.date{{font-size:11px;color:#657b8f}}.section-title{{text-align:center;font-weight:800;font-size:18px;margin:11px 0;color:#0d3f73}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.card{{border:1px solid #dbe5ef;border-radius:10px;padding:10px;background:#fbfdff;text-align:center;min-height:75px}}.card .label{{font-size:11px;color:#597086}}.card .value{{font-size:22px;font-weight:800;color:#0d3f73;margin-top:7px}}.card .unit{{font-size:10px;color:#7a8c9e;margin-right:4px}}.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}}.panel{{border:1px solid #dbe5ef;border-radius:10px;padding:10px;background:#fff}}.panel h3{{font-size:13px;margin:0 0 7px;color:#0d3f73}}.donut,.bars{{width:100%;height:180px}}svg text{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;fill:#213a52;font-size:11px}}svg .big{{font-size:18px;font-weight:800}}svg .small{{font-size:9px;fill:#6b7f91}}svg .legend{{font-size:9px}}svg .n{{font-size:8px}}.summary{{border-right:4px solid #1687e6;background:#f4f9fe;border-radius:7px;padding:10px 12px;margin-top:10px;font-size:11px;line-height:1.9}}.smart{{margin-top:9px;padding:8px 12px;background:#0d477e;color:white;border-radius:8px;font-size:10px;display:flex;justify-content:space-between}}table{{width:100%;border-collapse:collapse;font-size:9.3px;margin-top:7px}}th{{background:#eef4f9;color:#183a58;font-weight:700}}th,td{{border:1px solid #d6e0e8;padding:6px;text-align:center}}tr:nth-child(even) td{{background:#fbfcfe}}.technical-title{{font-size:16px;color:#123f68;margin:10px 0 5px;border-bottom:1px solid #cfdbe6;padding-bottom:5px}}.note{{font-size:8.5px;color:#6b7d8e;line-height:1.7;margin-top:6px}}.footer{{margin-top:10px;border-top:1px solid #d4dee8;padding-top:8px;font-size:9px;color:#607386;display:flex;justify-content:space-between}}.signatures{{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:18px}}.sig{{text-align:center;min-height:90px}}.sig img{{height:45px;max-width:130px;object-fit:contain}}.nodata{{height:150px;display:flex;align-items:center;justify-content:center;color:#8a9aaa}}'''
