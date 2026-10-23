"""Removes hidden metadata, above all the GPS position phones store in photos and videos, before a file is served.

Photos and videos are public on Wamiwa, so without this anyone could download a creator's photo and read where it
was taken. Only the metadata goes; the image or video itself is untouched. No outside libraries: each format's
structure is simple enough to edit directly.
"""
import mmap
import struct
from pathlib import Path


def strip_jpeg(data: bytes) -> bytes:
    """Drop APP1 (EXIF, which holds GPS, and XMP), APP13 (Photoshop/IPTC) and comment segments."""
    if not data.startswith(b"\xff\xd8"):
        return data
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of the image data: copy the rest as it is
            out += data[i:]
            return bytes(out)
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        segment = data[i:i + 2 + length]
        if marker not in (0xE1, 0xED, 0xFE):
            out += segment
        i += 2 + length
    out += data[i:]
    return bytes(out)


def strip_png(data: bytes) -> bytes:
    """Drop the eXIf chunk and text chunks (which can carry location and device details)."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    out, i = bytearray(data[:8]), 8
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        if kind not in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"):
            out += chunk
        i += 12 + length
    return bytes(out)


def strip_webp(data: bytes) -> bytes:
    """Drop EXIF and XMP chunks and clear their flags in the VP8X header."""
    if not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return data
    out, i = bytearray(data[:12]), 12
    while i + 8 <= len(data):
        kind = data[i:i + 4]
        length = struct.unpack("<I", data[i + 4:i + 8])[0]
        chunk = bytearray(data[i:i + 8 + length + (length & 1)])
        if kind == b"VP8X" and len(chunk) > 8:
            chunk[8] &= ~0x0C & 0xFF  # EXIF (0x08) and XMP (0x04) present flags
        if kind not in (b"EXIF", b"XMP "):
            out += chunk
        i += 8 + length + (length & 1)
    struct.pack_into("<I", out, 4, len(out) - 8)
    return bytes(out)


def strip_video_location(data: bytearray | mmap.mmap) -> int:
    """Blank the location in MP4/MOV files in place (the ©xyz box phones write, e.g. "+06.4531+003.3958/").
    The box stays, so the file's structure and every offset are unchanged. Returns how many were blanked."""
    found, start = 0, 0
    while (at := data.find(b"\xa9xyz", start)) != -1:
        size = struct.unpack(">I", data[at - 4:at])[0] if at >= 4 else 0
        if 8 <= size <= 256:
            data[at + 4:at - 4 + size] = b"\x00" * (size - 8)
            found += 1
        start = at + 4
    return found


IMAGE_STRIPPERS = {"image/jpeg": strip_jpeg, "image/png": strip_png, "image/webp": strip_webp}
VIDEO_TYPES = {"video/mp4", "video/quicktime"}


def scrub_file(path: Path, content_type: str) -> int:
    """Remove location and other hidden metadata from a stored file. Returns its new size. Runs in a worker thread."""
    if content_type in IMAGE_STRIPPERS:
        original = path.read_bytes()
        cleaned = IMAGE_STRIPPERS[content_type](original)
        if cleaned != original:
            path.write_bytes(cleaned)
        return len(cleaned)
    if content_type in VIDEO_TYPES and path.stat().st_size:
        with open(path, "r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapped:  # edited in place, not loaded
            if strip_video_location(mapped):
                mapped.flush()
    return path.stat().st_size
