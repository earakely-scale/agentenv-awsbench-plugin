"""The workspace's shared state: the store, the scenario's clock anchor, users and the agent identities that decide
which inbound items are the agent's."""

import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..store import NotFound, Store

DEFAULT_TZ = "America/New_York"
INTERNAL_DOMAIN = "vantleaf.com"
MENTION = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])")


class ToolError(ValueError):
    """A refusal the caller should see as the tool's error message."""


class Workspace:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.store = Store()
        self.scenario: str | None = None
        self.tz = ZoneInfo(DEFAULT_TZ)
        self.start: datetime | None = None
        self.now: datetime | None = None
        self.identities: dict[str, dict[str, str]] = {}
        self.charged_roles: list[str] = ["sre-primary"]
        self.counters: dict[str, int] = {}
        self.config: dict = {}

    # time
    def when(self, value) -> datetime | None:
        """A scenario time: 'HH:MM[:SS]' on the scenario's start date, or an ISO date-time (naive = scenario zone), in
        whole seconds, as every record shows it."""
        if value is None:
            return None
        if isinstance(value, datetime):
            t = value
        elif re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text := str(value).strip()):
            day = (self.start or datetime.now(self.tz)).astimezone(self.tz).date()
            t = datetime.combine(day, time(*(int(p) for p in text.split(":"))), self.tz)
        else:
            t = datetime.fromisoformat(text)
        return (t if t.tzinfo else t.replace(tzinfo=self.tz)).replace(microsecond=0)

    def iso(self, t: datetime | None) -> str | None:
        return t.astimezone(self.tz).isoformat() if t else None

    def at(self, t: datetime) -> datetime:
        self.now = max(self.now, t) if self.now else t
        return t

    def clock(self) -> datetime:
        return self.now or self.start or datetime.now(self.tz).replace(microsecond=0)

    def advance(self, seconds: float) -> datetime:
        return self.at(self.clock() + timedelta(seconds=seconds))

    def stamp(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%H:%M:%S ET")

    # identities
    def ident(self, role: str, app: str) -> str:
        return self.identities.get(role, {}).get(app, role)

    def agent_ids(self, app: str) -> set[str]:
        return {ids.get(app, role) for role, ids in self.identities.items()}

    def role_known(self, role: str | None) -> bool:
        return role in self.identities

    # users
    def user(self, uid: str) -> dict | None:
        uid = uid.lstrip("@").strip()
        if (u := self.store.get("chat.user", uid)) is not None:
            return u
        low = uid.lower()
        return next((u for u in self.store.items("chat.user")
                     if low in (u.get("name", "").lower(), (u.get("email") or "").lower())), None)

    def require_user(self, uid: str) -> dict:
        if (u := self.user(uid)) is None:
            raise ToolError(f"no user {uid!r}; chat_get_user lists nothing for it (try a handle like hannah.becker)")
        return u

    def mentions(self, text: str) -> list[str]:
        out = []
        for m in MENTION.findall(text or ""):
            if (u := self.user(m)) is not None and u["id"] not in out:
                out.append(u["id"])
        return out

    def next_id(self, key: str, start: int) -> int:
        self.counters[key] = max(self.counters.get(key, start - 1), start - 1) + 1
        return self.counters[key]

    # inbound
    def deliver(self, kind: str, t: datetime, frm: str, where: str, text: str, **extra) -> dict:
        preview = " ".join((text or "").split())
        item = {"t": self.iso(t), "kind": kind, "from": frm, "where": where,
                "preview": preview[:157] + "..." if len(preview) > 160 else preview, **extra}
        return self.store.deliver(item)


def as_list(value) -> list[str]:
    """A list argument as models send it: a list, one comma-separated string, or a JSON list in a string."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    items = [value] if isinstance(value, str) else list(value or [])
    return [part.strip() for item in items for part in str(item).split(",") if part.strip()]


def is_external(address: str) -> bool:
    return not address.lower().endswith("@" + INTERNAL_DOMAIN)


def terms_match(query: str | None, *texts) -> bool:
    if not query:
        return True
    hay = " ".join(str(t) for t in texts if t).lower()
    return all(term in hay for term in query.lower().split())


def snippet(text: str, query: str | None, width: int = 160) -> str:
    flat = " ".join((text or "").split())
    if not query:
        return flat[:width]
    i = flat.lower().find(query.lower().split()[0])
    lo = max(0, i - width // 3) if i >= 0 else 0
    return ("..." if lo else "") + flat[lo:lo + width]


__all__ = ["NotFound", "ToolError", "Workspace", "as_list", "is_external", "snippet", "terms_match"]
