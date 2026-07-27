"""Scraper registry.

To add a new site: create a BaseScraper subclass (in dealerships.py or its own
file) and add it to REGISTRY below. Enable/disable it from config.yaml -> sites.
"""
from .yad2 import Yad2Scraper
from .dealerships import (
    KalmobilScraper,
    ShlomoSixtScraper,
    AutoCenterScraper,
    EldanScraper,
    AutocarAlberScraper,
)

REGISTRY = {
    cls.name: cls
    for cls in [
        Yad2Scraper,
        KalmobilScraper,
        ShlomoSixtScraper,
        AutoCenterScraper,
        EldanScraper,
        AutocarAlberScraper,
    ]
}


def get_scraper(name: str):
    cls = REGISTRY.get(name)
    return cls() if cls else None


def all_site_labels() -> dict[str, str]:
    return {name: cls.label for name, cls in REGISTRY.items()}
