"""Wiki: pages in spaces, every edit a new version (history, restore), and page comments."""

from datetime import datetime

from .base import ToolError, Workspace, snippet, terms_match

K_PAGE = "wiki.page"


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    for p in doc.get("pages", []):
        versions = [{"version": v["version"], "author": v["author"], "ts": ws.iso(ws.when(v["ts"])), "body": v["body"],
                     "comment": v.get("comment", "")} for v in p["versions"]]
        cur = versions[-1]
        ws.store.create(K_PAGE, p["id"], {
            "id": p["id"], "space": p["space"], "title": p["title"], "labels": p.get("labels", []),
            "author": p.get("author", versions[0]["author"]), "created": ws.iso(ws.when(p.get("created"))) or versions[0]["ts"],
            "updated": cur["ts"], "version": cur["version"], "body": cur["body"], "versions": versions,
            "comments": []}, by="seed", t=t0)


def find(ws: Workspace, page: str) -> dict | None:
    """By id, 'SPACE/Title', exact title, or a title that starts with or uniquely contains the text."""
    pages = ws.store.items(K_PAGE)
    key = page.strip()
    low = key.lower()
    if (p := ws.store.get(K_PAGE, key)) is not None:
        return p
    space, _, title = key.partition("/")
    for test in (lambda p: title and p["space"].lower() == space.lower() and p["title"].lower() == title.lower(),
                 lambda p: p["title"].lower() == low,
                 lambda p: title and p["space"].lower() == space.lower() and p["title"].lower().startswith(title.lower()),
                 lambda p: p["title"].lower().startswith(low)):
        hits = [p for p in pages if test(p)]
        if len(hits) == 1:
            return hits[0]
    hits = [p for p in pages if low in p["title"].lower()]
    return hits[0] if len(hits) == 1 else None


def _page(ws: Workspace, page: str) -> dict:
    if (p := find(ws, page)) is None:
        raise ToolError(f"no wiki page {page!r}; wiki_search finds pages by words in their title or body")
    return p


def _resolved(p: dict, fields: list[str]) -> dict:
    return {"app": "wiki", "page": p["id"], "fields_changed": fields, "mentions": [], "recipients": [],
            "external": False}


def _view(p: dict, version: dict | None = None) -> dict:
    v = version or {"version": p["version"], "author": p["versions"][-1]["author"], "ts": p["updated"], "body": p["body"]}
    return {"id": p["id"], "space": p["space"], "title": p["title"], "labels": p["labels"], "version": v["version"],
            "latest_version": p["version"], "author": v["author"], "updated": v["ts"], "body": v["body"],
            "comments": p["comments"]}


# tools
def search(ws: Workspace, actor: str, t: datetime, query: str, space: str | None = None, limit: int = 10) -> dict:
    hits = [p for p in ws.store.items(K_PAGE)
            if (space is None or p["space"].lower() == space.lower()) and terms_match(query, p["title"], p["body"],
                                                                                        " ".join(p["labels"]))]
    return {"total": len(hits), "pages": [{"id": p["id"], "space": p["space"], "title": p["title"],
                                           "version": p["version"], "updated": p["updated"],
                                           "snippet": snippet(p["body"], query)} for p in hits[:max(1, limit)]]}


def get(ws: Workspace, actor: str, t: datetime, page: str, version: int | None = None) -> dict:
    p = _page(ws, page)
    if version is None:
        return {"page": _view(p)}
    v = next((v for v in p["versions"] if v["version"] == version), None)
    if v is None:
        raise ToolError(f"{p['id']} has versions {[v['version'] for v in p['versions']]}")
    return {"page": _view(p, v)}


def history(ws: Workspace, actor: str, t: datetime, page: str) -> dict:
    p = _page(ws, page)
    return {"id": p["id"], "title": p["title"], "versions": [{k: v[k] for k in ("version", "author", "ts", "comment")}
                                                             for v in reversed(p["versions"])]}


def _new_version(ws: Workspace, p: dict, body: str, by: str, t: datetime, comment: str, title: str | None = None):
    v = {"version": p["version"] + 1, "author": by, "ts": ws.iso(t), "body": body, "comment": comment}
    fields = {"version": v["version"], "body": body, "updated": v["ts"], "versions": p["versions"] + [v]}
    if title:
        fields["title"] = title
    ws.store.update(K_PAGE, p["id"], fields, by=by, t=t, op="edit")
    return v


def update(ws: Workspace, actor: str, t: datetime, page: str, body: str, comment: str = "",
           title: str | None = None, space: str | None = None):
    p = find(ws, page)
    if p is None:
        if not space:
            raise ToolError(f"no wiki page {page!r}; give space to create it")
        pid = f"{space.upper()}-PAGE-{ws.next_id('wiki', 1)}"
        p = ws.store.create(K_PAGE, pid, {
            "id": pid, "space": space.upper(), "title": title or page, "labels": [], "author": actor,
            "created": ws.iso(t), "updated": ws.iso(t), "version": 1, "body": body,
            "versions": [{"version": 1, "author": actor, "ts": ws.iso(t), "body": body, "comment": comment}],
            "comments": []}, by=actor, t=t)
        return {"created": {"id": pid, "space": space.upper(), "title": title or page, "version": 1}}, _resolved(
            p, ["created"])
    if body == p["body"] and not title:
        raise ToolError("the body is unchanged")
    v = _new_version(ws, p, body, actor, t, comment, title)
    return {"id": p["id"], "version": v["version"]}, _resolved(p, ["body"])


def restore(ws: Workspace, actor: str, t: datetime, page: str, version: int, comment: str = ""):
    p = _page(ws, page)
    old = next((v for v in p["versions"] if v["version"] == version), None)
    if old is None:
        raise ToolError(f"{p['id']} has versions {[v['version'] for v in p['versions']]}")
    if version == p["version"]:
        raise ToolError(f"version {version} is already current")
    v = _new_version(ws, p, old["body"], actor, t, comment or f"restored version {version}")
    return {"id": p["id"], "version": v["version"], "restored_from": version}, _resolved(p, ["body"])


def comment(ws: Workspace, actor: str, t: datetime, page: str, body: str):
    p = _page(ws, page)
    c = {"id": f"wc-{ws.next_id('wikicomment', 1)}", "author": actor, "ts": ws.iso(t), "body": body,
         "mentions": ws.mentions(body)}
    ws.store.append(K_PAGE, p["id"], "comments", c, by=actor, t=t, op="comment")
    return {"id": p["id"], "comment": c}, {**_resolved(p, ["comments"]), "mentions": c["mentions"]}
