"""In-memory record store: one global sequence, every record versioned with who changed what and when (virtual time),
so the grader can replay the collaboration state at any moment."""

import copy
from datetime import datetime
from typing import Any


class NotFound(LookupError):
    pass


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


class Store:
    def __init__(self):
        self.seq = 0
        self._records: dict[tuple[str, str], dict] = {}
        self.inbox: list[dict] = []
        self.action_log: list[dict] = []

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _entry(self, by: str, t: datetime, op: str, changes: dict) -> dict:
        return {"seq": self.next_seq(), "t": _iso(t), "by": by, "op": op, "changes": copy.deepcopy(changes)}

    def create(self, kind: str, rid: str, data: dict, *, by: str, t: datetime, op: str = "create") -> dict:
        if (kind, rid) in self._records:
            raise ValueError(f"{kind} {rid!r} already exists")
        entry = self._entry(by, t, op, data)
        self._records[(kind, rid)] = {"kind": kind, "id": rid, "data": copy.deepcopy(data), "created_seq": entry["seq"],
                                      "history": [entry]}
        return self._records[(kind, rid)]["data"]

    def get(self, kind: str, rid: str) -> dict | None:
        rec = self._records.get((kind, rid))
        return rec["data"] if rec else None

    def require(self, kind: str, rid: str) -> dict:
        if (data := self.get(kind, rid)) is None:
            raise NotFound(f"no {kind.split('.')[-1]} {rid!r}")
        return data

    def update(self, kind: str, rid: str, fields: dict, *, by: str, t: datetime, op: str = "update") -> dict:
        """Set fields; returns {field: {from, to}} for the ones that changed (nothing is recorded when none did)."""
        data = self.require(kind, rid)
        changes = {k: {"from": copy.deepcopy(data.get(k)), "to": copy.deepcopy(v)}
                   for k, v in fields.items() if data.get(k) != v}
        if changes:
            for k, v in fields.items():
                data[k] = copy.deepcopy(v)
            self._records[(kind, rid)]["history"].append(self._entry(by, t, op, changes))
        return changes

    def append(self, kind: str, rid: str, field: str, item: Any, *, by: str, t: datetime, op: str) -> None:
        data = self.require(kind, rid)
        data.setdefault(field, []).append(copy.deepcopy(item))
        self._records[(kind, rid)]["history"].append(self._entry(by, t, op, {field: {"added": item}}))

    def items(self, kind: str) -> list[dict]:
        return [r["data"] for (k, _), r in self._records.items() if k == kind]

    def history(self, kind: str, rid: str) -> list[dict]:
        self.require(kind, rid)
        return copy.deepcopy(self._records[(kind, rid)]["history"])

    def deliver(self, item: dict) -> dict:
        item = {"seq": self.next_seq(), **item}
        self.inbox.append(item)
        return item

    def inbox_since(self, since: int) -> list[dict]:
        return [i for i in self.inbox if i["seq"] > since]

    def log(self, entry: dict) -> None:
        self.action_log.append({"seq": self.next_seq(), **entry})

    def export(self) -> dict:
        """{app: {kind: [{id, data, history}]}} in creation order."""
        out: dict = {}
        for (kind, rid), rec in sorted(self._records.items(), key=lambda kv: kv[1]["created_seq"]):
            app, _, sub = kind.partition(".")
            out.setdefault(app, {}).setdefault(sub, []).append(
                {"id": rid, "data": copy.deepcopy(rec["data"]), "history": copy.deepcopy(rec["history"])})
        return out
