from datetime import datetime, timezone


def as_float(value):
    """The value as a float, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    """An ISO time as an aware datetime, UTC when it names no zone; None when unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
