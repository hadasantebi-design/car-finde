"""Official vehicle enrichment from data.gov.il (Israel MoT open data).

Given a license-plate number, returns authoritative facts the dealer listings
don't reliably show: ownership type (private / rental / leasing / company),
first on-road date, test (roadworthiness) validity, and fuel type. Best-effort
and cached; any failure returns None so the scan is never blocked.

NOTE: data.gov.il may rate-limit or geo-restrict; from a non-Israeli IP (e.g.
GitHub Actions) it might return nothing — the caller must degrade gracefully.
"""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger("scrapers.govdata")

# "רכב פרטי ומסחרי" resource on data.gov.il (CKAN datastore).
RESOURCE_ID = "053cea08-09bc-40ec-8f7a-156f0677aff3"
API = "https://data.gov.il/api/3/action/datastore_search"

_cache: dict[str, dict | None] = {}


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def lookup(plate) -> dict | None:
    """Return official facts for a plate, or None. Cached per plate."""
    p = _digits(plate)
    if len(p) < 5:
        return None
    if p in _cache:
        return _cache[p]
    data = None
    try:
        r = requests.get(
            API, params={"resource_id": RESOURCE_ID, "q": p, "limit": 3},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
        )
        recs = (r.json().get("result") or {}).get("records") or []
        rec = next((x for x in recs if _digits(x.get("mispar_rechev")) == p), None)
        if rec:
            data = {
                "ownership": rec.get("baalut"),
                "on_road": rec.get("moed_aliya_lakvish"),
                "test_valid": rec.get("tokef_dt"),
                "fuel": rec.get("sug_delek_nm"),
                "year": rec.get("shnat_yitzur"),
            }
    except Exception as e:
        log.warning("gov lookup failed for %s: %s", p, e)
    _cache[p] = data
    return data


def official_note(plate) -> str:
    """A compact Hebrew summary suitable for the listing's notes field, or ''."""
    d = lookup(plate)
    if not d:
        return ""
    parts = []
    if d.get("ownership"):
        parts.append(f"בעלות: {d['ownership']}")
    if d.get("on_road"):
        parts.append(f"עלה לכביש: {d['on_road']}")
    if d.get("test_valid"):
        parts.append(f"טסט עד: {d['test_valid']}")
    return " · ".join(parts)
