"""Scan engine: load config, run every enabled scraper, store results."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

import storage
from scrapers import get_scraper
from scrapers.base import SearchSpec

log = logging.getLogger("engine")
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def run_scan() -> dict:
    """Run one full scan across all enabled sites/searches. Returns a summary."""
    cfg = load_config()
    filters = cfg.get("filters", {})
    searches = cfg.get("searches", [])
    sites = cfg.get("sites", [])
    started = time.time()

    all_rows: list[dict] = []
    active_ids: list[str] = []
    per_site: dict[str, int] = {}

    for site in sites:
        scraper = get_scraper(site)
        if not scraper:
            log.warning("unknown site in config: %s", site)
            continue
        site_count = 0
        for s in searches:
            spec = SearchSpec(s["make"], s["model"], s.get("fuel", "any"), filters)
            try:
                results = scraper.search(spec)
            except Exception as e:
                log.exception("%s failed on %s: %s", site, spec, e)
                results = []
            for lst in results:
                row = lst.to_row()
                all_rows.append(row)
                active_ids.append(row["id"])
                site_count += 1
        per_site[site] = site_count
        log.info("%s -> %d listings", site, site_count)

    new_count = storage.upsert_listings(all_rows)
    storage.mark_inactive_except(active_ids)
    storage.record_model_snapshots(all_rows, ts=started)
    note = ", ".join(f"{k}:{v}" for k, v in per_site.items())
    storage.record_run(started, len(all_rows), new_count, note)

    summary = {
        "total": len(all_rows),
        "new": new_count,
        "per_site": per_site,
        "duration_sec": round(time.time() - started, 1),
    }
    log.info("scan done: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    storage.init_db()
    print(run_scan())
