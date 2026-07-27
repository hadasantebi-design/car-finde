"""Yad2 scraper.

Yad2 serves its car search through an internal JSON API. This scraper hits
that API and normalizes the results into ``Listing`` objects.

IMPORTANT / HONEST NOTES
------------------------
* Yad2 changes its API + HTML often and runs bot protection. Treat this as a
  best-effort scraper: if it stops returning results, the API shape probably
  changed and the parsing in ``_parse_item`` needs a small update.
* Yad2's Terms of Service restrict automated scraping. Use responsibly, keep
  the daily frequency low, and don't hammer the site.
* Manufacturer / model IDs below must match Yad2's own IDs. The three you
  asked for are pre-filled but SHOULD BE VERIFIED once against a live search
  URL (open Yad2, filter to the model, and read the ?manufacturer= & ?model=
  values from the address bar). Update the maps below if they differ.
"""
from __future__ import annotations

import logging

from .base import BaseScraper, Listing, SearchSpec

log = logging.getLogger("scrapers.yad2")

# Yad2 internal IDs. VERIFY THESE against live URLs; update as needed.
MANUFACTURER_IDS = {
    "kia": 48,
    "hyundai": 21,
    "toyota": 19,
}
MODEL_IDS = {
    ("kia", "niro"): 10298,
    ("hyundai", "elantra"): 10226,
    ("toyota", "corolla"): 10225,
}
# Yad2 engine/fuel codes (engineType): 1 gasoline, 2 diesel, 3 electric,
# 4 hybrid, 5 plugin hybrid, 6 gas. VERIFY as above.
FUEL_CODES = {
    "gasoline": 1,
    "diesel": 2,
    "electric": 3,
    "hybrid": 4,
    "plugin_hybrid": 5,
}

API = "https://gw.yad2.co.il/feed-search-legacy/vehicles/cars"


class Yad2Scraper(BaseScraper):
    name = "yad2"
    label = "יד2"

    def search(self, spec: SearchSpec) -> list[Listing]:
        make = spec.make.lower()
        model = spec.model.lower()
        f = spec.filters

        params: dict = {"page": 1, "forceLdLoad": "true"}
        if make in MANUFACTURER_IDS:
            params["manufacturer"] = MANUFACTURER_IDS[make]
        if (make, model) in MODEL_IDS:
            params["model"] = MODEL_IDS[(make, model)]
        if spec.fuel in FUEL_CODES:
            params["engineType"] = FUEL_CODES[spec.fuel]

        # Year / km / price ranges use Yad2's "min-max" query style.
        if f.get("year_min") or f.get("year_max"):
            params["year"] = f"{f.get('year_min') or ''}-{f.get('year_max') or ''}"
        if f.get("km_max") or f.get("km_min"):
            params["km"] = f"{f.get('km_min') or 0}-{f.get('km_max') or ''}"
        if f.get("price_max") or f.get("price_min"):
            params["price"] = f"{f.get('price_min') or 0}-{f.get('price_max') or ''}"
        if f.get("hand_max"):
            params["hand"] = f"0-{f['hand_max']}"

        listings: list[Listing] = []
        try:
            for page in range(1, 4):  # scan up to 3 pages
                params["page"] = page
                resp = self.get(API, params=params)
                if resp.status_code != 200:
                    log.warning("yad2 returned %s for %s %s", resp.status_code, make, model)
                    break
                data = resp.json()
                items = (
                    data.get("data", {}).get("feed", {}).get("feed_items")
                    or data.get("feed", {}).get("feed_items")
                    or []
                )
                items = [it for it in items if it.get("type") == "ad" or it.get("id")]
                if not items:
                    break
                for it in items:
                    lst = self._parse_item(it, spec)
                    if lst and self.passes_filters(lst, f):
                        listings.append(lst)
                if len(items) < 20:
                    break
        except Exception as e:  # never let one site crash the whole scan
            log.exception("yad2 scrape failed for %s %s: %s", make, model, e)

        return listings

    def _parse_item(self, it: dict, spec: SearchSpec) -> Listing | None:
        try:
            link_id = it.get("link_token") or it.get("id")
            url = f"https://www.yad2.co.il/vehicles/item/{link_id}" if link_id else ""
            row = it.get("row_1") or it.get("title_1") or ""
            price = it.get("price")
            if isinstance(price, str):
                price = int("".join(ch for ch in price if ch.isdigit()) or 0) or None

            def _num(v):
                if v is None:
                    return None
                try:
                    return int("".join(ch for ch in str(v) if ch.isdigit()))
                except ValueError:
                    return None

            return Listing(
                site=self.name,
                url=url,
                make=spec.make,
                model=spec.model,
                year=_num(it.get("year")),
                km=_num(it.get("kilometers") or it.get("km")),
                price=price,
                hand=_num(it.get("hand") or it.get("hand_num")),
                gearbox=str(it.get("gearBox") or it.get("gear") or ""),
                fuel=spec.fuel,
                title=str(row or it.get("title") or f"{spec.make} {spec.model}"),
                location=str(it.get("city") or it.get("area") or ""),
                image=(it.get("images", {}) or {}).get("src", "") if isinstance(it.get("images"), dict) else "",
                raw=it,
            )
        except Exception as e:
            log.warning("yad2 parse error: %s", e)
            return None
