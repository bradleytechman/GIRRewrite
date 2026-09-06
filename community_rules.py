"""Pure detection helpers for the community suite."""
import re


INVITE_PATTERN = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)
IMAGE_EXTENSIONS = {".avif", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}


def caps_percent(text: str) -> int:
    letters = [char for char in text if char.isalpha()]
    return round(100 * sum(char.isupper() for char in letters) / len(letters)) if letters else 0


def has_invite(text: str) -> bool:
    return bool(INVITE_PATTERN.search(text))


def recent_count(timestamps: list[float], now: float, window_seconds: int) -> int:
    return sum(now - stamp <= window_seconds for stamp in timestamps)


def is_image_attachment(content_type, filename: str) -> bool:
    """Recognize Discord image uploads even when their MIME type is unavailable."""
    if content_type and content_type.casefold().startswith("image/"):
        return True
    return any(filename.casefold().endswith(extension) for extension in IMAGE_EXTENSIONS)
