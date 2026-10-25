"""Map a remote job's free-text location requirement to a country code."""
from __future__ import annotations

import re

HOME_COUNTRY = "DE"       # region-wide remote roles are workable from here
OUTSIDE_TARGETS = "XX"    # location names a place outside the target countries

_REGION_TERMS = ("worldwide", "anywhere", "global", "europe", "european union", "eu", "emea")

_COUNTRY_TERMS = {
    "DE": ("germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
           "frankfurt", "cologne", "köln", "nuremberg", "nürnberg", "stuttgart"),
    "NL": ("netherlands", "holland", "amsterdam", "rotterdam", "utrecht", "eindhoven"),
    "CH": ("switzerland", "zurich", "zürich", "geneva", "basel", "lausanne"),
    "AE": ("uae", "united arab emirates", "emirates", "dubai", "abu dhabi"),
    "SA": ("saudi", "saudi arabia", "ksa", "riyadh", "jeddah"),
    "QA": ("qatar", "doha"),
}

# Words that carry no location information ("Remote", "Fully remote only")
_FILLER = {"remote", "fully", "only", "100", "position", "job"}


def _has_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms)


def infer_remote_country(location: str | None) -> str:
    text = (location or "").lower()
    if _has_term(text, _REGION_TERMS):
        return HOME_COUNTRY
    for code, terms in _COUNTRY_TERMS.items():
        if _has_term(text, terms):
            return code
    if not set(re.findall(r"\w+", text)) - _FILLER:
        return HOME_COUNTRY
    return OUTSIDE_TARGETS


def remote_city(location: str | None) -> str:
    """City field for a remote job, keeping the original requirement for traceability."""
    location = (location or "").strip()
    return f"Remote · {location}" if location and location.lower() != "remote" else "Remote"
