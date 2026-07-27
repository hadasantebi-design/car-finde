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
    print(f"wrote {DOCS/'data.json'} ({len(data['listings'])} listings) and index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scan", action="store_true", help="export current DB without scanning")
    args = ap.parse_args()
    build(scan=not args.no_scan)
