"""SQLite storage with de-duplication, price-history, favorites and trends.

Tables
------
listings       one row per car (deduped by site+url hash). Tracks first/last
               seen, active flag, "seen by user" flag, and favorite state.
price_history  one row every time a listing's price changes (and on first
               insert). Powers the per-car trend arrow, sparkline and the
               full price chart.
model_trends   one snapshot per scan per (make, model): median/min/max price
               and count of active listings. Powers the market-trend screen.
runs           one row per scan (for "last scan" info).
"""
from __future__ import annotations

import os
import sqlite3
import statistics
import time
from pathlib import Path

# Override with env var DB_PATH in the cloud to point at a persistent volume,
# e.g. DB_PATH=/app/data/listings.db
DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "listings.db"))


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(c, table: str) -> set:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id         TEXT PRIMARY KEY,
                site       TEXT,
                url        TEXT,
                make       TEXT,
                model      TEXT,
                year       INTEGER,
                km         INTEGER,
                price      INTEGER,
                hand       INTEGER,
                gearbox    TEXT,
                fuel       TEXT,
                title      TEXT,
                location   TEXT,
                image      TEXT,
                first_seen REAL,
                last_seen  REAL,
                is_active  INTEGER DEFAULT 1,
                seen_by_user INTEGER DEFAULT 0,
                favorite     INTEGER DEFAULT 0,
                favorite_price INTEGER,
                favorited_at REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT,
                price      INTEGER,
                ts         REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_ph_listing ON price_history(listing_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_trends (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     REAL,
                make   TEXT,
                model  TEXT,
                count  INTEGER,
                median_price INTEGER,
                min_price INTEGER,
                max_price INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started REAL, finished REAL,
                total INTEGER, new INTEGER, note TEXT
            )
        """)
        # Per-site outcome of each scan, so the UI can show "last successful
        # scan" and surface silently-failing sites.
        c.execute("""
            CREATE TABLE IF NOT EXISTS site_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, site TEXT, ok INTEGER, count INTEGER, error TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_sr_site ON site_runs(site)")
        # --- migrate older DBs: add columns that post-date the first schema ---
        have = _columns(c, "listings")
        for col, ddl in [
            ("favorite", "favorite INTEGER DEFAULT 0"),
            ("favorite_price", "favorite_price INTEGER"),
            ("favorited_at", "favorited_at REAL"),
            ("notes", "notes TEXT"),
            ("phone", "phone TEXT"),
            ("seller_type", "seller_type TEXT"),
        ]:
            if col not in have:
                c.execute(f"ALTER TABLE listings ADD COLUMN {ddl}")


def upsert_listings(rows: list[dict]) -> int:
    """Insert new listings, refresh existing ones, and record price changes.

    Returns the count of NEW listings (used for the "new" badge).
    """
    now = time.time()
    new_count = 0
    with _conn() as c:
        for r in rows:
            lid = r["id"]
            price = r.get("price")
            existing = c.execute(
                "SELECT price FROM listings WHERE id = ?", (lid,)
            ).fetchone()
            if existing:
                old_price = existing["price"]
                c.execute(
                    "UPDATE listings SET last_seen=?, is_active=1, price=?, km=?, "
                    "image=?, title=?, year=?, hand=?, gearbox=?, fuel=?, location=?, "
                    "notes=?, phone=?, seller_type=? WHERE id=?",
                    (now, price, r.get("km"), r.get("image"), r.get("title"), r.get("year"),
                     r.get("hand"), r.get("gearbox"), r.get("fuel"), r.get("location"),
                     r.get("notes"), r.get("phone"), r.get("seller_type"), lid),
                )
                # Record a history point only when the price actually changed.
                if price is not None and price != old_price:
                    c.execute(
                        "INSERT INTO price_history (listing_id, price, ts) VALUES (?,?,?)",
                        (lid, price, now),
                    )
            else:
                new_count += 1
                c.execute("""
                    INSERT INTO listings
                      (id, site, url, make, model, year, km, price, hand,
                       gearbox, fuel, title, location, image, notes, phone, seller_type,
                       first_seen, last_seen, is_active, seen_by_user)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)
                """, (
                    lid, r.get("site"), r.get("url"), r.get("make"),
                    r.get("model"), r.get("year"), r.get("km"), price,
                    r.get("hand"), r.get("gearbox"), r.get("fuel"),
                    r.get("title"), r.get("location"), r.get("image"),
                    r.get("notes"), r.get("phone"), r.get("seller_type"),
                    now, now,
                ))
                if price is not None:
                    c.execute(
                        "INSERT INTO price_history (listing_id, price, ts) VALUES (?,?,?)",
                        (lid, price, now),
                    )
    return new_count


def mark_inactive_except(active_ids: list[str]):
    """Listings not seen in the latest scan are marked inactive (sold/removed)."""
    if not active_ids:
        return
    placeholders = ",".join("?" * len(active_ids))
    with _conn() as c:
        c.execute(
            f"UPDATE listings SET is_active=0 WHERE id NOT IN ({placeholders})",
            active_ids,
        )


def record_model_snapshots(rows: list[dict], ts: float | None = None):
    """Snapshot per-(make,model) aggregates for the market-trend screen."""
    ts = ts or time.time()
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        p = r.get("price")
        if p is None:
            continue
        key = ((r.get("make") or "").strip(), (r.get("model") or "").strip())
        groups.setdefault(key, []).append(int(p))
    with _conn() as c:
        for (make, model), prices in groups.items():
            if not prices:
                continue
            c.execute(
                "INSERT INTO model_trends (ts, make, model, count, median_price, min_price, max_price) "
                "VALUES (?,?,?,?,?,?,?)",
                (ts, make, model, len(prices), int(statistics.median(prices)),
                 min(prices), max(prices)),
            )


def record_run(started: float, total: int, new: int, note: str = ""):
    with _conn() as c:
        c.execute(
            "INSERT INTO runs (started, finished, total, new, note) VALUES (?,?,?,?,?)",
            (started, time.time(), total, new, note),
        )


def record_site_run(site: str, ok: bool, count: int, error: str = "", ts: float | None = None):
    ts = ts or time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO site_runs (ts, site, ok, count, error) VALUES (?,?,?,?,?)",
            (ts, site, 1 if ok else 0, count, (error or "")[:300]),
        )


def get_site_status() -> dict:
    """Per-site: latest run + last time the site actually returned listings."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM site_runs ORDER BY ts ASC").fetchall()
    out: dict = {}
    for r in rows:
        s = r["site"]
        out.setdefault(s, {"last_success": None})
        out[s].update({"ts": r["ts"], "ok": bool(r["ok"]), "count": r["count"], "error": r["error"]})
        if r["count"] and r["count"] > 0:
            out[s]["last_success"] = r["ts"]
    return out


def _history_for(c, listing_id: str) -> list[dict]:
    return [
        {"ts": r["ts"], "price": r["price"]}
        for r in c.execute(
            "SELECT price, ts FROM price_history WHERE listing_id=? ORDER BY ts ASC",
            (listing_id,),
        ).fetchall()
    ]


def _enrich(row: dict, history: list[dict]) -> dict:
    """Add derived price-trend fields to a listing row."""
    prices = [h["price"] for h in history if h["price"] is not None]
    row["history"] = history
    row["favorite"] = bool(row.get("favorite"))
    first_price = prices[0] if prices else row.get("price")
    prev_price = prices[-2] if len(prices) >= 2 else None
    cur = row.get("price")
    row["first_price"] = first_price
    row["prev_price"] = prev_price
    # Trend vs the previous distinct price (drives the arrow on the card).
    row["price_change_last"] = (cur - prev_price) if (cur is not None and prev_price is not None) else 0
    # Total change since first seen.
    row["price_change_total"] = (cur - first_price) if (cur is not None and first_price is not None) else 0
    row["price_dropped"] = row["price_change_total"] < 0
    # Drop since the car was favorited (drives the favorites screen).
    fav_ref = row.get("favorite_price")
    if row["favorite"] and fav_ref and cur is not None:
        row["fav_price_change"] = cur - fav_ref
        row["fav_dropped"] = cur < fav_ref
    else:
        row["fav_price_change"] = 0
        row["fav_dropped"] = False
    return row


def get_listings(
    active_only: bool = True,
    site: str | None = None,
    favorite_only: bool = False,
    price_drop_only: bool = False,
) -> list[dict]:
    q = "SELECT * FROM listings"
    conds, args = [], []
    if active_only:
        conds.append("is_active = 1")
    if site:
        conds.append("site = ?")
        args.append(site)
    if favorite_only:
        conds.append("favorite = 1")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY first_seen DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
        out = [_enrich(r, _history_for(c, r["id"])) for r in rows]
    if price_drop_only:
        out = [r for r in out if r["price_dropped"]]
    return out


def get_history(listing_id: str) -> list[dict]:
    with _conn() as c:
        return _history_for(c, listing_id)


def set_favorite(listing_id: str, favorite: bool):
    now = time.time()
    with _conn() as c:
        if favorite:
            cur = c.execute(
                "SELECT price FROM listings WHERE id=?", (listing_id,)
            ).fetchone()
            price = cur["price"] if cur else None
            c.execute(
                "UPDATE listings SET favorite=1, favorite_price=?, favorited_at=? WHERE id=?",
                (price, now, listing_id),
            )
        else:
            c.execute(
                "UPDATE listings SET favorite=0 WHERE id=?", (listing_id,)
            )


def get_trends(make: str | None = None, model: str | None = None,
               days: int | None = None) -> dict:
    """Return market-trend series grouped by (make, model).

    Each series: [{ts, count, median_price, min_price, max_price}, ...] sorted
    by time. Optionally restrict to one make/model and/or the last N days.
    """
    q = "SELECT * FROM model_trends"
    conds, args = [], []
    if make:
        conds.append("make = ?"); args.append(make)
    if model:
        conds.append("model = ?"); args.append(model)
    if days:
        conds.append("ts >= ?"); args.append(time.time() - days * 86400)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts ASC"
    series: dict[str, list[dict]] = {}
    with _conn() as c:
        for r in c.execute(q, args).fetchall():
            key = f"{r['make']} {r['model']}".strip()
            series.setdefault(key, []).append({
                "ts": r["ts"], "count": r["count"],
                "median_price": r["median_price"],
                "min_price": r["min_price"], "max_price": r["max_price"],
            })
    return series


def mark_all_seen():
    with _conn() as c:
        c.execute("UPDATE listings SET seen_by_user = 1")


def stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM listings WHERE is_active=1").fetchone()[0]
        unseen = c.execute(
            "SELECT COUNT(*) FROM listings WHERE is_active=1 AND seen_by_user=0"
        ).fetchone()[0]
        favorites = c.execute(
            "SELECT COUNT(*) FROM listings WHERE is_active=1 AND favorite=1"
        ).fetchone()[0]
        last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    # Price drops (since first seen) among active listings.
    drops = sum(1 for r in get_listings(active_only=True) if r["price_dropped"])
    return {
        "total": total,
        "new_unseen": unseen,
        "favorites": favorites,
        "price_drops": drops,
        "last_run": dict(last) if last else None,
    }
