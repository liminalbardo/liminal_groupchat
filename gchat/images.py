"""Smaller copies of images: thumbnails for the chat view, and a size models
can take without sending megabytes on every reply.

Uses Pillow. Without it, the originals are used everywhere, which works but
is slower.
"""

import io
import os
import re
from functools import lru_cache

from . import settings

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

THUMB_SIDE = 720          # enough for the chat column on a high-DPI screen
MODEL_SIDE = 1024         # what vision models get
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def thumbs_dir():
    return os.path.join(settings.DATA_DIR, "thumbs")


def source_path(name):
    """A generated/uploaded image by file name - never anything outside MEDIA_DIR."""
    if not SAFE_NAME.fullmatch(name or "") or name.startswith("."):
        return None
    path = os.path.join(settings.MEDIA_DIR, name)
    return path if os.path.isfile(path) else None


def dimensions(name):
    path = thumbnail(name)
    if not path or Image is None:
        return None
    try:
        stat = os.stat(path)
        return _dimensions(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


@lru_cache(maxsize=2048)
def _dimensions(path, modified, size):
    try:
        with Image.open(path) as im:
            return im.size
    except (OSError, ValueError):
        return None


def thumbnail(name):
    """Path to a cached thumbnail of a media image, making it on first use.
    Returns the original's path for GIFs (keeps animation), small images, or
    if Pillow isn't installed; None if there's no such image."""
    src = source_path(name)
    if not src:
        return None
    if Image is None or name.lower().endswith(".gif"):
        return src
    out = os.path.join(thumbs_dir(), os.path.splitext(name)[0] + ".webp")
    if os.path.exists(out):
        return out
    try:
        with Image.open(src) as im:
            if max(im.size) <= THUMB_SIDE and os.path.getsize(src) < 400_000:
                return src
            im.thumbnail((THUMB_SIDE, THUMB_SIDE))
            os.makedirs(thumbs_dir(), exist_ok=True)
            tmp = out + ".tmp"
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(tmp, "WEBP", quality=82)
            os.replace(tmp, out)
        return out
    except (OSError, ValueError):
        return src


def for_model(path):
    """(bytes, mime) of an image sized for a vision model."""
    ext = path.rsplit(".", 1)[-1].lower()
    mime = "image/" + {"jpg": "jpeg"}.get(ext, ext)
    with open(path, "rb") as f:
        raw = f.read()
    if Image is None or ext == "gif":
        return raw, mime
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if max(im.size) <= MODEL_SIDE and len(raw) < 1_000_000:
                return raw, mime
            im.thumbnail((MODEL_SIDE, MODEL_SIDE))
            buf = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):
                im.convert("RGBA").save(buf, "PNG", optimize=True)
                return buf.getvalue(), "image/png"
            im.convert("RGB").save(buf, "JPEG", quality=85)
            return buf.getvalue(), "image/jpeg"
    except (OSError, ValueError):
        return raw, mime
