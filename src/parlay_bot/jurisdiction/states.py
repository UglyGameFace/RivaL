from __future__ import annotations

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


def normalize_state(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    code = cleaned.upper()
    if code in US_JURISDICTIONS:
        return code

    resolved = _NAME_TO_CODE.get(cleaned.casefold())
    if resolved is None:
        raise ValueError(f"Unsupported U.S. state or jurisdiction: {value!r}")
    return resolved


def state_name(code: str) -> str:
    return US_JURISDICTIONS[normalize_state(code)]
