"""Mapping from the app's natural-language categories to HERE Places category IDs, and validation of results.

Every ID below is a documented HERE category (HERE "Places category system") that was also confirmed against live
Geocoding & Search responses (``categories[].id`` on real places). IDs are hierarchical: ``400-4000`` (level 2) covers
``400-4000-4581`` (level 3). /browse accepts IDs at any level, comma separated, and ``!id`` to exclude one.

Why this exists: a free-text query ("airports in Odisha") makes HERE guess. It returned "Airport Hotel" because the
text matched. Filtering on the category ID and then re-checking the ID on every returned place means a result is only
ever presented as a cafe/airport/etc. when HERE itself classifies it that way.

Categories without a verified ID (supermarket, car wash, tourist attractions, ...) are NOT mapped here. They keep
using HERE's text search, and their results are labelled with HERE's own category name rather than the requested one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from services.intents import CATEGORY_ALIASES


@dataclass(frozen=True)
class CategorySpec:
    key: str
    singular: str
    plural: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    # Category IDs (any level) that identify the best matches; a place whose primary category is the first one
    # listed ranks ahead of one that only matches a later entry. Everything else that matches ranks after.
    prefer: tuple[str, ...] = ()
    default_radius_km: float = 5.0
    # What the category covers, shown to the user so a narrower/wider reading is never a surprise.
    covers: str = ""


SPECS: dict[str, CategorySpec] = {spec.key: spec for spec in (
    CategorySpec(
        "airport", "airport", "airports",
        include=("400-4000-4581", "400-4000-4580"),  # Airport; Public Sports Airport
        prefer=("400-4000-4581",),
        default_radius_km=30.0,
        covers="HERE category Airport (400-4000-4581). Arrival/departure terminal entries (400-4000-4582) are not airports and are left out.",
    ),
    CategorySpec(
        "hospital", "hospital", "hospitals",
        include=("800-8000-0159",),  # Hospital. (Hospital Emergency Room 800-8000-0325 is a duplicate "-ER" entry.)
        covers="HERE category Hospital (800-8000-0159). Clinics and doctors are a separate category.",
    ),
    CategorySpec(
        "medical", "medical facility", "medical facilities",
        include=("800-8000",),  # Hospital or Health Care Facility
        exclude=("800-8000-0161", "800-8000-0162"),  # Optical, Veterinarian
        prefer=("800-8000-0159", "800-8000-0160", "800-8000-0158"),  # Hospital, Urgent Care, Clinics
        covers="HERE category Hospital or Health Care Facility (800-8000), without opticians and vets.",
    ),
    CategorySpec(
        "cafe", "cafe", "cafes",
        include=("100-1100",),  # Coffee-Tea
        prefer=("100-1100-0010", "100-1100-0000", "100-1100-0331"),  # Coffee Shop, Coffee-Tea, Tea House
        covers="HERE category Coffee-Tea (100-1100): coffee shops first, then tea houses.",
    ),
    CategorySpec(
        "restaurant", "restaurant", "restaurants",
        include=("100-1000",),
        covers="HERE category Restaurant (100-1000).",
    ),
    CategorySpec(
        "hotel", "hotel", "hotels",
        include=("500-5000", "500-5100"),  # Hotel-Motel; Lodging
        exclude=("500-5100-0056", "500-5100-0060"),  # Campground, Short-Time Motel
        prefer=("500-5000",),
        covers="HERE categories Hotel-Motel (500-5000) and Lodging (500-5100), without campgrounds.",
    ),
    CategorySpec(
        "fuel", "petrol pump", "petrol pumps",
        include=("700-7600",),  # Fueling Station
        exclude=("700-7600-0322", "700-7600-0325"),  # EV charging station, EV battery swap
        covers="HERE category Fueling Station (700-7600), without EV charging and battery-swap points.",
    ),
    CategorySpec(
        "ev_charging", "EV charging station", "EV charging stations",
        include=("700-7600-0322",),
        default_radius_km=10.0,
        covers="HERE category EV Charging Station (700-7600-0322).",
    ),
    CategorySpec("pharmacy", "pharmacy", "pharmacies", include=("600-6400",), covers="HERE category Drugstore or Pharmacy (600-6400)."),
    CategorySpec("convenience", "convenience store", "convenience stores", include=("600-6000",), covers="HERE category Convenience Store (600-6000)."),
    CategorySpec("shopping_mall", "shopping mall", "shopping malls", include=("600-6100",), covers="HERE category Mall-Shopping Complex (600-6100)."),
    CategorySpec("bank", "bank", "banks", include=("700-7000",), covers="HERE category Banking (700-7000)."),
    CategorySpec("atm", "ATM", "ATMs", include=("700-7010",), covers="HERE category ATM (700-7010)."),
    CategorySpec("parking", "parking place", "parking places", include=("800-8500",), covers="HERE category Parking (800-8500)."),
    CategorySpec("car_repair", "car repair shop", "car repair shops", include=("700-7850",), covers="HERE category Car Repair-Service (700-7850)."),
    CategorySpec("pub_bar", "pub or bar", "pubs and bars", include=("200-2000-0011",), covers="HERE category Bar or Pub (200-2000-0011)."),
    CategorySpec("nightlife", "nightlife spot", "nightlife spots", include=("200-2000",), covers="HERE category Nightlife-Entertainment (200-2000)."),
)}


def spec_for(category: str | None) -> CategorySpec | None:
    return SPECS.get(str(category or "").strip().lower())


def browse_categories_param(spec: CategorySpec) -> str:
    """The value for /browse ``categories=``: includes, then ``!``-prefixed excludes."""
    return ",".join([*spec.include, *(f"!{cid}" for cid in spec.exclude)])


def _covers(parent: str, category_id: str) -> bool:
    return category_id == parent or category_id.startswith(parent + "-")


def _any_covers(parents: Iterable[str], category_id: str) -> bool:
    return any(_covers(parent, category_id) for parent in parents)


def classify(spec: CategorySpec, category_ids: list[str], primary_id: str | None = None) -> int | None:
    """Relevance of a place for ``spec``, or ``None`` if HERE does not classify it as that category.

    Lower is better. A place whose *primary* category matches ranks ahead of one that only lists the category as a
    secondary tag (a restaurant that is also tagged hotel); ``spec.prefer`` orders matches within each group.
    A place carrying an excluded category is rejected outright, mirroring HERE's own ``!id`` semantics.
    """
    ids = [str(c) for c in category_ids if c]
    if any(_any_covers(spec.exclude, cid) for cid in ids):
        return None
    matching = [cid for cid in ids if _any_covers(spec.include, cid)]
    if not matching:
        return None
    primary_matches = bool(primary_id) and primary_id in matching
    reference = primary_id if primary_matches else matching[0]
    rank = len(spec.prefer)
    for position, preferred in enumerate(spec.prefer):
        if _covers(preferred, str(reference)):
            rank = position
            break
    return (0 if primary_matches else 10) + rank


# ---- descriptive text in the query ---------------------------------------------------------------------------------
_STOPWORDS = frozenset(
    "a an the of for in at near by to me my our us all any some every list lists share show give get find search "
    "want need looking look suitable good great best top nice decent place places spot spots stop stops break "
    "lunch dinner breakfast brunch snack there here around nearby close next within from that this with and or "
    "please can you could would i we is are on route way trip journey half halfway midpoint middle point".split()
)

_QUALIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "quiet": re.compile(r"\b(?:quiet|peaceful|calm|serene|tranquil)\b", re.I),
    "family-friendly": re.compile(
        r"\b(?:family|kid|kids|child|children|baby|toddler)[- ]friendly\b|\bfor (?:the |a |an |my |our )?(?:family|families|kids?|children|baby|babies|toddlers?)\b|\bwith (?:kids|children|a baby|a toddler|my family)\b", re.I),
    "romantic": re.compile(r"\bromantic\b", re.I),
    "cheap": re.compile(r"\b(?:cheap|budget|affordable|inexpensive)\b", re.I),
    "luxury": re.compile(r"\b(?:luxury|luxurious|upscale|fine[- ]dining|[3-7][- ]star|(?:three|four|five)[- ]star)\b", re.I),
    "pet-friendly": re.compile(r"\bpet[- ]friendly\b", re.I),
    "vegetarian/vegan": re.compile(r"\b(?:vegetarian|vegan|pure[- ]veg|veg)\b", re.I),
    "open now": re.compile(r"\b(?:open now|open late|24[/ -]?7|24 hours)\b", re.I),
    "highly rated": re.compile(r"\b(?:highly[- ]rated|top[- ]rated|best[- ]rated)\b", re.I),
}


def extract_qualifiers(text: str) -> list[str]:
    """Subjective or attribute wishes in the user's wording ("quiet", "family-friendly", "cheap")."""
    found: list[str] = []
    for name, pattern in _QUALIFIER_PATTERNS.items():
        if pattern.search(text):
            found.append(name)
    return found


def _category_words(category: str) -> set[str]:
    words: set[str] = set()
    for phrase, key in CATEGORY_ALIASES.items():
        if key == category:
            for word in re.findall(r"[a-z0-9]+", phrase.lower()):
                words.add(word)
                words.add(word.rstrip("s"))
    words.update({category, category.rstrip("s")})
    # The words people use for these in everyday speech.
    extra = {
        "cafe": {"coffee", "cafes", "café", "tea", "espresso"},
        "restaurant": {"eat", "food", "meal", "eatery", "eateries", "dining"},
        "fuel": {"petrol", "pump", "pumps", "gas", "diesel", "fuel", "station", "stations", "bunk", "bunks"},
        "airport": {"airport", "airports", "airfield", "airfields", "aerodrome"},
        "medical": {"medical", "facility", "facilities", "clinic", "clinics", "doctor", "doctors", "health", "healthcare"},
        "hospital": {"hospital", "hospitals"},
        "hotel": {"hotel", "hotels", "stay", "stays", "accommodation", "accommodations", "lodging"},
        "ev_charging": {"ev", "charging", "charger", "chargers", "station", "stations"},
    }
    words |= extra.get(category, set())
    return words


def descriptive_words(category: str, query: str | None) -> list[str]:
    """Words in ``query`` that say something beyond the category itself (a cuisine, a brand, a style)."""
    if not query:
        return []
    ignorable = _category_words(category)
    for pattern in _QUALIFIER_PATTERNS.values():
        query = pattern.sub(" ", query)
    out = []
    for word in re.findall(r"[a-z0-9]+", query.lower()):
        if word in _STOPWORDS or word in ignorable or word.rstrip("s") in ignorable or word.isdigit() or word in {"km", "kms", "m", "metre", "metres", "meter", "meters", "kilometre", "kilometres", "kilometer", "kilometers"}:
            continue
        out.append(word)
    return out


def is_generic_query(category: str, query: str | None) -> bool:
    """True when the query only names the category ("the list of airports"), so category filtering alone is exact."""
    return not descriptive_words(category, query)
