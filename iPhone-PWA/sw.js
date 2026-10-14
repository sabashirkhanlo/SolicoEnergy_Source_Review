const CACHE='solico-iphone-pwa-v1.0.18-b22';
const ASSETS=['./','index.html','login.html','style.css','app.js','web-bridge.js','manifest.webmanifest','app_icon.png','icon-192.png','icon-512.png'];

// iPhone/iPad Home Screen apps use the browser-standard Push API.
// The Windows sender deliberately sends an empty Web Push payload, so no
// encryption library is needed server-side; the report itself is already
// mirrored through Firebase and appears in the app when opened.
self.addEventListener('push',e=>{
  let title='SolicoEnergy';
  let body='گزارش جدید نیروگاه آماده است.';
  try{
    if(e.data){
      const d=e.data.json();
      if(d&&d.title)title=String(d.title);
      if(d&&d.body)body=String(d.body);
    }
  }catch(_){ }
  e.waitUntil(self.registration.showNotification(title,{
    body,
    icon:'icon-192.png',
    badge:'icon-192.png',
    tag:'solico-report',
    renotify:true,
    data:{url:'./'}
  }));
});

self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;const u=new URL(e.request.url);if(u.origin!==location.origin)return;e.respondWith(fetch(e.request).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));return r}).catch(()=>caches.match(e.request).then(r=>r||caches.match('./'))))});
self.addEventListener('notificationclick',e=>{
  e.notification.close();
  const target=(e.notification&&e.notification.data&&e.notification.data.url)||'./';
  e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ws=>{
    for(const w of ws){if('focus'in w){try{w.navigate(target)}catch(_){ }return w.focus()}}
    return clients.openWindow(target);
  }));
});
self.addEventListener('message',e=>{if(e.data&&e.data.type==='SKIP_WAITING')self.skipWaiting()});

self.addEventListener('pushsubscriptionchange',event=>{
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ws=>Promise.all(ws.map(w=>w.postMessage({type:'SOLICO_PUSH_SUBSCRIPTION_CHANGED'})))));
});
