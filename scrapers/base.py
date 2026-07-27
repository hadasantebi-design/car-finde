"""Base classes shared by every site scraper.

To add a new site, create a file in this folder that subclasses
``BaseScraper``, implement ``search()``, and register it in
``scrapers/__init__.py``. That's it — the rest of the app picks it up.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any

import requests

log = logging.getLogger("scrapers")

# Cross-site make catalog. Each entry: (English canonical, primary Hebrew name,
# [extra spelling aliases]). Sites use Hebrew and/or English with inconsistent
# gereshim/spacing, so matching is normalized (see _norm) and checks every
# alias. The English canonical is what the UI/config store.
MAKES = [
    ("Toyota", "טויוטה", []),
    ("Hyundai", "יונדאי", []),
    ("Kia", "קיה", ["קאיה"]),
    ("Mazda", "מאזדה", []),
    ("Honda", "הונדה", []),
    ("Nissan", "ניסאן", []),
    ("Mitsubishi", "מיצובישי", []),
    ("Suzuki", "סוזוקי", []),
    ("Subaru", "סובארו", []),
    ("Skoda", "סקודה", ["שקודה"]),
    ("Volkswagen", "פולקסווגן", ["vw"]),
    ("Seat", "סיאט", []),
    ("Renault", "רנו", []),
    ("Peugeot", "פיגו", ["פיזו", "peugeot"]),
    ("Citroen", "סיטרואן", []),
    ("Opel", "אופל", []),
    ("Ford", "פורד", []),
    ("Chevrolet", "שברולט", []),
    ("MG", "אם ג'י", ["mg"]),
    ("Chery", "צ'רי", ["chery"]),
    ("Geely", "ג'ילי", ["geely"]),
    ("BYD", "בי.וויי.די", ["byd"]),
    ("Tesla", "טסלה", []),
    ("Volvo", "וולוו", []),
    ("Mercedes", "מרצדס", ["mercedes-benz", "מרצדס בנץ"]),
    ("BMW", "ב.מ.וו", ["bmw", "במוו"]),
    ("Audi", "אאודי", []),
    ("Jeep", "ג'יפ", ["jeep"]),
    ("Dacia", "דאצ'יה", []),
    ("Fiat", "פיאט", []),
    ("Mini", "מיני", []),
    ("Genesis", "ג'נסיס", ["genesis"]),
    ("Lexus", "לקסוס", []),
    ("Isuzu", "איסוזו", []),
    ("Land Rover", "לנד רובר", ["landrover"]),
]
MAKE_ALIASES = {en.lower(): [en.lower(), he, *extra] for en, he, extra in MAKES}
MAKE_HEBREW = {en.lower(): he for en, he, _ in MAKES}

MODEL_ALIASES = {
    "niro": ["niro", "נירו"],
    "elantra": ["elantra", "lantra", "אלנטרה", "לנטרה"],
    "corolla": ["corolla", "קורולה"],
}


def make_aliases(make: str) -> list[str]:
    return MAKE_ALIASES.get((make or "").strip().lower(), [(make or "").strip().lower()])


def model_aliases(model: str) -> list[str]:
    return MODEL_ALIASES.get((model or "").strip().lower(), [(model or "").strip().lower()])


def _norm(s: str) -> str:
    """Lowercase and strip spacing/punctuation/gereshim so spelling variants
    like פיג'ו / פיגו / "פי ג'ו" all compare equal."""
    return re.sub(r"[\s'`׳״\".\-_]", "", (s or "").lower())


def _contains_any(text: str, needles: list[str]) -> bool:
    t = _norm(text)
    return any(n and _norm(n) in t for n in needles)


@dataclass
class Listing:
    """A single car listing, normalized across all sites."""
    site: str
    url: str
    make: str = ""
    model: str = ""
    year: int | None = None
    km: int | None = None
    price: int | None = None
    hand: int | None = None          # number of previous owners (מספר יד)
    gearbox: str = ""
    fuel: str = ""
    title: str = ""
    location: str = ""
    image: str = ""
    notes: str = ""            # extra free text (origin/description) — powers red-flags & exclusions
    phone: str = ""            # contact phone if the source exposes one
    seller_type: str = ""      # "dealer" | "private" | ""
    raw: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable unique id used for de-duplication."""
        return hashlib.sha1(f"{self.site}|{self.url}".encode("utf-8")).hexdigest()

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["id"] = self.id
        return d


class SearchSpec:
    """One 'what to look for' entry plus the global filters."""
    def __init__(self, make: str, model: str, fuel: str, filters: dict):
        self.make = make
        self.model = model
        self.fuel = fuel
        self.filters = filters or {}

    def __repr__(self) -> str:
        return f"<SearchSpec {self.make} {self.model} {self.fuel}>"


class BaseScraper:
    """Subclass this for each site. Set ``name`` and implement ``search``."""
    name: str = "base"
    # A human label shown in the UI
    label: str = "Base"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })

    def search(self, spec: SearchSpec) -> list[Listing]:
        """Return listings for one make/model. Must be implemented by subclass."""
        raise NotImplementedError

    # ---- helpers subclasses can reuse -------------------------------------

    def passes_filters(self, listing: Listing, filters: dict) -> bool:
        """Apply the global numeric/text filters locally.

        Sites that can't filter server-side rely on this so results are
        always consistent regardless of the source.
        """
        f = filters or {}
        if listing.year is not None:
            if f.get("year_min") and listing.year < f["year_min"]:
                return False
            if f.get("year_max") and listing.year > f["year_max"]:
                return False
        if listing.km is not None:
            if f.get("km_max") and listing.km > f["km_max"]:
                return False
            if f.get("km_min") and listing.km < f["km_min"]:
                return False
        if listing.price is not None:
            if f.get("price_min") and listing.price < f["price_min"]:
                return False
            if f.get("price_max") and listing.price > f["price_max"]:
                return False
        if listing.hand is not None and f.get("hand_max"):
            if listing.hand > f["hand_max"]:
                return False
        gb = (f.get("gearbox") or "any").lower()
        if gb != "any" and listing.gearbox and gb not in listing.gearbox.lower():
            return False
        return True

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    # ---- make/model/fuel matching (for sites that filter client-side) -----

    def matches_make(self, spec: SearchSpec, *texts: str) -> bool:
        aliases = make_aliases(spec.make)
        return any(_contains_any(t, aliases) for t in texts if t)

    def matches_model(self, spec: SearchSpec, *texts: str) -> bool:
        aliases = model_aliases(spec.model)
        return any(_contains_any(t, aliases) for t in texts if t)

    def matches_fuel(self, spec: SearchSpec, fuel_text: str = "", *,
                     hybrid: bool | None = None, plugin: bool | None = None,
                     electric: bool | None = None) -> bool:
        """Match the requested fuel. Pass explicit booleans when the site
        exposes them; otherwise we substring-match the free-text fuel field."""
        want = (spec.fuel or "any").lower()
        if want == "any":
            return True
        if hybrid is not None or plugin is not None or electric is not None:
            if want == "hybrid":
                return bool(hybrid) and not bool(plugin)
            if want == "plugin_hybrid":
                return bool(plugin)
            if want == "electric":
                return bool(electric)
            return True
        ft = (fuel_text or "").lower()
        if not ft:
            return True  # unknown fuel -> don't exclude; global filters still apply
        keys = {
            "hybrid": ["hybrid", "היבריד"],
            "plugin_hybrid": ["plug", "פלאג", "נטען"],
            "electric": ["electric", "חשמל"],
            "gasoline": ["gasolin", "בנזין"],
            "diesel": ["diesel", "דיזל"],
        }.get(want, [want])
        # For plain "hybrid", avoid matching plug-in strings.
        if want == "hybrid" and _contains_any(ft, ["plug", "פלאג", "נטען"]):
            return False
        return _contains_any(ft, keys)

    @staticmethod
    def next_data(html: str) -> dict:
        """Extract and parse the Next.js __NEXT_DATA__ JSON blob, or {}."""
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            return {}


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class BrowserScraper(BaseScraper):
    """Base for sites that require a real browser (JS-rendered or bot-protected).

    Uses Playwright/Chromium, which runs both locally and in GitHub Actions.
    A single browser context is launched lazily and reused across the scan's
    searches; the engine calls ``close()`` when the site is done. Playwright is
    imported lazily so the requests-only scrapers still work without it.
    """
    NEEDS_BROWSER = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pw = None
        self._browser = None
        self._ctx = None

    def _context(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            self._ctx = self._browser.new_context(
                locale="he-IL", user_agent=UA, viewport={"width": 1366, "height": 900}
            )
            self._ctx.set_default_timeout(45000)
        return self._ctx

    def open(self, url: str, wait_selector: str | None = None,
             wait_ms: int = 2500, settle: str = "domcontentloaded"):
        """Navigate and return the page (caller must close it)."""
        page = self._context().new_page()
        page.goto(url, wait_until=settle, timeout=45000)
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=25000)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
        return page

    def close(self):
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = None
