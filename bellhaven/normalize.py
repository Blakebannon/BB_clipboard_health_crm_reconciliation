"""Conservative normalization for names, addresses, phones and care offerings.

Design rules
------------
* Normalized forms are for *comparison only*; original strings are preserved
  on the records and are what the reviewer sees.
* Street normalization canonicalizes directionals and suffixes to their short
  USPS forms (``West`` -> ``w``, ``Avenue`` -> ``ave``) so ``1120 W Main St``
  and ``1120 West Main Street`` compare equal.
* ``St`` is only a street suffix when it is in suffix position. ``45 St
  Lawrence Dr`` is *Saint Lawrence Drive*; it must never become *Street
  Lawrence Drive*. Both spellings canonicalize to ``45 st lawrence dr``.
* Nothing here guesses: an unparseable number is ``None``, not ``0``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

DIRECTIONALS = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    "n": "n",
    "s": "s",
    "e": "e",
    "w": "w",
    "ne": "ne",
    "nw": "nw",
    "se": "se",
    "sw": "sw",
}
DIRECTIONAL_LONG = {
    "n": "North",
    "s": "South",
    "e": "East",
    "w": "West",
    "ne": "Northeast",
    "nw": "Northwest",
    "se": "Southeast",
    "sw": "Southwest",
}

SUFFIXES = {
    "avenue": "ave",
    "ave": "ave",
    "av": "ave",
    "boulevard": "blvd",
    "blvd": "blvd",
    "circle": "cir",
    "cir": "cir",
    "court": "ct",
    "ct": "ct",
    "drive": "dr",
    "dr": "dr",
    "highway": "hwy",
    "hwy": "hwy",
    "lane": "ln",
    "ln": "ln",
    "parkway": "pkwy",
    "pkwy": "pkwy",
    "pike": "pike",
    "pk": "pike",
    "place": "pl",
    "pl": "pl",
    "point": "pt",
    "pt": "pt",
    "road": "rd",
    "rd": "rd",
    "square": "sq",
    "sq": "sq",
    "street": "st",
    "st": "st",
    "terrace": "ter",
    "ter": "ter",
    "trail": "trl",
    "trl": "trl",
    "way": "way",
    "broadway": "broadway",
    "turnpike": "tpke",
    "tpke": "tpke",
}
SUFFIX_LONG = {
    "ave": "Avenue",
    "blvd": "Boulevard",
    "cir": "Circle",
    "ct": "Court",
    "dr": "Drive",
    "hwy": "Highway",
    "ln": "Lane",
    "pkwy": "Parkway",
    "pike": "Pike",
    "pl": "Place",
    "pt": "Point",
    "rd": "Road",
    "sq": "Square",
    "st": "Street",
    "ter": "Terrace",
    "trl": "Trail",
    "way": "Way",
    "tpke": "Turnpike",
}

UNIT_WORDS = {"suite", "ste", "unit", "apt", "apartment", "bldg", "building", "floor", "fl", "#"}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s#&-]")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")
_PO_BOX = re.compile(r"^\s*(p\.?\s*o\.?\s*box|post\s+office\s+box|box)", re.I)


def is_street_address(street: str | None) -> bool:
    """True when the string looks like a physical street address (has a house number, not a PO Box)."""
    if not street or _PO_BOX.match(street):
        return False
    return bool(parse_street(street).number)


def _clean_tokens(text: str) -> list[str]:
    text = text.replace(".", " ").replace(",", " ")
    text = _PUNCT.sub(" ", text)
    text = text.replace("-", " ")
    return [t for t in _WS.split(text.strip().lower()) if t]


def norm_text(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace. ``&`` -> ``and``."""
    if not text:
        return ""
    text = text.replace("&", " and ")
    return " ".join(_clean_tokens(text))


def norm_name(name: str | None) -> str:
    """Facility-name normalization for *secondary* comparison."""
    t = norm_text(name)
    tokens = [tok for tok in t.split() if tok not in {"the", "of", "at", "a", "an"}]
    return " ".join(tokens)


def norm_state(state: str | None) -> str:
    return (state or "").strip().upper()[:2]


def norm_zip(zip_code: str | None) -> str:
    if not zip_code:
        return ""
    digits = re.sub(r"\D", "", str(zip_code))
    return digits[:5] if len(digits) >= 5 else ""


def norm_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


@dataclass(frozen=True)
class StreetParts:
    number: str
    core: str  # canonical street tokens, space separated
    unit: str  # canonical unit tokens, may be ""
    raw: str

    @property
    def key(self) -> str:
        return f"{self.number} {self.core}".strip()


def _is_suffix_position(tokens: list[str], i: int) -> bool:
    """Is token ``i`` in a street-suffix position?

    A suffix is the last meaningful token, or is followed only by a
    directional / unit designator. ``st`` in ``45 st lawrence dr`` is not in
    suffix position (it is followed by a name) so it is treated as *Saint*.
    """
    rest = tokens[i + 1 :]
    if not rest:
        return True
    for tok in rest:
        if tok in DIRECTIONALS or tok in UNIT_WORDS or tok.isdigit() or tok.startswith("#"):
            continue
        return False
    return True


def parse_street(street: str | None) -> StreetParts:
    raw = (street or "").strip()
    tokens = _clean_tokens(raw)
    if not tokens:
        return StreetParts("", "", "", raw)

    number = ""
    if tokens and re.match(r"^\d+[a-z]?$", tokens[0]):
        number = tokens.pop(0)

    # split off a unit designator (suite / ste / unit / apt / #)
    unit_tokens: list[str] = []
    core_tokens: list[str] = []
    in_unit = False
    for tok in tokens:
        if in_unit:
            unit_tokens.append(tok)
            continue
        if tok in UNIT_WORDS or tok.startswith("#"):
            in_unit = True
            unit_tokens.append(tok.lstrip("#") or "#")
            continue
        core_tokens.append(tok)

    out: list[str] = []
    n = len(core_tokens)
    for i, tok in enumerate(core_tokens):
        if _ORDINAL.match(tok):
            out.append(tok)  # keep 1st / 2nd / 3rd as-is
        elif tok in DIRECTIONALS and (i == 0 or i == n - 1 or _is_suffix_position(core_tokens, i)):
            out.append(DIRECTIONALS[tok])
        elif tok == "saint":
            out.append("st")
        elif tok == "st" and not _is_suffix_position(core_tokens, i):
            out.append("st")  # Saint, canonical short form identical
        elif tok in SUFFIXES and _is_suffix_position(core_tokens, i):
            out.append(SUFFIXES[tok])
        elif tok in SUFFIXES and tok in {"pike", "pk", "broadway", "way"}:
            out.append(tok)
        else:
            out.append(tok)
    unit = " ".join(u for u in unit_tokens if u)
    return StreetParts(number=number, core=" ".join(out), unit=unit, raw=raw)


def norm_street(street: str | None) -> str:
    return parse_street(street).key


def expand_street(street: str | None) -> str:
    """Human-readable long form, e.g. ``45 St Lawrence Dr`` -> ``45 Saint Lawrence Drive``.

    Used only for display; never for matching or writing.
    """
    parts = parse_street(street)
    if not parts.raw:
        return ""
    tokens = parts.core.split()
    n = len(tokens)
    words: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == "st":
            words.append("Street" if _is_suffix_position(tokens, i) else "Saint")
        elif tok in SUFFIX_LONG and _is_suffix_position(tokens, i):
            words.append(SUFFIX_LONG[tok])
        elif tok in DIRECTIONAL_LONG and (i == 0 or _is_suffix_position(tokens, i)):
            words.append(DIRECTIONAL_LONG[tok])
        elif _ORDINAL.match(tok):
            words.append(tok)
        else:
            words.append(tok.capitalize())
    out = " ".join(([parts.number] if parts.number else []) + words)
    if parts.unit:
        out += " " + parts.unit.title()
    return out


@dataclass(frozen=True)
class AddressKey:
    number: str
    street: str
    state: str
    zip5: str
    city: str

    @property
    def strict(self) -> tuple[str, str, str, str]:
        """number + street + state + zip."""
        return (self.number, self.street, self.state, self.zip5)

    @property
    def loose(self) -> tuple[str, str, str, str]:
        """number + street + state + city (for records with a missing ZIP)."""
        return (self.number, self.street, self.state, self.city)

    @property
    def is_usable(self) -> bool:
        return bool(self.number and self.street and self.state)


def address_key(street: str | None, city: str | None, state: str | None, zip_code: str | None) -> AddressKey:
    parts = parse_street(street)
    return AddressKey(
        number=parts.number,
        street=parts.core,
        state=norm_state(state),
        zip5=norm_zip(zip_code),
        city=norm_text(city),
    )


# --- money -----------------------------------------------------------------


def parse_money(value) -> Decimal | None:
    """Parse a financial field. Returns ``None`` (unsafe) when missing or unparseable.

    Accepts ints, floats, Decimals, and strings like ``"$51,250.00"``.
    An empty string / None / nonsense is *not* zero.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


# --- care offerings ---------------------------------------------------------

# Website offering -> CRM care type.  Verified against live CRM values at scan
# time (docs/API_DISCOVERY.md); unknown offerings map to None and are kept as
# evidence only.
CARE_MAP: dict[str, str] = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "assisted living": "Assisted Living",
}


def map_care_offering(offering: str | None) -> str | None:
    if not offering:
        return None
    k = " ".join(offering.strip().lower().replace("–", "-").split())
    return CARE_MAP.get(k)


def map_care_offerings(offerings: list[str]) -> list[str]:
    seen: list[str] = []
    for o in offerings:
        m = map_care_offering(o)
        if m and m not in seen:
            seen.append(m)
    return seen
