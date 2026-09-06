"""Pure detection helpers for the community suite."""
import re


INVITE_PATTERN = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)


def caps_percent(text: str) -> int:
    letters = [char for char in text if char.isalpha()]
    return round(100 * sum(char.isupper() for char in letters) / len(letters)) if letters else 0


def has_invite(text: str) -> bool:
    return bool(INVITE_PATTERN.search(text))


def recent_count(timestamps: list[float], now: float, window_seconds: int) -> int:
    return sum(now - stamp <= window_seconds for stamp in timestamps)
