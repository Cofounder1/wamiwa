"""Personal information in text: email addresses, phone numbers, bank account, ID and card numbers.

Used two ways: chats are redacted before an agent sees them, and anything an agent writes is refused if it
contains personal information, so an agent can't be talked into giving someone's details out.
Street addresses and names are not detected here; that needs a dedicated service.
"""
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A run of 10 to 19 digits, allowing spaces, dots, dashes and brackets between them, optionally starting with +.
NUMBER = re.compile(r"(?<![\w/])\+?\d(?:[\s().-]?\d){9,18}(?![\w/])")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _kind(raw: str) -> str:
    digits = _digits(raw)
    if raw.lstrip().startswith("+") or re.match(r"0[789][01]\d{8}$", digits) or digits.startswith("234"):
        return "phone number"
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return "card number"
    if len(digits) == 10:
        return "bank account number"
    if len(digits) == 11:
        return "ID number"  # BVN and NIN are 11 digits
    return "phone number"


# Number words, so "zero eight zero three one two three..." is caught as well as digits. English, plus the
# Yoruba words for 0-9 (odo, ookan, eeji, eeta, eerin, aarun, eefa, eeje, eejo, eesan).
NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "odo": "0", "ookan": "1", "okan": "1", "eeji": "2", "eji": "2", "eeta": "3", "eta": "3", "eerin": "4", "erin": "4",
    "aarun": "5", "arun": "5", "eefa": "6", "efa": "6", "eeje": "7", "eje": "7", "eejo": "8", "ejo": "8",
    "eesan": "9", "esan": "9",
}
REPEATS = {"double": 2, "triple": 3}
TOKEN = re.compile(r"[a-z]+|\d")


def _spelled_numbers(text: str) -> list[str]:
    """Runs of 10 or more digits where at least some are written as words, e.g. "zero eight double zero one...".
    Runs written only in digits are left to NUMBER."""
    runs, current, words, repeat = [], "", False, 1

    def close() -> None:
        if len(current) >= 10 and words:
            runs.append(current)

    for token in TOKEN.findall(text.lower()):
        if token in REPEATS:
            repeat = REPEATS[token]
            continue
        digit = token if token.isdigit() else NUMBER_WORDS.get(token)
        if digit is None:
            close()
            current, words, repeat = "", False, 1
            continue
        current += digit * repeat
        words = words or not token.isdigit()
        repeat = 1
    close()
    return runs


def find(text: str | None) -> list[str]:
    """The kinds of personal information in the text, e.g. ["email address", "phone number"]."""
    if not text:
        return []
    kinds = ["email address"] * len(EMAIL.findall(text))
    kinds += [_kind(m.group()) for m in NUMBER.finditer(EMAIL.sub(" ", text))]
    kinds += [_kind(run) for run in _spelled_numbers(EMAIL.sub(" ", text))]
    return sorted(set(kinds))


def redact(text: str | None) -> str:
    if not text:
        return ""
    text = EMAIL.sub("[email address removed]", text)
    return NUMBER.sub(lambda m: f"[{_kind(m.group())} removed]", text)
