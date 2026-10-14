"""Consent-gated archive protocol v1. Network operations run off the GUI thread."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from .paths import data_dir

ACTIVE = ('pending', 'approved', 'sending', 'awaiting_receipt', 'error')
LABELS = {'pending':'منتظر تأیید گوشی', 'approved':'تأیید شد؛ آماده ارسال',
          'sending':'در حال ارسال', 'awaiting_receipt':'ارسال شد؛ منتظر رسید گوشی',
          'completed':'دریافت کامل توسط گوشی', 'legacy_sent':'آرشیو برای گوشی آماده شد', 'rejected':'رد شده توسط گوشی',
          'expired':'مهلت پاسخ تمام شد', 'cancelled':'لغو شده', 'error':'خطا؛ تلاش مجدد خودکار'}

@contextmanager
def _con():
    c = sqlite3.connect(str(data_dir() / 'archive_transfers.db'), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, device TEXT, state TEXT, data TEXT)')
    try:
        with c:
            yield c
    finally:
        c.close()

def _put(c, r):
    c.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?)',
              (r['request_id'], r['device_id'], r['status'], json.dumps(r, ensure_ascii=False)))

def _get(c, rid):
    row = c.execute('SELECT data FROM requests WHERE id=?', (rid,)).fetchone()
    return json.loads(row[0]) if row else None

def list_requests():
    with _con() as c:
        rows = [json.loads(x[0]) for x in c.execute('SELECT data FROM requests')]
        for r in rows:
            if r['status'] == 'pending' and time.time() >= r['expires_at']:
                r['status'] = 'expired'; _put(c, r)
    return sorted(rows, key=lambda r:r['created_at'], reverse=True)

def status_for_device(did):
    r = next((r for r in list_requests() if r['device_id'] == did), None)
    if r:
        text = LABELS.get(r['status'], r['status'])
        if r['status'] in ('sending','awaiting_receipt','completed','error'):
            text += ' (%s/%s)' % (r['next_index'], len(r['reports']))
        return text
    try:
        from . import mobile_push as m
        if m.mobile_device_archive_needed(did):
            return 'گوشی جدید/دوباره متصل شد؛ آرشیو ارسال نشده'
    except Exception:
        pass
    return '—'


def _is_pwa_device(device):
    return str((device or {}).get('id_scheme') or '')=='web_pwa_v1' or str((device or {}).get('platform') or '')=='ios_web'

def _pwa_control_key(did):
    from . import mobile_push as m
    return m._control_topic(did)

def _pwa_firebase_publish(did, payload, event_id=None):
    from . import mobile_push as m
    key=_pwa_control_key(did)
    mid=str(event_id or payload.get('event_id') or ('evt-'+uuid.uuid4().hex))
    body=dict(payload)
    body.setdefault('secret',m.NTFY_REGISTRY_SECRET)
    body.setdefault('device_id',did)
    body['created_at_ms']=int(time.time()*1000)
    return m._db_request('PUT',f'pwa_control/{key}/{mid}',body,timeout=18)



def _pwa_archive_snapshot_publish(request):
    """Publish the complete numeric archive as one durable Firebase snapshot.

    Unlike event/batch delivery this survives missed polling windows and allows the
    iPhone PWA to re-download the same approved archive until it has imported it.
    """
    from . import mobile_push as m
    did=str(request.get('device_id') or '')
    key=_pwa_control_key(did)
    rows=[]
    for idx,row in enumerate(request.get('reports') or []):
        report=m._history_payload_for_report(row)
        report.pop('pdf_url',None)
        # Stable per-report key: repeated archive requests replace rather than duplicate.
        report['event_id']='archive-snapshot-'+str(row.get('report_type') or '')+'-'+str(row.get('period_key') or idx)
        rows.append(report)
    body={
        'kind':'archive_snapshot',
        'protocol':2,
        'secret':m.NTFY_REGISTRY_SECRET,
        'request_id':str(request.get('request_id') or ''),
        'device_id':did,
        'snapshot_mode':'replace_current',
        'count':len(rows),
        'updated_at_ms':int(time.time()*1000),
        'reports':rows,
    }
    # Keep this as a durable single node per device. A PWA that was closed during
    # transfer can fetch it later without depending on ntfy/SSE timing.
    m._db_request('PUT',f'pwa_archive_snapshots/{key}',body,timeout=35)
    return len(rows)

def _pull_pwa_decision(rid,did):
    from . import mobile_push as m
    key=_pwa_control_key(did)
    try:
        payload=m._db_request('GET',f'pwa_archive_decisions/{key}/{rid}',timeout=12)
    except Exception:
        return False
    if not isinstance(payload,dict): return False
    if str(payload.get('request_id') or '')!=str(rid) or str(payload.get('device_id') or '')!=str(did): return False
    ok=handle_event(payload)
    if ok:
        try:m._db_request('DELETE',f'pwa_archive_decisions/{key}/{rid}',timeout=8)
        except Exception:pass
    return ok

def _watch_pwa_decision(rid,did):
    from . import mobile_push as m
    deadline=time.time()+900
    while time.time()<deadline:
        try:
            if _pull_pwa_decision(rid,did):
                process_transfers();return
            with _con() as c:r=_get(c,rid)
            if not r or r.get('status')!='pending':return
        except Exception:
            m.log.exception('PWA archive decision polling failed')
        time.sleep(3)

def request_archive(did):
    from . import mobile_push as m, db
    all_devices=m.list_mobile_devices(sync=False)
    device = next((d for d in all_devices
                   if d.get('device_id') == did and d.get('enabled', True)), None)
    if device is None: raise ValueError('گوشی انتخاب‌شده فعال نیست یا حذف شده است.')
    scheme=str(device.get('id_scheme') or '')
    if scheme!='web_pwa_v1' and tuple(int(x) for x in re.findall(r'\d+',str(device.get('app_version','')))[:3]) < (1,5,7):
        raise ValueError('فقط گوشی انتخاب‌شده باید نسخه 1.5.7 یا جدیدتر داشته باشد.')
    with db.connect() as c:
        reports = [dict(x) for x in c.execute('SELECT report_type,period_key,path,created_at FROM generated_reports ORDER BY created_at,id')]
    valid = [r for r in reports if r['report_type'] in ('daily','monthly')]
    if not valid: raise ValueError('هیچ گزارش انرژی روزانه یا ماهانه ثبت‌شده‌ای پیدا نشد.')
    list_requests()  # expire old unanswered requests
    now = time.time()
    r = dict(request_id=uuid.uuid4().hex, device_id=did, status='pending', created_at=now,
             expires_at=now+86400, token=secrets.token_urlsafe(32), reports=valid,
             missing_count=len(reports)-len(valid), next_index=0, lease_until=0, error='')
    with _con() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT data FROM requests WHERE device=?', (did,)):
            old = json.loads(row[0])
            if old['status'] in ACTIVE:
                raise ValueError('برای این گوشی درخواست باز وجود دارد؛ ابتدا آن را لغو کنید یا منتظر پاسخ بمانید.')
        _put(c, r)
    is_pwa=_is_pwa_device(device)
    offer_payload={
        'kind':'archive_offer', 'protocol':1, 'secret':m.NTFY_REGISTRY_SECRET,
        'request_id':r['request_id'], 'device_id':did, 'decision_token':r['token'],
        'source_instance_id':m.windows_instance_id(),
        'report_count':len(valid), 'missing_count':r['missing_count'], 'expires_at':r['expires_at'],
        'title':'درخواست دریافت آرشیو گزارش‌ها',
        'body':f'آیا دریافت {len(valid)} گزارش قبلی را تأیید می‌کنید؟',
        'event_id':'archive-offer-'+r['request_id']
    }
    sent_any=False; notice_warning=''
    if is_pwa:
        try:
            _pwa_firebase_publish(did,offer_payload,event_id=offer_payload['event_id']); sent_any=True
        except Exception as ex:
            m.log.warning('Firebase PWA archive offer failed for %s: %s',did,ex)
            notice_warning='ارسال درخواست آرشیو از Firebase ناموفق بود؛ مسیر پشتیبان امتحان شد.'
    try:
        m._ntfy_publish(m._control_topic(did), offer_payload, title='SolicoEnergy archive request'); sent_any=True
    except Exception as ex:
        m.log.warning('ntfy archive offer failed for %s: %s',did,ex)
        if not notice_warning: notice_warning='مسیر ntfy در دسترس نبود.'

    if not is_pwa:
        offer_data={'action':'archive_offer', 'target_device_id':did,
                    'request_id':r['request_id'], 'source_instance_id':m.windows_instance_id(),
                    'decision_token':r['token'], 'protocol':'1',
                    'report_count':str(len(valid)), 'expires_at':str(r['expires_at']),
                    'title':'دریافت آرشیو گزارش‌ها',
                    'body':'گزارش‌های قبلی ذخیره‌شده در ویندوز دریافت شوند؟',
                    'event_id':'archive-offer-'+r['request_id']}
        token=str(device.get('fcm_token') or '').strip()
        direct_error=''; sent=False
        if token:
            try:
                m._fcm_send_message({'token':token,'data':offer_data,'android':{'priority':'high','ttl':'86400s'}});sent=True;sent_any=True
            except Exception as ex:
                direct_error=str(ex);m.log.warning('Direct-token FCM archive offer failed for %s: %s',did,ex)
                try:
                    if 'UNREGISTERED' in direct_error.upper() or 'NOTREGISTERED' in direct_error.upper():m._clear_stale_fcm_token(did,token)
                except Exception:pass
        if not sent:
            try:
                m._fcm_send_message({'topic':'solico_device_'+did,'data':offer_data,'android':{'priority':'high','ttl':'86400s'}});sent=True;sent_any=True
            except Exception as ex:
                m.log.warning('Private-topic FCM archive offer failed for %s: %s',did,ex)
                notice_warning=(notice_warning+' ' if notice_warning else '')+'اعلان Firebase مستقیم/Topic ارسال نشد.'

    if not sent_any:
        with _con() as c:
            current=_get(c,r['request_id']);current['status']='cancelled';_put(c,current)
        raise RuntimeError('ارسال درخواست آرشیو از همه مسیرها ناموفق بود.')
    if is_pwa:
        threading.Thread(target=_watch_pwa_decision,args=(r['request_id'],did),name='SolicoPwaArchiveDecision',daemon=True).start()
    try:
        m.clear_mobile_device_archive_needed(did)
    except Exception:
        pass
    return {'request_id':r['request_id'], 'count':len(valid), 'missing_count':r['missing_count'], 'warning':notice_warning}

def cancel_for_device(did):
    with _con() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT data FROM requests WHERE device=?', (did,)).fetchall():
            r=json.loads(row[0])
            if r['status'] in ACTIVE:
                r['status']='cancelled'; _put(c,r)

def handle_event(payload):
    """Only exact boolean decisions and matching per-offer token are accepted."""
    from . import mobile_push as m
    if payload.get('secret') != m.NTFY_REGISTRY_SECRET: return False
    with _con() as c:
        c.execute('BEGIN IMMEDIATE')
        r=_get(c, str(payload.get('request_id') or ''))
        if not r or payload.get('device_id') != r['device_id']: return False
        if payload.get('kind') in ('history_accept','history_request','history_reject'):
            # FIX28 does not echo the new token. Require an unpredictable, locally
            # issued request ID, exact device, registry secret and source binding.
            target=str(payload.get('target_instance_id') or '')
            if target and target != m.windows_instance_id(): return False
            if r['status'] != 'pending': return False
            if time.time() >= r['expires_at']:
                r['status']='expired'; _put(c,r); return False
            # Both explicit acceptance and the legacy follow-up request authorize
            # this exact pending offer. A second message is not required.
            r['status']='rejected' if payload['kind']=='history_reject' else 'approved'
            r['transport']='pwa_firebase' if str(payload.get('transport') or '')=='pwa_firebase' else 'fix28'; _put(c,r); return True
        token=payload.get('decision_token')
        if not isinstance(token,str) or not hmac.compare_digest(token,r['token']): return False
        if payload.get('kind') == 'archive_decision':
            if r['status'] != 'pending' or type(payload.get('approved')) is not bool: return False
            if time.time() >= r['expires_at']: r['status']='expired'
            else: r['status']='approved' if payload['approved'] else 'rejected'
        elif payload.get('kind') == 'archive_receipt':
            if r['status'] != 'awaiting_receipt': return False
            if type(payload.get('received_count')) is not int or payload['received_count'] != len(r['reports']): return False
            r['status']='completed'
        else: return False
        _put(c,r)
    return True

def process_transfers():
    """Bounded resumable work. FIX28/history transport batches several numeric reports
    per ntfy message while staying under a conservative payload size.
    """
    from . import mobile_push as m
    device_map={d['device_id']:d for d in m.list_mobile_devices(sync=False) if d.get('enabled',True)}
    devices=set(device_map)
    for listed in list_requests():
        if listed.get('status')=='pending' and _is_pwa_device(device_map.get(listed.get('device_id'))):
            try:_pull_pwa_decision(listed['request_id'],listed['device_id'])
            except Exception:m.log.exception('PWA decision sync failed')
        rid=listed['request_id']
        with _con() as c:
            c.execute('BEGIN IMMEDIATE'); r=_get(c,rid)
            if r['status'] not in ('approved','sending','error'): continue
            if r['lease_until'] > time.time(): continue
            if r['device_id'] not in devices:
                r['status']='cancelled'; _put(c,r); continue
            r['status']='sending'; r['lease_until']=time.time()+300; _put(c,r)
        try:
            # PWA R9+: publish one durable Firebase snapshot instead of relying on
            # short-lived batches. ntfy/control events remain as compatibility fallback.
            with _con() as c: r=_get(c,rid)
            if r.get('transport')=='pwa_firebase':
                count=_pwa_archive_snapshot_publish(r)
                complete=dict(protocol=2,secret=m.NTFY_REGISTRY_SECRET,request_id=rid,
                              device_id=r['device_id'],kind='history_complete',count=count,
                              snapshot_mode='replace_current',event_id='archive-complete-'+rid)
                try:_pwa_firebase_publish(r['device_id'],complete,event_id=complete['event_id'])
                except Exception:m.log.exception('PWA archive complete event publish failed')
                try:m._ntfy_publish(m._control_topic(r['device_id']),complete)
                except Exception:pass
                with _con() as c:
                    c.execute('BEGIN IMMEDIATE'); current=_get(c,rid)
                    if current and current.get('status')=='sending':
                        current['next_index']=len(current.get('reports') or [])
                        current['status']='legacy_sent';current['error']='';_put(c,current)
                try:m.clear_mobile_device_archive_needed(r['device_id'])
                except Exception:pass
                continue

            for _ in range(12):
                with _con() as c: r=_get(c,rid)
                if r['status'] != 'sending': break
                i=r['next_index']
                base=dict(protocol=1, secret=m.NTFY_REGISTRY_SECRET, request_id=rid, device_id=r['device_id'])
                if i >= len(r['reports']):
                    legacy=r.get('transport') in ('fix28','pwa_firebase')
                    complete=dict(base,kind='history_complete' if legacy else 'archive_complete',count=i,snapshot_mode='replace_current',event_id='archive-complete-'+rid)
                    if r.get('transport')=='pwa_firebase':
                        _pwa_firebase_publish(r['device_id'],complete,event_id=complete['event_id'])
                        try:m._ntfy_publish(m._control_topic(r['device_id']),complete)
                        except Exception:pass
                    else:
                        m._ntfy_publish(m._control_topic(r['device_id']),complete)
                    with _con() as c:
                        c.execute('BEGIN IMMEDIATE'); r=_get(c,rid)
                        if r['status']=='sending': r['status']='legacy_sent' if legacy else 'awaiting_receipt'; _put(c,r)
                    try: m.clear_mobile_device_archive_needed(r['device_id'])
                    except Exception: pass
                    break

                if r.get('transport') in ('fix28','pwa_firebase'):
                    batch=[]; j=i
                    while j < len(r['reports']) and len(batch)<4:
                        row=r['reports'][j]
                        report=m._history_payload_for_report(row)
                        report.pop('pdf_url',None)
                        report['event_id']='archive-'+rid+'-'+str(j)
                        candidate=batch+[report]
                        packet=dict(base,kind='history_batch',snapshot_mode='replace_current',reports=candidate)
                        size=len(json.dumps(packet,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
                        if batch and size>3500:
                            break
                        batch=candidate; j+=1
                        if size>3500:
                            break
                    if not batch:
                        raise RuntimeError('بسته آرشیو ساخته نشد.')
                    packet=dict(base,kind='history_batch',snapshot_mode='replace_current',reports=batch,event_id='archive-batch-'+rid+'-'+str(i))
                    if r.get('transport')=='pwa_firebase':
                        _pwa_firebase_publish(r['device_id'],packet,event_id=packet['event_id'])
                        try:m._ntfy_publish(m._control_topic(r['device_id']),packet)
                        except Exception:pass
                    else:
                        m._ntfy_publish(m._control_topic(r['device_id']),packet)
                    advance=len(batch)
                else:
                    row=r['reports'][i]
                    report=m._history_payload_for_report(row)
                    report.pop('pdf_url',None)
                    report['event_id']='archive-'+rid+'-'+str(i)
                    m._ntfy_publish(m._control_topic(r['device_id']), dict(base,kind='archive_item',index=i,report=report))
                    advance=1

                with _con() as c:
                    c.execute('BEGIN IMMEDIATE'); r=_get(c,rid)
                    if r['status']=='sending':
                        r['next_index']=min(len(r['reports']),i+advance); r['error']=''; _put(c,r)
        except Exception as ex:
            m.log.exception('archive transfer failed')
            with _con() as c:
                c.execute('BEGIN IMMEDIATE'); r=_get(c,rid)
                if r['status']=='sending': r['status']='error'; r['error']=str(ex); _put(c,r)
        finally:
            with _con() as c:
                c.execute('BEGIN IMMEDIATE'); r=_get(c,rid); r['lease_until']=0; _put(c,r)
