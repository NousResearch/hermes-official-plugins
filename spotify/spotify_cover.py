"""Playlist cover images for the Spotify tools.

Spotify takes a cover as raw base64-encoded JPEG data in the request body
(``Content-Type: image/jpeg``) and refuses anything over 256 KB. The bytes are
resolved through Hermes' image-source safety path, then re-encoded as JPEG so
the body fits the limit.
"""

from __future__ import annotations

import base64
import io

from .spotify_client import SpotifyError

# Spotify's documented payload ceiling for a playlist cover.
MAX_COVER_BYTES = 256 * 1024

# Quality used for the JPEG re-encode; the image is shrunk rather than the
# quality dropped to nothing when it does not fit.
_JPEG_QUALITY = 85
# Smallest edge the shrink loop gives up at.
_MIN_EDGE = 64


def _flatten(image) -> object:
    """Drop the alpha channel onto white; JPEG has no transparency."""
    from PIL import Image

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.split()[-1])
    return background


def _encode_jpeg(image) -> str:
    """Base64-encode *image* as JPEG, without the data-URL prefix."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def encode_cover_jpeg(data: bytes) -> str:
    """Base64-encode *data* as a JPEG body that fits Spotify's size limit.

    The source may be any format the image reader understands; it is re-encoded
    as JPEG because that is the only format the endpoint accepts. An image that
    is still too large is shrunk until it fits.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a plugin dependency
        raise SpotifyError("Pillow is required to upload a playlist cover.") from exc

    try:
        with Image.open(io.BytesIO(data)) as source:
            image = _flatten(source) if source.mode in ("RGBA", "LA", "P") else source.convert("RGB")
            width, height = image.size
            while True:
                encoded = _encode_jpeg(image)
                if len(encoded.encode("ascii")) <= MAX_COVER_BYTES:
                    return encoded
                if width < _MIN_EDGE and height < _MIN_EDGE:
                    raise SpotifyError(
                        "The image is still over Spotify's 256 KB cover limit after shrinking it.")
                width, height = max(width // 2, _MIN_EDGE), max(height // 2, _MIN_EDGE)
                image = image.resize((width, height))
    except SpotifyError:
        raise
    except Exception as exc:
        raise SpotifyError(f"Could not read the image for the playlist cover: {exc}") from exc


def resolve_cover_image(image_url: str) -> str:
    """Resolve *image_url* to a base64-encoded JPEG body for Spotify.

    Only an http(s) URL or a ``data:`` URL is accepted: a local path is
    rejected rather than read from the host's filesystem. The bytes are
    fetched through Hermes' image-source resolver, so the usual size, type
    and URL-policy checks apply.
    """
    source = (image_url or "").strip()
    if not source:
        raise SpotifyError("image_url is required for action='upload_cover'.")
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "data:")):
        pass
    else:
        raise SpotifyError(
            "image_url must be an http(s) URL or a data: URL; local file paths are not accepted.")

    from model_tools import _run_async
    from tools.image_source import ImageResolutionError, ResolveContext, resolve_image_source

    try:
        resolved = _run_async(resolve_image_source(source, ResolveContext()))
    except ImageResolutionError as exc:
        raise SpotifyError(f"Could not read the image for the playlist cover: {exc}") from exc
    except Exception as exc:
        raise SpotifyError(f"Could not read the image for the playlist cover: {exc}") from exc
    return encode_cover_jpeg(resolved.data)


def cover_content_type() -> str:
    """The content type the cover endpoint expects."""
    return "image/jpeg"
