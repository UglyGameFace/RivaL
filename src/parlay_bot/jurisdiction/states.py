from __future__ import annotations

import difflib

US_JURISDICTIONS: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_NAME_TO_CODE = {name.casefold(): code for code, name in US_JURISDICTIONS.items()}
_NAME_TO_CODE["washington dc"] = "DC"
_NAME_TO_CODE["washington d.c."] = "DC"
_NAME_TO_CODE["d.c."] = "DC"


# Typo tolerance for full-name input (2026-11-22): a misspelled state name
# resolves only when one jurisdiction is a clear, unambiguous best match.
# Two-letter codes are NEVER fuzzy-matched — a typo'd code is usually a
# different valid code ("CT" vs "CA"), so codes stay exact-only.
_FUZZY_MIN_RATIO = 0.86
_FUZZY_MIN_MARGIN = 0.05


def _fuzzy_state_code(cleaned: str) -> str | None:
    folded = cleaned.casefold()
    if len(folded) < 4:
        return None
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, folded, name.casefold()).ratio(), code)
            for code, name in US_JURISDICTIONS.items()
        ),
        reverse=True,
    )
    (best_ratio, best_code), (second_ratio, _) = scored[0], scored[1]
    if best_ratio >= _FUZZY_MIN_RATIO and best_ratio - second_ratio >= _FUZZY_MIN_MARGIN:
        return best_code
    return None


def normalize_state(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    code = cleaned.upper()
    if code in US_JURISDICTIONS:
        return code

    resolved = _NAME_TO_CODE.get(cleaned.casefold())
    if resolved is not None:
        return resolved

    # Punctuation variants the exact lookups miss: "C.T.", "N.Y.",
    # "Washington, DC" — strip periods/commas and retry both lookups.
    squashed = " ".join(cleaned.replace(".", " ").replace(",", " ").split())
    if squashed != cleaned:
        # "C.T." squashes to "C T"; the compact form is the code.
        code = squashed.replace(" ", "").upper()
        if code in US_JURISDICTIONS:
            return code
        resolved = _NAME_TO_CODE.get(squashed.casefold())
        if resolved is not None:
            return resolved
        cleaned = squashed

    resolved = _fuzzy_state_code(cleaned)
    if resolved is None:
        raise ValueError(f"Unsupported U.S. state or jurisdiction: {value!r}")
    return resolved


def state_name(code: str) -> str:
    return US_JURISDICTIONS[normalize_state(code)]
