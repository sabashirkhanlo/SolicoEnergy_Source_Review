from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import settings as settings_mod
from .paths import data_dir, user_root

log = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging https://www.googleapis.com/auth/firebase.database https://www.googleapis.com/auth/userinfo.email"
TOKEN_URL_DEFAULT = "https://oauth2.googleapis.com/token"
TOPIC = "solico_reports"
DEFAULT_PORT = 8765
FIREBASE_DB_URL = "https://REDACTED-PRODUCTION-ENDPOINT.invalid"  # fallback only; R19 discovers deployed DB URL
FIREBASE_DB_CONFIG_URL = "https://REDACTED-PRODUCTION-ENDPOINT.invalid"
FIREBASE_DB_FALLBACKS = [
    "https://REDACTED-PRODUCTION-ENDPOINT.invalid",
    "https://REDACTED-PRODUCTION-ENDPOINT.invalid",
    "https://REDACTED-PRODUCTION-ENDPOINT.invalid",
    "https://REDACTED-PRODUCTION-ENDPOINT.invalid",
]
NTFY_BASE = "https://ntfy.sh"
NTFY_REGISTRY_TOPIC = "REDACTED_REVIEW_TOPIC"
NTFY_REGISTRY_SECRET = "REDACTED_REVIEW_ONLY"
NTFY_WINDOWS_TOPIC = "solico-win-" + hashlib.sha256((NTFY_REGISTRY_SECRET+"|windows-report-sync").encode("utf-8")).hexdigest()[:24]

# R28: standards-based Web Push for iPhone/iPad Home Screen PWAs.
# Public key is deployed in the PWA; the private scalar stays only on Windows.
WEBPUSH_VAPID_PUBLIC = "REDACTED_REVIEW_ONLY"
WEBPUSH_VAPID_PRIVATE_HEX = "REDACTED_REVIEW_ONLY"
WEBPUSH_SUBJECT = "mailto:admin@solicoenergy.local"

_server_lock = threading.Lock()
_server = None
_server_thread = None
_server_port = None
_token_lock = threading.Lock()
_cached_token = None
_cached_token_expiry = 0.0
_devices_lock = threading.RLock()
_windows_sync_lock = threading.RLock()
_windows_sync_thread = None
_windows_sync_stop = threading.Event()
_windows_history_sent = {}


def _powershell_json_request(url, *, method="GET", body=None, content_type=None, headers=None, timeout=20):
    # Fallback HTTP transport using the Windows/.NET networking stack.
    if os.name != "nt":
        raise RuntimeError("PowerShell HTTP fallback is only available on Windows.")

    exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise RuntimeError("PowerShell was not found for network fallback.")

    env = os.environ.copy()
    env["SOLICO_HTTP_URL"] = str(url)
    env["SOLICO_HTTP_METHOD"] = str(method or "GET").upper()
    env["SOLICO_HTTP_BODY"] = "" if body is None else (body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body))
    env["SOLICO_HTTP_CONTENT_TYPE"] = str(content_type or "")
    env["SOLICO_HTTP_TIMEOUT"] = str(max(3, int(timeout or 20)))
    h = dict(headers or {})
    env["SOLICO_HTTP_AUTH"] = str(h.get("Authorization") or "")
    env["SOLICO_HTTP_ACCEPT"] = str(h.get("Accept") or "application/json")

    script = r'''$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
try { [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials } catch {}
$uri = $env:SOLICO_HTTP_URL
$method = $env:SOLICO_HTTP_METHOD
$timeout = [int]$env:SOLICO_HTTP_TIMEOUT
$headers = @{}
if ($env:SOLICO_HTTP_AUTH) { $headers["Authorization"] = $env:SOLICO_HTTP_AUTH }
if ($env:SOLICO_HTTP_ACCEPT) { $headers["Accept"] = $env:SOLICO_HTTP_ACCEPT }
$params = @{
  Uri = $uri
  Method = $method
  Headers = $headers
  TimeoutSec = $timeout
  ErrorAction = "Stop"
}
if ($env:SOLICO_HTTP_CONTENT_TYPE) { $params["ContentType"] = $env:SOLICO_HTTP_CONTENT_TYPE }
if ($method -ne "GET" -and $method -ne "HEAD") { $params["Body"] = $env:SOLICO_HTTP_BODY }
$r = Invoke-RestMethod @params
if ($null -eq $r) { "{}" } else { $r | ConvertTo-Json -Depth 25 -Compress }
'''
    cp = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=max(8, int(timeout or 20) + 8),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (cp.stdout or "").strip().lstrip("\ufeff")
    err = (cp.stderr or "").strip()
    if cp.returncode != 0:
        raise RuntimeError("Windows HTTP fallback failed: " + (err or out or f"exit {cp.returncode}"))
    if not out:
        return {}
    candidates = [x.strip() for x in out.splitlines() if x.strip()]
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out}



def _powershell_text_request(url, *, timeout=20):
    """GET text through Windows networking stack (for ntfy NDJSON feeds)."""
    if os.name != "nt":
        raise RuntimeError("PowerShell HTTP fallback is only available on Windows.")
    exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise RuntimeError("PowerShell was not found for network fallback.")
    env = os.environ.copy()
    env["SOLICO_HTTP_URL"] = str(url)
    env["SOLICO_HTTP_TIMEOUT"] = str(max(3, int(timeout or 20)))
    script = r'''$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
try { [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials } catch {}
$r = Invoke-WebRequest -UseBasicParsing -Uri $env:SOLICO_HTTP_URL -Method Get -TimeoutSec ([int]$env:SOLICO_HTTP_TIMEOUT) -Headers @{"Accept"="application/x-ndjson, application/json, text/plain"}
[Console]::Out.Write($r.Content)
'''
    cp = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
        input=script, text=True, capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=max(8, int(timeout or 20)+8),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out=(cp.stdout or "").lstrip("\ufeff")
    err=(cp.stderr or "").strip()
    if cp.returncode != 0:
        raise RuntimeError("Windows HTTP text fallback failed: "+(err or out or f"exit {cp.returncode}"))
    return out


def _http_text_get(url, *, timeout=20, purpose="HTTP text GET"):
    req=urllib.request.Request(url,headers={"Accept":"application/x-ndjson, application/json, text/plain"},method="GET")
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return r.read().decode("utf-8",errors="replace")
    except Exception as first:
        if os.name != "nt":
            raise
        log.warning("%s failed via urllib (%s); retrying through Windows network stack",purpose,first)
        try:
            raw=_powershell_text_request(url,timeout=timeout)
            log.info("%s succeeded through Windows network fallback",purpose)
            return raw
        except Exception as second:
            raise RuntimeError(f"{purpose} failed via urllib and Windows fallback: {first}; {second}") from second


def _urllib_json_request(req, *, timeout=20, fallback_url=None, fallback_method=None,
                         fallback_body=None, fallback_content_type=None, fallback_headers=None,
                         purpose="HTTP request"):
    # Use urllib first; on network/SSL failures retry through Windows PowerShell.
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError:
        raise
    except Exception as first:
        if os.name != "nt":
            raise
        url = fallback_url or getattr(req, "full_url", None)
        method = fallback_method or getattr(req, "method", None) or "GET"
        body = fallback_body if fallback_body is not None else getattr(req, "data", None)
        headers = dict(fallback_headers or {})
        if not headers:
            try:
                headers.update(dict(req.header_items()))
            except Exception:
                pass
        ct = fallback_content_type or headers.get("Content-type") or headers.get("Content-Type")
        log.warning("%s failed via urllib (%s); retrying through Windows network stack", purpose, first)
        try:
            result = _powershell_json_request(
                url, method=method, body=body, content_type=ct,
                headers=headers, timeout=timeout,
            )
            log.info("%s succeeded through Windows network fallback", purpose)
            return result
        except Exception as second:
            raise RuntimeError(f"{purpose} failed via urllib and Windows fallback: {first}; {second}") from second


def _login_requests_path() -> Path:
    return data_dir() / "mobile_login_requests.json"


def _load_login_requests():
    try:
        p=_login_requests_path()
        if p.exists():
            obj=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj,list):
                return obj
    except Exception:
        pass
    return []


def _save_login_requests(items):
    p=_login_requests_path()
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")


def _record_login_request(payload, seen):
    request_id=str(payload.get("request_id") or "").strip()
    device_id=str(payload.get("device_id") or "").strip()
    if not request_id or not device_id:
        return

    items=_load_login_requests()
    found=None
    for x in items:
        if str(x.get("request_id"))==request_id:
            found=x
            break

    if found is None:
        found={
            "request_id":request_id,
            "device_id":device_id,
            "status":"pending",
            "requested_at":seen,
        }
        items.append(found)

    if str(found.get("status") or "") not in ("approved","rejected"):
        found.update({
            "device_id":device_id,
            "username":str(payload.get("username") or ""),
            "name":str(payload.get("name") or "Android device"),
            "app_version":str(payload.get("app_version") or ""),
            "android":str(payload.get("android") or ""),
            "phone_ip":str(payload.get("local_ip") or ""),
            "requested_at":seen,
            "status":"pending",
        })

    _save_login_requests(items)


def list_mobile_login_requests(sync=True):
    if sync:
        try:
            sync_cloud_devices()
        except Exception:
            log.exception("login request sync failed")
    rows=_load_login_requests()
    order={"pending":0,"approved":1,"rejected":2}
    return sorted(
        rows,
        key=lambda x:(order.get(str(x.get("status") or ""),9),str(x.get("requested_at") or "")),
        reverse=False,
    )


def _control_topic(device_id):
    raw=(NTFY_REGISTRY_SECRET+"|"+str(device_id)).encode("utf-8")
    return "solico-ctl-"+hashlib.sha256(raw).hexdigest()[:24]


def _mirror_pwa_latest_report(device_id, payload):
    """Persist the newest iPhone/PWA report independently of push delivery."""
    did=str(device_id or "").strip()
    if not did:
        return False
    body=dict(payload or {})
    body.update({
        "kind":"report_notification",
        "secret":NTFY_REGISTRY_SECRET,
        "device_id":did,
        "created_at_ms":int(time.time()*1000),
        "transport":"firebase_latest_snapshot",
    })
    _db_request("PUT",f"pwa_latest_reports/{_control_topic(did)}",body,timeout=18)
    return True


def _request_pwa_webpush_resubscribe(device_id, reason="stale"):
    did=str(device_id or "").strip()
    if not did:
        return False
    payload={
        "kind":"webpush_resubscribe",
        "secret":NTFY_REGISTRY_SECRET,
        "device_id":did,
        "reason":str(reason or "stale"),
        "created_at_ms":int(time.time()*1000),
    }
    key=_control_topic(did)
    event_key="push-repair-"+uuid.uuid4().hex
    _db_request("PUT",f"pwa_control/{key}/{event_key}",payload,timeout=18)
    return True


def _ntfy_publish(topic, payload, title="SolicoEnergy"):
    req_obj={
        "topic":str(topic),
        "title":str(title),
        "message":json.dumps(payload,ensure_ascii=False,separators=(",",":")),
        "priority":3,
    }
    data=json.dumps(req_obj,ensure_ascii=False).encode("utf-8")
    req=urllib.request.Request(
        NTFY_BASE.rstrip("/")+"/",
        data=data,
        headers={"Content-Type":"application/json; charset=utf-8"},
        method="POST",
    )
    return _urllib_json_request(
        req, timeout=15, fallback_url=NTFY_BASE.rstrip("/")+"/",
        fallback_method="POST", fallback_body=data,
        fallback_content_type="application/json; charset=utf-8",
        fallback_headers={"Content-Type":"application/json; charset=utf-8"},
        purpose="ntfy publish",
    )


def _devices_path() -> Path:
    return data_dir() / "mobile_devices.json"


def _pwa_push_cache_path() -> Path:
    """Durable local Safari Web Push cache.

    R34 keeps the iPhone push subscription locally so report notifications do not
    need Firebase/ntfy registry access at send time. Registration/sync may still use
    cloud transports, but the notification path itself can work with Windows VPN off.
    """
    return data_dir() / "pwa_webpush_cache.json"


def _read_pwa_push_cache():
    try:
        p=_pwa_push_cache_path()
        if p.exists():
            obj=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj,dict):
                return obj
    except Exception:
        log.exception("reading local PWA push cache failed")
    return {}


def _write_pwa_push_cache(obj):
    try:
        p=_pwa_push_cache_path()
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception:
        log.exception("writing local PWA push cache failed")


def _refresh_pwa_push_cache(items):
    """Persist last-known-good iPhone subscriptions without erasing them on outages."""
    cache=_read_pwa_push_cache()
    now_ms=int(time.time()*1000)
    for d in (items or []):
        if not isinstance(d,dict):
            continue
        did=str(d.get("device_id") or "").strip()
        if not did or str(d.get("id_scheme") or "")!="web_pwa_v1":
            continue
        sub=d.get("webpush_subscription")
        if isinstance(sub,str):
            try: sub=json.loads(sub)
            except Exception: sub=None
        if isinstance(sub,dict) and str(sub.get("endpoint") or "").startswith("https://"):
            cache[did]={
                "device_id":did,
                "id_scheme":"web_pwa_v1",
                "platform":d.get("platform") or "ios_web",
                "name":d.get("name") or ("iPhone WebApp • "+did[-4:].upper()),
                "owner_name":d.get("owner_name") or d.get("owner") or "",
                "enabled":bool(d.get("enabled",True)),
                "webpush_subscription":sub,
                "push_subscription_updated_at_ms":d.get("push_subscription_updated_at_ms") or now_ms,
                "cached_at_ms":now_ms,
            }
        elif did in cache:
            # Keep the last known subscription; only refresh non-destructive metadata.
            cache[did]["enabled"]=bool(d.get("enabled",cache[did].get("enabled",True)))
            cache[did]["name"]=d.get("name") or cache[did].get("name")
            cache[did]["owner_name"]=d.get("owner_name") or d.get("owner") or cache[did].get("owner_name","")
    _write_pwa_push_cache(cache)


def _notification_devices_local_only():
    """Return notification targets without touching Firebase/ntfy.

    This is the critical R34 change. A report notification must not wait for or
    depend on filtered cloud registry endpoints. iPhone Web Push subscriptions are
    merged from the durable local cache and sent directly to the Apple push endpoint.
    """
    items=_dedupe_legacy_devices(_load_devices())
    by_id={str(d.get("device_id") or ""):dict(d) for d in items if d.get("device_id")}
    cache=_read_pwa_push_cache()
    for did,cached in cache.items():
        if not isinstance(cached,dict) or not did:
            continue
        prev=by_id.get(did) or {}
        merged=dict(cached)
        merged.update(prev)
        if not prev.get("webpush_subscription") and cached.get("webpush_subscription"):
            merged["webpush_subscription"]=cached.get("webpush_subscription")
        merged["id_scheme"]=prev.get("id_scheme") or cached.get("id_scheme") or "web_pwa_v1"
        merged["enabled"]=bool(prev.get("enabled",cached.get("enabled",True)))
        by_id[did]=merged
    out=[]
    for d in by_id.values():
        did=str(d.get("device_id") or "").strip()
        if not did or _is_device_tombstoned(did) or not bool(d.get("enabled",True)):
            continue
        out.append(d)
    return _dedupe_legacy_devices(out)


def _load_devices():
    with _devices_lock:
        try:
            if _devices_path().exists():
                obj=json.loads(_devices_path().read_text(encoding="utf-8"))
                if isinstance(obj,list): return obj
        except Exception: pass
        return []


def _save_devices(items):
    with _devices_lock:
        _devices_path().parent.mkdir(parents=True,exist_ok=True)
        _devices_path().write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
    # R34: keep the last valid Safari Web Push subscription in a separate local
    # cache so a temporary Firebase/ntfy outage can never erase notification ability.
    try:
        _refresh_pwa_push_cache(items)
    except Exception:
        log.exception("refreshing local PWA push cache failed")


def _purge_deleted_or_disabled_devices():
    """Old versions kept disabled devices. New behavior is hard delete."""
    items=[d for d in _load_devices() if bool(d.get("enabled",True))]
    _save_devices(items)
    return items


def _device_tombstones_path() -> Path:
    return data_dir() / "mobile_device_tombstones.json"


def _archive_needed_path() -> Path:
    return data_dir() / "mobile_archive_needed.json"


def _load_json_dict(path):
    try:
        if path.exists():
            obj=json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj,dict): return obj
    except Exception:
        pass
    return {}


def _save_json_dict(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")


def _is_device_tombstoned(device_id):
    return str(device_id or "").strip() in _load_json_dict(_device_tombstones_path())


def _mark_device_deleted(device_id, *, deleted_at=None, login_session_id=""):
    did=str(device_id or "").strip()
    if not did: return
    obj=_load_json_dict(_device_tombstones_path())
    obj[did]={"deleted_at":float(deleted_at or time.time()),"login_session_id":str(login_session_id or "")}
    _save_json_dict(_device_tombstones_path(),obj)
    needed=_load_json_dict(_archive_needed_path()); needed.pop(did,None); _save_json_dict(_archive_needed_path(),needed)


def _allow_device_reconnect(device_id):
    did=str(device_id or "").strip()
    obj=_load_json_dict(_device_tombstones_path())
    existed=did in obj
    if existed:
        obj.pop(did,None); _save_json_dict(_device_tombstones_path(),obj)
    if did:
        needed=_load_json_dict(_archive_needed_path())
        needed[did]={"reason":"reconnect","time":time.time()}
        _save_json_dict(_archive_needed_path(),needed)
    return existed


def mark_device_archive_needed(device_id, reason="new"):
    did=str(device_id or "").strip()
    if not did:return
    needed=_load_json_dict(_archive_needed_path())
    needed[did]={"reason":str(reason or "new"),"time":time.time()}
    _save_json_dict(_archive_needed_path(),needed)


def mobile_device_archive_needed(device_id):
    return str(device_id or "").strip() in _load_json_dict(_archive_needed_path())


def clear_mobile_device_archive_needed(device_id):
    did=str(device_id or "").strip()
    needed=_load_json_dict(_archive_needed_path())
    if did in needed:
        needed.pop(did,None); _save_json_dict(_archive_needed_path(),needed)


def set_mobile_device_owner(device_id, owner_name):
    device_id=str(device_id or "").strip()
    owner_name=str(owner_name or "").strip()
    if not device_id:
        return False
    items=_load_devices(); found=False
    for d in items:
        if str(d.get("device_id"))==device_id:
            d["owner_name"]=owner_name
            found=True
            break
    if not found:
        return False
    _save_devices(items)
    try:
        _ntfy_publish(
            NTFY_REGISTRY_TOPIC,
            {"kind":"device_owner_update","secret":NTFY_REGISTRY_SECRET,
             "device_id":device_id,"owner_name":owner_name},
            title="SolicoEnergy device owner",
        )
    except Exception:
        log.exception("device owner cloud sync failed")
    return True


def delete_mobile_device(device_id):
    """Delete the current phone session. A fresh credential login may reconnect it."""
    device_id=str(device_id or "").strip()
    if not device_id:
        return False
    before=_load_devices()
    previous=next((d for d in before if str(d.get("device_id"))==device_id),{})
    _mark_device_deleted(device_id,login_session_id=previous.get("login_session_id") or "")
    after=[d for d in before if str(d.get("device_id"))!=device_id]
    _save_devices(after)
    reqs=[r for r in _load_login_requests() if str(r.get("device_id"))!=device_id]
    _save_login_requests(reqs)
    try:
        _send_device_access_control(device_id,False)
    except Exception:
        log.exception("device remote disable failed during delete")
    try:
        _ntfy_publish(
            NTFY_REGISTRY_TOPIC,
            {"kind":"device_delete","secret":NTFY_REGISTRY_SECRET,"device_id":device_id,
             "deleted_at":time.time()},
            title="SolicoEnergy device delete",
        )
    except Exception:
        log.exception("device delete cloud sync failed")
    return len(after)!=len(before)


def list_mobile_devices(sync=True):
    if sync:
        try:
            sync_cloud_devices()
        except Exception:
            log.exception("cloud device sync failed")
    rows=_purge_deleted_or_disabled_devices()
    return sorted(rows, key=lambda x: str(x.get("last_seen") or ""), reverse=True)


def register_mobile_device(payload, remote_ip=""):
    device_id=str(payload.get("device_id") or "").strip()
    token=str(payload.get("token") or "").strip()
    if not device_id or not token: raise ValueError("device_id/token required")
    if _is_device_tombstoned(device_id):
        # A reinstalled Android app may have restored an old logged-in SharedPreferences
        # snapshot. Keep the tombstone and actively force that stale session back to Login.
        try:
            _send_device_access_control(device_id,False)
        except Exception:
            log.exception("stale tombstoned device disable failed during direct register")
        return {"device_id":device_id,"enabled":False}
    now=time.strftime("%Y-%m-%d %H:%M:%S")
    items=_load_devices(); found=None
    for d in items:
        if str(d.get("device_id"))==device_id: found=d; break
    if found is None:
        found={"device_id":device_id,"first_seen":now,"enabled":True}; items.append(found)
    found.update({"token":token,"name":str(payload.get("name") or "Android device"),"app_version":str(payload.get("app_version") or ""),"android":str(payload.get("android") or ""),"last_seen":now,"ip":remote_ip,"owner_name":str(found.get("owner_name") or "")})
    _save_devices(items)
    return dict(found)



def _history_payload_for_report(row):
    from datetime import date as _date
    from . import db as _db

    rtype=str(row.get("report_type") or "")
    period=str(row.get("period_key") or "")
    path=str(row.get("path") or "")

    payload={
        "event_id":"history-"+rtype+"-"+period,
        "title":"گزارش قبلی Solico Energy",
        "body":"این گزارش قبلاً در نرم‌افزار ویندوز ساخته شده است.",
        "report_kind":rtype,
        "report_title":"گزارش "+rtype,
        "report_date":period.replace("-","/"),
        "report_date_iso":"",
        "pdf_url":"",
    }

    try:
        if rtype=="daily":
            d=_date.fromisoformat(period)
            payload.update(_energy_mobile_kpi(kind="daily",report_date=d))
            payload["title"]=payload.get("report_title") or "گزارش روزانه"
            payload["body"]="گزارش روزانه قبلی همگام‌سازی شد."
            return payload

        if rtype=="monthly":
            jy,jm=[int(x) for x in period.split("-")[:2]]
            payload.update(_energy_mobile_kpi(kind="monthly",jalali_year=jy,jalali_month=jm))
            payload["title"]=payload.get("report_title") or "گزارش ماهانه"
            payload["body"]="گزارش ماهانه قبلی همگام‌سازی شد."
            return payload

        if rtype.startswith("generic_daily_"):
            from ..core.jalali import format_jalali
            d=_date.fromisoformat(period)
            payload.update({
                "report_kind":"generic_daily",
                "report_title":"گزارش تجهیزات "+format_jalali(d),
                "report_date":format_jalali(d),
                "report_date_iso":d.isoformat(),
                "title":"گزارش تجهیزات",
                "body":"گزارش تجهیزات قبلی همگام‌سازی شد.",
            })
            return payload

        if rtype.startswith("generic_monthly_"):
            payload.update({
                "report_kind":"generic_monthly",
                "report_title":"گزارش ماهانه تجهیزات "+period.replace("-","/"),
                "report_date":period.replace("-","/"),
                "title":"گزارش ماهانه تجهیزات",
                "body":"گزارش ماهانه تجهیزات قبلی همگام‌سازی شد.",
            })
            return payload
    except Exception:
        log.exception("history payload build failed for %s %s",rtype,period)

    return payload


def sync_existing_reports_to_device(device_id, request_id):
    raise RuntimeError("ارسال آرشیو فقط از مسیر درخواست و تأیید گوشی مجاز است.")


def respond_mobile_login(request_id, approved):
    request_id=str(request_id or "").strip()
    rows=_load_login_requests()
    req=None
    for x in rows:
        if str(x.get("request_id"))==request_id:
            req=x
            break
    if req is None:
        raise RuntimeError("درخواست ورود پیدا نشد.")

    device_id=str(req.get("device_id") or "").strip()
    if not device_id:
        raise RuntimeError("شناسه دستگاه درخواست ورود خالی است.")

    decision={
        "kind":"login_decision",
        "secret":NTFY_REGISTRY_SECRET,
        "request_id":request_id,
        "device_id":device_id,
        "approved":bool(approved),
    }
    _ntfy_publish(_control_topic(device_id),decision,title="SolicoEnergy login")
    # Keep all Windows installations on the same approval state.
    try:
        _ntfy_publish(
            NTFY_REGISTRY_TOPIC,
            {"kind":"admin_login_decision","secret":NTFY_REGISTRY_SECRET,
             "request_id":request_id,"device_id":device_id,"approved":bool(approved)},
            title="SolicoEnergy admin decision",
        )
    except Exception:
        log.exception("admin decision cloud sync failed")

    req["status"]="approved" if approved else "rejected"
    req["decided_at"]=time.strftime("%Y-%m-%d %H:%M:%S")
    _save_login_requests(rows)

    if approved:
        # Approval also re-enables the phone. This prevents an old disabled
        # state from blocking notifications after a successful login.
        try:
            items=_load_devices()
            found=False
            for d in items:
                if str(d.get("device_id"))==device_id:
                    d["enabled"]=True
                    found=True
                    break
            if found:
                _save_devices(items)
            _send_device_access_control(device_id,True)
        except Exception:
            log.exception("could not auto-enable approved device")

        # Archive backfill now requires a separate, explicit phone consent.

    return dict(req)


def _safe_history_sync(device_id,request_id):
    try:
        count=sync_existing_reports_to_device(device_id,request_id)
        _write_status(
            "history_sync",
            f"{count} گزارش قبلی برای گوشی تأییدشده همگام‌سازی شد.",
            device_id=device_id,
            history_count=count,
        )
    except Exception as ex:
        log.exception("historical mobile sync failed")
        _write_status(
            "history_sync_error",
            "همگام‌سازی گزارش‌های قبلی ناموفق بود: "+str(ex),
            device_id=device_id,
        )


def _send_device_access_control(device_id, enabled):
    msg={
        "topic":"solico_device_"+str(device_id),
        "data":{
            "action":"device_access",
            "target_device_id":str(device_id),
            "enabled":"1" if enabled else "0",
            "event_id":uuid.uuid4().hex,
        },
        "android":{"priority":"high"},
    }
    return _fcm_send_message(msg)


def set_mobile_device_enabled(device_id, enabled):
    device_id=str(device_id or "").strip()
    if not device_id:
        return False

    items=_load_devices()
    found=False
    for d in items:
        if str(d.get("device_id"))==device_id:
            d["enabled"]=bool(enabled)
            found=True
            break

    if not found:
        return False

    _save_devices(items)

    # Remote command is broadcast through FCM; only the matching device applies it.
    try:
        _send_device_access_control(device_id,bool(enabled))
    except Exception as ex:
        raise RuntimeError(
            "وضعیت دستگاه در ویندوز ذخیره شد، ولی فرمان به گوشی ارسال نشد:\\n"+str(ex)
        )
    return True


def active_mobile_devices():
    """Return active devices quickly for report delivery.

    R26 deliberately avoids a full ntfy registry poll in the critical path. In
    filtered networks that poll could consume tens of seconds per attempt and was
    the reason Android notifications arrived minutes late. Existing Android tokens
    come from the local device cache; iPhone Web Push subscriptions get one short
    Firebase refresh so a just-repaired subscription is picked up before sending.
    """
    items=_dedupe_legacy_devices(_load_devices())
    by_id={str(d.get("device_id")):dict(d) for d in items if d.get("device_id")}
    try:
        before_pwa=sum(1 for d in by_id.values() if str(d.get("id_scheme") or "")=="web_pwa_v1")
        public_merged=_merge_public_pwa_registry(by_id, timeout=5)
        event_merged=0
        merged=0
        if not public_merged:
            event_merged=_merge_firebase_pwa_registration_events(by_id, timeout=4)
            merged=_merge_firebase_pwa_registry(by_id, timeout=4)
        items=_dedupe_legacy_devices(list(by_id.values()))
        # Never drop a previously known iPhone merely because the current cloud
        # refresh returned zero. by_id started from the local cache, so this save
        # preserves the last known Web Push subscription during transient outages.
        _save_devices(items)
        after_pwa=sum(1 for d in items if str(d.get("id_scheme") or "")=="web_pwa_v1")
        log.info("quick PWA registry refresh public=%s registry=%s events=%s cached_before=%s cached_after=%s",public_merged,merged,event_merged,before_pwa,after_pwa)
    except Exception:
        log.exception("quick Firebase PWA refresh before send failed; cached iPhone rows preserved")
    return [d for d in items if bool(d.get("enabled",True)) and not _is_device_tombstoned(d.get("device_id"))]

def _firebase_db_url_cache_path():
    return data_dir() / "pwa_registry_db_url.txt"


def _firebase_db_urls():
    """Return every usable RTDB endpoint, with last-known-good first.

    R27-R29 accidentally truncated the candidate list to two URLs. If the actual
    Firebase instance was on another regional endpoint, Windows could not see the
    iPhone registry and could not mirror the newest report. R30 never truncates the
    list; the successful endpoint is cached and becomes first on later calls.
    """
    urls=[]

    # Last-known-good first: fastest path after the first successful call.
    try:
        p=_firebase_db_url_cache_path()
        if p.exists():
            u=p.read_text(encoding="utf-8").strip().rstrip("/")
            if u and u not in urls:
                urls.append(u)
    except Exception:
        pass

    try:
        raw=_http_text_get(FIREBASE_DB_CONFIG_URL+"?_="+str(int(time.time()*1000)),timeout=6,purpose="PWA registry DB config")
        cfg=json.loads(raw or "{}")
        vals=[]
        if isinstance(cfg,dict):
            if cfg.get("databaseUrl"):
                vals.append(cfg.get("databaseUrl"))
            if isinstance(cfg.get("databaseUrls"),list):
                vals.extend(cfg.get("databaseUrls"))
        for u in vals:
            u=str(u or "").strip().rstrip("/")
            if u and u not in urls:
                urls.append(u)
    except Exception as ex:
        log.warning("PWA registry DB config fetch failed: %s",ex)

    for u in [FIREBASE_DB_URL]+list(FIREBASE_DB_FALLBACKS):
        u=str(u or "").strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)
    return urls


def _public_rtdb_request(method, path="", payload=None, timeout=8, query=None):
    """Use the PWA's rule-gated RTDB bridge without a Google OAuth token.

    This path intentionally exists for the iPhone/PWA control plane.  It removes the
    fragile dependency on the Windows service-account/IAM path for device discovery,
    registration ACKs and the newest-report snapshot.  Security is enforced by the
    RTDB rules using the same application secret already embedded in the installed PWA.
    """
    data=None
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    errors=[]
    for base in _firebase_db_urls():
        url=base.rstrip("/") + "/" + str(path or "").lstrip("/")
        if not url.endswith(".json"):
            url += ".json"
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        req=urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type":"application/json; charset=utf-8","Accept":"application/json","Cache-Control":"no-cache"},
            method=method,
        )
        try:
            result=_urllib_json_request(
                req, timeout=max(3,min(int(timeout or 8),6)),
                fallback_url=url, fallback_method=method, fallback_body=data,
                fallback_content_type="application/json; charset=utf-8",
                fallback_headers={"Content-Type":"application/json; charset=utf-8","Accept":"application/json","Cache-Control":"no-cache"},
                purpose="Firebase PWA public bridge",
            )
            try:_firebase_db_url_cache_path().write_text(base,encoding="utf-8")
            except Exception:pass
            return result
        except Exception as ex:
            errors.append(f"{base}: {ex}")
    raise RuntimeError("Firebase PWA public bridge unavailable: "+" | ".join(errors)[-1800:])


def _merge_public_pwa_registry(by_id, timeout=7):
    """Merge iPhone registrations through the rule-gated public bridge.

    The read is only permitted by database.rules.json when the request is filtered by
    the application secret.  This is the primary iPhone discovery path in R32.
    """
    try:
        raw=_public_rtdb_request(
            "GET","pwa_device_registry",timeout=timeout,
            query={"orderBy":json.dumps("secret"),"equalTo":json.dumps(NTFY_REGISTRY_SECRET)},
        ) or {}
    except Exception as ex:
        log.warning("public PWA registry refresh failed: %s",ex)
        return 0
    if not isinstance(raw,dict):
        return 0
    merged=0
    for key,payload in raw.items():
        if not isinstance(payload,dict):
            continue
        if str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
            continue
        did=str(payload.get("device_id") or key or "").strip()
        if not did:
            continue
        updated=payload.get("updated_at_ms") or payload.get("created_at_ms") or time.time()*1000
        try:
            ts=float(updated)
            if ts>100000000000:ts/=1000.0
            seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(ts))
        except Exception:
            seen=time.strftime("%Y-%m-%d %H:%M:%S")
        if _merge_pwa_payload(by_id,payload,did,seen,backend="firebase_public"):
            merged+=1
            try:_write_pwa_registration_receipt(did,payload)
            except Exception:log.exception("public PWA receipt failed for %s",did)
    return merged


def _db_request(method, path="", payload=None, timeout=12):
    token=_access_token()
    data=None
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8")

    errors=[]
    for base in _firebase_db_urls():
        url=base.rstrip("/") + "/" + str(path or "").lstrip("/")
        if not url.endswith(".json"):
            url += ".json"

        def make_req(tok):
            return urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization":"Bearer "+tok,
                    "Content-Type":"application/json; charset=utf-8",
                    "Accept":"application/json",
                },
                method=method,
            )

        def run(tok):
            req=make_req(tok)
            per_timeout=max(3,min(int(timeout or 12),4))
            return _urllib_json_request(
                req, timeout=per_timeout,
                fallback_url=url, fallback_method=method, fallback_body=data,
                fallback_content_type="application/json; charset=utf-8",
                fallback_headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json; charset=utf-8","Accept":"application/json"},
                purpose="Firebase Realtime Database registry",
            )

        try:
            result=run(token)
            try:
                _firebase_db_url_cache_path().write_text(base,encoding="utf-8")
            except Exception:
                pass
            return result
        except urllib.error.HTTPError as ex:
            try:
                detail=ex.read().decode("utf-8",errors="replace")[:500]
            except Exception:
                detail=str(ex)
            if ex.code in (401,403):
                try:
                    token=_access_token(force=True)
                    result=run(token)
                    try:
                        _firebase_db_url_cache_path().write_text(base,encoding="utf-8")
                    except Exception:
                        pass
                    return result
                except Exception as retry_ex:
                    errors.append(f"{base}: auth retry failed: {retry_ex}")
                    continue
            errors.append(f"{base}: HTTP {ex.code}: {detail}")
        except Exception as ex:
            errors.append(f"{base}: {ex}")

    raise RuntimeError("Firebase PWA registry unavailable on all known URLs: "+" | ".join(errors)[-1800:])


def _format_cloud_time(value):
    try:
        ms=float(value)
        if ms>100000000000:
            ms/=1000.0
        return time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(ms))
    except Exception:
        return str(value or "")


def _merge_public_pwa_registration_events(by_id, timeout=5):
    """Read append-only Safari registration beacons through the rule-gated bridge.

    This is intentionally independent of Google OAuth and ntfy.  Safari can send these
    beacons with a simple no-CORS POST even when a normal cross-origin PUT/preflight is
    flaky.  Windows only reads rows filtered by the application secret.
    """
    try:
        raw=_public_rtdb_request(
            "GET","pwa_registration_events",timeout=timeout,
            query={"orderBy":json.dumps("secret"),"equalTo":json.dumps(NTFY_REGISTRY_SECRET)},
        ) or {}
    except Exception as ex:
        log.warning("public PWA registration-event refresh failed: %s",ex)
        return 0
    if not isinstance(raw,dict):
        return 0
    cutoff=time.time()*1000-14*86400*1000
    latest={}
    for event_id,payload in raw.items():
        if not isinstance(payload,dict) or str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
            continue
        if str(payload.get("kind") or "")!="device_register":
            continue
        did=str(payload.get("device_id") or "").strip()
        if not did:
            continue
        try: created=float(payload.get("created_at_ms") or payload.get("_web_sent_at") or 0)
        except Exception: created=0
        if created and created<cutoff:
            continue
        cur=latest.get(did)
        if cur is None or created>=cur[0]: latest[did]=(created,payload,event_id)
    merged=0
    for did,(created,payload,event_id) in latest.items():
        seen=None
        if created:
            try: seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(created/1000.0))
            except Exception: pass
        if _merge_pwa_payload(by_id,payload,did,seen,backend="firebase_public_event"):
            merged+=1
            try:_write_pwa_registration_receipt(did,payload)
            except Exception:log.exception("public event receipt failed for %s",did)
    return merged


def _merge_firebase_pwa_registry(by_id, timeout=15):
    """Merge iPhone/iPad PWA registrations from Firebase RTDB into the device table.

    ntfy remains as a compatibility/fallback transport, but Web/PWA registration is
    intentionally mirrored to RTDB because browser POST/webhook delivery can be
    unreliable on iOS and public ntfy can rate-limit busy installations.
    """
    try:
        raw=_db_request("GET","pwa_device_registry",timeout=timeout) or {}
    except Exception as ex:
        log.warning("Firebase PWA registry refresh failed: %s", ex)
        return 0
    if not isinstance(raw,dict):
        return 0

    merged=0
    for key,payload in raw.items():
        if not isinstance(payload,dict):
            continue
        if str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
            continue
        device_id=str(payload.get("device_id") or key or "").strip()
        if not device_id:
            continue

        updated=payload.get("updated_at_ms") or payload.get("updated_at") or time.time()*1000
        try:
            ts=float(updated)
            if ts>100000000000:
                ts/=1000.0
            seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(ts))
        except Exception:
            seen=time.strftime("%Y-%m-%d %H:%M:%S")

        if _is_device_tombstoned(device_id):
            tomb=_load_json_dict(_device_tombstones_path()).get(device_id) or {}
            old_session=str(tomb.get("login_session_id") or "")
            new_session=str(payload.get("login_session_id") or "")
            if not new_session or new_session==old_session:
                continue
            _allow_device_reconnect(device_id)

        was_known=device_id in by_id
        prev=by_id.get(device_id,{})
        by_id[device_id]={
            "device_id":device_id,
            "id_scheme":str(payload.get("id_scheme") or prev.get("id_scheme") or "web_pwa_v1"),
            "name":str(payload.get("name") or prev.get("name") or "iPhone WebApp"),
            "app_version":str(payload.get("app_version") or prev.get("app_version") or ""),
            "android":str(payload.get("android") or prev.get("android") or "iOS Web"),
            "last_seen":seen,
            "first_seen":prev.get("first_seen") or seen,
            "enabled":bool(prev.get("enabled",True)),
            "phone_ip":str(payload.get("local_ip") or prev.get("phone_ip") or ""),
            "ip":"Internet / Firebase",
            "cloud":True,
            "owner_name":str(payload.get("owner_name") or prev.get("owner_name") or ""),
            "fcm_token":str(payload.get("fcm_token") or prev.get("fcm_token") or ""),
            "webpush_subscription":payload.get("webpush_subscription") or prev.get("webpush_subscription") or None,
            "login_session_id":str(payload.get("login_session_id") or prev.get("login_session_id") or ""),
            "platform":"ios_web",
            "registry_backend":"firebase_rtdb",
        }
        if not was_known:
            try:
                mark_device_archive_needed(device_id,"new")
            except Exception:
                log.exception("marking new PWA archive-needed failed for %s",device_id)
        # A verified deterministic Firebase registration is a real Windows-visible
        # registration too. Earlier builds only acknowledged append-only beacons,
        # leaving the iPhone UI permanently in an optimistic/ambiguous state.
        _write_pwa_registration_receipt(device_id,payload)
        merged+=1
    return merged


def _merge_pwa_payload(by_id, payload, device_id=None, seen=None, backend="firebase_event"):
    if not isinstance(payload,dict):
        return False
    if str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
        return False
    did=str(payload.get("device_id") or device_id or "").strip()
    if not did:
        return False
    if _is_device_tombstoned(did):
        tomb=_load_json_dict(_device_tombstones_path()).get(did) or {}
        old_session=str(tomb.get("login_session_id") or "")
        new_session=str(payload.get("login_session_id") or "")
        if not new_session or new_session==old_session:
            return False
        _allow_device_reconnect(did)
    if not seen:
        updated=payload.get("created_at_ms") or payload.get("updated_at_ms") or time.time()*1000
        try:
            ts=float(updated)
            if ts>100000000000: ts/=1000.0
            seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(ts))
        except Exception:
            seen=time.strftime("%Y-%m-%d %H:%M:%S")
    prev=by_id.get(did,{})
    was_known=did in by_id
    by_id[did]={
        "device_id":did,
        "id_scheme":str(payload.get("id_scheme") or prev.get("id_scheme") or "web_pwa_v1"),
        "name":str(payload.get("name") or prev.get("name") or "iPhone WebApp"),
        "app_version":str(payload.get("app_version") or prev.get("app_version") or ""),
        "android":str(payload.get("android") or prev.get("android") or "iOS Web"),
        "last_seen":seen,
        "first_seen":prev.get("first_seen") or seen,
        "enabled":bool(prev.get("enabled",True)),
        "phone_ip":str(payload.get("local_ip") or prev.get("phone_ip") or ""),
        "ip":"Internet / "+("Firebase" if "firebase" in backend else "ntfy"),
        "cloud":True,
        "owner_name":str(payload.get("owner_name") or prev.get("owner_name") or ""),
        "fcm_token":str(payload.get("fcm_token") or prev.get("fcm_token") or ""),
        "webpush_subscription":payload.get("webpush_subscription") or prev.get("webpush_subscription") or None,
        "push_permission":str(payload.get("push_permission") or prev.get("push_permission") or ""),
        "push_key_id":str(payload.get("push_key_id") or prev.get("push_key_id") or ""),
        "push_subscription_updated_at_ms":payload.get("push_subscription_updated_at_ms") or prev.get("push_subscription_updated_at_ms") or 0,
        "login_session_id":str(payload.get("login_session_id") or prev.get("login_session_id") or ""),
        "platform":"ios_web",
        "registry_backend":backend,
    }
    if not was_known:
        try: mark_device_archive_needed(did,"new")
        except Exception: log.exception("marking new PWA archive-needed failed for %s",did)
    return True


def _latest_existing_daily_payload():
    """Build the newest stored daily report as a mobile payload, or None."""
    try:
        from . import db
        with db.connect() as c:
            row=c.execute("SELECT report_type,period_key,path,created_at FROM generated_reports WHERE report_type='daily' ORDER BY period_key DESC,created_at DESC,id DESC LIMIT 1").fetchone()
        if not row:
            return None
        payload=_history_payload_for_report(dict(row))
        if not isinstance(payload,dict) or not payload.get("report_date"):
            return None
        payload=dict(payload)
        payload["event_id"]="latest-existing-daily-"+str(payload.get("report_date") or payload.get("report_date_iso") or time.time())
        payload["title"]=str(payload.get("report_title") or payload.get("title") or "گزارش روزانه")
        payload["body"]="آخرین گزارش روزانه ذخیره‌شده در ویندوز همگام‌سازی شد."
        return payload
    except Exception as ex:
        log.warning("building latest existing daily payload failed: %s",ex)
        return None


def _send_pwa_registration_ack_ntfy(device_id, payload=None, latest_payload=None):
    """Acknowledge iPhone registration over ntfy, independent of Firebase access."""
    did=str(device_id or "").strip()
    if not did:
        return False
    payload=payload or {}
    sub=payload.get("webpush_subscription")
    if isinstance(sub,str):
        try: sub=json.loads(sub)
        except Exception: sub={}
    latest_payload=latest_payload if isinstance(latest_payload,dict) else _latest_existing_daily_payload()
    msg={
        "kind":"registration_ack",
        "secret":NTFY_REGISTRY_SECRET,
        "device_id":did,
        "login_session_id":str(payload.get("login_session_id") or ""),
        "ack_at_ms":int(time.time()*1000),
        "push_registered":bool(isinstance(sub,dict) and str(sub.get("endpoint") or "").strip()),
        "latest_report_date":str((latest_payload or {}).get("report_date") or ""),
        "windows_instance_id":windows_instance_id(),
    }
    _ntfy_publish(_control_topic(did),msg,title="SolicoEnergy iPhone registration")
    return True


def _send_latest_existing_to_pwa_ntfy(device_id):
    did=str(device_id or "").strip()
    payload=_latest_existing_daily_payload()
    if not did or not payload:
        return False
    msg=dict(payload)
    msg.update({"kind":"report_notification","secret":NTFY_REGISTRY_SECRET,"device_id":did,"created_at_ms":int(time.time()*1000),"transport":"ntfy_latest_sync"})
    _ntfy_publish(_control_topic(did),msg,title="SolicoEnergy latest report")
    return True


def _write_pwa_registration_receipt(device_id, payload=None):
    did=str(device_id or "").strip()
    if not did:
        return False
    payload=payload or {}
    sub=payload.get("webpush_subscription")
    if isinstance(sub,str):
        try: sub=json.loads(sub)
        except Exception: sub={}
    push_registered=bool(isinstance(sub,dict) and str(sub.get("endpoint") or "").strip())
    latest=_latest_existing_daily_payload()
    body={
        "device_id":did,
        "secret":NTFY_REGISTRY_SECRET,
        "ack_at_ms":int(time.time()*1000),
        "windows_instance_id":windows_instance_id(),
        "app_version":str(payload.get("app_version") or ""),
        "login_session_id":str(payload.get("login_session_id") or ""),
        "push_registered":push_registered,
        "push_key_id":str(payload.get("push_key_id") or ""),
        "push_subscription_updated_at_ms":payload.get("push_subscription_updated_at_ms") or 0,
        "latest_report_date":str((latest or {}).get("report_date") or ""),
    }
    fb_ok=False
    try:
        _public_rtdb_request("PUT",f"pwa_registration_receipts/{did}",body,timeout=6)
        fb_ok=True
    except Exception as public_ex:
        log.warning("PWA registration receipt public bridge failed for %s: %s",did,public_ex)
        try:
            _db_request("PUT",f"pwa_registration_receipts/{did}",body,timeout=6)
            fb_ok=True
        except Exception as ex:
            log.warning("PWA registration receipt authenticated write failed for %s: %s",did,ex)
    # R32: ACK through public RTDB first, with ntfy as independent fallback. Android devices already prove this transport is
    # reachable from the plant Windows installation, so iPhone registration no longer
    # depends on Windows being able to reach Realtime Database.
    ntfy_ok=False
    try:
        _send_pwa_registration_ack_ntfy(did,payload,latest)
        ntfy_ok=True
    except Exception as ex:
        log.warning("PWA registration ntfy ACK failed for %s: %s",did,ex)
    return fb_ok or ntfy_ok


def _merge_firebase_pwa_registration_events(by_id, timeout=8):
    """Merge append-only iPhone registration beacons and acknowledge them.\n\n    The browser posts these with a simple no-CORS request, avoiding Safari/VPN\n    preflight failures. Only the newest event per device from the last 14 days is used.\n    """
    try:
        raw=_db_request("GET","pwa_registration_events",timeout=timeout) or {}
    except Exception as ex:
        log.warning("Firebase PWA registration-event refresh failed: %s",ex)
        return 0
    if not isinstance(raw,dict):
        return 0
    cutoff=time.time()*1000-14*86400*1000
    latest={}
    for event_id,payload in raw.items():
        if not isinstance(payload,dict) or str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
            continue
        if str(payload.get("kind") or "")!="device_register":
            continue
        did=str(payload.get("device_id") or "").strip()
        if not did:
            continue
        try: created=float(payload.get("created_at_ms") or payload.get("_web_sent_at") or 0)
        except Exception: created=0
        if created and created<cutoff:
            continue
        cur=latest.get(did)
        if cur is None or created>=cur[0]:
            latest[did]=(created,payload,event_id)
    merged=0
    for did,(created,payload,event_id) in latest.items():
        seen=None
        if created:
            try: seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(created/1000.0))
            except Exception: seen=None
        if _merge_pwa_payload(by_id,payload,did,seen,backend="firebase_event"):
            merged+=1
            _write_pwa_registration_receipt(did,payload)
    return merged


def _pwa_latest_broadcast_key():
    raw=(NTFY_REGISTRY_SECRET+"|latest-broadcast").encode("utf-8")
    return "latest-"+hashlib.sha256(raw).hexdigest()[:24]


def _mirror_pwa_latest_broadcast(payload):
    """Persist one newest report for all iPhone PWAs, independent of registration."""
    body=dict(payload or {})
    body.update({
        "kind":"report_notification",
        "secret":NTFY_REGISTRY_SECRET,
        "created_at_ms":int(time.time()*1000),
        "transport":"firebase_latest_broadcast",
    })
    body.pop("device_id",None)
    try:
        _public_rtdb_request("PUT",f"pwa_latest_broadcast/{_pwa_latest_broadcast_key()}",body,timeout=6)
        return True
    except Exception as public_ex:
        log.warning("public latest-report bridge failed, trying authenticated RTDB: %s",public_ex)
        _db_request("PUT",f"pwa_latest_broadcast/{_pwa_latest_broadcast_key()}",body,timeout=6)
        return True


def _mirror_latest_existing_daily_report():
    """Republish the newest stored daily report to the durable Firebase broadcast."""
    payload=_latest_existing_daily_payload()
    if not payload:
        return False
    try:
        _mirror_pwa_latest_broadcast(payload)
        return True
    except Exception as ex:
        log.warning("mirroring latest existing daily report failed: %s",ex)
        return False


def _dedupe_legacy_devices(rows):
    stable=[]
    legacy=[]
    for d in rows:
        if str(d.get("id_scheme") or "") in ("stable_v2","web_pwa_v1"):
            stable.append(d)
        else:
            legacy.append(d)

    newest={}
    for d in legacy:
        key=(
            str(d.get("name") or "").strip().lower(),
            str(d.get("app_version") or "").strip().lower(),
            str(d.get("android") or "").strip().lower(),
        )
        cur=newest.get(key)
        if cur is None or str(d.get("last_seen") or "") > str(cur.get("last_seen") or ""):
            newest[key]=d

    rows=list(newest.values())+stable

    stable_keys={
        (str(d.get("name") or "").strip().lower(), str(d.get("android") or "").strip().lower())
        for d in stable
    }
    cleaned=[]
    for d in rows:
        key=(str(d.get("name") or "").strip().lower(), str(d.get("android") or "").strip().lower())
        if str(d.get("id_scheme") or "") not in ("stable_v2","web_pwa_v1") and key in stable_keys:
            continue
        cleaned.append(d)

    return sorted(cleaned,key=lambda x:str(x.get("last_seen") or ""),reverse=True)


def sync_cloud_devices():
    # R32: rule-gated public RTDB bridge + hybrid iPhone core: ntfy registration/ACK plus Firebase mirror. Android still
    # uses ntfy normally, but a slow/filtered ntfy route can no longer hide an iPhone
    # that is already registered in Realtime Database.
    try:
        pre_rows=_dedupe_legacy_devices(_load_devices())
        pre_by_id={str(d.get("device_id")):dict(d) for d in pre_rows if d.get("device_id")}
        pre_public_count=_merge_public_pwa_registry(pre_by_id, timeout=4)
        pre_public_event_count=_merge_public_pwa_registration_events(pre_by_id, timeout=4)
        pre_event_count=0
        pre_fb_count=0
        if not (pre_public_count or pre_public_event_count):
            pre_event_count=_merge_firebase_pwa_registration_events(pre_by_id, timeout=4)
            pre_fb_count=_merge_firebase_pwa_registry(pre_by_id, timeout=4)
        if pre_public_count or pre_public_event_count or pre_event_count or pre_fb_count:
            _save_devices(_dedupe_legacy_devices([d for d in pre_by_id.values() if bool(d.get("enabled",True))]))
            log.info("PWA registry merge public=%s public_events=%s auth_events=%s registry=%s",pre_public_count,pre_public_event_count,pre_event_count,pre_fb_count)
    except Exception:
        log.exception("Firebase-first PWA registry merge failed; continuing with cached/ntfy devices")

    state={}
    try:
        if _ntfy_state_path().exists():
            state=json.loads(_ntfy_state_path().read_text(encoding="utf-8"))
    except Exception:
        state={}

    cursor=str(state.get("last_id") or "all")
    url=(
        NTFY_BASE.rstrip("/")+"/"+NTFY_REGISTRY_TOPIC+
        "/json?poll=1&since="+urllib.parse.quote(cursor,safe="")
    )

    try:
        raw=_http_text_get(url,timeout=8,purpose="ntfy device registry refresh")
    except Exception as ex:
        # The saved ntfy cursor can occasionally become stale/invalid after older
        # builds. Before falling back to cached devices, retry from retained events.
        try:
            recovery_url=(NTFY_BASE.rstrip("/")+"/"+NTFY_REGISTRY_TOPIC+"/json?poll=1&since=all")
            raw=_http_text_get(recovery_url,timeout=10,purpose="ntfy full retained registry recovery")
            state={}
            cursor="all"
            log.warning("normal ntfy registry refresh failed (%s); full retained recovery succeeded",ex)
        except Exception as recovery_ex:
            rows=_dedupe_legacy_devices(_load_devices())
            by_id={str(d.get("device_id")):dict(d) for d in rows if d.get("device_id")}
            public_count=_merge_public_pwa_registry(by_id, timeout=4)
            public_event_count=_merge_public_pwa_registration_events(by_id, timeout=4)
            event_count=0
            fb_count=0
            if not (public_count or public_event_count):
                event_count=_merge_firebase_pwa_registration_events(by_id, timeout=4)
                fb_count=_merge_firebase_pwa_registry(by_id, timeout=4)
            rows=_dedupe_legacy_devices([d for d in by_id.values() if bool(d.get("enabled",True))])
            _save_devices(rows)
            if rows or fb_count:
                _write_status(
                    "registry_partial",
                    f"ntfy در دسترس نبود؛ لیست ذخیره‌شده + Firebase PWA نمایش داده شد • iPhone Web: {public_count+public_event_count+fb_count+event_count}. خطا: {str(recovery_ex)[:150]}",
                    device_count=len(rows), ios_web_count=public_count+public_event_count+fb_count+event_count,
                )
                return rows
            raise RuntimeError("دریافت دستگاه‌ها از ntfy و Firebase انجام نشد: "+str(recovery_ex))

    # Always recover very recent registration/reconnect events. A previous build can
    # legitimately advance the ntfy cursor before understanding a newer device scheme
    # (such as iPhone Web/PWA), which would otherwise make that phone invisible forever.
    # Only registration events are replayed here, so archive decisions are not duplicated.
    try:
        catch_url=(NTFY_BASE.rstrip("/")+"/"+NTFY_REGISTRY_TOPIC+"/json?poll=1&since=12h")
        catch_raw=_http_text_get(catch_url,timeout=8,purpose="ntfy recent 12h registration recovery")
        recovered=[]
        for catch_line in catch_raw.splitlines():
            try:
                catch_event=json.loads(catch_line)
                if catch_event.get("event")!="message":
                    continue
                catch_payload=json.loads(str(catch_event.get("message") or ""))
                if str(catch_payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
                    continue
                if str(catch_payload.get("kind") or "") not in ("device_register","device_reconnect","pwa_sync_request"):
                    continue
                recovered.append(catch_line)
            except Exception:
                continue
        if recovered:
            # Prepend recovery lines so a newer cursor from the normal poll remains the
            # final cursor written below. device_id dedupe makes replay idempotent.
            raw="\n".join(recovered)+("\n"+raw if raw.strip() else "")
    except Exception:
        log.exception("ntfy recent registration recovery failed")

    items=_dedupe_legacy_devices(_load_devices())
    by_id={str(d.get("device_id")):dict(d) for d in items if d.get("device_id")}
    latest_id=str(state.get("last_id") or "")

    for line in raw.splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            event=json.loads(line)
        except Exception:
            continue

        event_id=str(event.get("id") or "")
        if event_id:
            latest_id=event_id

        if event.get("event")!="message":
            continue

        try:
            payload=json.loads(str(event.get("message") or ""))
        except Exception:
            continue

        if str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET:
            continue

        # Build the event timestamp BEFORE branching by kind.
        # FIX13 referenced "seen" inside login_request before it was assigned,
        # so Windows silently skipped login requests.
        seen_ts=event.get("time")
        try:
            seen=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(float(seen_ts)))
        except Exception:
            seen=time.strftime("%Y-%m-%d %H:%M:%S")

        kind=str(payload.get("kind") or "")
        if kind in ("archive_decision", "archive_receipt", "history_accept", "history_request", "history_reject"):
            from .archive_transfer import handle_event
            handle_event(payload)
            continue

        if kind=="login_request":
            _record_login_request(payload,seen)
            continue

        if kind=="device_delete":
            did=str(payload.get("device_id") or "").strip()
            if did:
                previous=by_id.get(did,{})
                _mark_device_deleted(did,deleted_at=payload.get("deleted_at") or event.get("time") or time.time(),
                                     login_session_id=previous.get("login_session_id") or "")
                by_id.pop(did,None)
                _save_login_requests([r for r in _load_login_requests() if str(r.get("device_id"))!=did])
            continue

        if kind=="device_reconnect":
            did=str(payload.get("device_id") or "").strip()
            if did:
                _allow_device_reconnect(did)
            continue

        if kind=="device_owner_update":
            did=str(payload.get("device_id") or "").strip()
            if did and did in by_id:
                by_id[did]["owner_name"]=str(payload.get("owner_name") or "").strip()
            continue

        if kind=="admin_login_decision":
            did=str(payload.get("device_id") or "").strip()
            rid=str(payload.get("request_id") or "").strip()
            approved=bool(payload.get("approved"))
            reqs=_load_login_requests()
            changed=False
            for rr in reqs:
                if (rid and str(rr.get("request_id"))==rid) or (did and str(rr.get("device_id"))==did and str(rr.get("status"))=="pending"):
                    rr["status"]="approved" if approved else "rejected"
                    rr["decided_at"]=seen
                    changed=True
            if changed:
                _save_login_requests(reqs)
            if approved and did in by_id:
                by_id[did]["enabled"]=True
            continue

        if kind=="pwa_sync_request":
            did=str(payload.get("device_id") or "").strip()
            if did:
                # Respond on the private ntfy control topic even when Firebase is
                # filtered. This delivers a real Windows ACK plus the newest report.
                try:_send_pwa_registration_ack_ntfy(did,payload)
                except Exception:log.exception("PWA sync-request ACK failed for %s",did)
                try:_send_latest_existing_to_pwa_ntfy(did)
                except Exception:log.exception("PWA latest-report ntfy sync failed for %s",did)
            continue

        if kind!="device_register":
            continue

        device_id=str(payload.get("device_id") or "").strip()
        if not device_id:
            continue
        if _is_device_tombstoned(device_id):
            tomb=_load_json_dict(_device_tombstones_path()).get(device_id) or {}
            old_session=str(tomb.get("login_session_id") or "")
            new_session=str(payload.get("login_session_id") or "")
            if not new_session or new_session==old_session:
                # Do not resurrect the deleted row. Also tell a restored/reinstalled phone
                # that its old local session is invalid, so it must show the Login screen.
                try:
                    _send_device_access_control(device_id,False)
                except Exception:
                    log.exception("stale tombstoned device disable failed for %s",device_id)
                continue
            _allow_device_reconnect(device_id)

        was_known=device_id in by_id
        prev=by_id.get(device_id,{})
        enabled=bool(prev.get("enabled",True))

        local_ip=str(payload.get("local_ip") or "").strip()
        by_id[device_id]={
            "device_id":device_id,
            "id_scheme":str(payload.get("id_scheme") or prev.get("id_scheme") or ""),
            "name":str(payload.get("name") or prev.get("name") or "Android device"),
            "app_version":str(payload.get("app_version") or prev.get("app_version") or ""),
            "android":str(payload.get("android") or prev.get("android") or ""),
            "last_seen":seen,
            "first_seen":prev.get("first_seen") or seen,
            "enabled":enabled,
            "phone_ip":local_ip or str(prev.get("phone_ip") or ""),
            "ip":"Internet / ntfy",
            "cloud":True,
            "owner_name":str(prev.get("owner_name") or ""),
            "fcm_token":str(payload.get("fcm_token") or prev.get("fcm_token") or ""),
            "webpush_subscription":payload.get("webpush_subscription") or prev.get("webpush_subscription") or None,
            "push_permission":str(payload.get("push_permission") or prev.get("push_permission") or ""),
            "push_key_id":str(payload.get("push_key_id") or prev.get("push_key_id") or ""),
            "push_subscription_updated_at_ms":payload.get("push_subscription_updated_at_ms") or prev.get("push_subscription_updated_at_ms") or 0,
            "login_session_id":str(payload.get("login_session_id") or prev.get("login_session_id") or ""),
            "platform":str(payload.get("platform") or prev.get("platform") or ""),
        }
        if not was_known:
            mark_device_archive_needed(device_id,"new")
        if str(payload.get("platform") or "")=="ios_web" or str(payload.get("id_scheme") or "")=="web_pwa_v1":
            try:_write_pwa_registration_receipt(device_id,payload)
            except Exception:log.exception("iPhone registration ACK failed for %s",device_id)
            try:_send_latest_existing_to_pwa_ntfy(device_id)
            except Exception:log.exception("iPhone latest report catch-up failed for %s",device_id)

    public_pwa_count=_merge_public_pwa_registry(by_id, timeout=4)
    public_event_count=_merge_public_pwa_registration_events(by_id, timeout=4)
    firebase_event_count=0
    firebase_pwa_count=0
    if not (public_pwa_count or public_event_count):
        firebase_event_count=_merge_firebase_pwa_registration_events(by_id, timeout=4)
        firebase_pwa_count=_merge_firebase_pwa_registry(by_id, timeout=4)
    rows=_dedupe_legacy_devices([d for d in by_id.values() if bool(d.get("enabled",True))])
    if not firebase_pwa_count:
        cached_ios=sum(1 for d in rows if str(d.get("id_scheme") or "")=="web_pwa_v1" or str(d.get("platform") or "")=="ios_web")
        if cached_ios:
            log.warning("Firebase PWA registry returned 0 rows; preserving %s cached iPhone device(s)",cached_ios)
    _save_devices(rows)

    if latest_id:
        try:
            _ntfy_state_path().write_text(
                json.dumps({"last_id":latest_id},ensure_ascii=False,indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    from .archive_transfer import process_transfers
    process_transfers()
    # Also repair missed latest-report state on every explicit device refresh.
    _mirror_latest_existing_daily_report()
    ios_count=sum(1 for d in rows if str(d.get("platform") or "")=="ios_web")
    _write_status("hybrid_devices",f"{len(rows)} دستگاه ثبت‌شده موجود است"+(f" • iPhone Web: {ios_count}" if ios_count else "")+(f" • Firebase مستقیم: {public_pwa_count}" if public_pwa_count else "")+(f" • Beacon عمومی: {public_event_count}" if public_event_count else "")+(f" • Firebase Auth: {firebase_pwa_count}" if firebase_pwa_count else "")+(f" • Beacon Auth: {firebase_event_count}" if firebase_event_count else ""),device_count=len(rows),ios_web_count=ios_count,firebase_pwa_count=firebase_pwa_count,public_event_count=public_event_count)
    return rows



def _windows_instance_path() -> Path:
    return data_dir() / "windows_instance_id.txt"


def _windows_sync_state_path() -> Path:
    return data_dir() / "windows_report_sync_state.json"


def windows_instance_id():
    p=_windows_instance_path()
    try:
        if p.exists():
            value=p.read_text(encoding="utf-8").strip()
            if value:
                return value
    except Exception:
        pass
    value=uuid.uuid4().hex
    try:
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(value,encoding="utf-8")
    except Exception:
        pass
    return value


def _ntfy_publish_attachment(topic, file_path, payload):
    p=Path(file_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    # ntfy.sh currently accepts uploaded attachments up to 2 MB. Daily Solico
    # PDFs are well below this. Keep a clear error if a future report grows.
    if p.stat().st_size > 2*1024*1024:
        raise RuntimeError("حجم PDF برای همگام‌سازی اینترنتی بیشتر از ۲ مگابایت است.")
    safe_type=str(payload.get("report_type") or "report").replace("/","_")
    safe_period=str(payload.get("period_key") or "").replace("/","-")
    filename=("Solico_"+safe_type+"_"+safe_period+".pdf").encode("ascii","ignore").decode("ascii") or "Solico_Report.pdf"
    headers={
        "Content-Type":"application/pdf",
        "Filename":filename,
        "Title":"SolicoEnergy Windows report sync",
        "Message":json.dumps(payload,ensure_ascii=True,separators=(",",":")),
    }
    req=urllib.request.Request(
        NTFY_BASE.rstrip("/")+"/"+str(topic),
        data=p.read_bytes(),headers=headers,method="PUT",
    )
    with urllib.request.urlopen(req,timeout=45) as r:
        return json.loads(r.read().decode("utf-8",errors="replace") or "{}")


def broadcast_windows_report(report_type, period_key, report_path, target_instance_id=""):
    p=Path(report_path)
    if not p.exists() or p.suffix.lower()!=".pdf":
        return False
    payload={
        "kind":"windows_report",
        "secret":NTFY_REGISTRY_SECRET,
        "source_instance_id":windows_instance_id(),
        "target_instance_id":str(target_instance_id or ""),
        "report_type":str(report_type or ""),
        "period_key":str(period_key or ""),
        "original_name":p.name,
        "created_at":time.strftime("%Y-%m-%d %H:%M:%S"),
        "event_id":uuid.uuid4().hex,
    }
    _ntfy_publish_attachment(NTFY_WINDOWS_TOPIC,p,payload)
    _write_status("windows_report_shared","گزارش با نسخه‌های ویندوز دیگر همگام شد.",report_type=report_type,period_key=period_key)
    return True


def broadcast_windows_report_async(report_type, period_key, report_path):
    def _job():
        try:
            broadcast_windows_report(report_type,period_key,report_path)
        except Exception as ex:
            log.exception("Windows report broadcast failed")
            _write_status("windows_report_share_error","همگام‌سازی گزارش ویندوز انجام نشد: "+str(ex))
    threading.Thread(target=_job,name="SolicoWindowsReportBroadcast",daemon=True).start()


def request_windows_report_sync():
    payload={
        "kind":"windows_sync_request",
        "secret":NTFY_REGISTRY_SECRET,
        "source_instance_id":windows_instance_id(),
        "requested_at":time.strftime("%Y-%m-%d %H:%M:%S"),
        "event_id":uuid.uuid4().hex,
    }
    return _ntfy_publish(NTFY_WINDOWS_TOPIC,payload,title="SolicoEnergy Windows sync request")


def _send_mobile_snapshot_to_instance(target_instance_id):
    target_instance_id=str(target_instance_id or "").strip()
    if not target_instance_id:
        return
    try:
        _ntfy_publish(
            NTFY_WINDOWS_TOPIC,
            {"kind":"windows_device_snapshot_reset","secret":NTFY_REGISTRY_SECRET,
             "source_instance_id":windows_instance_id(),"target_instance_id":target_instance_id},
            title="SolicoEnergy device snapshot",
        )
        reqs=_load_login_requests()
        latest={}
        for rr in reqs:
            did=str(rr.get("device_id") or "")
            if not did: continue
            old=latest.get(did)
            if old is None or str(rr.get("requested_at") or "")>=str(old.get("requested_at") or ""):
                latest[did]=rr
        for d in _load_devices():
            if not bool(d.get("enabled",True)):
                continue
            did=str(d.get("device_id") or "")
            if not did: continue
            rr=latest.get(did,{})
            payload={
                "kind":"windows_device_state","secret":NTFY_REGISTRY_SECRET,
                "source_instance_id":windows_instance_id(),"target_instance_id":target_instance_id,
                "device_id":did,"owner_name":str(d.get("owner_name") or ""),
                "name":str(d.get("name") or "Android device"),"app_version":str(d.get("app_version") or ""),
                "android":str(d.get("android") or ""),"last_seen":str(d.get("last_seen") or ""),
                "phone_ip":str(d.get("phone_ip") or ""),
                "request_id":str(rr.get("request_id") or ""),"username":str(rr.get("username") or ""),
                "request_status":str(rr.get("status") or ""),"requested_at":str(rr.get("requested_at") or ""),
            }
            _ntfy_publish(NTFY_WINDOWS_TOPIC,payload,title="SolicoEnergy device state")
            time.sleep(0.08)
    except Exception:
        log.exception("Windows mobile snapshot send failed")


def _send_existing_reports_to_instance(target_instance_id):
    target_instance_id=str(target_instance_id or "").strip()
    if not target_instance_id or target_instance_id==windows_instance_id():
        return 0
    now=time.time()
    if now-float(_windows_history_sent.get(target_instance_id,0) or 0) < 90:
        return 0
    _windows_history_sent[target_instance_id]=now
    _send_mobile_snapshot_to_instance(target_instance_id)
    from . import db as _db
    with _db.connect() as con:
        rows=[dict(r) for r in con.execute('SELECT report_type,period_key,path,created_at FROM generated_reports ORDER BY created_at,id')]
    sent=0
    for row in rows:
        p=Path(str(row.get("path") or ""))
        if not p.exists() or p.suffix.lower()!=".pdf":
            continue
        try:
            broadcast_windows_report(row.get("report_type"),row.get("period_key"),str(p),target_instance_id=target_instance_id)
            sent+=1
            time.sleep(0.35)
        except Exception:
            log.exception("history report upload failed for Windows peer")
    return sent


def _download_windows_report(event,payload):
    target_id=str(payload.get("target_instance_id") or "").strip()
    me=windows_instance_id()
    if target_id and target_id!=me:
        return False
    if str(payload.get("source_instance_id") or "")==me:
        return False
    rtype=str(payload.get("report_type") or "").strip()
    period=str(payload.get("period_key") or "").strip()
    if not rtype or not period:
        return False
    attachment=event.get("attachment") or {}
    url=str(attachment.get("url") or "").strip()
    if not url:
        return False
    from . import db as _db
    existing=_db.report_exists(rtype,period)
    if existing and Path(str(existing.get("path") or "")).exists():
        return False
    original=Path(str(payload.get("original_name") or "")).name
    if not original.lower().endswith(".pdf"):
        original=("Solico_"+rtype+"_"+period+".pdf").replace("/","-")
    out_dir=settings_mod.ensure_output_dir()
    out_dir.mkdir(parents=True,exist_ok=True)
    target=out_dir/original
    req=urllib.request.Request(url,method="GET")
    with urllib.request.urlopen(req,timeout=45) as r:
        data=r.read(3*1024*1024)
    if len(data)<100 or not data.startswith(b"%PDF"):
        raise RuntimeError("فایل PDF همگام‌شده معتبر نیست.")
    target.write_bytes(data)
    _db.mark_report(rtype,period,str(target),broadcast=False)
    _write_status("windows_report_received","گزارش از یک نسخه ویندوز دیگر دریافت شد.",report_type=rtype,period_key=period,path=str(target))
    return True


def sync_windows_reports():
    state={}
    try:
        p=_windows_sync_state_path()
        if p.exists(): state=json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        state={}
    cursor=str(state.get("last_id") or "all")
    url=(NTFY_BASE.rstrip("/")+"/"+NTFY_WINDOWS_TOPIC+"/json?poll=1&since="+urllib.parse.quote(cursor,safe=""))
    req=urllib.request.Request(url,method="GET")
    with urllib.request.urlopen(req,timeout=20) as r:
        raw=r.read().decode("utf-8",errors="replace")
    latest_id=str(state.get("last_id") or "")
    received=0
    for line in raw.splitlines():
        try: event=json.loads(line.strip())
        except Exception: continue
        eid=str(event.get("id") or "")
        if eid: latest_id=eid
        if event.get("event")!="message": continue
        try: payload=json.loads(str(event.get("message") or ""))
        except Exception: continue
        if str(payload.get("secret") or "")!=NTFY_REGISTRY_SECRET: continue
        kind=str(payload.get("kind") or "")
        target_id=str(payload.get("target_instance_id") or "").strip()
        if target_id and target_id!=windows_instance_id():
            continue
        if kind=="windows_sync_request":
            requester=str(payload.get("source_instance_id") or "").strip()
            if requester and requester!=windows_instance_id():
                threading.Thread(target=lambda rid=requester:_send_existing_reports_to_instance(rid),name="SolicoWindowsHistorySend",daemon=True).start()
            continue
        if kind=="windows_device_snapshot_reset":
            if str(payload.get("source_instance_id") or "")!=windows_instance_id():
                _save_devices([])
                _save_login_requests([])
            continue
        if kind=="windows_device_state":
            did=str(payload.get("device_id") or "").strip()
            if not did: continue
            items=_load_devices(); found=None
            for d in items:
                if str(d.get("device_id"))==did: found=d; break
            if found is None:
                found={"device_id":did,"first_seen":str(payload.get("last_seen") or time.strftime("%Y-%m-%d %H:%M:%S"))}
                items.append(found)
            found.update({
                "owner_name":str(payload.get("owner_name") or ""),"name":str(payload.get("name") or "Android device"),
                "app_version":str(payload.get("app_version") or ""),"android":str(payload.get("android") or ""),
                "last_seen":str(payload.get("last_seen") or ""),"phone_ip":str(payload.get("phone_ip") or ""),
                "ip":"Internet / ntfy","cloud":True,"enabled":True,
            })
            _save_devices(items)
            rid=str(payload.get("request_id") or "").strip()
            if rid:
                reqs=_load_login_requests(); rr=None
                for x in reqs:
                    if str(x.get("request_id"))==rid: rr=x; break
                if rr is None:
                    rr={"request_id":rid,"device_id":did}; reqs.append(rr)
                rr.update({"device_id":did,"username":str(payload.get("username") or ""),
                           "name":str(payload.get("name") or "Android device"),
                           "app_version":str(payload.get("app_version") or ""),
                           "phone_ip":str(payload.get("phone_ip") or ""),
                           "status":str(payload.get("request_status") or "pending"),
                           "requested_at":str(payload.get("requested_at") or "")})
                _save_login_requests(reqs)
            continue
        if kind=="windows_report":
            try:
                if _download_windows_report(event,payload): received+=1
            except Exception:
                log.exception("Windows report download failed")
    if latest_id:
        try:
            _windows_sync_state_path().write_text(json.dumps({"last_id":latest_id},indent=2),encoding="utf-8")
        except Exception: pass
    return received


def start_windows_report_sync():
    global _windows_sync_thread
    with _windows_sync_lock:
        if _windows_sync_thread and _windows_sync_thread.is_alive():
            return True
        _windows_sync_stop.clear()
        def _loop():
            time.sleep(2.0)
            try: request_windows_report_sync()
            except Exception: log.exception("initial Windows report sync request failed")
            while not _windows_sync_stop.is_set():
                try: sync_windows_reports()
                except Exception: log.exception("Windows report sync poll failed")
                _windows_sync_stop.wait(10.0)
        _windows_sync_thread=threading.Thread(target=_loop,name="SolicoWindowsReportSync",daemon=True)
        _windows_sync_thread.start()
    return True


def service_account_path() -> Path:
    return user_root() / "firebase" / "service-account.json"



def install_service_account(source_path) -> Path:
    """Validate and copy a Firebase service-account JSON to the correct user folder."""
    src = Path(source_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    try:
        obj = json.loads(src.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ValueError(f"فایل JSON کلید Firebase معتبر نیست: {e}") from e

    required = ("project_id", "private_key", "client_email")
    missing = [k for k in required if not str(obj.get(k) or "").strip()]
    if missing:
        raise ValueError("این فایل Service Account معتبر نیست. فیلدهای لازم پیدا نشد: " + ", ".join(missing))

    project_id = str(obj.get("project_id") or "").strip()
    if project_id != "solicoenergy":
        raise ValueError(
            f"این کلید مربوط به پروژه Firebase دیگری است ({project_id}). "
            "کلید پروژه solicoenergy را انتخاب کنید."
        )

    dst = service_account_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(src, dst)
    _write_status("ready", "کلید Firebase نصب شد.", service_account=str(dst))
    return dst

def _status_path() -> Path:
    return data_dir() / "mobile_push_status.json"


def _ntfy_state_path() -> Path:
    return data_dir() / "ntfy_registry_state.json"


def _config_path() -> Path:
    return data_dir() / "mobile_push_config.json"


def _write_status(state: str, message: str = "", **extra):
    obj = {
        "state": state,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "message": str(message or ""),
        **extra,
    }
    try:
        _status_path().write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def read_status():
    try:
        if _status_path().exists():
            obj = json.loads(_status_path().read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _load_or_create_secret() -> str:
    p = _config_path()
    try:
        if p.exists():
            obj = json.loads(p.read_text(encoding="utf-8"))
            secret = str(obj.get("link_secret") or "").strip()
            if len(secret) >= 32:
                return secret
    except Exception:
        pass
    secret = secrets.token_urlsafe(32)
    try:
        p.write_text(
            json.dumps({"link_secret": secret}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return secret


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _read_tlv(data: bytes, pos: int = 0):
    if pos >= len(data):
        raise ValueError("DER پایان غیرمنتظره دارد.")
    tag = data[pos]
    pos += 1
    if pos >= len(data):
        raise ValueError("DER length ناقص است.")
    first = data[pos]
    pos += 1
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4 or pos + n > len(data):
            raise ValueError("DER length نامعتبر است.")
        length = int.from_bytes(data[pos:pos+n], "big")
        pos += n
    end = pos + length
    if end > len(data):
        raise ValueError("DER value ناقص است.")
    return tag, data[pos:end], end


def _seq_items(content: bytes):
    pos = 0
    out = []
    while pos < len(content):
        tag, value, nxt = _read_tlv(content, pos)
        out.append((tag, value))
        pos = nxt
    return out


def _int_from_der(value: bytes) -> int:
    if value and value[0] == 0:
        value = value[1:]
    return int.from_bytes(value, "big", signed=False)


def _parse_rsa_private_key_pem(pem: str):
    lines = [ln.strip() for ln in str(pem).strip().splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Private key خالی است.")
    header = lines[0]
    footer = lines[-1]
    body = "".join(lines[1:-1])
    der = base64.b64decode(body)

    tag, top_content, end = _read_tlv(der, 0)
    if tag != 0x30 or end != len(der):
        raise ValueError("ساختار Private key نامعتبر است.")

    if "BEGIN PRIVATE KEY" in header and "BEGIN RSA PRIVATE KEY" not in header:
        items = _seq_items(top_content)
        if len(items) < 3 or items[2][0] != 0x04:
            raise ValueError("PKCS#8 Private key نامعتبر است.")
        rsa_der = items[2][1]
        tag, rsa_content, rsa_end = _read_tlv(rsa_der, 0)
        if tag != 0x30 or rsa_end != len(rsa_der):
            raise ValueError("RSA Private key داخل PKCS#8 نامعتبر است.")
    elif "BEGIN RSA PRIVATE KEY" in header:
        rsa_content = top_content
    else:
        raise ValueError("فرمت Private key پشتیبانی نمی‌شود.")

    ints = [
        _int_from_der(v)
        for tag, v in _seq_items(rsa_content)
        if tag == 0x02
    ]
    # PKCS#1: version, n, e, d, p, q, dP, dQ, qInv
    if len(ints) < 4:
        raise ValueError("RSA Private key ناقص است.")
    n = ints[1]
    d = ints[3]
    if n <= 0 or d <= 0:
        raise ValueError("RSA key نامعتبر است.")
    return n, d


def _rsa_sha256_sign(private_key_pem: str, message: bytes) -> bytes:
    n, d = _parse_rsa_private_key_pem(private_key_pem)
    digest = hashlib.sha256(message).digest()
    # ASN.1 DigestInfo prefix for SHA-256.
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    k = (n.bit_length() + 7) // 8
    pad_len = k - len(digest_info) - 3
    if pad_len < 8:
        raise ValueError("RSA key برای SHA-256 کوچک است.")
    em = b"\x00\x01" + (b"\xff" * pad_len) + b"\x00" + digest_info
    m = int.from_bytes(em, "big")
    sig = pow(m, d, n)
    return sig.to_bytes(k, "big")


def _service_account():
    p = service_account_path()
    if not p.exists():
        raise FileNotFoundError(
            f"کلید Firebase پیدا نشد:\n{p}\n"
            "فایل Service Account را با نام service-account.json در این مسیر قرار دهید."
        )
    obj = json.loads(p.read_text(encoding="utf-8"))
    for key in ("project_id", "client_email", "private_key"):
        if not str(obj.get(key) or "").strip():
            raise ValueError(f"فیلد {key} در service-account.json وجود ندارد.")
    return obj


def _access_token(force=False) -> str:
    global _cached_token, _cached_token_expiry
    now = time.time()
    with _token_lock:
        if not force and _cached_token and now < (_cached_token_expiry - 90):
            return _cached_token

        sa = _service_account()
        iat = int(now)
        exp = iat + 3600
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": sa["client_email"],
            "scope": FCM_SCOPE,
            "aud": sa.get("token_uri") or TOKEN_URL_DEFAULT,
            "iat": iat,
            "exp": exp,
        }
        unsigned = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        ).encode("ascii")
        signature = _rsa_sha256_sign(sa["private_key"], unsigned)
        assertion = unsigned.decode("ascii") + "." + _b64url(signature)

        form = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode("ascii")
        req = urllib.request.Request(
            sa.get("token_uri") or TOKEN_URL_DEFAULT,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            payload = _urllib_json_request(
                req, timeout=15,
                fallback_url=sa.get("token_uri") or TOKEN_URL_DEFAULT,
                fallback_method="POST", fallback_body=form,
                fallback_content_type="application/x-www-form-urlencoded",
                fallback_headers={"Content-Type":"application/x-www-form-urlencoded"},
                purpose="Firebase OAuth token",
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"دریافت Access Token ناموفق بود: HTTP {e.code}\n{detail}")
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("Firebase Access Token دریافت نشد.")
        expires_in = int(payload.get("expires_in") or 3600)
        _cached_token = token
        _cached_token_expiry = now + expires_in
        return token


def _fcm_send_message(message):
    sa=_service_account(); token=_access_token(); url="https://fcm.googleapis.com/v1/projects/"+urllib.parse.quote(str(sa["project_id"]),safe="")+"/messages:send"
    payload={"message":message}
    body=json.dumps(payload,ensure_ascii=False).encode("utf-8")
    def request(tok):
        return urllib.request.Request(url,data=body,headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json; charset=utf-8"},method="POST")
    def send(tok):
        return _urllib_json_request(
            request(tok), timeout=15, fallback_url=url, fallback_method="POST",
            fallback_body=body, fallback_content_type="application/json; charset=utf-8",
            fallback_headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json; charset=utf-8"},
            purpose="Firebase FCM send",
        )
    try:
        return send(token)
    except urllib.error.HTTPError as ex:
        detail=ex.read().decode("utf-8",errors="replace")[:1200]
        if ex.code==401:
            token=_access_token(force=True)
            return send(token)
        raise RuntimeError(f"ارسال FCM ناموفق بود: HTTP {ex.code}\\n{detail}")



# ---- Standards-based Web Push (RFC 8030 + RFC 8292) -----------------------
# Apple/iOS Web Push accepts an empty payload. That means we only need VAPID
# authentication here; no AES-GCM payload encryption is required. The service
# worker displays a generic new-report notification and the actual report is
# already mirrored through Firebase/ntfy for the app to open.
_P256_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_P256_A = (_P256_P - 3) % _P256_P
_P256_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_P256_G = (
    0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
    0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5,
)
_P256_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551


def _ec_inv(v, mod):
    return pow(v % mod, -1, mod)


def _ec_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1,y1=p1; x2,y2=p2
    if x1==x2 and (y1+y2)%_P256_P==0:
        return None
    if p1==p2:
        if y1%_P256_P==0:
            return None
        m=((3*x1*x1+_P256_A)*_ec_inv(2*y1,_P256_P))%_P256_P
    else:
        m=((y2-y1)*_ec_inv(x2-x1,_P256_P))%_P256_P
    x3=(m*m-x1-x2)%_P256_P
    y3=(m*(x1-x3)-y1)%_P256_P
    return x3,y3


def _ec_mul(k, point=_P256_G):
    k=int(k)%_P256_N
    out=None; cur=point
    while k:
        if k&1:
            out=_ec_add(out,cur)
        cur=_ec_add(cur,cur)
        k>>=1
    return out


def _rfc6979_k(priv_int, digest):
    qlen=32
    x=int(priv_int).to_bytes(qlen,'big')
    h1=(int.from_bytes(digest,'big')%_P256_N).to_bytes(qlen,'big')
    v=b'\x01'*32; k=b'\x00'*32
    k=hmac.new(k,v+b'\x00'+x+h1,hashlib.sha256).digest()
    v=hmac.new(k,v,hashlib.sha256).digest()
    k=hmac.new(k,v+b'\x01'+x+h1,hashlib.sha256).digest()
    v=hmac.new(k,v,hashlib.sha256).digest()
    while True:
        v=hmac.new(k,v,hashlib.sha256).digest()
        candidate=int.from_bytes(v,'big')
        if 1<=candidate<_P256_N:
            return candidate
        k=hmac.new(k,v+b'\x00',hashlib.sha256).digest()
        v=hmac.new(k,v,hashlib.sha256).digest()


def _ecdsa_p256_sign_raw(message: bytes, private_hex: str) -> bytes:
    d=int(private_hex,16)
    digest=hashlib.sha256(message).digest()
    z=int.from_bytes(digest,'big')
    while True:
        k=_rfc6979_k(d,digest)
        pt=_ec_mul(k)
        if pt is None:
            continue
        r=pt[0]%_P256_N
        if not r:
            continue
        ss=(_ec_inv(k,_P256_N)*(z+r*d))%_P256_N
        if not ss:
            continue
        if ss>_P256_N//2:
            ss=_P256_N-ss
        return r.to_bytes(32,'big')+ss.to_bytes(32,'big')


def _webpush_vapid_jwt(endpoint: str) -> str:
    u=urllib.parse.urlsplit(str(endpoint or ''))
    if u.scheme!='https' or not u.netloc:
        raise ValueError('Web Push endpoint نامعتبر است.')
    aud=f'{u.scheme}://{u.netloc}'
    now=int(time.time())
    header={'typ':'JWT','alg':'ES256'}
    claims={'aud':aud,'exp':now+12*3600,'sub':WEBPUSH_SUBJECT}
    unsigned=(
        _b64url(json.dumps(header,separators=(',',':')).encode('utf-8'))+'.'+
        _b64url(json.dumps(claims,separators=(',',':')).encode('utf-8'))
    ).encode('ascii')
    sig=_ecdsa_p256_sign_raw(unsigned,WEBPUSH_VAPID_PRIVATE_HEX)
    return unsigned.decode('ascii')+'.'+_b64url(sig)


def _powershell_empty_webpush(endpoint, headers, timeout=10):
    if os.name!='nt':
        raise RuntimeError('PowerShell fallback only available on Windows')
    exe=shutil.which('powershell.exe') or shutil.which('powershell') or shutil.which('pwsh')
    if not exe:
        raise RuntimeError('PowerShell not found')
    env=os.environ.copy()
    env['SOLICO_PUSH_URL']=str(endpoint)
    env['SOLICO_PUSH_AUTH']=str(headers.get('Authorization') or '')
    env['SOLICO_PUSH_TTL']=str(headers.get('TTL') or '172800')
    env['SOLICO_PUSH_URGENCY']=str(headers.get('Urgency') or 'high')
    env['SOLICO_PUSH_TOPIC']=str(headers.get('Topic') or 'solico-report')
    ps_script = '''$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$h=@{ Authorization=$env:SOLICO_PUSH_AUTH; TTL=$env:SOLICO_PUSH_TTL; Urgency=$env:SOLICO_PUSH_URGENCY; Topic=$env:SOLICO_PUSH_TOPIC }
$r=Invoke-WebRequest -UseBasicParsing -Uri $env:SOLICO_PUSH_URL -Method Post -Headers $h -Body ([byte[]]@()) -TimeoutSec 10
Write-Output ([string]$r.StatusCode)
'''
    cp=subprocess.run([exe,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',ps_script],capture_output=True,text=True,env=env,timeout=max(8,int(timeout)+8),creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0))
    if cp.returncode!=0:
        raise RuntimeError((cp.stderr or cp.stdout or 'PowerShell Web Push failed').strip()[-1000:])
    return (cp.stdout or '').strip()


def _send_standard_webpush(subscription, ttl=172800, timeout=10):
    if isinstance(subscription,str):
        try: subscription=json.loads(subscription)
        except Exception: subscription={}
    if not isinstance(subscription,dict):
        raise ValueError('Web Push subscription موجود نیست.')
    endpoint=str(subscription.get('endpoint') or '').strip()
    if not endpoint:
        raise ValueError('Web Push endpoint خالی است.')
    jwt=_webpush_vapid_jwt(endpoint)
    headers={
        'Authorization':f'vapid t={jwt}, k={WEBPUSH_VAPID_PUBLIC}',
        'TTL':str(int(ttl)),
        'Urgency':'high',
        'Topic':'solico-report',
        'User-Agent':'SolicoEnergy/1.0.27',
    }
    req=urllib.request.Request(endpoint,data=b'',headers=headers,method='POST')
    try:
        with urllib.request.urlopen(req,timeout=max(5,int(timeout))) as resp:
            code=int(getattr(resp,'status',0) or resp.getcode() or 0)
            if 200<=code<300:
                return {'status':code,'transport':'urllib'}
            raise RuntimeError(f'Web Push HTTP {code}')
    except urllib.error.HTTPError as ex:
        detail=''
        try: detail=ex.read().decode('utf-8',errors='replace')[:600]
        except Exception: pass
        if int(ex.code) in (404,410):
            raise RuntimeError(f'WEBPUSH_GONE HTTP {ex.code} {detail}')
        if os.name=='nt':
            try:
                out=_powershell_empty_webpush(endpoint,headers,timeout=timeout)
                return {'status':out or 'ok','transport':'powershell'}
            except Exception:
                pass
        raise RuntimeError(f'Web Push HTTP {ex.code} {detail}')
    except Exception:
        if os.name=='nt':
            out=_powershell_empty_webpush(endpoint,headers,timeout=timeout)
            return {'status':out or 'ok','transport':'powershell'}
        raise


def _clear_stale_fcm_token(device_id, token):
    device_id=str(device_id or "").strip()
    token=str(token or "").strip()
    if not device_id or not token:
        return
    try:
        items=_load_devices()
        changed=False
        for d in items:
            if str(d.get("device_id") or "")==device_id and str(d.get("fcm_token") or "")==token:
                d["fcm_token"]=""
                changed=True
                break
        if changed:
            _save_devices(items)
    except Exception:
        log.exception("clearing stale FCM token failed")

def device_registration_url():
    port=start_report_server(); ip=_get_lan_ip()
    if ip=="127.0.0.1": return ""
    return f"http://{ip}:{port}/device/register"


def _background_pwa_cloud_sync(device_id, payload):
    """Best-effort report mirror after a direct iOS push. Never blocks notification."""
    did=str(device_id or "").strip()
    if not did:
        return
    def _job():
        try:
            _mirror_pwa_latest_report(did,payload)
        except Exception as ex:
            log.info("background PWA latest mirror skipped/failed for %s: %s",did,ex)
        try:
            key=_control_topic(did)
            event_key=str(payload.get("event_id") or ("report-"+uuid.uuid4().hex))
            _db_request("PUT",f"pwa_control/{key}/{event_key}",payload,timeout=6)
        except Exception as ex:
            log.info("background Firebase PWA control skipped/failed for %s: %s",did,ex)
        try:
            _ntfy_publish(_control_topic(did),payload,title=str(payload.get("title") or "SolicoEnergy report"))
        except Exception as ex:
            log.info("background ntfy PWA control skipped/failed for %s: %s",did,ex)
    threading.Thread(target=_job,name="Solico-iOS-cloud-sync",daemon=True).start()


def send_topic_notification(title: str, body: str, pdf_url: str = "", extra_data=None):
    """Send a report notification only to currently active devices.

    R34 keeps notification delivery independent from registry/network refreshes.
    Android uses locally cached FCM tokens; iPhone PWA uses the locally cached Safari
    Web Push subscription and sends directly to the Apple push endpoint. Firebase/ntfy
    syncing runs separately and can fail without blocking notification delivery.
    """
    data={
        "title":str(title or "گزارش جدید Solico Energy"),
        "body":str(body or "گزارش جدید نیروگاه آماده است."),
        "event_id":uuid.uuid4().hex,
    }
    if pdf_url:
        data["pdf_url"]=str(pdf_url)

    server=device_registration_url()
    if server:
        data["server_url"]=server

    if extra_data:
        for key,value in dict(extra_data).items():
            if value is not None:
                data[str(key)]=str(value)

    # R28: always mirror the newest report once, even if no iPhone is currently
    # visible in the Windows device table. This prevents a temporarily unregistered
    # iPhone from remaining stuck on an older report date.
    try:
        _mirror_pwa_latest_broadcast(data)
    except Exception as ex:
        log.warning("global PWA latest-report mirror failed: %s",ex)

    # R34: notification delivery is deliberately offline-first. Do NOT refresh
    # Firebase/ntfy here; that was the reason iPhone notifications required the
    # Windows VPN and Android sends were delayed. Use the local device/token cache.
    try:
        devices=_notification_devices_local_only()
    except Exception:
        log.exception("local notification-device cache failed")
        devices=[d for d in _load_devices() if bool(d.get("enabled",True)) and not _is_device_tombstoned(d.get("device_id"))]

    # Keep only real active device IDs and dedupe by device_id. Newest registry
    # rows are already sorted first by sync_cloud_devices().
    deduped=[]
    seen=set()
    for d in devices:
        did=str(d.get("device_id") or "").strip()
        if not did or did in seen:
            continue
        seen.add(did)
        deduped.append(d)
    devices=deduped

    if not devices:
        _write_status(
            "sent_no_devices",
            "گزارش ساخته شد؛ دستگاه فعال برای ارسال اعلان وجود ندارد.",
            active_device_count=0,
            direct_ok=0,
            private_topic_ok=0,
            failed_count=0,
        )
        return {
            "mode":"active_devices_parallel",
            "response":None,
            "active_device_count":0,
            "direct_ok":0,
            "direct_attempted":0,
            "private_topic_ok":0,
            "failed_count":0,
            "topic_ok":False,
        }

    try:
        workers=int(os.environ.get("SOLICO_FCM_WORKERS") or "20")
    except Exception:
        workers=20
    workers=max(4,min(32,workers,len(devices)))

    def send_one(d):
        did=str(d.get("device_id") or "").strip()
        token=str(d.get("fcm_token") or "").strip()
        scheme=str(d.get("id_scheme") or "").strip()
        result={"device_id":did,"ok":False,"mode":"","direct_attempted":False}
        direct_error=""

        # iPhone/iPad PWA R34: notification transport is direct Apple Web Push
        # using the locally cached Safari subscription. No Firebase/ntfy lookup is
        # performed in the critical path, so Windows VPN is not required for the
        # notification itself after the device has been registered once.
        if scheme=="web_pwa_v1":
            payload=dict(data)
            payload.update({"kind":"report_notification","secret":NTFY_REGISTRY_SECRET,"device_id":did,"created_at_ms":int(time.time()*1000)})
            subscription=d.get("webpush_subscription")
            if subscription:
                try:
                    response=_send_standard_webpush(subscription,timeout=10)
                    result.update(ok=True,mode="webpush_ios_direct_cached",direct_attempted=True,
                                  webpush_ok=True,response=response)
                    # Cloud report mirroring remains best-effort and asynchronous.
                    # It may need VPN, but it can never delay or block the notification.
                    _background_pwa_cloud_sync(did,payload)
                    log.info("iPhone direct Web Push sent from local cache device=%s transport=%s",did,response.get("transport") if isinstance(response,dict) else response)
                    return result
                except Exception as ex:
                    err=str(ex)
                    log.warning("direct cached iPhone Web Push failed for %s: %s",did,err)
                    if "WEBPUSH_GONE" in err.upper():
                        try:_request_pwa_webpush_resubscribe(did,"apple_endpoint_gone")
                        except Exception:log.exception("requesting PWA push resubscribe failed for %s",did)
                    result.update(webpush_ok=False,direct_attempted=True,error=("webpush: "+err)[:900])
            else:
                result.update(webpush_ok=False,direct_attempted=False,error="webpush: local subscription missing; one successful iPhone registration is required")

            # Optional legacy FCM token fallback only; this does not require a
            # registry refresh and therefore does not reintroduce the VPN dependency.
            if token:
                try:
                    push_data={str(k):str(v) for k,v in payload.items() if v is not None}
                    response=_fcm_send_message({
                        "token":token,
                        "notification":{
                            "title":str(data.get("title") or "SolicoEnergy"),
                            "body":str(data.get("body") or "گزارش جدید نیروگاه آماده است."),
                        },
                        "data":push_data,
                    })
                    result.update(ok=True,mode="fcm_web_legacy_cached",response=response)
                    _background_pwa_cloud_sync(did,payload)
                    return result
                except Exception as ex:
                    prior=str(result.get("error") or "")
                    result["error"]=(prior+" | fcm_web: "+str(ex))[-900:]

            # Keep cloud sync best-effort in the background even if push failed; it
            # must never make the report-generation UI wait for filtered endpoints.
            _background_pwa_cloud_sync(did,payload)
            return result

        if token:
            result["direct_attempted"]=True
            # One short retry absorbs transient network hiccups without turning a
            # single slow phone into a serial delay for every other phone.
            for attempt in range(2):
                try:
                    response=_fcm_send_message({
                        "token":token,
                        "data":data,
                        "android":{"priority":"high","ttl":"172800s"},
                    })
                    result.update(ok=True,mode="token",response=response)
                    return result
                except Exception as ex:
                    direct_error=str(ex)
                    upper=direct_error.upper()
                    if "UNREGISTERED" in upper or "NOTREGISTERED" in upper:
                        try:
                            _clear_stale_fcm_token(did,token)
                        except Exception:
                            log.exception("clearing stale token failed for %s",did)
                        break
                    if attempt==0:
                        time.sleep(0.25)

        # Private topic fallback is still device-specific, therefore safe for
        # deleted-device semantics and useful when a fresh token has not reached
        # Windows yet.
        try:
            response=_fcm_send_message({
                "topic":"solico_device_"+did,
                "data":data,
                "android":{"priority":"high","ttl":"172800s"},
            })
            result.update(ok=True,mode="private_topic",response=response)
            if direct_error:
                result["direct_error"]=direct_error[:700]
            return result
        except Exception as ex:
            result["error"]=(str(ex) or direct_error or "FCM send failed")[:700]
            if direct_error:
                result["direct_error"]=direct_error[:700]
            return result

    results=[]
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="solico-fcm") as pool:
        futures=[pool.submit(send_one,d) for d in devices]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as ex:
                log.exception("parallel FCM worker failed")
                results.append({"device_id":"","ok":False,"mode":"worker","error":str(ex)[:700],"direct_attempted":False})

    direct_attempted=sum(1 for r in results if r.get("direct_attempted"))
    direct_ok=sum(1 for r in results if r.get("ok") and r.get("mode")=="token")
    private_topic_ok=sum(1 for r in results if r.get("ok") and r.get("mode")=="private_topic")
    pwa_ok=sum(1 for r in results if r.get("ok") and ("firebase" in str(r.get("mode") or "") or "webpush_ios" in str(r.get("mode") or "") or "ntfy" in str(r.get("mode") or "")))
    pwa_webpush_ok=sum(1 for r in results if r.get("webpush_ok"))
    ios_direct_no_vpn_ok=sum(1 for r in results if r.get("ok") and r.get("mode")=="webpush_ios_direct_cached")
    pwa_webpush_failed=sum(1 for r in results if str(r.get("error") or "").startswith("webpush:"))
    web_ntfy_ok=pwa_ok  # compatibility field retained for older status readers
    success_count=sum(1 for r in results if r.get("ok"))
    failed=[r for r in results if not r.get("ok")]

    status_msg=f"گزارش برای {success_count}/{len(devices)} دستگاه فعال ارسال/Sync شد."
    if ios_direct_no_vpn_ok:
        status_msg += f" iPhone مستقیم بدون VPN ویندوز: {ios_direct_no_vpn_ok} موفق."
    if pwa_webpush_failed:
        status_msg += f" اعلان پس‌زمینه iPhone: {pwa_webpush_ok} موفق، {pwa_webpush_failed} ناموفق."
    _write_status(
        "sent_parallel_active_devices" if success_count else "send_failed_all_devices",
        status_msg,
        direct_results=results,
        direct_ok=direct_ok,
        direct_attempted=direct_attempted,
        private_topic_ok=private_topic_ok,
        web_ntfy_ok=web_ntfy_ok,
        pwa_ok=pwa_ok,
        pwa_webpush_ok=pwa_webpush_ok,
        pwa_webpush_failed=pwa_webpush_failed,
        ios_direct_no_vpn_ok=ios_direct_no_vpn_ok,
        failed_count=len(failed),
        worker_count=workers,
        active_device_count=len(devices),
        topic_ok=private_topic_ok>0,
    )

    if success_count==0:
        detail="; ".join((r.get("device_id") or "?")+": "+str(r.get("error") or "")[:180] for r in failed[:5])
        raise RuntimeError("ارسال اعلان به همه دستگاه‌های فعال ناموفق بود." + (("\n"+detail) if detail else ""))

    if failed:
        log.warning("report notification partial success: %s/%s sent",success_count,len(devices))

    return {
        "mode":"active_devices_parallel",
        "response":{"sent":success_count,"failed":len(failed),"workers":workers},
        "active_device_count":len(devices),
        "direct_ok":direct_ok,
        "direct_attempted":direct_attempted,
        "private_topic_ok":private_topic_ok,
        "web_ntfy_ok":web_ntfy_ok,
        "pwa_ok":pwa_ok,
        "pwa_webpush_ok":pwa_webpush_ok,
        "pwa_webpush_failed":pwa_webpush_failed,
        "ios_direct_no_vpn_ok":ios_direct_no_vpn_ok,
        "failed_count":len(failed),
        "topic_ok":private_topic_ok>0,
    }

def send_registration_ping():
    rows=sync_cloud_devices()
    return {"mode":"ntfy_registry","device_count":len(rows)}

def _get_lan_ip() -> str:
    """Return the LAN address Windows actually uses for Internet traffic.

    Previous builds collected all private IPv4 addresses and then sorted them.
    On PCs with virtual adapters / hotspot / VMware / Hyper-V this could choose
    the wrong 192.168.x.x address.  That produced a valid-looking notification
    link that the phone could not reach.

    The default-route socket address is now returned first.  Hostname addresses
    are used only as a fallback.
    """
    def is_private_ipv4(ip):
        try:
            p = [int(x) for x in str(ip).split(".")]
            if len(p) != 4 or any(x < 0 or x > 255 for x in p):
                return False
            if p[0] == 10:
                return True
            if p[0] == 192 and p[1] == 168:
                return True
            if p[0] == 172 and 16 <= p[1] <= 31:
                return True
            if p[0] == 169 and p[1] == 254:
                return True
            return False
        except Exception:
            return False

    # Most important: address chosen by the active Windows default route.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            route_ip = s.getsockname()[0]
        finally:
            s.close()
        if is_private_ipv4(route_ip):
            return route_ip
    except Exception:
        pass

    # Fallback: preserve discovery order and return the first usable private IP.
    candidates = []
    try:
        host = socket.gethostname()
        for item in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = item[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except Exception:
        pass

    for ip in candidates:
        if is_private_ipv4(ip):
            return ip

    return "127.0.0.1"


def _sign_report_name(filename: str) -> str:
    secret = _load_or_create_secret().encode("utf-8")
    return hmac.new(secret, filename.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _http_send(sock, status_code: int, reason: str, body: bytes = b"", headers=None):
    """Minimal HTTP/1.1 response writer.

    Implemented with ``socket`` instead of ``http.server`` because the installed
    SolicoEnergy launcher is a trimmed/frozen Python runtime and does not ship
    the optional stdlib ``http.server`` module.
    """
    headers = dict(headers or {})
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Connection", "close")
    headers.setdefault("Server", "SolicoEnergyReport/1.0")
    head = [f"HTTP/1.1 {int(status_code)} {reason}"]
    for key, value in headers.items():
        head.append(f"{key}: {value}")
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("iso-8859-1", errors="replace")
    sock.sendall(raw)
    if body:
        sock.sendall(body)


def _handle_report_client(conn, addr):
    try:
        conn.settimeout(8); data=bytearray()
        while b"\r\n\r\n" not in data and len(data)<65536:
            chunk=conn.recv(4096)
            if not chunk: break
            data.extend(chunk)
        if not data:return
        header_raw,_,body0=bytes(data).partition(b"\r\n\r\n")
        lines=header_raw.decode("iso-8859-1",errors="replace").split("\r\n")
        try: method,raw_path,_version=lines[0].split(" ",2)
        except Exception:_http_send(conn,400,"Bad Request",b"Bad Request");return
        headers={}
        for line in lines[1:]:
            if ":" in line:
                k,v=line.split(":",1);headers[k.strip().lower()]=v.strip()
        length=int(headers.get("content-length") or 0); body=bytearray(body0)
        while len(body)<length:
            chunk=conn.recv(min(4096,length-len(body)))
            if not chunk:break
            body.extend(chunk)
        parsed=urllib.parse.urlparse(raw_path)
        if method.upper()=="GET" and parsed.path=="/device/ping":
            raw=json.dumps({"ok":True,"server":"SolicoEnergy","time":time.time()}).encode("utf-8");_http_send(conn,200,"OK",raw,{"Content-Type":"application/json; charset=utf-8"});return
        if method.upper()=="POST" and parsed.path=="/device/register":
            try: payload=json.loads(bytes(body[:length]).decode("utf-8")); item=register_mobile_device(payload,addr[0] if addr else "")
            except Exception as ex:
                _http_send(conn,400,"Bad Request",str(ex).encode("utf-8"));return
            if not bool(item.get("enabled",True)):
                _http_send(conn,403,"Forbidden",json.dumps({"ok":False,"enabled":False}).encode("utf-8"),{"Content-Type":"application/json"});return
            raw=json.dumps({"ok":True,"enabled":True},ensure_ascii=False).encode("utf-8");_http_send(conn,200,"OK",raw,{"Content-Type":"application/json; charset=utf-8"});return
        if method.upper()!="GET":_http_send(conn,405,"Method Not Allowed",b"Method Not Allowed",{"Allow":"GET, POST"});return
        parts=[urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        if len(parts)!=3 or parts[0]!="report":_http_send(conn,404,"Not Found",b"Not Found");return
        signature,filename=parts[1],parts[2]
        if not hmac.compare_digest(signature,_sign_report_name(filename)):_http_send(conn,403,"Forbidden",b"Forbidden");return
        out_dir=settings_mod.ensure_output_dir().resolve();target=(out_dir/Path(filename).name).resolve()
        if target.parent!=out_dir or target.suffix.lower()!=".pdf" or not target.exists():_http_send(conn,404,"Not Found",b"Not Found");return
        size=target.stat().st_size;headers={"Content-Type":"application/pdf","Content-Length":str(size),"Content-Disposition":"attachment; filename*=UTF-8''"+urllib.parse.quote(target.name),"Cache-Control":"private, max-age=60","Connection":"close","Server":"SolicoEnergyReport/1.1"};head=["HTTP/1.1 200 OK"]+[f"{k}: {v}" for k,v in headers.items()];conn.sendall(("\r\n".join(head)+"\r\n\r\n").encode("iso-8859-1",errors="replace"));
        with target.open("rb") as f:
            while True:
                chunk=f.read(1024*256)
                if not chunk:break
                conn.sendall(chunk)
    except (BrokenPipeError,ConnectionResetError,TimeoutError,socket.timeout):pass
    except Exception:
        log.exception("report server request failed from %r",addr)
        try:_http_send(conn,500,"Internal Server Error",b"Internal Server Error")
        except Exception:pass
    finally:
        try:conn.close()
        except Exception:pass


def _serve_report_socket(srv):
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            return
        threading.Thread(
            target=_handle_report_client,
            args=(conn, addr),
            name="SolicoMobileReportClient",
            daemon=True,
        ).start()


def start_report_server(preferred_port: int = DEFAULT_PORT):
    global _server, _server_thread, _server_port
    with _server_lock:
        if _server is not None:
            return _server_port
        last_error = None
        for port in range(int(preferred_port), int(preferred_port) + 10):
            srv = None
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("0.0.0.0", port))
                srv.listen(8)
                th = threading.Thread(
                    target=_serve_report_socket,
                    args=(srv,),
                    name="SolicoMobileReportServer",
                    daemon=True,
                )
                th.start()
                _server, _server_thread, _server_port = srv, th, port
                _write_status(
                    "server",
                    "سرور گزارش موبایل فعال است.",
                    lan_ip=_get_lan_ip(),
                    port=port,
                )
                return port
            except OSError as e:
                last_error = e
                if srv is not None:
                    try:
                        srv.close()
                    except Exception:
                        pass
        raise OSError(f"پورت آزاد برای سرور گزارش پیدا نشد: {last_error}")


def report_url(report_path) -> str:
    p = Path(report_path).resolve()
    if not p.exists() or p.suffix.lower() != ".pdf":
        raise FileNotFoundError(str(p))
    out_dir = settings_mod.ensure_output_dir().resolve()
    if p.parent != out_dir:
        # To keep the LAN server restricted to one safe folder, copy report there if needed.
        target = out_dir / p.name
        if target.resolve() != p:
            import shutil
            shutil.copy2(p, target)
        p = target.resolve()
    port = start_report_server()
    ip = _get_lan_ip()
    if ip == "127.0.0.1":
        raise RuntimeError(
            "IP شبکه محلی پیدا نشد. کامپیوتر باید به همان Wi‑Fi/LAN گوشی متصل باشد."
        )
    filename = p.name
    sig = _sign_report_name(filename)
    return (
        f"http://{ip}:{port}/report/{sig}/"
        + urllib.parse.quote(filename, safe="")
    )


_FA_DIGITS = str.maketrans("0123456789.-", "۰۱۲۳۴۵۶۷۸۹٫-")


def _fa_num(value, decimals=1):
    if value is None:
        return None
    try:
        return f"{float(value):,.{int(decimals)}f}".translate(_FA_DIGITS)
    except Exception:
        return None


def _fmt_mwh(kwh):
    n = _fa_num(float(kwh) / 1000.0, 1) if kwh is not None else None
    return (n + " MWh") if n is not None else None


def _fmt_mw(kw):
    n = _fa_num(float(kw) / 1000.0, 2) if kw is not None else None
    return (n + " MW") if n is not None else None


def _fmt_pct(v):
    n = _fa_num(v, 1)
    return (n + "٪") if n is not None else None


def _fmt_pf(v):
    return _fa_num(v, 3)


def _join_parts(parts):
    return " | ".join(str(x) for x in parts if x)


def _energy_notification_context(kind=None, report_date=None, source_id=None, as_of=None,
                                 jalali_year=None, jalali_month=None,
                                 compare_mode=None):
    """Build a compact management summary from the same calculation engine as the PDF.

    This intentionally reuses services.calculations instead of re-reading the CSV, so
    notification values stay aligned with the Windows report calculation rules.
    """
    if not kind:
        return None, None
    try:
        from ..core.jalali import format_jalali, jalali_month_bounds
        from ..services.calculations import day_summary, monthly_summary, comparison_summary

        if kind == "daily" and report_date is not None:
            s = day_summary(report_date, source_id=source_id, as_of=as_of)
            fac = s.get("factory") or {}
            grd = s.get("grid") or {}
            line1 = _join_parts([
                "مصرف: " + _fmt_mwh(fac.get("active_kwh")) if _fmt_mwh(fac.get("active_kwh")) else None,
                "نیروگاه: " + _fmt_mwh(s.get("plant_energy")) if _fmt_mwh(s.get("plant_energy")) else None,
                "شبکه: " + _fmt_mwh(grd.get("active_kwh")) if _fmt_mwh(grd.get("active_kwh")) else None,
            ])
            line2 = _join_parts([
                "سهم نیروگاه: " + _fmt_pct(s.get("plant_share")) if _fmt_pct(s.get("plant_share")) else None,
                "پیک کارخانه: " + _fmt_mw(fac.get("max_kw")) if _fmt_mw(fac.get("max_kw")) else None,
                "متوسط توان: " + _fmt_mw(fac.get("avg_kw")) if _fmt_mw(fac.get("avg_kw")) else None,
            ])
            title = ("گزارش روزانه موقت " if as_of is not None else "گزارش روزانه ") + format_jalali(report_date).translate(_FA_DIGITS)
            body = "\n".join(x for x in (line1, line2) if x)
            return title, body or None

        if kind == "monthly" and jalali_year is not None and jalali_month is not None:
            start, end = jalali_month_bounds(int(jalali_year), int(jalali_month))
            s = monthly_summary(start, end, source_id=source_id)
            peak = s.get("peak")
            peak_kw = peak[0] if peak else None
            line1 = _join_parts([
                "مصرف: " + _fmt_mwh(s.get("factory_energy")) if _fmt_mwh(s.get("factory_energy")) else None,
                "نیروگاه: " + _fmt_mwh(s.get("plant_energy")) if _fmt_mwh(s.get("plant_energy")) else None,
                "شبکه: " + _fmt_mwh(s.get("grid_energy")) if _fmt_mwh(s.get("grid_energy")) else None,
            ])
            line2 = _join_parts([
                "سهم نیروگاه: " + _fmt_pct(s.get("plant_share")) if _fmt_pct(s.get("plant_share")) else None,
                "پیک کارخانه: " + _fmt_mw(peak_kw) if _fmt_mw(peak_kw) else None,
                "متوسط توان: " + _fmt_mw(s.get("factory_avg")) if _fmt_mw(s.get("factory_avg")) else None,
            ])
            title = f"گزارش ماهانه {int(jalali_year):04d}/{int(jalali_month):02d}".translate(_FA_DIGITS)
            body = "\n".join(x for x in (line1, line2) if x)
            return title, body or None

        if kind == "comparison" and report_date is not None:
            s = comparison_summary(report_date, compare_mode or "day", source_id=source_id)
            cur = s.get("current") or {}
            prv = s.get("previous") or {}

            def pct_change(a, b):
                if a is None or b in (None, 0):
                    return None
                return (float(a) - float(b)) / float(b) * 100.0

            fac_ch = pct_change(cur.get("factory_energy"), prv.get("factory_energy"))
            share_cur, share_prev = cur.get("plant_share"), prv.get("plant_share")
            share_pp = (float(share_cur)-float(share_prev)) if share_cur is not None and share_prev is not None else None
            pf_cur, pf_prev = cur.get("plant_pf"), prv.get("plant_pf")
            pf_delta = (float(pf_cur)-float(pf_prev)) if pf_cur is not None and pf_prev is not None else None

            parts=[]
            if fac_ch is not None:
                sign="+" if fac_ch >= 0 else ""
                parts.append("تغییر مصرف: " + (sign + f"{fac_ch:.1f}%").translate(_FA_DIGITS))
            if share_pp is not None:
                sign="+" if share_pp >= 0 else ""
                parts.append("تغییر سهم نیروگاه: " + (sign + f"{share_pp:.1f} واحد٪").translate(_FA_DIGITS))
            if pf_delta is not None:
                direction = "بهبود" if pf_delta > 0 else ("کاهش" if pf_delta < 0 else "بدون تغییر")
                parts.append("PF: " + direction)
            title = "گزارش مقایسه " + format_jalali(report_date).translate(_FA_DIGITS)
            return title, _join_parts(parts) or None
    except Exception:
        log.exception("building KPI notification summary failed")
    return None, None



def _energy_mobile_kpi(**context):
    """Return the six dashboard KPIs as strings for the Android app."""
    kind = context.get("kind")
    report_date = context.get("report_date")
    source_id = context.get("source_id")
    as_of = context.get("as_of")
    jalali_year = context.get("jalali_year")
    jalali_month = context.get("jalali_month")

    try:
        from ..core.jalali import format_jalali, jalali_month_bounds
        from ..services.calculations import day_summary, monthly_summary

        if kind == "daily" and report_date is not None:
            s = day_summary(report_date, source_id=source_id, as_of=as_of)
            fac = s.get("factory") or {}
            grd = s.get("grid") or {}
            title = ("گزارش روزانه موقت " if as_of is not None else "گزارش روزانه ") + format_jalali(report_date).translate(_FA_DIGITS)
            return {
                "report_kind": "daily",
                "report_title": title,
                "report_date": format_jalali(report_date).translate(_FA_DIGITS),
                "report_date_iso": report_date.isoformat(),
                "kpi_factory": _fmt_mwh(fac.get("active_kwh")) or "—",
                "kpi_plant": _fmt_mwh(s.get("plant_energy")) or "—",
                "kpi_grid": _fmt_mwh(grd.get("active_kwh")) or "—",
                "kpi_share": _fmt_pct(s.get("plant_share")) or "—",
                "kpi_peak": _fmt_mw(fac.get("max_kw")) or "—",
                "kpi_avg": _fmt_mw(fac.get("avg_kw")) or "—",
                "kpi_pf": _fmt_pf(s.get("plant_pf")) or "—",
                "kpi_factory_raw": "" if fac.get("active_kwh") is None else str(float(fac.get("active_kwh"))/1000.0),
                "kpi_plant_raw": "" if s.get("plant_energy") is None else str(float(s.get("plant_energy"))/1000.0),
                "kpi_grid_raw": "" if grd.get("active_kwh") is None else str(float(grd.get("active_kwh"))/1000.0),
                "kpi_share_raw": "" if s.get("plant_share") is None else str(s.get("plant_share")),
                "kpi_peak_raw": "" if fac.get("max_kw") is None else str(float(fac.get("max_kw"))/1000.0),
                "kpi_avg_raw": "" if fac.get("avg_kw") is None else str(float(fac.get("avg_kw"))/1000.0),
                "kpi_pf_raw": "" if s.get("plant_pf") is None else str(s.get("plant_pf")),
            }

        if kind == "monthly" and jalali_year is not None and jalali_month is not None:
            start, end = jalali_month_bounds(int(jalali_year), int(jalali_month))
            s = monthly_summary(start, end, source_id=source_id)
            peak = s.get("peak")
            peak_kw = peak[0] if peak else None
            title = f"گزارش ماهانه {int(jalali_year):04d}/{int(jalali_month):02d}".translate(_FA_DIGITS)
            return {
                "report_kind": "monthly",
                "report_title": title,
                "report_date": f"{int(jalali_year):04d}/{int(jalali_month):02d}".translate(_FA_DIGITS),
                "kpi_factory": _fmt_mwh(s.get("factory_energy")) or "—",
                "kpi_plant": _fmt_mwh(s.get("plant_energy")) or "—",
                "kpi_grid": _fmt_mwh(s.get("grid_energy")) or "—",
                "kpi_share": _fmt_pct(s.get("plant_share")) or "—",
                "kpi_peak": _fmt_mw(peak_kw) or "—",
                "kpi_avg": _fmt_mw(s.get("factory_avg")) or "—",
                "kpi_pf": _fmt_pf(s.get("plant_pf")) or "—",
                "kpi_factory_raw": "" if s.get("factory_energy") is None else str(float(s.get("factory_energy"))/1000.0),
                "kpi_plant_raw": "" if s.get("plant_energy") is None else str(float(s.get("plant_energy"))/1000.0),
                "kpi_grid_raw": "" if s.get("grid_energy") is None else str(float(s.get("grid_energy"))/1000.0),
                "kpi_share_raw": "" if s.get("plant_share") is None else str(s.get("plant_share")),
                "kpi_peak_raw": "" if peak_kw is None else str(float(peak_kw)/1000.0),
                "kpi_avg_raw": "" if s.get("factory_avg") is None else str(float(s.get("factory_avg"))/1000.0),
                "kpi_pf_raw": "" if s.get("plant_pf") is None else str(s.get("plant_pf")),
            }
    except Exception:
        log.exception("building structured KPI payload failed")

    return {}


def notify_report(report_path, label: str = "گزارش جدید", **context):
    cfg = settings_mod.load()
    if not bool(cfg.get("mobile_notify_enabled", True)):
        return {"skipped": True, "reason": "disabled"}
    # Engineer/secondary Windows installations intentionally do not ship the
    # private Firebase key. Report generation must still succeed quietly.
    if not service_account_path().exists():
        _write_status("firebase_key_missing", "کلید Firebase روی این کامپیوتر نصب نیست؛ اعلان موبایل رد شد و گزارش بدون مشکل ساخته شد.", report=str(report_path))
        return {"skipped": True, "reason": "firebase_key_missing"}
    url = report_url(report_path)
    title, body = _energy_notification_context(**context)
    if not title:
        title = "گزارش جدید نیروگاه آماده شد"
    if not body:
        body = f"{label} آماده است. برای مشاهده PDF لمس کنید."
    else:
        body = body + "\nبرای مشاهده PDF لمس کنید."
    mobile_kpi = _energy_mobile_kpi(**context)
    response = send_topic_notification(title, body, pdf_url=url, extra_data=mobile_kpi)
    _write_status(
        "sent",
        "گزارش و خلاصه KPI برای موبایل اعلان شد.",
        report=str(report_path),
        pdf_url=url,
        notification_title=title,
        notification_body=body,
        fcm=response,
    )
    return {"ok": True, "url": url, "response": response, "title": title, "body": body}


def notify_report_async(report_path, label: str = "گزارش جدید", **context):
    """Send a manual-report notification with short retries and persistent fallback.

    Manual reports used to make one fire-and-forget attempt.  A short device-registry
    or Internet hiccup therefore produced a PDF but no phone notification.
    """
    def _job():
        last=None
        for delay in (0,5,15):
            if delay:
                time.sleep(delay)
            try:
                notify_report(report_path, label, **context)
                return
            except Exception as e:
                last=e
                log.exception("mobile notification attempt failed")
        _write_status("error", str(last or 'notification failed'), report=str(report_path))
        try:
            from ..scheduler import _queue_notification
            kind=str(context.get('kind') or 'daily')
            rd=context.get('report_date')
            extra={k:v for k,v in context.items() if k not in ('kind','report_date')}
            _queue_notification(report_path,label,kind=kind,report_date=rd,**extra)
            log.warning("manual report notification queued for retry: %s",report_path)
        except Exception:
            log.exception("queueing manual report notification failed")
    threading.Thread(target=_job, name="SolicoMobilePush", daemon=True).start()



def enable_mobile_firewall():
    """Ask Windows (via UAC) to allow only local-subnet TCP access to report ports."""
    if os.name != "nt":
        return {"ok": True, "message": "Windows Firewall فقط روی ویندوز لازم است."}

    import ctypes

    # Restrict inbound access to the local subnet, even when Windows classified
    # a phone hotspot as a Public network.
    rule_name = "SolicoEnergy Mobile PDF"
    cmd = (
        '/c '
        'netsh advfirewall firewall delete rule name="' + rule_name + '" >nul 2>&1 '
        '& netsh advfirewall firewall add rule '
        'name="' + rule_name + '" '
        'dir=in action=allow protocol=TCP localport=8765-8774 '
        'remoteip=localsubnet profile=any'
    )

    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", cmd, None, 0
    )
    if int(result) <= 32:
        raise RuntimeError(
            "اجازه Administrator برای Firewall داده نشد. "
            "دوباره بزنید و در پنجره Windows روی Yes بزنید."
        )

    _write_status(
        "firewall",
        "درخواست فعال‌سازی دسترسی PDF موبایل در Windows Firewall ارسال شد.",
        lan_ip=_get_lan_ip(),
        port_range="8765-8774",
    )
    return {
        "ok": True,
        "message": "پنجره Windows را با Yes تأیید کنید. دسترسی فقط برای Local Subnet باز می‌شود.",
        "lan_ip": _get_lan_ip(),
        "ports": "8765-8774",
    }


def mobile_lan_text() -> str:
    ip = _get_lan_ip()
    port = _server_port or DEFAULT_PORT
    return f"{ip}:{port}"


def send_test_notification():
    cfg = settings_mod.load()
    if not bool(cfg.get("mobile_notify_enabled", True)):
        raise RuntimeError("ارسال نوتیفیکیشن موبایل در تنظیمات غیرفعال است.")
    response = send_topic_notification(
        "تست Solico Energy",
        "اتصال نرم‌افزار ویندوز به گوشی برقرار است.",
        pdf_url="",
    )
    return response


def mobile_status_text() -> str:
    key = service_account_path()
    st = read_status()
    key_text = "موجود" if key.exists() else "پیدا نشد"
    state = st.get("message") or "هنوز تست نشده"
    return (
        f"کلید Firebase: {key_text} | وضعیت: {state}\n"
        f"آدرس PDF در شبکه: {mobile_lan_text()}"
    )
