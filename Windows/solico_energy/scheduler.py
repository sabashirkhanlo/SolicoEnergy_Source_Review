from __future__ import annotations
import argparse, logging, json
from datetime import date, timedelta, datetime
from .core.db import init_db, report_exists, last_report_key
from .core.logging_setup import setup_logging
from .core.jalali import gregorian_to_jalali, previous_jalali_month_for
from .services.importer import import_all
from .services.calculations import daily_completeness
from .core import settings as settings_mod
from .reports.daily import generate as daily
from .reports.monthly import generate as monthly
from .core.mobile_push import notify_report

log=logging.getLogger(__name__)


def _status_path():
    return settings_mod.data_dir()/'auto_report_status.json'


def _write_status(state, d=None, message='', reasons=None, path=None):
    obj={
        'state':state,'time':datetime.now().isoformat(timespec='seconds'),
        'date':d.isoformat() if d else None,'message':message,
        'reasons':list(reasons or []),'path':str(path) if path else ''
    }
    try:_status_path().write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:pass


# AUTO_REPORT_HOTFIX_R2: persistent mobile notification retry queue.
def _pending_notifications_path():
    return settings_mod.data_dir()/'pending_mobile_notifications.json'


def _load_pending_notifications():
    p=_pending_notifications_path()
    if not p.exists():
        return []
    try:
        obj=json.loads(p.read_text(encoding='utf-8'))
        return obj if isinstance(obj,list) else []
    except Exception:
        log.exception('reading pending mobile notification queue failed')
        return []


def _save_pending_notifications(items):
    p=_pending_notifications_path()
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(list(items or []),ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(p)


def _queue_notification(path, title, kind='daily', report_date=None, **context):
    item={
        'path':str(path or ''),
        'title':str(title or 'گزارش جدید Solico Energy'),
        'kind':str(kind or 'daily'),
        'report_date':report_date.isoformat() if hasattr(report_date,'isoformat') else str(report_date or ''),
        'created_at':datetime.now().isoformat(timespec='seconds'),
        'attempts':0,
        'last_error':'',
        'context':{k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in context.items() if v is not None and (isinstance(v,(str,int,float,bool)) or hasattr(v,'isoformat'))},
    }
    items=_load_pending_notifications()
    key=(item['path'],item['kind'],item['report_date'])
    for old in items:
        if (str(old.get('path') or ''),str(old.get('kind') or ''),str(old.get('report_date') or ''))==key:
            return
    items.append(item)
    _save_pending_notifications(items[-50:])


def retry_pending_notifications():
    """Retry report notifications without blocking report generation.

    Queue is persistent, so a short Internet/Firebase outage at 08:00 does not lose
    the phone notification.  Missing PDFs and items older than 14 days are dropped.
    """
    items=_load_pending_notifications()
    if not items:
        return {'sent':0,'remaining':0}
    keep=[]; sent=0
    now=datetime.now()
    for item in items:
        try:
            created=datetime.fromisoformat(str(item.get('created_at') or ''))
            if (now-created).days>14:
                continue
        except Exception:
            pass
        p=str(item.get('path') or '')
        if not p:
            continue
        try:
            rd=None
            raw=str(item.get('report_date') or '').strip()
            if raw:
                try: rd=date.fromisoformat(raw)
                except Exception: rd=raw
            ctx=dict(item.get('context') or {}) if isinstance(item.get('context'),dict) else {}
            if ctx.get('as_of'):
                try: ctx['as_of']=datetime.fromisoformat(str(ctx['as_of']))
                except Exception: ctx.pop('as_of',None)
            if rd is not None:
                ctx['report_date']=rd
            notify_report(p,str(item.get('title') or 'گزارش جدید Solico Energy'),kind=str(item.get('kind') or 'daily'),**ctx)
            sent+=1
        except Exception as e:
            item['attempts']=int(item.get('attempts') or 0)+1
            item['last_attempt']=datetime.now().isoformat(timespec='seconds')
            item['last_error']=str(e)[:1200]
            keep.append(item)
            log.exception('pending mobile notification retry failed for %s',p)
    _save_pending_notifications(keep)
    return {'sent':sent,'remaining':len(keep)}


def _safe_daily(d, require_complete=True):
    existing=report_exists('daily',d.isoformat())
    if existing:
        _write_status('exists',d,'گزارش این روز قبلاً ساخته شده است.',path=existing.get('path'))
        return True
    if require_complete:
        check=daily_completeness(d)
        if not check.get('complete'):
            reasons=check.get('reasons') or []
            _write_status('incomplete',d,'گزارش خودکار ساخته نشد؛ داده روز کامل نیست.',reasons=reasons)
            log.warning('daily report skipped for %s: %s',d,'; '.join(reasons))
            return False
    try:
        out=daily(d)
        try:
            notify_report(out,'گزارش روزانه خودکار',kind='daily',report_date=d)
            _write_status('success',d,'گزارش روزانه خودکار با موفقیت ساخته شد و اعلان موبایل ارسال شد.',path=out)
        except Exception as notify_error:
            log.exception('automatic mobile notification failed for %s; queued for retry',d)
            _queue_notification(out,'گزارش روزانه خودکار',kind='daily',report_date=d)
            _write_status('success_notify_pending',d,'گزارش روزانه ساخته شد؛ ارسال اعلان موبایل موقتاً ناموفق بود و برای ارسال مجدد در صف قرار گرفت.',reasons=[str(notify_error)[:500]],path=out)
        return True
    except Exception as e:
        _write_status('error',d,f'خطا در ساخت گزارش خودکار: {e}')
        log.exception('daily generation failed for %s',d)
        return False


def _safe_monthly(jy:int,jm:int):
    key=f'{int(jy):04d}-{int(jm):02d}'
    existing=report_exists('monthly',key)
    if existing:
        return True
    try:
        out=monthly(int(jy),int(jm))
        try:
            notify_report(out,'گزارش ماهانه خودکار',kind='monthly',jalali_year=int(jy),jalali_month=int(jm))
        except Exception as notify_error:
            log.exception('automatic monthly mobile notification failed for %s; queued for retry',key)
            _queue_notification(out,'گزارش ماهانه خودکار',kind='monthly',jalali_year=int(jy),jalali_month=int(jm))
        return True
    except Exception:
        log.exception('monthly generation failed for %s',key)
        return False


def catchup():
    try: retry_pending_notifications()
    except Exception: log.exception('catchup notification retry failed')
    cfg=settings_mod.load(); import_all()
    yesterday=date.today()-timedelta(days=1 if datetime.now().hour>=8 else 2)
    last=last_report_key('daily')
    # On first use only create yesterday. Afterwards fill all missed days.
    if last:
        try: start=date.fromisoformat(last)+timedelta(days=1)
        except Exception: start=yesterday
    else: start=yesterday
    # Safety cap protects a new/very old installation from generating years of PDFs unexpectedly.
    if (yesterday-start).days>62: start=yesterday-timedelta(days=62)
    d=start
    while d<=yesterday:
        if cfg.get('auto_report_enabled',True): _safe_daily(d,True)
        d+=timedelta(days=1)

    # Always check the completed previous Jalali month, even if PC was off on day 1.
    py,pm=previous_jalali_month_for(date.today()); key=f'{py:04d}-{pm:02d}'
    if not report_exists('monthly',key):
        _safe_monthly(py,pm)


def run_daily():
    try: retry_pending_notifications()
    except Exception: log.exception('pre-daily notification retry failed')
    cfg=settings_mod.load()
    if not cfg.get('auto_report_enabled',True):
        _write_status('disabled',date.today()-timedelta(days=1),'گزارش‌گیری خودکار غیرفعال است.')
        return
    if datetime.now().hour<8:
        _write_status('waiting',date.today()-timedelta(days=1),'گزارش روز قبل از ساعت ۸ صبح ساخته می‌شود.')
        return
    import_all(); _safe_daily(date.today()-timedelta(days=1),True)


def run_monthly():
    try: retry_pending_notifications()
    except Exception: log.exception('pre-monthly notification retry failed')
    import_all(); jy,jm,jd=gregorian_to_jalali(date.today().year,date.today().month,date.today().day)
    if jd!=1:return
    py,pm=previous_jalali_month_for(date.today()); key=f'{py:04d}-{pm:02d}'
    if not report_exists('monthly',key): _safe_monthly(py,pm)


def main():
    setup_logging(); init_db(); ap=argparse.ArgumentParser(); ap.add_argument('--mode',choices=['daily','monthly','catchup'],default='catchup'); a=ap.parse_args()
    {'daily':run_daily,'monthly':run_monthly,'catchup':catchup}[a.mode]()
if __name__=='__main__':main()
