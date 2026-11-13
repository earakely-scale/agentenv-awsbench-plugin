"""Time parsing, seeded draws and small matching helpers shared by the world modules."""

import fnmatch
import hashlib
import re
from datetime import UTC, datetime, timedelta

_DUR = re.compile(r"^(-)?P(?:T)?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")


def parse_time(value, base: datetime) -> datetime:
    """`HH:MM[:SS]` on `base`'s date and zone, or an RFC 3339 string, or a datetime."""
    if isinstance(value, datetime):
        return value.astimezone(base.tzinfo)
    s = str(value)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        parts = [int(p) for p in s.split(":")] + [0]
        return base.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(base.tzinfo)


def seconds(value) -> float:
    """A duration as seconds: a number, or ISO 8601 `PT8M`/`-PT1M`."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    m = _DUR.match(str(value).strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    sign = -1 if m.group(1) else 1
    h, mi, se = (float(g) if g else 0.0 for g in m.group(2, 3, 4))
    return sign * (h * 3600 + mi * 60 + se)


def unit(seed: str, *parts) -> float:
    """A deterministic draw in [0, 1) keyed by the seed and the parts."""
    key = "|".join([seed, *map(str, parts)])
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16) / 2**64


def jitter(seed: str, key: str, lo_hi) -> timedelta:
    if not lo_hi:
        return timedelta(0)
    lo, hi = lo_hi
    return timedelta(seconds=lo + (hi - lo) * unit(seed, "jitter", key))


def pick(seed: str, key: str, variants):
    if isinstance(variants, str):
        return variants
    return variants[int(unit(seed, "variant", key) * len(variants))]


def iso(t: datetime | None) -> str | None:
    return t.replace(microsecond=0).isoformat() if t else None


def hms(t: datetime) -> str:
    return t.strftime("%H:%M:%S")


def zulu(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%H:%M:%SZ")


def glob_match(pattern: str, value: str) -> bool:
    if pattern.startswith("(") and pattern.endswith(")"):
        return any(glob_match(p, value) for p in pattern[1:-1].split("|"))
    return fnmatch.fnmatchcase(value or "", pattern)


def dig(obj, path: str):
    """`a.b.c` lookup through dicts; None when missing."""
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(text_of(v) for v in value)
    if isinstance(value, dict):
        return " ".join(text_of(v) for v in value.values())
    return str(value)
