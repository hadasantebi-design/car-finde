"""Build the static data bundle for GitHub Pages.

Runs a scan (unless --no-scan), then writes everything the static dashboard
needs into docs/data.json and copies the dashboard into docs/index.html.

The SQLite DB is kept (and committed by the GitHub Action) so price history
accumulates across daily runs.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import engine
import storage
from scrapers import all_site_labels

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"


def build(scan: bool = True):
    storage.init_db()
    if scan:
        summary = engine.run_scan()
        print("scan:", summary)

    data = {
        "generated_at": time.time(),
        "config": engine.load_config(),
        "site_labels": all_site_labels(),
        # include inactive too, so favorited-but-removed cars still render
        "listings": storage.get_listings(active_only=False),
        "stats": storage.stats(),
        "trends": storage.get_trends(),
        "site_status": storage.get_site_status(),
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    # The dashboard is one file; ship the same one to Pages.
    shutil.copyfile(ROOT / "static" / "index.html", DOCS / "index.html")
    # Pages must not run Jekyll (it would ignore files); mark as plain static.
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    _write_pwa()


def _write_pwa():
    """Write the PWA assets (manifest, icon, service worker) into docs/."""
    icon = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>"
            "<rect width='512' height='512' rx='96' fill='#EC4899'/>"
            "<text x='50%' y='54%' font-size='300' text-anchor='middle' "
            "dominant-baseline='central'>\U0001F697</text></svg>")
    (DOCS / "icon.svg").write_text(icon, encoding="utf-8")
    manifest = {
        "name": "גולי תמצא לי רכב", "short_name": "גולי רכב",
        "start_url": ".", "scope": ".", "display": "standalone",
        "background_color": "#091A23", "theme_color": "#EC4899", "dir": "rtl", "lang": "he",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    }
    (DOCS / "manifest.webmanifest").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    sw = (
        "const C='cf-v1';\n"
        "self.addEventListener('install',e=>{self.skipWaiting();});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(clients.claim());});\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);\n"
        " if(e.request.method!=='GET'){return;}\n"
        " if(u.pathname.endsWith('data.json')){\n"
        "  e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(C).then(x=>x.put(e.request,c));return r;}).catch(()=>caches.match(e.request)));return;}\n"
        " e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(res=>{const c=res.clone();caches.open(C).then(x=>x.put(e.request,c));return res;}).catch(()=>caches.match('index.html'))));\n"
        "});\n"
    )
    (DOCS / "sw.js").write_text(sw, encoding="utf-8")
    print(f"wrote {DOCS/'data.json'} ({len(data['listings'])} listings) and index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scan", action="store_true", help="export current DB without scanning")
    args = ap.parse_args()
    build(scan=not args.no_scan)
