(() => {
  'use strict';

  const VERSION = 'Web 1.0.18';
  const VERSION_BUILD = 22;
  const NTFY_BASE = 'https://ntfy.sh';
  const STANDARD_WEB_PUSH_PUBLIC_KEY = "REDACTED_REVIEW_ONLY";
  const STANDARD_WEB_PUSH_KEY_ID = "REDACTED_REVIEW_KEY_ID";
  const FIREBASE_DB_FALLBACKS = [
    'https://REDACTED-PRODUCTION-ENDPOINT.invalid',
    'https://REDACTED-PRODUCTION-ENDPOINT.invalid',
    'https://REDACTED-PRODUCTION-ENDPOINT.invalid',
    'https://REDACTED-PRODUCTION-ENDPOINT.invalid'
  ];
  const REGISTRY_TOPIC = "REDACTED_REVIEW_TOPIC";
  const REG_SECRET = "REDACTED_REVIEW_ONLY";
  const K = {
    logged: 'solico_web_logged_in',
    device: 'solico_web_device_id',
    session: 'solico_web_login_session_id',
    reports: 'solico_web_reports_v1',
    status: 'solico_web_status',
    lastNtfy: 'solico_web_last_ntfy_id',
    offer: 'solico_web_last_offer',
    lastReg: 'solico_web_last_register',
    firebaseSeen: 'solico_web_firebase_seen',
    archiveSnapshot: 'solico_web_archive_snapshot_id',
    pushToken: 'solico_web_fcm_web_token',
    pushSub: 'solico_web_push_subscription',
    pushKeyId: 'solico_web_push_key_id',
    regReceipt: 'solico_web_registration_receipt_ms'
  };

  const isLoggedIn = () => localStorage.getItem(K.logged) === '1';
  if (!isLoggedIn() && !location.pathname.endsWith('/login.html')) {
    location.replace('login.html');
    return;
  }

  function hex(bytes){return [...bytes].map(b=>b.toString(16).padStart(2,'0')).join('')}
  async function sha256(text){return hex(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))))}
  function randomId(){
    if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g,'').slice(0,24);
    const a=new Uint8Array(16);crypto.getRandomValues(a);return hex(a).slice(0,24);
  }
  function deviceId(){let id=localStorage.getItem(K.device);if(!id){id=randomId();localStorage.setItem(K.device,id)}return id}
  function loginSession(){let s=localStorage.getItem(K.session);if(!s){s=crypto.randomUUID?crypto.randomUUID():randomId()+randomId();localStorage.setItem(K.session,s)}return s}
  function deviceName(){
    const ua=navigator.userAgent||'';
    const suffix=deviceId().slice(-4).toUpperCase();
    if(/iPhone/i.test(ua)) return `iPhone WebApp • ${suffix}`;
    if(/iPad/i.test(ua)) return `iPad WebApp • ${suffix}`;
    return `Solico WebApp • ${suffix}`;
  }
  function iosVersion(){const m=(navigator.userAgent||'').match(/OS (\d+)[_\.](\d+)/);return m?`${m[1]}.${m[2]}`:'Web';}
  function setStatus(s){localStorage.setItem(K.status,String(s||''));window.refreshDeviceStatus&&window.refreshDeviceStatus()}

  // R15: Safari can leave cross-origin fetch() pending for a long time when a
  // filtered/blocked route is used. Every cloud request therefore has a hard
  // deadline, otherwise the UI remains forever on "در حال ثبت آیفون..." and
  // the latest-report/push-subscription sync never gets a chance to finish.
  async function fetchTimed(url, options={}, timeoutMs=6000){
    const controller=('AbortController' in window)?new AbortController():null;
    let timer=null;
    try{
      const opts={...options};
      if(controller){opts.signal=controller.signal;timer=setTimeout(()=>controller.abort(),timeoutMs)}
      const r=await fetch(url,opts);
      return r;
    }finally{if(timer)clearTimeout(timer)}
  }
  function shortErr(e){
    const t=String(e&&e.name==='AbortError'?'timeout':(e&&e.message||e||'network error'));
    return t.slice(0,180);
  }
  function promiseTimed(promise, timeoutMs=6000, label='operation'){
    return new Promise((resolve,reject)=>{
      let done=false;
      const t=setTimeout(()=>{if(done)return;done=true;reject(new Error(label+' timeout'))},timeoutMs);
      Promise.resolve(promise).then(v=>{if(done)return;done=true;clearTimeout(t);resolve(v)},e=>{if(done)return;done=true;clearTimeout(t);reject(e)});
    });
  }
  function reports(){
    try{
      const a=JSON.parse(localStorage.getItem(K.reports)||'[]');
      return Array.isArray(a)?a.map(normalizeStoredReport):[];
    }catch(e){return []}
  }
  function saveReports(rows){
    const map=new Map();
    for(const r of rows||[]){
      const key=String(r.event_id||`${r.report_kind||''}|${r.report_date||''}|${r.report_title||''}`);
      map.set(key,r);
    }
    const out=[...map.values()].sort((a,b)=>(a._received_at||0)-(b._received_at||0)).slice(-700);
    localStorage.setItem(K.reports,JSON.stringify(out));
  }
  function latinDigits(v){
    return String(v??'')
      .replace(/[۰-۹]/g,ch=>String('۰۱۲۳۴۵۶۷۸۹'.indexOf(ch)))
      .replace(/[٠-٩]/g,ch=>String('٠١٢٣٤٥٦٧٨٩'.indexOf(ch)));
  }
  function normalizeDate(v){
    let s=latinDigits(v).replace(/[\u200e\u200f\u202a-\u202e]/g,'').trim();
    s=s.replace(/[.\-]/g,'/').replace(/\/+/g,'/');
    const d=s.replace(/\D/g,'');
    if(d.length===8)return `${d.slice(0,4)}/${d.slice(4,6)}/${d.slice(6,8)}`;
    if(d.length===6)return `${d.slice(0,4)}/${d.slice(4,6)}`;
    return s;
  }
  function normalizeStoredReport(r){
    if(!r||typeof r!=='object')return r;
    const x={...r};
    if(x.report_date!==undefined)x.report_date=normalizeDate(x.report_date);
    return x;
  }
  function upsertReport(r,notify=false){
    if(!r||typeof r!=='object')return false;
    const x={...r,_received_at:Date.now()};
    const rows=reports();
    const key=String(x.event_id||`${x.report_kind||''}|${x.report_date||''}|${x.report_title||''}`);
    const idx=rows.findIndex(z=>String(z.event_id||`${z.report_kind||''}|${z.report_date||''}|${z.report_title||''}`)===key);
    const isNew=idx<0;
    if(idx>=0) rows[idx]={...rows[idx],...x}; else rows.push(x);
    saveReports(rows);
    window.refreshLatestReport&&window.refreshLatestReport();
    // Windows mirrors PWA events through Firebase + ntfy. Notify only once per event_id.
    if(notify&&isNew) localNotify(x.title||'گزارش جدید Solico',x.body||x.report_title||'گزارش جدید آماده است.');
    return isNew;
  }

  function importArchiveReports(items){
    const rows=reports();
    const index=new Map(rows.map((z,i)=>[String(z.event_id||`${z.report_kind||''}|${z.report_date||''}|${z.report_title||''}`),i]));
    let imported=0;
    let stamp=Date.now();
    for(const r of (Array.isArray(items)?items:[])){
      if(!r||typeof r!=='object')continue;
      const x=normalizeStoredReport({...r,_received_at:stamp++});
      const key=String(x.event_id||`${x.report_kind||''}|${x.report_date||''}|${x.report_title||''}`);
      const i=index.get(key);
      if(i===undefined){index.set(key,rows.length);rows.push(x)}else rows[i]={...rows[i],...x};
      imported++;
    }
    saveReports(rows);
    window.refreshLatestReport&&window.refreshLatestReport();
    return imported;
  }

  async function pollRegistryForSelf(since='30m'){
    try{
      const url=`${NTFY_BASE}/${encodeURIComponent(REGISTRY_TOPIC)}/json?poll=1&since=${encodeURIComponent(since)}&_=${Date.now()}`;
      const r=await fetchTimed(url,{method:'GET',mode:'cors',cache:'no-store',headers:{'Accept':'application/x-ndjson, application/json, text/plain'}},5000);
      if(!r.ok)return false;
      const raw=await r.text();
      for(const line of raw.split('\n')){
        if(!line.trim())continue;
        try{
          const e=JSON.parse(line); if(e.event!=='message')continue;
          const p=JSON.parse(String(e.message||''));
          if(String(p.secret||'')===REG_SECRET && String(p.kind||'')==='device_register' && String(p.device_id||'')===deviceId())return true;
        }catch(_){ }
      }
    }catch(_){ }
    return false;
  }

  async function publish(topic,payload){
    const url=`${NTFY_BASE}/${encodeURIComponent(topic)}`;
    const body=JSON.stringify({...payload,_web_sent_at:Date.now()});

    // A readable CORS POST is best because we know ntfy accepted it.
    try{
      const r=await fetchTimed(url,{method:'POST',mode:'cors',cache:'no-store',headers:{'Content-Type':'text/plain;charset=UTF-8'},body},4500);
      if(r.ok)return true;
    }catch(_){ }

    // Safari/iOS fallback: a simple no-cors POST is allowed to leave the browser.
    // Its response is opaque, so registration is verified separately below.
    try{
      await fetchTimed(url,{method:'POST',mode:'no-cors',cache:'no-store',headers:{'Content-Type':'text/plain;charset=UTF-8'},body,keepalive:true},4500);
      return true;
    }catch(_){ }

    // GET webhook fallback. The Image request deliberately ignores the response type;
    // reaching the URL is enough to publish the JSON string as the ntfy message.
    const hook=`${url}/publish?message=${encodeURIComponent(body)}&_=${Date.now()}`;
    try{
      await new Promise(resolve=>{
        const img=new Image();let done=false;
        const finish=()=>{if(done)return;done=true;resolve(true)};
        img.onload=finish;img.onerror=finish;img.src=hook;
        setTimeout(finish,4500);
      });
      return true;
    }catch(_){ }

    try{
      if(navigator.sendBeacon){
        const ok=navigator.sendBeacon(url,new Blob([body],{type:'text/plain;charset=UTF-8'}));
        if(ok)return true;
      }
    }catch(_){ }
    throw new Error('ntfy publish failed');
  }
  async function registry(payload){return publish(REGISTRY_TOPIC,{...payload,secret:REG_SECRET})}
  async function controlTopic(){
    const h=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(`${REG_SECRET}|${deviceId()}`));
    return 'solico-ctl-'+hex(new Uint8Array(h).slice(0,12));
  }
  async function globalLatestKey(){
    const h=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(`${REG_SECRET}|latest-broadcast`));
    return 'latest-'+hex(new Uint8Array(h).slice(0,12));
  }

  function firebaseSeen(){
    try{return JSON.parse(localStorage.getItem(K.firebaseSeen)||'[]')}catch(_){return []}
  }
  function rememberFirebaseSeen(id){
    if(!id)return;const a=firebaseSeen().filter(x=>x!==id);a.push(id);
    localStorage.setItem(K.firebaseSeen,JSON.stringify(a.slice(-500)));
  }
  async function firebaseControlKey(){return controlTopic()}
  async function firebaseWriteDecision(payload){
    const key=await firebaseControlKey();
    const body={...payload,secret:REG_SECRET,request_id:String(payload.request_id||''),device_id:deviceId(),created_at_ms:Date.now(),transport:'pwa_firebase'};
    const errors=[];
    for(const base of await firebaseDbUrls()){
      const url=`${base}/pwa_archive_decisions/${encodeURIComponent(key)}/${encodeURIComponent(body.request_id)}.json?_=${Date.now()}`;
      try{
        const r=await fetchTimed(url,{method:'PUT',mode:'cors',cache:'no-store',headers:{'Content-Type':'application/json;charset=UTF-8','Cache-Control':'no-cache'},body:JSON.stringify(body)},6000);
        if(r.ok)return true;errors.push(`HTTP ${r.status}`);
      }catch(e){errors.push(String(e&&e.message||e))}
    }
    throw new Error(errors.join(' | ')||'Firebase decision failed');
  }

  async function firebaseDbUrls(){
    const urls=[];
    const add=u=>{const v=String(u||'').trim().replace(/\/$/,'');if(v&&!urls.includes(v))urls.push(v)};
    // Build 21: trust the URL generated by GitHub Actions first. The previous
    // builds could try a stale cached/guessed regional URL before the verified DB.
    try{
      const r=await fetchTimed(`registry-config.json?_=${Date.now()}`,{cache:'no-store',headers:{'Cache-Control':'no-store'}},3500);
      if(r.ok){
        const cfg=await r.json();
        add(cfg.databaseUrl);
        if(Array.isArray(cfg.databaseUrls))for(const u of cfg.databaseUrls)add(u);
      }
    }catch(_){ }
    // The workflow has verified that this is the default instance for this project.
    add('https://REDACTED-PRODUCTION-ENDPOINT.invalid');
    // A previously working URL is only a fallback now, never the primary source.
    add(localStorage.getItem('solico_web_registry_db_url'));
    return urls;
  }

  async function sendRegistrationEvent(payload){
    const body={...payload,secret:REG_SECRET,kind:'device_register',created_at_ms:Date.now(),transport:'pwa_registration_event'};
    let firebaseDispatched=false;
    const bases=await firebaseDbUrls();
    for(const base of bases){
      const url=`${base}/pwa_registration_events.json`;
      // POST creates an append-only event. text/plain + no-cors avoids the fragile
      // Safari CORS/preflight path that made PUT registration fail on some VPNs.
      try{
        await fetchTimed(url,{method:'POST',mode:'no-cors',cache:'no-store',headers:{'Content-Type':'text/plain;charset=UTF-8'},body:JSON.stringify(body),keepalive:true},3200);
        firebaseDispatched=true;
        break;
      }catch(_){ }
    }

    // ntfy is an independent fallback. It is deliberately fire-and-forget; the
    // Windows receipt below is the only thing that marks registration as confirmed.
    registry(body).catch(e=>console.warn('ntfy registration fallback failed',e));
    return firebaseDispatched;
  }

  async function pollRegistrationReceipt(){
    if(!isLoggedIn()||!navigator.onLine)return false;
    const currentSession=loginSession();
    for(const base of await firebaseDbUrls()){
      try{
        const url=`${base}/pwa_registration_receipts/${encodeURIComponent(deviceId())}.json?_=${Date.now()}`;
        const r=await fetchTimed(url,{method:'GET',mode:'cors',cache:'no-store',headers:{'Cache-Control':'no-cache'}},3000);
        if(!r.ok)continue;
        const j=await r.json();
        if(!j||String(j.device_id||'')!==deviceId())continue;
        // A receipt belongs to the current login/session only. This prevents an old
        // Windows acknowledgement from making the UI look connected after a failed
        // re-registration.
        const receiptSession=String(j.login_session_id||'');
        if(receiptSession && receiptSession!==currentSession)continue;
        const ack=Number(j.ack_at_ms||j.updated_at_ms||0);
        if(!(ack>0))continue;
        localStorage.setItem(K.regReceipt,String(ack));
        localStorage.setItem('solico_web_registry_db_url',base);
        const d=normalizeDate(j.latest_report_date||'');
        const localPush=!!localStorage.getItem(K.pushSub);
        const pushConfirmed=!!j.push_registered;
        setStatus(`ثبت آیفون در ویندوز تأیید شد${localPush?(pushConfirmed?' • Push در ویندوز ثبت شد':' • Push منتظر ثبت در ویندوز'):''}${d?' • آخرین گزارش '+d:''}`);
        return true;
      }catch(_){ }
    }
    return false;
  }

  async function registerViaNtfyVerified(payload){
    try{
      await registry(payload);
      // Do not trust an opaque no-cors send. Verify the exact device_register
      // event is actually visible on the registry topic.
      for(let i=0;i<3;i++){
        await new Promise(r=>setTimeout(r,650));
        if(await pollRegistryForSelf('10m'))return true;
      }
    }catch(_){ }
    return false;
  }

  async function registerDeviceFirebase(payload){
    const body={...payload,secret:REG_SECRET,updated_at_ms:Date.now(),registry_backend:'firebase_rtdb'};
    const errors=[];
    const bases=await firebaseDbUrls();
    for(const base of bases){
      const url=`${base}/pwa_device_registry/${encodeURIComponent(deviceId())}.json?_=${Date.now()}`;
      try{
        const r=await fetchTimed(url,{method:'PUT',mode:'cors',cache:'no-store',headers:{'Content-Type':'application/json;charset=UTF-8'},body:JSON.stringify(body)},3000);
        if(!r.ok){errors.push(`${base} -> HTTP ${r.status}`);continue}
        // RTDB PUT returns the stored JSON. Parsing it confirms that the request was
        // accepted by the database instead of merely leaving Safari as an opaque send.
        let saved=null;try{saved=await r.json()}catch(_){saved=body}
        if(saved && String(saved.device_id||'')===deviceId()){
          localStorage.setItem('solico_web_registry_db_url',base);
          localStorage.setItem('solico_web_registry_diag',`Firebase OK: ${base}`);
          localStorage.setItem('solico_web_registry_last_ok_ms',String(Date.now()));
          return {ok:true,base};
        }
        errors.push(`${base} -> پاسخ ثبت معتبر نبود`);
      }catch(e){errors.push(`${base} -> ${shortErr(e)}`)}
    }
    const msg=errors.join(' | ').slice(0,1200);
    localStorage.setItem('solico_web_registry_diag',msg||'Firebase registry failed');
    throw new Error(msg||'Firebase registry failed');
  }

  let registerInFlight=null;
  async function registerDevice(reconnect=false,force=false){
    if(!isLoggedIn())return false;
    const now=Date.now(),last=Number(localStorage.getItem(K.lastReg)||0);
    if(!force && last && now-last<90*1000){
      // Registration and report sync are independent. Never block foreground sync
      // merely because an ACK has not yet arrived from Windows.
      pollRegistrationReceipt().catch(()=>{});
      pollFirebaseLatestReport().catch(()=>{});
      return true;
    }
    if(registerInFlight)return registerInFlight;
    registerInFlight=(async()=>{
      const payload={kind:'device_register',device_id:deviceId(),id_scheme:'web_pwa_v1',local_ip:'',name:deviceName(),android:`iOS ${iosVersion()}`,enabled:true,login_session_id:loginSession(),app_version:VERSION,platform:'ios_web',fcm_token:String(localStorage.getItem(K.pushToken)||''),webpush_subscription:(()=>{try{return JSON.parse(localStorage.getItem(K.pushSub)||'null')}catch(_){return null}})(),push_permission:('Notification' in window?Notification.permission:'unsupported'),push_key_id:String(localStorage.getItem(K.pushKeyId)||''),push_subscription_updated_at_ms:Number(localStorage.getItem('solico_web_push_updated_at_ms')||0),build:VERSION_BUILD,created_at_ms:Date.now()};
      setStatus(`در حال ارسال ثبت آیفون • Build ${VERSION_BUILD}...`);

      // Restore the stable R11 behaviour: registration itself never depends on
      // Service Worker/Web Push. Try deterministic RTDB first, then independent
      // append-only Firebase + ntfy fallbacks.
      let firebaseOk=false, ntfyOk=false, beaconOk=false;
      try{firebaseOk=!!(await registerDeviceFirebase(payload))}catch(e){console.warn('Firebase registry failed',e)}
      try{beaconOk=!!(await sendRegistrationEvent(payload))}catch(_){ }
      try{ntfyOk=!!(await promiseTimed(registry(payload),4200,'ntfy register'))}catch(_){ }
      if(reconnect){registry({kind:'device_reconnect',device_id:deviceId(),login_session_id:loginSession(),name:deviceName(),android:iosVersion(),app_version:VERSION,platform:'ios_web',build:VERSION_BUILD}).catch(()=>{});}

      if(!(firebaseOk||ntfyOk||beaconOk)){
        localStorage.removeItem(K.lastReg);
        setStatus(`ثبت آیفون ارسال نشد • Build ${VERSION_BUILD} • VPN/اینترنت را بررسی کنید`);
        return false;
      }
      localStorage.setItem(K.lastReg,String(Date.now()));
      setStatus(`ثبت آیفون ارسال شد • منتظر تازه‌سازی Windows • Build ${VERSION_BUILD}`);

      // Request ACK/latest report, but do NOT wait here. Waiting for ACK was the
      // regression that made the whole iPhone flow look frozen.
      registry({kind:'pwa_sync_request',device_id:deviceId(),login_session_id:loginSession(),name:deviceName(),app_version:VERSION,platform:'ios_web',build:VERSION_BUILD,created_at_ms:Date.now()}).catch(()=>{});
      setTimeout(()=>pollRegistrationReceipt().catch(()=>{}),600);
      setTimeout(()=>pollFirebaseLatestReport().catch(()=>{}),800);
      setTimeout(()=>syncControl('24h').catch(()=>{}),1200);
      return true;
    })();
    try{return await registerInFlight}finally{registerInFlight=null}
  }

  async function localNotify(title,body){
    try{
      if(!('Notification' in window)||Notification.permission!=='granted')return;
      const reg=await navigator.serviceWorker.ready;
      await reg.showNotification(title,{body,icon:'icon-192.png',badge:'icon-192.png',tag:'solico-report',data:{url:'./'}});
    }catch(e){}
  }

  async function handleMessage(msg,eventId){
    if(!msg||msg.secret!==REG_SECRET)return;
    if(msg.device_id && String(msg.device_id)!==deviceId())return;
    if(eventId)localStorage.setItem(K.lastNtfy,eventId);
    const kind=String(msg.kind||'');
    if(kind==='registration_ack'){
      if(String(msg.device_id||'')!==deviceId())return;
      const sess=String(msg.login_session_id||'');
      if(sess && sess!==loginSession())return;
      const ack=Number(msg.ack_at_ms||Date.now());
      localStorage.setItem(K.regReceipt,String(ack));
      const push=!!msg.push_registered;
      const d=normalizeDate(msg.latest_report_date||'');
      setStatus(`ثبت آیفون در Windows تأیید شد • ${push?'Push ثبت شد':'Push هنوز ثبت نشده'}${d?' • آخرین گزارش '+d:''} • Build ${VERSION_BUILD}`);
      return;
    }
    if(kind==='report_notification'){
      const r={...msg};delete r.kind;delete r.secret;delete r.device_id;
      upsertReport(r,true);return;
    }
    if(kind==='webpush_resubscribe'){
      // Windows received 404/410 from Apple Push Service. Re-create the
      // subscription on the next foreground sync and publish it back to Registry.
      try{localStorage.removeItem(K.pushKeyId);localStorage.removeItem(K.pushSub)}catch(_){ }
      standardWebPushReady=null;
      if('Notification' in window&&Notification.permission==='granted'){
        setTimeout(()=>setupWebPush(false),250);
        setStatus('اشتراک اعلان آیفون در حال نوسازی است...');
      }
      return;
    }
    if(kind==='device_access' && String(msg.enabled)==='0'){
      localStorage.removeItem(K.logged);localStorage.removeItem(K.session);setStatus('این دستگاه از ویندوز حذف شده است');location.replace('login.html');return;
    }
    if(kind==='archive_offer'){
      const rid=String(msg.request_id||''); if(!rid||localStorage.getItem(K.offer)===rid)return;
      localStorage.setItem(K.offer,rid);
      const count=Number(msg.report_count||0);
      await localNotify('درخواست دریافت آرشیو',`ویندوز درخواست ارسال ${count||''} گزارش قبلی را داده است. برنامه را باز کنید و تأیید کنید.`);
      const ok=confirm(`ویندوز درخواست ارسال ${count||''} گزارش قبلی را داده است. دریافت شود؟`);
      const decision={kind:ok?'history_accept':'history_reject',request_id:rid,device_id:deviceId(),name:deviceName(),app_version:VERSION,target_instance_id:String(msg.source_instance_id||''),transport:'pwa_firebase'};
      let fb=false;try{fb=await firebaseWriteDecision(decision)}catch(e){console.warn('Firebase archive decision failed',e)}
      try{await registry(decision)}catch(e){console.warn('ntfy archive decision fallback failed',e)}
      setStatus(ok?(fb?'دریافت آرشیو تأیید شد • Firebase':'دریافت آرشیو تأیید شد'):'دریافت آرشیو رد شد');
      return;
    }
    if(kind==='history_batch'){
      const arr=Array.isArray(msg.reports)?msg.reports:[];arr.forEach(r=>upsertReport(r,false));
      setStatus(`آرشیو در حال دریافت • ${arr.length} گزارش جدید`);return;
    }
    if(kind==='archive_item' && msg.report){upsertReport(msg.report,false);return;}
    if(kind==='history_complete'||kind==='archive_complete'){
      setStatus(`آرشیو ویندوز دریافت شد • ${Number(msg.count||0)} گزارش`);
      window.refreshLatestReport&&window.refreshLatestReport();
      window.refreshArchiveView&&window.refreshArchiveView();
      return;
    }
  }

  async function pollControl(sinceFallback='12h'){
    if(!isLoggedIn()||!navigator.onLine)return;
    try{
      const t=await controlTopic();
      const since=localStorage.getItem(K.lastNtfy)||sinceFallback;
      const url=`${NTFY_BASE}/${encodeURIComponent(t)}/json?poll=1&since=${encodeURIComponent(since)}`;
      const r=await fetch(url,{cache:'no-store',mode:'cors'});if(!r.ok)return;
      const raw=await r.text();
      for(const line of raw.split('\n')){
        if(!line.trim())continue;
        try{const e=JSON.parse(line);if(e.event!=='message')continue;const m=JSON.parse(e.message||'{}');await handleMessage(m,e.id)}catch(_){ }
      }
    }catch(e){}
  }

  async function pollFirebaseControl(){
    if(!isLoggedIn()||!navigator.onLine)return;
    try{
      const key=await firebaseControlKey();
      const seen=new Set(firebaseSeen());
      for(const base of await firebaseDbUrls()){
        try{
          const url=`${base}/pwa_control/${encodeURIComponent(key)}.json?_=${Date.now()}`;
          const r=await fetchTimed(url,{method:'GET',mode:'cors',cache:'no-store',headers:{'Cache-Control':'no-cache'}},3200);
          if(!r.ok)continue;
          const data=await r.json();if(!data||typeof data!=='object')continue;
          const rows=Object.entries(data).map(([id,msg])=>({id,msg})).sort((a,b)=>Number(a.msg&&a.msg.created_at_ms||0)-Number(b.msg&&b.msg.created_at_ms||0));
          for(const row of rows){
            if(seen.has(row.id))continue;
            const msg=row.msg||{};
            if(String(msg.secret||'')!==REG_SECRET||String(msg.device_id||'')!==deviceId()){rememberFirebaseSeen(row.id);continue}
            await handleMessage(msg,'fb:'+row.id);rememberFirebaseSeen(row.id);
          }
          return;
        }catch(e){console.warn('Firebase control poll failed',e)}
      }
    }catch(e){}
  }
  async function pollFirebaseLatestReport(){
    // R17: the newest report is also mirrored to a global durable snapshot. This
    // makes report catch-up independent of whether this phone happened to be in the
    // Windows device list at the exact moment the report was generated.
    if(!isLoggedIn()||!navigator.onLine)return false;
    try{
      const key=await firebaseControlKey();
      const gkey=await globalLatestKey();
      for(const base of await firebaseDbUrls()){
        for(const path of [`pwa_latest_broadcast/${encodeURIComponent(gkey)}`,`pwa_latest_reports/${encodeURIComponent(key)}`]){
          try{
            const url=`${base}/${path}.json?_=${Date.now()}`;
            const r=await fetchTimed(url,{method:'GET',mode:'cors',cache:'no-store',headers:{'Cache-Control':'no-cache'}},3000);
            if(!r.ok)continue;
            const msg=await r.json();
            if(!msg||typeof msg!=='object')continue;
            if(String(msg.secret||'')!==REG_SECRET||String(msg.kind||'')!=='report_notification')continue;
            if(msg.device_id && String(msg.device_id)!==deviceId())continue;
            localStorage.setItem('solico_web_registry_db_url',base);
            await handleMessage(msg,'');
            const d=normalizeDate(msg.report_date||'');
            if(d){
              const ack=Number(localStorage.getItem(K.regReceipt)||0);
              setStatus(`${ack?'ثبت آیفون در ویندوز تأیید شد':'گزارش ابری دریافت شد'} • آخرین گزارش ${d}`);
            }
            return true;
          }catch(e){console.warn('Firebase latest report poll failed',e)}
        }
      }
    }catch(e){ }
    return false;
  }

  async function pollFirebaseArchiveSnapshot(){
    if(!isLoggedIn()||!navigator.onLine)return false;
    const key=await firebaseControlKey();
    const last=localStorage.getItem(K.archiveSnapshot)||'';
    const errors=[];
    for(const base of await firebaseDbUrls()){
      try{
        const url=`${base}/pwa_archive_snapshots/${encodeURIComponent(key)}.json?_=${Date.now()}`;
        const r=await fetchTimed(url,{method:'GET',mode:'cors',cache:'no-store',headers:{'Cache-Control':'no-cache'}},3200);
        if(!r.ok){errors.push(`${base} HTTP ${r.status}`);continue}
        const snap=await r.json();
        if(!snap||typeof snap!=='object')continue;
        if(String(snap.secret||'')!==REG_SECRET||String(snap.device_id||'')!==deviceId())continue;
        const rid=String(snap.request_id||'');
        const arr=Array.isArray(snap.reports)?snap.reports:[];
        if(!rid||!arr.length)continue;
        // A durable snapshot is idempotent. Import again only when Windows created
        // a new approved archive request.
        if(rid===last)return true;
        const n=importArchiveReports(arr);
        if(n>0){
          localStorage.setItem(K.archiveSnapshot,rid);
          setStatus(`آرشیو ویندوز دریافت شد • ${n} گزارش`);
          window.refreshArchiveView&&window.refreshArchiveView();
          window.onArchiveSnapshotImported&&window.onArchiveSnapshotImported();
          await localNotify('آرشیو Solico',`${n} گزارش قبلی روی آیفون ذخیره شد.`);
          return true;
        }
      }catch(e){errors.push(String(e&&e.message||e))}
    }
    if(errors.length)console.warn('Firebase archive snapshot poll failed',errors.join(' | '));
    return false;
  }

  async function syncControl(since='12h'){await Promise.allSettled([pollFirebaseLatestReport(),pollFirebaseControl(),pollFirebaseArchiveSnapshot(),pollControl(since)])}

  let es=null;
  async function startStream(){
    try{
      if(es)es.close();
      const t=await controlTopic();
      const since=localStorage.getItem(K.lastNtfy)||'10m';
      es=new EventSource(`${NTFY_BASE}/${encodeURIComponent(t)}/sse?since=${encodeURIComponent(since)}`);
      es.onmessage=async ev=>{try{const e=JSON.parse(ev.data);if(e.event!=='message')return;const m=JSON.parse(e.message||'{}');await handleMessage(m,e.id)}catch(_){}};
      es.onerror=()=>{};
    }catch(e){}
  }

  let standardWebPushReady=null;
  function b64urlToBytes(v){
    let s=String(v||'').replace(/-/g,'+').replace(/_/g,'/');
    while(s.length%4)s+='=';
    const raw=atob(s), out=new Uint8Array(raw.length);
    for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);
    return out;
  }
  async function setupWebPush(interactive=false){
    if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)){
      localStorage.setItem('solico_web_push_diag','unsupported');
      return false;
    }
    if(Notification.permission==='default'&&interactive){
      const perm=await promiseTimed(Notification.requestPermission(),8000,'notification permission').catch(()=>Notification.permission);
      if(perm!=='granted'){localStorage.setItem('solico_web_push_diag','permission denied');return false}
    }
    if(Notification.permission!=='granted'){
      localStorage.setItem('solico_web_push_diag','permission not granted');
      return false;
    }
    if(standardWebPushReady)return standardWebPushReady;
    standardWebPushReady=(async()=>{
      try{
        const reg=await promiseTimed(navigator.serviceWorker.ready,6000,'service worker ready');
        let sub=await promiseTimed(reg.pushManager.getSubscription(),4500,'get push subscription');
        const oldKey=localStorage.getItem(K.pushKeyId)||'';
        if(sub && oldKey && oldKey!==STANDARD_WEB_PUSH_KEY_ID){
          try{await promiseTimed(sub.unsubscribe(),3500,'unsubscribe old push')}catch(_){ }
          sub=null;
        }
        if(!sub){
          sub=await promiseTimed(reg.pushManager.subscribe({
            userVisibleOnly:true,
            applicationServerKey:b64urlToBytes(STANDARD_WEB_PUSH_PUBLIC_KEY)
          }),9000,'push subscribe');
        }
        const j=sub&&sub.toJSON?sub.toJSON():(sub?{endpoint:sub.endpoint}:null);
        if(!j||!j.endpoint)throw new Error('Web Push subscription endpoint was empty');
        localStorage.setItem(K.pushSub,JSON.stringify(j));
        localStorage.setItem(K.pushKeyId,STANDARD_WEB_PUSH_KEY_ID);
        localStorage.setItem('solico_web_push_updated_at_ms',String(Date.now()));
        localStorage.setItem('solico_web_push_diag','ready');
        localStorage.removeItem(K.pushToken);
        // Publish the new subscription separately. Do not hold the repair button or
        // core device registration waiting for Windows acknowledgement.
        registerDevice(true,true).catch(()=>{});
        return true;
      }catch(e){
        standardWebPushReady=null;
        localStorage.setItem('solico_web_push_diag',shortErr(e));
        console.warn('Standard Web Push setup failed',e);
        return false;
      }
    })();
    return standardWebPushReady;
  }

  async function enableNotifications(){
    try{
      if(!('Notification' in window)){setStatus('این مرورگر اعلان وب را پشتیبانی نمی‌کند');return}
      const p=Notification.permission==='granted'?'granted':await Notification.requestPermission();
      if(p!=='granted'){setStatus('اجازه اعلان داده نشده است');return}
      await setupWebPush(true);
    }catch(e){setStatus('فعال‌سازی اعلان ناموفق بود')}
  }

  function latestReport(){
    const a=reports();if(!a.length)return null;
    // The dashboard must show the newest REPORT DATE, not whichever archive item
    // happened to be imported last. Jalali YYYY/MM/DD is lexicographically sortable.
    const daily=a.filter(r=>String(r.report_kind||'')==='daily'&&normalizeDate(r.report_date));
    const pool=daily.length?daily:a;
    return [...pool].sort((x,y)=>{
      const dx=normalizeDate(x.report_date||''),dy=normalizeDate(y.report_date||'');
      if(dx!==dy)return dy.localeCompare(dx,'en');
      return Number(y._received_at||0)-Number(x._received_at||0);
    })[0];
  }
  function historyForDate(d){d=normalizeDate(d);return reports().filter(r=>normalizeDate(r.report_date)===d)}
  function historyForMonth(m){m=normalizeDate(m);return reports().filter(r=>normalizeDate(r.report_date).startsWith(m+'/'))}

  // R11 migration: older archive snapshots were stored with Persian digits in
  // report_date (for example ۱۴۰۵/۰۷/۲۰), while the calendar asks for
  // 1405/07/20. Normalize the already-downloaded local archive in place so the
  // user does not have to request/send the archive again.
  try{
    const migrated=reports();
    if(migrated.length)saveReports(migrated);
  }catch(_){ }

  window.AndroidBridge={
    getLatestReportJson:()=>JSON.stringify(latestReport()),
    getCurrentUsername:()=>isLoggedIn()?'admin':'',
    getFcmStatus:()=>('Notification' in window?(Notification.permission==='granted'?(localStorage.getItem(K.pushSub)?'اعلان پس‌زمینه فعال • Web Push':'اعلان فعال • برای حالت بسته «ترمیم اعلان‌ها» را بزنید'):'اعلان مرورگر غیرفعال'):'اعلان وب پشتیبانی نمی‌شود'),
    getDeviceStatus:()=>localStorage.getItem(K.status)||'در حال اتصال آیفون...',
    getConnectionStatus:()=>navigator.onLine?'اینترنت متصل است':'اینترنت قطع است',
    getNotificationHistoryJson:(d)=>JSON.stringify(historyForDate(d)),
    getMonthlyHistoryJson:(m)=>JSON.stringify(historyForMonth(m)),
    getArchiveDatesJson:()=>JSON.stringify([...new Set(reports().filter(r=>String(r.report_kind||'')==='daily').map(r=>normalizeDate(r.report_date)).filter(Boolean))]),
    getComparisonJson:()=>JSON.stringify([]),
    openLatestReport:()=>{},
    openArchivedReport:()=>{},
    repairNotifications:()=>{localStorage.removeItem(K.lastReg);setStatus(`در حال ترمیم اتصال آیفون • Build ${VERSION_BUILD}`);registerDevice(true,true).catch(()=>{});pollFirebaseLatestReport().catch(()=>{});syncControl('24h').catch(()=>{});setTimeout(()=>setupWebPush(true).catch(()=>{}),250);return true;},
    registerDeviceNow:()=>registerDevice(true,true),
    syncHistoryNow:()=>syncControl('24h'),
    getAppVersion:()=>`${VERSION} • Build ${VERSION_BUILD}`,
    checkForUpdates:async()=>{try{const r=await navigator.serviceWorker.getRegistration();if(r)await r.update();setTimeout(()=>location.reload(),800)}catch(e){}},
    chooseUpdateApk:()=>{},
    logout:()=>{localStorage.removeItem(K.logged);localStorage.removeItem(K.session);location.replace('login.html')}
  };

  async function hardRefreshTo(version){
    try{
      if('serviceWorker' in navigator){
    navigator.serviceWorker.addEventListener('message',ev=>{
      if(ev.data&&ev.data.type==='SOLICO_PUSH_SUBSCRIPTION_CHANGED'){
        try{localStorage.removeItem(K.pushKeyId);localStorage.removeItem(K.pushSub)}catch(_){ }
        standardWebPushReady=null;
        if('Notification' in window&&Notification.permission==='granted')setTimeout(()=>setupWebPush(false),200);
      }
    });
        const regs=await navigator.serviceWorker.getRegistrations();
        await Promise.all(regs.map(r=>r.unregister().catch(()=>false)));
      }
      if('caches' in window){
        const keys=await caches.keys();
        await Promise.all(keys.filter(k=>k.startsWith('solico-iphone-pwa-')).map(k=>caches.delete(k)));
      }
    }catch(_){ }
    const u=new URL(location.href);u.searchParams.set('v',String(version||Date.now()));u.searchParams.set('_',String(Date.now()));
    location.replace(u.toString());
  }

  async function checkServerVersion(manual=false){
    try{
      const r=await fetch(`version.json?_=${Date.now()}`,{cache:'no-store',headers:{'Cache-Control':'no-cache'}});
      if(!r.ok)throw new Error('version http '+r.status);
      const v=await r.json();
      const remote=Number(v.build||0);
      if(remote>VERSION_BUILD){
        setStatus(`نسخه جدید ${v.version||''} پیدا شد • در حال بروزرسانی...`);
        await hardRefreshTo(remote);
        return true;
      }
      if(manual)setStatus(`برنامه بروز است • ${VERSION}`);
      return false;
    }catch(e){
      if(manual)setStatus('بررسی بروزرسانی ناموفق بود • اینترنت/VPN را بررسی کنید');
      return false;
    }
  }

  // Expose the reliable version check to the existing UI button.
  window.AndroidBridge.checkForUpdates=()=>checkServerVersion(true);

  if('serviceWorker' in navigator){
    let swReloading=false;
    window.addEventListener('load',async()=>{
      // version.json is never cached; this is the primary auto-update mechanism.
      const updated=await checkServerVersion(false); if(updated)return;
      try{
        const reg=await navigator.serviceWorker.register('sw.js',{scope:'./',updateViaCache:'none'});
        await reg.update();
        if('Notification' in window&&Notification.permission==='granted')setTimeout(()=>setupWebPush(false),500);
      }catch(_){ }
    });
    navigator.serviceWorker.addEventListener('controllerchange',()=>{
      if(swReloading)return;
      const k='solico_sw_reload_'+VERSION_BUILD;
      if(sessionStorage.getItem(k)==='1')return;
      sessionStorage.setItem(k,'1');swReloading=true;location.reload();
    });
  }else{
    window.addEventListener('load',()=>checkServerVersion(false));
  }


  // R17 one-time recovery: keep identity, login, archive and Push subscription.
  // Only clear throttling/state that can hide a new registration attempt.
  try{
    const mk='solico_web_r22_core_recovery';
    if(localStorage.getItem(mk)!=='1'){
      localStorage.removeItem(K.lastReg);
      localStorage.removeItem(K.regReceipt);
      localStorage.setItem(K.status,`Build ${VERSION_BUILD} فعال شد • اتصال پایدار در حال بازیابی است...`);
      localStorage.setItem(mk,'1');
    }
  }catch(_){ }
  window.addEventListener('online',()=>{checkServerVersion(false);registerDevice(false,true);pollRegistrationReceipt();pollFirebaseLatestReport();syncControl('12h');startStream();if('Notification' in window&&Notification.permission==='granted')setupWebPush(false)});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){checkServerVersion(false);registerDevice(false,false);pollRegistrationReceipt();pollFirebaseLatestReport();syncControl('12h');startStream();if('Notification' in window&&Notification.permission==='granted')setupWebPush(false)}});
  setTimeout(()=>registerDevice(false,true),250);
  setTimeout(()=>registerDevice(false,true),6500);
  setTimeout(()=>registerDevice(false,true),16000);
  setTimeout(()=>pollRegistrationReceipt(),700);
  setTimeout(()=>pollFirebaseLatestReport(),450);
  setTimeout(()=>syncControl('24h'),3000);
  setTimeout(()=>startStream(),3200);
  setInterval(()=>{if(!document.hidden)pollRegistrationReceipt()},15000);
  setInterval(()=>{if(!document.hidden)pollFirebaseLatestReport()},20000);
  setInterval(()=>{if(!document.hidden)syncControl('24h')},45000);
  setInterval(()=>{if(!document.hidden)registerDevice(false,false)},10*60*1000);
  setInterval(()=>{if(!document.hidden)checkServerVersion(false)},60*1000);
})();
