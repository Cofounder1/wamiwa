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


def find(text: str | None) -> list[str]:
    """The kinds of personal information in the text, e.g. ["email address", "phone number"]."""
    if not text:
        return []
    kinds = ["email address"] * len(EMAIL.findall(text))
    kinds += [_kind(m.group()) for m in NUMBER.finditer(EMAIL.sub(" ", text))]
    return sorted(set(kinds))


def redact(text: str | None) -> str:
    if not text:
        return ""
    text = EMAIL.sub("[email address removed]", text)
    return NUMBER.sub(lambda m: f"[{_kind(m.group())} removed]", text)
