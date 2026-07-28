const C='cf-v1';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(clients.claim());});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(e.request.method!=='GET'){return;}
 if(u.pathname.endsWith('data.json')){
  e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(C).then(x=>x.put(e.request,c));return r;}).catch(()=>caches.match(e.request)));return;}
 e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(res=>{const c=res.clone();caches.open(C).then(x=>x.put(e.request,c));return res;}).catch(()=>caches.match('index.html'))));
});
