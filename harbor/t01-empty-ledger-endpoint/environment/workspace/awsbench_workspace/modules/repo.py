"""Code host, read-only: repositories with their files, commits, pull requests and deploys."""

from datetime import datetime

from .base import ToolError, Workspace, terms_match

K_REPO = "repo.repo"


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    for r in doc.get("repos", []):
        tree = {path: (v if isinstance(v, str) else v.get("content", "")) for path, v in r.get("tree", {}).items()}
        stamp = lambda items, *keys: [dict(i, **{k: ws.iso(ws.when(i[k])) for k in keys if i.get(k)}) for i in items]
        ws.store.create(K_REPO, r["name"], {
            "name": r["name"], "description": r.get("description", ""), "default_branch": r.get("default_branch", "main"),
            "protected": r.get("protected", []), "tree": tree, "commits": stamp(r.get("commits", []), "ts"),
            "prs": stamp(r.get("prs", []), "created", "merged_at"), "deploys": stamp(r.get("deploys", []), "ts")},
            by="seed", t=t0)


def _repo(ws: Workspace, name: str) -> dict:
    name = name.strip()
    repo = ws.store.get(K_REPO, name) or ws.store.get(K_REPO, f"vantleaf/{name}")
    if repo is None:
        raise ToolError(f"no repository {name!r}; repo_list lists them")
    return repo


def list_(ws: Workspace, actor: str, t: datetime) -> dict:
    return {"repos": [{"name": r["name"], "description": r["description"], "default_branch": r["default_branch"],
                       "files": len(r["tree"]), "open_prs": sum(1 for p in r["prs"] if p.get("state") != "merged")}
                      for r in ws.store.items(K_REPO)]}


def get(ws: Workspace, actor: str, t: datetime, repo: str, pr: int | None = None) -> dict:
    r = _repo(ws, repo)
    if pr is not None:
        hit = next((p for p in r["prs"] if p["number"] == pr), None)
        if hit is None:
            raise ToolError(f"{r['name']} has no PR #{pr}")
        return {"repo": r["name"], "pr": hit}
    return {"repo": {k: r[k] for k in ("name", "description", "default_branch", "protected")},
            "files": sorted(r["tree"]), "prs": [{k: p.get(k) for k in ("number", "title", "author", "state", "created",
                                                                      "merged_at")} for p in r["prs"]],
            "deploys": r["deploys"]}


def read(ws: Workspace, actor: str, t: datetime, repo: str, path: str) -> dict:
    r = _repo(ws, repo)
    path = path.strip().lstrip("/")
    if path not in r["tree"]:
        near = [p for p in r["tree"] if p.startswith(path.rstrip("/") + "/")]
        if near:
            return {"repo": r["name"], "path": path, "directory": sorted(near)}
        raise ToolError(f"{r['name']} has no file {path!r}; repo_get lists its files")
    return {"repo": r["name"], "path": path, "ref": r["default_branch"], "content": r["tree"][path]}


def search(ws: Workspace, actor: str, t: datetime, query: str, repo: str | None = None, limit: int = 30) -> dict:
    repos = [_repo(ws, repo)] if repo else ws.store.items(K_REPO)
    hits = []
    for r in repos:
        for path, content in r["tree"].items():
            if terms_match(query, path):
                hits.append({"repo": r["name"], "path": path, "line": None, "text": "(path match)"})
            for n, line in enumerate(content.splitlines(), 1):
                if terms_match(query, line):
                    hits.append({"repo": r["name"], "path": path, "line": n, "text": line.strip()[:200]})
    return {"query": query, "total": len(hits), "matches": hits[:max(1, limit)]}


def log(ws: Workspace, actor: str, t: datetime, repo: str, path: str | None = None, limit: int = 20) -> dict:
    r = _repo(ws, repo)
    commits = [c for c in r["commits"] if path is None or path in c.get("files", [path])]
    merged = [{"pr": p["number"], "title": p["title"], "author": p.get("author"), "merged_at": p["merged_at"],
               "files": p.get("files", [])} for p in r["prs"] if p.get("merged_at")
              and (path is None or path in p.get("files", []))]
    commits = sorted(commits, key=lambda c: c.get("ts") or "", reverse=True)
    return {"repo": r["name"], "commits": commits[:limit], "merged_prs": merged, "deploys": r["deploys"]}
