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

from .base import BaseScraper, BrowserScraper, Listing, SearchSpec, MAKE_HEBREW

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
            imgs = it.get("images") or []
            img_url = (it.get("mainImage") or (imgs[0].get("url") if imgs else "")) or ""
            img_url = img_url.replace(" ", "%20")  # Eldan CDN paths contain spaces
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
                image=img_url,
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
    # Hebrew manufacturer names for Albar's search URL. Albar spells Kia קאיה.
    MAKE_HE = {**MAKE_HEBREW, "kia": "קאיה"}
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


class ShlomoSixtScraper(BrowserScraper):
    """Shlomo (shlomo.co.il) — Next.js, two-level. We filter to the make via
    the client-side filter (?manufactures=), collect matching model-group
    links, then open each group's detail page and read the per-car cards."""
    name = "shlomo_sixt"
    label = "שלמה SIXT"
    HOST = "https://www.shlomo.co.il"
    BASE = "https://www.shlomo.co.il/sales/first-hand"
    MAX_GROUPS = 6

    def search(self, spec: SearchSpec) -> list[Listing]:
        from urllib.parse import quote, unquote
        out: list[Listing] = []
        try:
            lp = self.open(f"{self.BASE}?manufactures={quote(spec.make)}",
                           wait_selector='a[href*="/sales/first-hand/"]', wait_ms=5000)
            hrefs = lp.eval_on_selector_all(
                'a[href*="/sales/first-hand/"]',
                "els=>[...new Set(els.map(e=>e.getAttribute('href')))]") or []
            lp.close()
            groups = []
            for h in hrefs:
                if not h or h.count("/") < 4:
                    continue
                seg = h.strip("/").split("/")
                name = unquote(seg[-2]) if len(seg) >= 2 else unquote(h)
                if self.matches_model(spec, name) and self.matches_fuel(spec, name):
                    groups.append((h, name))
            for h, name in groups[:self.MAX_GROUPS]:
                dp = self.open(self.HOST + h,
                               wait_selector='[class*="CarCardByPlate_carCard"]', wait_ms=3500)
                cards = dp.query_selector_all('[class*="CarCardByPlate_carCard"]')
                for c in cards:
                    txt = c.inner_text() or ""
                    plate = re.search(r"מס[׳']?\s*רכב[\s|]*([\d​\-]+)", txt)
                    plate = re.sub(r"[^\d\-]", "", plate.group(1)) if plate else None
                    price = re.search(r"מחיר\s*שלמה[\s|]*([\d,]+)", txt)
                    km = re.search(r"קילומטר[\s|]*([\d,]+)", txt)
                    year = re.search(r"עליה\s*לכביש[\s|]*\d{2}/(\d{4})", txt)
                    origin = "ליסינג" if "ליסינג" in txt else ("השכרה" if "השכרה" in txt else "")
                    lst = Listing(
                        site=self.name,
                        url=f"{self.HOST}{h}#{plate}" if plate else self.HOST + h,
                        make=spec.make, model=spec.model,
                        year=int(year.group(1)) if year else None,
                        km=_digits(km.group(1)) if km else None,
                        price=_digits(price.group(1)) if price else None,
                        fuel=spec.fuel, title=f"{name} {year.group(1)}".strip() if year else name,
                        image="", raw={"plate": plate, "origin": origin},
                    )
                    if self.passes_filters(lst, spec.filters):
                        out.append(lst)
                dp.close()
        except Exception as e:
            log.exception("shlomo_sixt scrape failed for %s %s: %s", spec.make, spec.model, e)
        return out


class KalmobilScraper(BrowserScraper):
    """Kalmobil = Colmobil (colmobil.co.il). Listings render client-side via a
    Next.js server action, so we drive a headless browser, filter by brand
    (+ engine) in the URL, and parse the rendered cards."""
    name = "kalmobil"
    label = "כלמוביל"
    BASE = "https://www.colmobil.co.il/trade/cars/"
    FUEL_EN = {"hybrid": "hybrid", "plugin_hybrid": "hybrid", "electric": "electric",
               "gasoline": "gasoline", "diesel": "diesel"}

    def search(self, spec: SearchSpec) -> list[Listing]:
        from urllib.parse import urlencode
        params = {"brand": spec.make}
        eng = self.FUEL_EN.get(spec.fuel)
        if eng:
            params["engine"] = eng
        url = self.BASE + "?" + urlencode(params)
        out: list[Listing] = []
        try:
            page = self.open(url, wait_selector='[class*="StyledCarCard"]', wait_ms=2500)
            cards = page.query_selector_all('[class*="StyledCarCard"]')
            for c in cards:
                txt = c.inner_text() or ""
                if not self.matches_model(spec, txt):
                    continue
                a = c.query_selector('a[href^="/trade/cars/"]')
                href = a.get_attribute("href") if a else None
                abs_url = ("https://www.colmobil.co.il" + href) if href and href.startswith("/") else (href or url)
                parts = [p.strip() for p in re.split(r"[\n|]", txt)]
                model_part = next((p for p in parts if p and not re.search(r'[\d₪]|צרו|להשוואה|לחודש|השווא', p)), "")
                if not model_part:
                    model_part = f"{spec.make} {spec.model}"
                year = re.search(r"שנה\s*(\d{4})", txt)
                hand = re.search(r"יד\s*0*(\d+)", txt)
                km = re.search(r"([\d,]+)\s*ק", txt)
                price = re.search(r"₪\s*([\d,]+)", txt)
                lst = Listing(
                    site=self.name, url=abs_url, make=spec.make, model=spec.model,
                    year=int(year.group(1)) if year else None,
                    km=_digits(km.group(1)) if km else None,
                    price=_digits(price.group(1)) if price else None,
                    hand=int(hand.group(1)) if hand else None,
                    fuel=spec.fuel,
                    title=f"{model_part} {year.group(1)}".strip() if year else (model_part or f"{spec.make} {spec.model}"),
                    image="", raw={},
                )
                if self.passes_filters(lst, spec.filters):
                    out.append(lst)
            page.close()
        except Exception as e:
            log.exception("kalmobil scrape failed for %s: %s", spec.make, e)
        return out


class AutoCenterScraper(BrowserScraper):
    """Auto Center (autocenter.co.il). Behind a Cloudflare WAF, but a headless
    browser passes it; we then call the site's Magento GraphQL product-search
    from inside the page (so it inherits the Cloudflare clearance)."""
    name = "auto_center"
    label = "אוטו סנטר"
    HOME = "https://www.autocenter.co.il/cars.html"
    _GQL = ('{products(search:"%s" pageSize:60){items{name sku url_key '
            '... on SimpleProduct{year km manufacturer_name} '
            'price_range{maximum_price{final_price{value}}}}}}')

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ready = None

    def _ready_page(self):
        if self._ready is None:
            self._ready = self.open(self.HOME, wait_selector='[class^="item-root-"]', wait_ms=800)
        return self._ready

    def search(self, spec: SearchSpec) -> list[Listing]:
        # search by a Hebrew model alias when we have one (Magento search is Hebrew)
        term = next((a for a in self.model_aliases_for(spec.model) if any(ord(ch) > 127 for ch in a)), spec.model)
        out: list[Listing] = []
        try:
            pg = self._ready_page()
            q = self._GQL % term.replace('"', '')
            items = pg.evaluate(
                "async(q)=>{try{const r=await fetch('/graphql?query='+encodeURIComponent(q));"
                "const j=await r.json();return (j.data&&j.data.products&&j.data.products.items)||[];}"
                "catch(e){return [];}}", q,
            ) or []
            for it in items:
                name = it.get("name") or ""
                if not self.matches_make(spec, it.get("manufacturer_name") or "", name):
                    continue
                if not self.matches_model(spec, name):
                    continue
                if not self.matches_fuel(spec, name):
                    continue
                key = it.get("url_key") or it.get("sku")
                price = (((it.get("price_range") or {}).get("maximum_price") or {}).get("final_price") or {}).get("value")
                lst = Listing(
                    site=self.name,
                    url=f"https://www.autocenter.co.il/{key}.html" if key else self.HOME,
                    make=spec.make, model=spec.model,
                    year=it.get("year"), km=it.get("km"),
                    price=int(price) if price else None,
                    fuel=spec.fuel, title=name.strip() or f"{spec.make} {spec.model}",
                    image="", raw={"sku": it.get("sku")},
                )
                if self.passes_filters(lst, spec.filters):
                    out.append(lst)
        except Exception as e:
            log.exception("auto_center scrape failed for %s: %s", spec.model, e)
        return out

    @staticmethod
    def model_aliases_for(model):
        from .base import model_aliases
        return model_aliases(model)


class FreesbeScraper(BaseScraper):
    """freesbe (freesbe.com) — Carasso / Cal-Auto group, large ex-leasing stock.
    Next.js SSR with a per-model listing URL; data in __NEXT_DATA__."""
    name = "freesbe"
    label = "freesbe (קרסו)"
    LIST = "https://freesbe.com/used-car-for-sale/listings/{make}/{model}"

    def search(self, spec: SearchSpec) -> list[Listing]:
        from urllib.parse import unquote
        url = self.LIST.format(make=spec.make.lower(), model=spec.model.lower())
        out: list[Listing] = []
        try:
            r = self.get(url)
            if r.status_code != 200:
                log.warning("freesbe %s/%s -> %s", spec.make, spec.model, r.status_code)
                return []
            ssr = self.next_data(r.text).get("props", {}).get("pageProps", {}).get("carsSSR", {}) or {}
            for it in (ssr.get("cars") or []):
                model = it.get("carZoneModelName") or ""
                name = it.get("name") or ""
                desc = unquote(it.get("carProgrammaticDescription") or "")
                if not self.matches_model(spec, model, name):
                    continue
                if not self.matches_fuel(spec, f"{name} {desc}"):
                    continue
                cn = it.get("carNumber")
                imgs = it.get("images") or []
                img = (imgs[0].get("url") or "").replace(" ", "%20") if imgs and isinstance(imgs[0], dict) else ""
                origin = it.get("previousCarOwnershipHand") or ""
                lst = Listing(
                    site=self.name,
                    url=f"https://freesbe.com/used-car-for-sale/listings/{spec.make.lower()}/{spec.model.lower()}#{cn}",
                    make=spec.make, model=spec.model,
                    year=it.get("year"), km=it.get("kms"), price=it.get("carPrice"),
                    hand=it.get("carHand"), fuel=spec.fuel,
                    title=f"{it.get('carZoneManufacturerName','')} {model} {it.get('year') or ''}".strip(),
                    image=img, notes=(f"{origin} {desc}").strip()[:250], seller_type="dealer",
                    raw={"origin": origin},
                )
                if self.passes_filters(lst, spec.filters):
                    out.append(lst)
        except Exception as e:
            log.exception("freesbe scrape failed for %s %s: %s", spec.make, spec.model, e)
        return out


class UmiTradeScraper(BaseScraper):
    """UMI Trade (umi-trade.co.il) — importer/leasing dealer. Server-rendered
    schema.org JSON-LD Vehicle nodes (first page); filter client-side."""
    name = "umi_trade"
    label = "UMI טרייד"

    def search(self, spec: SearchSpec) -> list[Listing]:
        from urllib.parse import quote
        out: list[Listing] = []
        try:
            r = self.get("https://www.umi-trade.co.il/" + quote("חיפוש-כלי-רכב") + "/")
            if r.status_code != 200:
                return []
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup.find_all("script", type="application/ld+json"):
                try:
                    blob = json.loads(tag.string or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                for node in (blob if isinstance(blob, list) else [blob]):
                    if not isinstance(node, dict):
                        continue
                    if str(node.get("@type", "")).lower() not in ("vehicle", "car", "product"):
                        continue
                    brand = node.get("brand")
                    brand = brand.get("name") if isinstance(brand, dict) else (brand or "")
                    name = node.get("name") or ""
                    model = node.get("model") if isinstance(node.get("model"), str) else name
                    if not self.matches_make(spec, brand, name):
                        continue
                    if not self.matches_model(spec, model, name):
                        continue
                    if not self.matches_fuel(spec, f"{node.get('fuelType') or ''} {name}"):
                        continue
                    off = node.get("offers") or {}
                    off = off[0] if isinstance(off, list) and off else off
                    price = _digits(off.get("price")) if isinstance(off, dict) else None
                    mil = node.get("mileageFromOdometer")
                    km = _digits(mil.get("value")) if isinstance(mil, dict) else _digits(mil)
                    url = node.get("URL") or node.get("url") or (off.get("url") if isinstance(off, dict) else "") or "https://www.umi-trade.co.il/"
                    lst = Listing(
                        site=self.name, url=url, make=spec.make, model=spec.model,
                        year=_digits(node.get("modelDate") or node.get("productionDate")),
                        km=km, price=price, fuel=spec.fuel,
                        title=name or f"{spec.make} {spec.model}",
                        image=node.get("image") if isinstance(node.get("image"), str) else "",
                        seller_type="dealer", raw={},
                    )
                    if self.passes_filters(lst, spec.filters):
                        out.append(lst)
        except Exception as e:
            log.exception("umi_trade scrape failed: %s", e)
        return out
