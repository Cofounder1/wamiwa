"""Checks that a file really is what it says it is, from its first bytes, instead of trusting the label the
browser sent. Stops, for example, a web page being uploaded as "image/jpeg" and served back from our domain."""


def _ftyp(head: bytes, brands: tuple[bytes, ...]) -> bool:
    """ISO media files (mp4, mov, m4a, heic) carry 'ftyp' and a brand at byte 4."""
    return head[4:8] == b"ftyp" and (not brands or head[8:12] in brands)


SIGNATURES = {
    "image/jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "image/png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP",
    "image/heic": lambda h: _ftyp(h, (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis")),
    "video/mp4": lambda h: _ftyp(h, ()),
    "video/quicktime": lambda h: _ftyp(h, ()) or h[4:8] in (b"moov", b"mdat", b"wide", b"free"),
    "video/webm": lambda h: h.startswith(b"\x1a\x45\xdf\xa3"),
    "audio/webm": lambda h: h.startswith(b"\x1a\x45\xdf\xa3"),
    "audio/mpeg": lambda h: h.startswith(b"ID3") or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0),
    "audio/mp4": lambda h: _ftyp(h, ()),
    "audio/aac": lambda h: len(h) > 1 and h[0] == 0xFF and h[1] & 0xF6 == 0xF0,
    "audio/ogg": lambda h: h.startswith(b"OggS"),
    "audio/wav": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WAVE",
    "application/pdf": lambda h: h.startswith(b"%PDF-"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": lambda h: h.startswith(b"PK\x03\x04"),
    "text/plain": lambda h: b"\x00" not in h and not h.lstrip().lower().startswith((b"<!doctype", b"<html", b"<script", b"<svg")),
}


def matches(content_type: str, head: bytes) -> bool:
    check = SIGNATURES.get(content_type)
    return bool(check and head and check(head))
