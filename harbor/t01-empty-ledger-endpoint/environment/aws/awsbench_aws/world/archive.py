"""s3://vl-endpointd-plan-archive served from endpointd's archive, so the bucket holds every generation the planner
wrote, as the design says, and agrees with `epctl plan show --from-archive`. Operators may not write to it: writes are
refused, and the bridge logs them like any other call."""

import hashlib
import io
from datetime import UTC, datetime

from botocore.response import StreamingBody

from ..errors import AwsError

BUCKET = "vl-endpointd-plan-archive"
READS = ("ListObjects", "ListObjectsV2", "GetObject", "HeadObject")
WRITES = ("Put", "Delete", "Copy", "Create", "Upload", "Complete", "Restore", "Abort")


class PlanArchive:
    def __init__(self, endpointd):
        self.ep = endpointd

    def _objects(self, now: datetime) -> dict[str, int]:
        return {self.ep.archive_key(g): g for g in sorted(self.ep.archive) if self.ep.created[g] <= now}

    def _meta(self, gen: int) -> tuple[bytes, str, datetime]:
        body = self.ep.archive_body(gen).encode()
        return body, f'"{hashlib.md5(body).hexdigest()}"', self.ep.created[gen].astimezone(UTC)

    def serve(self, op: str, p: dict, now: datetime) -> dict | None:
        if p.get("Bucket") != BUCKET:
            return None
        if op.startswith(WRITES):
            raise AwsError("AccessDenied", "Access Denied", 403)
        if op not in READS:
            return None
        objects = self._objects(now)
        if op.startswith("List"):
            return self._list(op, p, objects)
        gen = objects.get(p.get("Key", ""))
        if gen is None:
            raise AwsError("404", "Not Found", 404) if op == "HeadObject" else AwsError(
                "NoSuchKey", "The specified key does not exist.", 404)
        body, etag, modified = self._meta(gen)
        out = {"AcceptRanges": "bytes", "LastModified": modified.strftime("%a, %d %b %Y %H:%M:%S GMT"),
               "ContentLength": len(body), "ETag": etag, "ContentType": "application/json", "Metadata": {}}
        if op == "GetObject":
            out["Body"] = StreamingBody(io.BytesIO(body), len(body))
        return out

    def _list(self, op: str, p: dict, objects: dict[str, int]) -> dict:
        prefix, delim = p.get("Prefix") or "", p.get("Delimiter")
        size = max(1, min(int(p.get("MaxKeys") or 1000), 1000))
        after = p.get("ContinuationToken") or p.get("StartAfter") or p.get("Marker") or ""
        items: list[tuple[str, str, str]] = []
        for key in objects:
            if not key.startswith(prefix) or key <= after:
                continue
            rest = key[len(prefix):]
            if delim and delim in rest:
                cp = prefix + rest.split(delim, 1)[0] + delim
                if items and items[-1][:2] == ("p", cp):
                    items[-1] = ("p", cp, key)
                else:
                    items.append(("p", cp, key))
            else:
                items.append(("k", key, key))
        page, more = items[:size], len(items) > size
        contents = []
        for kind, key, _ in page:
            if kind == "k":
                body, etag, modified = self._meta(objects[key])
                contents.append({"Key": key, "LastModified": modified.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 "ETag": etag, "Size": len(body), "StorageClass": "STANDARD"})
        out = {"Name": BUCKET, "Prefix": prefix, "MaxKeys": size, "IsTruncated": more}
        if contents:
            out["Contents"] = contents
        prefixes = [{"Prefix": key} for kind, key, _ in page if kind == "p"]
        if prefixes:
            out["CommonPrefixes"] = prefixes
        if delim:
            out["Delimiter"] = delim
        if op == "ListObjectsV2":
            out["KeyCount"] = len(page)
            for k in ("ContinuationToken", "StartAfter"):
                if p.get(k):
                    out[k] = p[k]
            if more:
                out["NextContinuationToken"] = page[-1][2]
        else:
            out["Marker"] = p.get("Marker") or ""
            if more:
                out["NextMarker"] = page[-1][2]
        return out
