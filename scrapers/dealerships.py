"""Dealership scrapers: Kalmobil, Shlomo Sixt, Auto Center, Eldan, Autocar Alber.

Every dealership site lists its used cars a little differently, so this module
provides a robust generic base (``DealershipScraper``) that works in two ways:

1. schema.org JSON-LD (preferred) — most modern car sites embed structured
   ``Car`` / ``Vehicle`` / ``Product`` data in <script type="application/ld+json">.
   We parse that first because it survives most redesigns.
2. CSS-selector fallback — if a site has no JSON-LD, fill in the SELECTORS dict
   for that site (card / title / price / km / year / link).

HONEST NOTE
-----------
I could not open these sites from my environment, so the SEARCH_URL values and
CSS selectors below are sensible starting points, not verified. For each site:
open its used-car page, filter to a model, copy the resulting URL into
SEARCH_URL, and (if needed) fill SELECTORS from the page's HTML. The JSON-LD
path often works with zero extra tuning.

Some of these sites are JavaScript-rendered. If ``requests`` returns an empty
page, that site needs a headless browser (Playwright) — see README, section
"Sites that need a real browser".
"""
from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from .base import BaseScraper, Listing, SearchSpec

log = logging.getLogger("scrapers.dealerships")


def _digits(text) -> int | None:
    if text is None:
        return None
    m = re.findall(r"\d[\d,]*", str(text))
    if not m:
        return None
    try:
        return int(m[0].replace(",", ""))
    except ValueError:
        return None


class DealershipScraper(BaseScraper):
    # Where the used-car catalog lives. {make}/{model} are lower-cased and
    # url-safe-ish; adjust the template per site.
    SEARCH_URL = ""
    # Optional CSS fallback selectors (only used when JSON-LD is absent).
    SELECTORS: dict = {}
    # Set True for sites that require JS rendering (handled via Playwright).
    NEEDS_BROWSER = False

    def build_url(self, spec: SearchSpec) -> str:
        return self.SEARCH_URL.format(
            make=spec.make.lower(), model=spec.model.lower()
        )

    def search(self, spec: SearchSpec) -> list[Listing]:
        if not self.SEARCH_URL:
            log.info("%s: SEARCH_URL not configured yet — skipping", self.name)
            return []
        url = self.build_url(spec)
        try:
            resp = self.get(url)
            if resp.status_code != 200:
                log.warning("%s returned %s", self.name, resp.status_code)
                return []
            html = resp.text
        except Exception as e:
            log.exception("%s fetch failed: %s", self.name, e)
            return []

        listings = self._from_jsonld(html, spec, url)
        if not listings and self.SELECTORS:
            listings = self._from_css(html, spec, url)
        return [l for l in listings if self.passes_filters(l, spec.filters)]

    # -- JSON-LD path -------------------------------------------------------

    def _from_jsonld(self, html: str, spec: SearchSpec, page_url: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        out: list[Listing] = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for node in self._iter_car_nodes(blob):
                lst = self._node_to_listing(node, spec, page_url)
                if lst:
                    out.append(lst)
        return out

    def _iter_car_nodes(self, blob):
        stack = [blob]
        car_types = {"car", "vehicle", "product", "offer"}
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type", "")
                t = t if isinstance(t, str) else (t[0] if t else "")
                if str(t).lower() in car_types:
                    yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))

    def _node_to_listing(self, node: dict, spec: SearchSpec, page_url: str) -> Listing | None:
        offer = node.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        url = node.get("url") or offer.get("url") or page_url
        km = None
        mileage = node.get("mileageFromOdometer")
        if isinstance(mileage, dict):
            km = _digits(mileage.get("value"))
        elif mileage is not None:
            km = _digits(mileage)
        return Listing(
            site=self.name,
            url=url,
            make=node.get("brand", {}).get("name", spec.make) if isinstance(node.get("brand"), dict) else spec.make,
            model=node.get("model") if isinstance(node.get("model"), str) else spec.model,
            year=_digits(node.get("productionDate") or node.get("modelDate") or node.get("vehicleModelDate")),
            km=km,
            price=_digits(offer.get("price") or node.get("price")),
            fuel=spec.fuel,
            title=node.get("name") or f"{spec.make} {spec.model}",
            image=node.get("image") if isinstance(node.get("image"), str) else "",
            raw=node,
        )

    # -- CSS fallback path --------------------------------------------------

    def _from_css(self, html: str, spec: SearchSpec, page_url: str) -> list[Listing]:
        s = self.SELECTORS
        soup = BeautifulSoup(html, "lxml")
        out: list[Listing] = []
        for card in soup.select(s.get("card", "")):
            def pick(key):
                sel = s.get(key)
                if not sel:
                    return None
                el = card.select_one(sel)
                return el.get_text(strip=True) if el else None
            link_el = card.select_one(s.get("link", "a"))
            href = link_el.get("href") if link_el else None
            if href and href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(page_url, href)
            out.append(Listing(
                site=self.name,
                url=href or page_url,
                make=spec.make,
                model=spec.model,
                year=_digits(pick("year")),
                km=_digits(pick("km")),
                price=_digits(pick("price")),
                fuel=spec.fuel,
                title=pick("title") or f"{spec.make} {spec.model}",
                location=pick("location") or "",
            ))
        return out


# ---------------------------------------------------------------------------
# Per-site classes. Fill SEARCH_URL (and SELECTORS if no JSON-LD) after
# checking each live site. The framework does the rest.
# ---------------------------------------------------------------------------

class EldanScraper(BaseScraper):
    """Eldan (eldan.co.il) — Next.js SSR, listings embedded in __NEXT_DATA__.

    The full used-car catalog is make-agnostic, so we fetch every page once per
    scan, cache it, and filter each search locally.
    """
    name = "eldan"
    label = "אלדן"
    LIST_URL = "https://www.eldan.co.il/search/firsthand?page={page}"
    MAX_PAGES = 50

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._catalog: list[dict] | None = None

    def _load_catalog(self) -> list[dict]:
        if self._catalog is not None:
            return self._catalog
        items: list[dict] = []
        page = 1
        total_pages = 1
        while page <= min(total_pages, self.MAX_PAGES):
            resp = self.get(self.LIST_URL.format(page=page))
            if resp.status_code != 200:
                log.warning("eldan page %s -> %s", page, resp.status_code)
                break
            lobby = (
                self.next_data(resp.text)
                .get("props", {}).get("pageProps", {}).get("carLobby", {})
            )
            batch = lobby.get("items") or []
            items.extend(batch)
            total_pages = lobby.get("totalPageCount") or 1
            if not batch or not lobby.get("hasNextPage"):
                break
            page += 1
        self._catalog = items
        log.info("eldan: loaded %d cars across %d page(s)", len(items), page)
        return items

    def search(self, spec: SearchSpec) -> list[Listing]:
        out: list[Listing] = []
        for it in self._load_catalog():
            make_he = it.get("manufacturerName", "")
            model = it.get("modelName", "")
            if not self.matches_make(spec, make_he):
                continue
            if not self.matches_model(spec, model, it.get("carDisplayName", "")):
                continue
            if not self.matches_fuel(spec, hybrid=it.get("hybrid"),
                                     plugin=it.get("plugInCar"),
                                     electric=it.get("electric")):
                continue
            from urllib.parse import quote
            car_no = it.get("carNumber")
            if car_no:
                slug = quote(f"{make_he}-{model}")
                url = f"https://www.eldan.co.il/search/firsthand/{car_no}/{slug}"
            else:
                url = self.LIST_URL
            lst = Listing(
                site=self.name,
                url=url,
                make=spec.make,
                model=spec.model,
                year=it.get("modelYear"),
                km=it.get("mileage"),
                price=it.get("price"),
                fuel=spec.fuel,
                title=it.get("carDisplayName") or f"{make_he} {model}",
                image=it.get("mainImage") or "",
                raw={"carNumber": car_no},
            )
            if self.passes_filters(lst, spec.filters):
                out.append(lst)
        return out


class AutocarAlberScraper(BaseScraper):
    """Albar (albar.co.il) — static server-rendered HTML; each card exposes all
    fields in a hidden ``div.hiddencar`` block with stable class names.

    Filtering is by make (+ engine) in the URL; the site returns only ~6 cards
    per request, so we page through the ``Results?index=N`` endpoint on the same
    session (which remembers the filter via cookie) and dedupe by plate.
    """
    name = "autocar_alber"
    label = "אוטו קאר אלבר"
    BASE = "https://www.albar.co.il/חיפוש-רכבים-חדש/"
    PAGE = "https://www.albar.co.il/umbraco/Surface/Cars/Results?index={index}"
    MAX_PAGES = 20
    # Hebrew manufacturer names as Albar spells them (Kia = קאיה, not קיה).
    MAKE_HE = {"kia": "קאיה", "hyundai": "יונדאי", "toyota": "טויוטה"}
    FUEL_HE = {"hybrid": "היברידי", "plugin_hybrid": "היברידי פלאג אין",
               "electric": "חשמלי", "gasoline": "רגיל"}

    def _parse_cards(self, html: str, spec: SearchSpec, base_url: str,
                     seen: set, out: list):
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.hiddencar")
        added = 0
        for card in cards:
            def txt(cls):
                el = card.select_one(f"span.{cls}")
                return el.get_text(strip=True) if el else ""
            plate = None
            icon = card.find_next("div", class_="icon_img")
            if icon and icon.get("data-lp"):
                plate = icon["data-lp"]
            key = plate or f"{txt('Model')}|{txt('year1')}|{txt('km1')}|{txt('SalePrice')}"
            if key in seen:
                continue
            seen.add(key)
            added += 1
            if not self.matches_model(spec, txt("Model")):
                continue
            if not self.matches_fuel(spec, txt("fueltype1")):
                continue
            lst = Listing(
                site=self.name,
                url=(f"https://www.albar.co.il/רכבים-מכירת-רכב/#lp-{plate}"
                     if plate else base_url),
                make=spec.make,
                model=spec.model,
                year=_digits(txt("year1")),
                km=_digits(txt("km1")),
                price=_digits(txt("SalePrice")),
                hand=_digits(txt("PreviousOwners1")),
                gearbox=txt("Gear1"),
                fuel=spec.fuel,
                title=f"{txt('Manufacture')} {txt('Model')} {txt('year1')}".strip(),
                image=txt("imageUrl"),
                raw={"plate": plate},
            )
            if self.passes_filters(lst, spec.filters):
                out.append(lst)
        return len(cards), added

    def search(self, spec: SearchSpec) -> list[Listing]:
        make_he = self.MAKE_HE.get(spec.make.lower())
        if not make_he:
            return []
        from urllib.parse import urlencode, quote
        params = {"Manufacture": make_he}
        engine = self.FUEL_HE.get(spec.fuel)
        if engine:
            params["EngineType"] = engine
        base_url = self.BASE + "?" + urlencode(params, quote_via=quote)
        out: list[Listing] = []
        seen: set = set()
        try:
            resp = self.get(base_url)
            if resp.status_code != 200:
                log.warning("albar %s -> %s", spec.make, resp.status_code)
                return []
            self._parse_cards(resp.text, spec, base_url, seen, out)
            # The Results?index=N endpoint is cumulative (returns the first
            # N*pagesize cars), so keep paging while the count keeps growing.
            prev_total = 0
            for idx in range(1, self.MAX_PAGES + 1):
                pr = self.get(self.PAGE.format(index=idx))
                if pr.status_code != 200:
                    break
                total, _ = self._parse_cards(pr.text, spec, base_url, seen, out)
                if total == 0 or total <= prev_total:
                    break
                prev_total = total
        except Exception as e:
            log.exception("albar scrape failed for %s: %s", spec.make, e)
        return out


class ShlomoSixtScraper(BaseScraper):
    """Shlomo (shlomo.co.il) — Next.js SSR. Placeholder until the two-level
    RSC parsing is wired (list of model groups -> per-trim detail pages)."""
    name = "shlomo_sixt"
    label = "שלמה SIXT"

    def search(self, spec: SearchSpec) -> list[Listing]:
        log.info("shlomo_sixt: parser not wired yet — skipping")
        return []


class KalmobilScraper(DealershipScraper):
    """Kalmobil = Colmobil (colmobil.co.il). Listings load via a Next.js
    server-action POST — needs a browser/Playwright. Deferred."""
    name = "kalmobil"
    label = "כלמוביל"
    NEEDS_BROWSER = True
    SEARCH_URL = ""


class AutoCenterScraper(DealershipScraper):
    """Auto Center (autocenter.co.il). GraphQL API behind a Cloudflare WAF —
    needs a browser/Playwright or a TLS-spoofing client. Deferred."""
    name = "auto_center"
    label = "אוטו סנטר"
    NEEDS_BROWSER = True
    SEARCH_URL = ""
