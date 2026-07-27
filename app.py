"""Car Finder – web app.

Run locally:   uvicorn app:app --reload --port 8000
Then open:     http://localhost:8000

The daily scan runs automatically via APScheduler using the cron in config.yaml.
Everything (models, filters, sites, schedule) is editable from the UI or config.
"""
from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import engine
import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI(title="Car Finder")
scheduler = BackgroundScheduler()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    storage.init_db()
    _reschedule()
    cfg = engine.load_config()
    if cfg.get("schedule", {}).get("run_on_startup"):
        _safe_scan()
    scheduler.start()
    log.info("Car Finder started.")


@app.on_event("shutdown")
def _shutdown():
    scheduler.shutdown(wait=False)


def _safe_scan():
    try:
        return engine.run_scan()
    except Exception as e:
        log.exception("scheduled scan failed: %s", e)
        return {"error": str(e)}


def _reschedule():
    cfg = engine.load_config()
    sch = cfg.get("schedule", {})
    cron = sch.get("cron", "0 8 * * *")
    tz = sch.get("timezone", "Asia/Jerusalem")
    scheduler.remove_all_jobs()
    scheduler.add_job(
        _safe_scan,
        CronTrigger.from_crontab(cron, timezone=tz),
        id="daily_scan",
        replace_existing=True,
    )
    log.info("daily scan scheduled: cron='%s' tz=%s", cron, tz)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/listings")
def api_listings(
    active_only: bool = True,
    site: str | None = None,
    favorite_only: bool = False,
    price_drop_only: bool = False,
):
    return {
        "listings": storage.get_listings(active_only, site, favorite_only, price_drop_only),
        "stats": storage.stats(),
    }


@app.get("/api/history/{listing_id}")
def api_history(listing_id: str):
    return {"history": storage.get_history(listing_id)}


@app.get("/api/trends")
def api_trends(make: str | None = None, model: str | None = None, days: int | None = None):
    return {"trends": storage.get_trends(make, model, days)}


class FavoriteIn(BaseModel):
    id: str
    favorite: bool = True


@app.post("/api/favorite")
def api_favorite(body: FavoriteIn):
    storage.set_favorite(body.id, body.favorite)
    return {"ok": True}


@app.get("/api/config")
def api_get_config():
    from scrapers import all_site_labels
    return {"config": engine.load_config(), "site_labels": all_site_labels()}


class ConfigIn(BaseModel):
    config: dict


@app.post("/api/config")
def api_set_config(body: ConfigIn):
    engine.save_config(body.config)
    _reschedule()
    return {"ok": True}


@app.post("/api/scan")
def api_scan():
    return _safe_scan()


@app.post("/api/mark-seen")
def api_mark_seen():
    storage.mark_all_seen()
    return {"ok": True}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
