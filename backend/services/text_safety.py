"""Cleaning of model-written text before it reaches a user.

The model writes from general knowledge and has no live data. Whatever it writes is plain text only, and any sentence
that states a figure it cannot know (a temperature, a price, a rating, a review count, a distance or travel time that
the map service did not supply) is removed rather than shown. Used by the destination guide and by conversational
answers.
"""
from __future__ import annotations

import re
from typing import Any

from services.llm_assistant import numbers_grounded

# Figures only a data provider could supply: money, temperatures, percentages, rainfall, ratings and review counts.
UNSUPPORTED_FIGURE = re.compile(
    r"[°℃℉₹$€£]|\b(?:rs\.?|inr|usd)\s*\d|\b\d+(?:\.\d+)?\s*(?:mm|cm|inch(?:es)?|degrees?|celsius|fahrenheit|rupees?|rs)\b|\b\d{1,3}\s*%"
    r"|\b\d(?:\.\d)?\s*(?:/\s*5|out of 5|stars?)\b|\brated\s+\d|\b\d[\d,]*\s+(?:traveller|traveler|tripadvisor)?\s*reviews?\b"
    r"|\b\d{1,2}[:.]\d{2}\s*(?:am|pm)?\b|\b\d{1,2}\s*(?:am|pm)\b",  # opening times
    re.IGNORECASE,
)
# A distance or travel time. Allowed only when its number came from the map service or the user.
QUANTITY = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*-?\s*(?:km|kms|kilomet(?:er|re)s?|miles?|metres?|meters?|m|minutes?|mins?|hours?|hrs?)\b(?:'s)?",
    re.IGNORECASE,
)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MARKUP = re.compile(r"[*_`#]{1,3}|<[^>]{0,40}>")
PLACE_ID_TEXT = re.compile(r"\s*\(\s*p\d{1,2}\s*\)|\bp\d{1,2}\b")  # the model sometimes writes "(p3)" into a sentence


def clean_line(value: Any, limit: int) -> str:
    """One clean line of plain text, cut at a word boundary."""
    if not isinstance(value, str):
        return ""
    cleaned = PLACE_ID_TEXT.sub("", MARKUP.sub("", CONTROL.sub(" ", value)))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def drop_unsupported(value: str, *grounded_in: Any) -> str:
    """Remove sentences that state a figure the model cannot know. May return an empty string.

    A distance or travel time survives only if its number appears in ``grounded_in`` (the map service's results, the
    user's own message). Years, centuries and counts of things are general knowledge and are left alone.
    """
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", value):
        if not sentence or UNSUPPORTED_FIGURE.search(sentence):
            continue
        quantities = [m.group(0) for m in QUANTITY.finditer(sentence)]
        if quantities and not all(numbers_grounded(q, *grounded_in) for q in quantities):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def is_supported(value: str, *grounded_in: Any) -> bool:
    """True if ``value`` is a single statement that survives :func:`drop_unsupported` unchanged."""
    return bool(value) and drop_unsupported(value, *grounded_in) == value.strip()


_STOPWORDS = frozenset("a an and the of to in for on or is are it its be by with at as from this that during".split())


def _words(sentence: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", sentence.lower()) if w not in _STOPWORDS}


def without_repeats(text: str, lead: str) -> str:
    """Drop sentences of ``text`` that only say ``lead`` again (the headline and the summary often do)."""
    head = _words(lead)
    if not head:
        return text
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = _words(sentence)
        if sentence and len(words & head) / max(1, len(words | head)) < 0.5:
            kept.append(sentence)
    return " ".join(kept).strip()
