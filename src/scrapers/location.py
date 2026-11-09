"""Map free-text job locations to a country code."""
from __future__ import annotations

import re

HOME_COUNTRY = "DE"       # default for unrecognised places on German-focused boards
REMOTE = "REMOTE"         # remote role; eligibility is judged by the scorer from the posting
OUTSIDE_TARGETS = "XX"    # location names a place outside the target countries

_REGION_TERMS = ("worldwide", "anywhere", "global", "europe", "european union", "eu", "emea")

_COUNTRY_TERMS = {
    "DE": ("germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
           "frankfurt", "cologne", "köln", "nuremberg", "nürnberg", "stuttgart"),
    "NL": ("netherlands", "nederland", "holland", "amsterdam", "rotterdam", "utrecht", "eindhoven",
           "den haag", "the hague", "leiden", "delft", "haarlem", "groningen", "hertogenbosch", "schiphol",
           "noord-brabant", "zuid-holland", "noord-holland", "overijssel", "gelderland"),
    "CH": ("switzerland", "schweiz", "suisse", "zurich", "zürich", "geneva", "basel", "lausanne",
           "bern", "zug", "luzern", "lucerne", "winterthur", "st. gallen", "lugano"),
    "AE": ("uae", "united arab emirates", "emirates", "dubai", "abu dhabi"),
    "SA": ("saudi", "saudi arabia", "ksa", "riyadh", "jeddah"),
    "QA": ("qatar", "doha"),
}

# Places outside the target countries that show up on German-focused boards
_OUTSIDE_TERMS = (
    "united kingdom", "uk", "gb", "england", "scotland", "wales", "northern ireland",
    "london", "manchester", "leeds", "edinburgh", "glasgow", "cardiff", "belfast", "dundee",
    "birmingham", "bristol", "bicester", "battersea", "brighton", "cambridge", "oxford",
    "austria", "österreich", "vienna", "wien", "graz", "linz", "salzburg", "innsbruck",
    "usa", "united states", "canada", "australia", "sydney", "lithuania", "vilnius",
    "poland", "warsaw", "spain", "madrid", "barcelona", "italy", "milan", "portugal", "lisbon",
    "france", "paris", "lyon", "toulouse", "lille", "strasbourg", "bordeaux", "marseille", "nantes",
    "montpellier", "grenoble", "rennes", "caen", "annecy", "la rochelle", "orly", "occitanie",
    "belgium", "brussels", "bruxelles", "ireland", "dublin", "denmark", "copenhagen",
    "sweden", "stockholm", "norway", "oslo", "czech", "prague", "india", "singapore",
)

# A flag emoji is two regional-indicator symbols spelling the ISO country code (🇫🇷 -> "FR")
_FLAG = re.compile("([\U0001F1E6-\U0001F1FF])([\U0001F1E6-\U0001F1FF])")


def _flag_countries(text: str) -> set[str]:
    return {"".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in m.groups()) for m in _FLAG.finditer(text)}


def _flag_country(text: str) -> str | None:
    """Country from a flag emoji: a target country's code, otherwise OUTSIDE_TARGETS."""
    flags = _flag_countries(text)
    if not flags:
        return None
    known = [code for code in _COUNTRY_TERMS if code in flags]
    return known[0] if known else OUTSIDE_TARGETS


def names_country(location: str | None, code: str) -> bool:
    """True if the location text itself names the country (by a known place or a flag)."""
    text = (location or "").lower()
    return _has_term(text, _COUNTRY_TERMS.get(code, ())) or code in _flag_countries(location or "")

# Words that carry no location information ("Remote", "Fully remote only")
_FILLER = {"remote", "fully", "only", "100", "position", "job"}


def _has_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms)


def infer_remote_country(location: str | None) -> str:
    text = (location or "").lower()
    if _has_term(text, _REGION_TERMS):
        return REMOTE
    if flag := _flag_country(location or ""):
        return flag
    for code, terms in _COUNTRY_TERMS.items():
        if _has_term(text, terms):
            return code
    if not set(re.findall(r"\w+", text)) - _FILLER:
        return REMOTE
    return OUTSIDE_TARGETS


def remote_city(location: str | None) -> str:
    """City field for a remote job, keeping the original requirement for traceability."""
    location = (location or "").strip()
    return f"Remote · {location}" if location and location.lower() != "remote" else "Remote"


def infer_onsite_country(location: str | None) -> str:
    """Country for a job on a German-focused board; unrecognised places are assumed German."""
    text = (location or "").lower()
    if flag := _flag_country(location or ""):
        return flag
    for code, terms in _COUNTRY_TERMS.items():   # DE first: "Germany, Manchester" stays DE
        if _has_term(text, terms):
            return code
    if _has_term(text, _OUTSIDE_TERMS):
        return OUTSIDE_TARGETS
    words = set(re.findall(r"\w+", text))
    if "remote" in words and not words - _FILLER:
        return REMOTE
    return HOME_COUNTRY
