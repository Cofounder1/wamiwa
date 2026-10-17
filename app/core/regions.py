"""Countries and territories people can choose as their region or as target regions.

Stored as snake_case English names ("nigeria", "united_kingdom"), the form existing data already uses.
Common short forms ("uk", "usa", "uae") are accepted and stored under the canonical name.
"""
import re
import unicodedata

NAMES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus", "Belgium",
    "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada", "Central African Republic", "Chad",
    "Chile", "China", "Colombia", "Comoros", "Congo", "Costa Rica", "Cote d'Ivoire", "Croatia", "Cuba", "Cyprus",
    "Czechia", "Democratic Republic of the Congo", "Denmark", "Djibouti", "Dominica", "Dominican Republic",
    "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia", "Fiji",
    "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala",
    "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong", "Hungary", "Iceland", "India",
    "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya",
    "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon", "Lesotho", "Liberia", "Libya",
    "Liechtenstein", "Lithuania", "Luxembourg", "Macau", "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali",
    "Malta", "Marshall Islands", "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco",
    "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands",
    "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea", "North Macedonia", "Norway", "Oman",
    "Pakistan", "Palau", "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Poland",
    "Portugal", "Puerto Rico", "Qatar", "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis", "Saint Lucia",
    "Saint Vincent and the Grenadines", "Samoa", "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal",
    "Serbia", "Seychelles", "Sierra Leone", "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia",
    "South Africa", "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden",
    "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste", "Togo", "Tonga",
    "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu", "Uganda", "Ukraine",
    "United Arab Emirates", "United Kingdom", "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Vatican City",
    "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()  # Côte -> Cote
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower().replace("'", "")).strip("_")


REGIONS: dict[str, str] = {_key(name): name for name in NAMES}

ALIASES: dict[str, str] = {
    "uk": "united_kingdom", "gb": "united_kingdom", "great_britain": "united_kingdom", "britain": "united_kingdom",
    "england": "united_kingdom", "scotland": "united_kingdom", "wales": "united_kingdom",
    "northern_ireland": "united_kingdom",
    "us": "united_states", "usa": "united_states", "america": "united_states",
    "united_states_of_america": "united_states",
    "uae": "united_arab_emirates", "emirates": "united_arab_emirates", "dubai": "united_arab_emirates",
    "ng": "nigeria", "gh": "ghana", "ke": "kenya", "za": "south_africa", "rsa": "south_africa",
    "drc": "democratic_republic_of_the_congo", "dr_congo": "democratic_republic_of_the_congo",
    "republic_of_the_congo": "congo", "ivory_coast": "cote_divoire", "cape_verde": "cabo_verde",
    "swaziland": "eswatini", "burma": "myanmar", "czech_republic": "czechia", "holland": "netherlands",
    "korea": "south_korea", "republic_of_korea": "south_korea", "turkiye": "turkey", "east_timor": "timor_leste",
    "the_gambia": "gambia", "the_bahamas": "bahamas", "macao": "macau", "vatican": "vatican_city",
}


def normalize(value: str | None) -> str | None:
    """The canonical key for a region, or None if it is not a known country or territory."""
    if not value or not value.strip():
        return None
    key = _key(value)
    key = ALIASES.get(key, key)
    return key if key in REGIONS else None


def options() -> list[dict[str, str]]:
    return [{"id": key, "name": name} for key, name in sorted(REGIONS.items(), key=lambda kv: kv[1])]
