"""Delaware-only geographic gate.

DelawareScene lists events *in Delaware*. Our sources are Delaware
organizations, but several of them legitimately program out of state — the
Delaware Academy of Vocal Arts performs in Philadelphia and New York, and the
Delaware Nature Society runs programs at Bucktoe Creek Preserve in Avondale,
PA. Those must not reach the calendar.

Every check here is anchored to a *position* in the address rather than a
substring anywhere in it, because the two collide constantly in real data:

  - "20 Baltimore Avenue, Rehoboth Beach, DE 19971" is Delaware; Baltimore is
    the street.
  - "3120 Barley Mill Rd, Mt. Cuba, DE 19807" is Delaware; "Mt." is not
    Montana.
  - "15411 Abbotts Pond Road" has a five-digit house number, not a ZIP.

Verdicts: 'de' (confirmed in Delaware), 'out' (confirmed elsewhere),
'unknown' (no location evidence). Callers keep 'unknown' — every source is a
Delaware organization, so a missing address is not evidence of absence.
"""

from __future__ import annotations

import re

# Delaware ZIP ranges: New Castle 19701-19736, Wilmington 19801-19899,
# Kent & Sussex 19901-19980. A leading "19" does not by itself mean Delaware —
# 19001-19699 are Pennsylvania (Avondale is 19311, Kennett Square 19348).
DE_ZIP_RANGES = ((19701, 19736), (19801, 19899), (19901, 19980))

STATE_ABBRS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
}
STATE_NAMES = {
    "pennsylvania", "new jersey", "maryland", "new york", "virginia",
    "west virginia", "ohio", "connecticut", "massachusetts", "north carolina",
    "south carolina", "district of columbia", "florida", "california",
    "illinois", "texas", "georgia", "michigan", "vermont", "maine",
    "new hampshire", "rhode island", "indiana", "tennessee",
}

# Delaware municipalities, CDPs, and common USPS spellings.
DE_PLACES = {
    # New Castle County
    "wilmington", "newark", "middletown", "bear", "brookside", "glasgow",
    "hockessin", "pike creek", "pike creek valley", "claymont", "new castle",
    "elsmere", "newport", "delaware city", "odessa", "townsend", "greenville",
    "centreville", "montchanin", "rockland", "yorklyn", "winterthur", "arden",
    "ardentown", "ardencroft", "bellefonte", "christiana", "edgemoor",
    "marshallton", "stanton", "talleyville", "kirkwood", "port penn",
    "saint georges", "st georges", "wilmington manor", "north star",
    "hares corner", "mt cuba", "mount cuba", "greenville de",
    # Kent County
    "dover", "dover afb", "dover air force base", "smyrna", "camden",
    "camden wyoming", "wyoming", "harrington", "felton", "frederica",
    "clayton", "cheswold", "hartly", "kenton", "leipsic", "little creek",
    "magnolia", "viola", "woodside", "bowers", "bowers beach", "farmington",
    "houston", "marydel", "milford", "rising sun", "rising sun lebanon",
    # Sussex County
    "georgetown", "seaford", "lewes", "rehoboth beach", "rehoboth",
    "millsboro", "laurel", "milton", "selbyville", "bridgeville", "delmar",
    "bethany beach", "dewey beach", "fenwick island", "frankford",
    "greenwood", "henlopen acres", "blades", "dagsboro", "ellendale",
    "slaughter beach", "south bethany", "ocean view", "millville", "bethel",
    "nassau", "long neck", "oak orchard", "angola", "harbeson", "lincoln",
    "gumboro", "roxana", "omar", "whitesville", "bethany",
}

# Out-of-state localities our sources actually program in. Compared against
# whole address segments and city fields only — never as substrings — so a
# street called "Baltimore Avenue" is unaffected. Deliberately excludes every
# name that is also a Delaware municipality (Camden, Newark, Milford,
# Georgetown, Dover, Smyrna, Laurel, Seaford, Clayton, Milton, Lincoln).
NON_DE_PLACES = {
    "philadelphia", "philly", "baltimore", "west chester", "kennett square",
    "chadds ford", "avondale", "landenberg", "media", "coatesville",
    "downingtown", "exton", "malvern", "king of prussia", "elkton",
    "chesapeake city", "perryville", "havre de grace", "aberdeen", "bel air",
    "chestertown", "salisbury", "ocean city", "snow hill", "princess anne",
    "cape may", "wildwood", "vineland", "bridgeton", "pennsville",
    "carneys point", "woodstown", "swedesboro", "cherry hill", "rahway",
    "new york", "new york city", "manhattan", "brooklyn", "trenton",
    "princeton", "washington", "arlington", "alexandria", "richmond",
    "pittsburgh", "harrisburg", "lancaster", "reading", "allentown",
    "bethlehem", "scranton", "atlantic city", "annapolis", "towson",
    "frederick", "hagerstown", "oxford", "nottingham", "north east",
    "chester", "wayne", "berwyn", "paoli", "phoenixville", "pottstown",
}

# Well-known out-of-state venues whose names carry no locality word. Matched
# on the venue name, so each entry must be specific enough that no Delaware
# venue shares it.
NON_DE_VENUES = {
    "longwood gardens", "kimmel center", "verizon hall", "kennett flash",
    "brandywine river museum", "chester county historical society",
    "please touch museum", "franklin institute", "barnes foundation",
    "philadelphia museum of art", "academy of music", "mann center",
    "xfinity live", "wells fargo center", "citizens bank park",
    "lincoln financial field", "madison square garden", "carnegie hall",
    "lincoln center", "kennedy center", "merriam theater", "walnut street theatre",
    "world cafe live", "union transfer", "the met philadelphia",
    "camden county boathouse", "bb&t pavilion", "freedom mortgage pavilion",
}

_ZIP_AFTER_STATE_RE = re.compile(r"\b[A-Z]{2}\.?,?\s+(\d{5})(?:-\d{4})?\b")
_TRAILING_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_STATE_TOKEN_RE = re.compile(r",\s*([A-Za-z][A-Za-z. ]{0,22}?)\s*,?\s*(\d{5}(?:-\d{4})?)?\s*$")
_ALL_ABBR_RE = re.compile(r",\s*([A-Za-z]{2})\.?\s*(?=,|\s|$)")


def _is_de_zip(z: int) -> bool:
    return any(lo <= z <= hi for lo, hi in DE_ZIP_RANGES)


def _normalize_place(text):
    """Lowercase, drop punctuation, collapse whitespace."""
    text = (text or "").strip().lower()
    text = re.sub(r"[.'’]", "", text)
    text = re.sub(r"[-/]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _segments(text):
    """Comma-separated address parts, normalized. Whole segments only —
    substring matching is what makes 'Baltimore Avenue' look like Maryland."""
    if not text:
        return []
    return [s for s in (_normalize_place(p) for p in str(text).split(",")) if s]


def _zip_from(text):
    """A ZIP must sit in the ZIP position: right after a state token, or at
    the end of the string. Five-digit house numbers are common in Sussex and
    Kent addresses ('15411 Abbotts Pond Road') and must not be read as ZIPs.
    """
    if not text:
        return None
    m = _ZIP_AFTER_STATE_RE.search(text.upper())
    if m:
        return int(m[1])
    m = _TRAILING_ZIP_RE.search(text.strip())
    if m:
        return int(m[1])
    return None


def _state_from(text):
    """State token in the state position: last comma-separated element,
    optionally followed by a ZIP.

    Anchoring to that position is what keeps abbreviations that are also
    ordinary address words out — 'Mt. Cuba, DE 19807' must read DE, not MT,
    and '… Street, Dover' must not read DR.
    """
    if not text:
        return None
    m = _STATE_TOKEN_RE.search(str(text).strip())
    if not m:
        return None
    token = _normalize_place(m[1])
    if token in ("delaware", "de"):
        return "DE"
    if token.upper() in STATE_ABBRS:
        return token.upper()
    if token in STATE_NAMES:
        return token
    return None


def verdict(venue_city=None, venue_address=None, venue_name=None):
    """-> ('de' | 'out' | 'unknown', reason)"""
    # Each field is examined on its own rather than concatenated: joining them
    # moves the state position to the end of the *last* field, so
    # "…, DE 19807" followed by a venue name "Mt. Cuba Center" would read as
    # Montana. venue_name is included because extractors often put the whole
    # address there — the ICS route sets venue_name from the raw VEVENT
    # LOCATION, e.g. "Wilmington Campus | 4101 Washington Street, Wilmington,
    # DE, 19802".
    fields = [f for f in (venue_address, venue_city, venue_name) if f]

    # 1. Explicit state token in the state position.
    for field in fields:
        state = _state_from(field)
        if state == "DE":
            return "de", "address states DE"
        if state:
            return "out", f"address states {state}"

    # 2. ZIP in the ZIP position.
    for field in fields:
        z = _zip_from(field)
        if z is not None:
            if _is_de_zip(z):
                return "de", f"Delaware ZIP {z}"
            return "out", f"non-Delaware ZIP {z}"

    # 3. Whole address/city segments against the place lists. Delaware is
    #    checked first so that names shared with other states (Newark,
    #    Camden, Milford, Dover) resolve here unless a state said otherwise.
    # Locality matching uses only the city and address fields, where a
    # segment really is a locality. Venue names are handled separately.
    parts = _segments(venue_city) + _segments(venue_address)
    for part in parts:
        if part in DE_PLACES:
            return "de", f"Delaware locality '{part}'"
    for part in parts:
        if part in NON_DE_PLACES:
            return "out", f"out-of-state locality '{part}'"

    # 4. Named out-of-state venues (Longwood Gardens carries no locality
    #    word). Full-name matching only — generic words like "Washington" or
    #    "Media" appear inside Delaware venue names.
    name = _normalize_place(venue_name)
    if name:
        for venue in NON_DE_VENUES:
            if venue in name:
                return "out", f"out-of-state venue '{venue}'"

    return "unknown", "no location evidence"
