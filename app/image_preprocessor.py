from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .schemas import AgentError, PreparedImage

Image.MAX_IMAGE_PIXELS = 50_000_000


class ImagePreprocessError(AgentError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preprocess_image(source: Path, destination: Path, max_edge: int = 896, jpeg_quality: int = 88) -> PreparedImage:
    source = source.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ImagePreprocessError(f"image does not exist: {source}")
    if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ImagePreprocessError("supported image formats are JPEG, PNG, and WebP")
    try:
        source_sha = sha256_file(source)
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            original_size = image.size
            image = image.convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest > max_edge:
                scale = max_edge / longest
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                image = image.resize(new_size, Image.Resampling.LANCZOS)
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="JPEG", quality=jpeg_quality, optimize=True)
            return PreparedImage(
                source_path=source,
                prepared_path=destination,
                source_sha256=source_sha,
                original_size=original_size,
                prepared_size=image.size,
                bytes_written=destination.stat().st_size,
            )
    except Image.DecompressionBombError as exc:
        raise ImagePreprocessError("image is too large and was rejected as a decompression bomb risk") from exc
    except UnidentifiedImageError as exc:
        raise ImagePreprocessError("image is damaged or not a real supported image") from exc
    except OSError as exc:
        raise ImagePreprocessError(f"image preprocessing failed: {exc}") from exc
