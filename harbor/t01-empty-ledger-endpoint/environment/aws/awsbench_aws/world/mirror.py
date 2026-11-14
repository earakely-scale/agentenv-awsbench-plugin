"""The world's cached view of the AWS state it shares with the agent, read and written only through the AwsPort."""

import json

WORLD_PRINCIPAL = "world:physics"


def _s(item, key, default=None):
    v = item.get(key)
    if not isinstance(v, dict):
        return default
    if "S" in v:
        return v["S"]
    if "N" in v:
        n = v["N"]
        return int(n) if str(n).lstrip("-").isdigit() else float(n)
    if "BOOL" in v:
        return v["BOOL"]
    return default


def typed(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, (dict, list)):
        return {"S": json.dumps(value, separators=(",", ":"), sort_keys=True)}
    return {"S": str(value)}


def untyped(item: dict) -> dict:
    return {k: _s(item, k) for k in item}


class AwsMirror:
    """Route 53 records of one zone, the endpointd-plans table and SSM parameters, as last read or written."""

    def __init__(self, aws, zone: str, table: str, ssm_paths: list[str], ssm_names: list[str]):
        self.aws = aws
        self.zone = zone
        self.table = table
        self.ssm_paths = ssm_paths
        self.ssm_names = ssm_names
        self.zone_id: str | None = None
        self.records: dict[str, list[dict]] = {}
        self.items: dict[str, dict] = {}
        self.ssm: dict[str, str] = {}

    def call(self, service, operation, params, principal=WORLD_PRINCIPAL, world_made=True):
        return self.aws.call(service, operation, params, principal=principal, world_made=world_made)

    def sync(self) -> None:
        self.sync_records()
        self.sync_items()
        self.sync_ssm()

    def sync_records(self) -> None:
        if self.zone_id is None:
            out = self.call("route53", "ListHostedZonesByName", {"DNSName": self.zone})
            for z in out.get("HostedZones", []):
                if z["Name"].rstrip(".") == self.zone:
                    self.zone_id = z["Id"].split("/")[-1]
                    break
        if self.zone_id is None:
            return
        records: dict[str, list[dict]] = {}
        params = {"HostedZoneId": self.zone_id}
        while True:
            out = self.call("route53", "ListResourceRecordSets", params)
            for rr in out.get("ResourceRecordSets", []):
                if rr.get("Type") != "A":
                    continue
                name = rr["Name"].rstrip(".")
                for value in rr.get("ResourceRecords", []):
                    records.setdefault(name, []).append({
                        "SetIdentifier": rr.get("SetIdentifier"), "Value": value["Value"],
                        "Weight": rr.get("Weight", 100), "TTL": rr.get("TTL", 60)})
            if not out.get("IsTruncated"):
                break
            params = {"HostedZoneId": self.zone_id, "StartRecordName": out["NextRecordName"],
                      "StartRecordType": out.get("NextRecordType", "A")}
            if out.get("NextRecordIdentifier"):
                params["StartRecordIdentifier"] = out["NextRecordIdentifier"]
        self.records = records

    def sync_items(self) -> None:
        items = {}
        params = {"TableName": self.table}
        while True:
            out = self.call("dynamodb", "Scan", params)
            for it in out.get("Items", []):
                items[_s(it, "pk")] = untyped(it)
            if not out.get("LastEvaluatedKey"):
                break
            params = {"TableName": self.table, "ExclusiveStartKey": out["LastEvaluatedKey"]}
        self.items = items

    def sync_ssm(self) -> None:
        values = {}
        for path in self.ssm_paths:
            params = {"Path": path, "Recursive": True}
            while True:
                out = self.call("ssm", "GetParametersByPath", params)
                for p in out.get("Parameters", []):
                    values[p["Name"]] = p["Value"]
                if not out.get("NextToken"):
                    break
                params = {"Path": path, "Recursive": True, "NextToken": out["NextToken"]}
        names = [n for n in self.ssm_names if n not in values]
        for i in range(0, len(names), 10):
            out = self.call("ssm", "GetParameters", {"Names": names[i:i + 10]})
            for p in out.get("Parameters", []):
                values[p["Name"]] = p["Value"]
        self.ssm = values

    # ---- reads from the cache ----
    def live(self, fqdn: str) -> list[dict]:
        return list(self.records.get(fqdn, []))

    def plan_gens(self) -> list[int]:
        return sorted(int(v["generation"]) for k, v in self.items.items() if k.startswith("plan#"))

    def plan(self, gen: int) -> dict | None:
        return self.items.get(f"plan#{gen:010d}")

    def pointers(self) -> dict[str, dict]:
        return {k[len("applied#"):]: v for k, v in self.items.items() if k.startswith("applied#")}

    # ---- writes, mirrored into the cache once the port accepts them ----
    def put_item(self, item: dict, principal=WORLD_PRINCIPAL, world_made=True) -> None:
        self.call("dynamodb", "PutItem", {"TableName": self.table, "Item": {k: typed(v) for k, v in item.items()}},
                  principal, world_made)
        self.items[item["pk"]] = dict(item)

    def update_item(self, pk: str, fields: dict, principal=WORLD_PRINCIPAL, world_made=True) -> None:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": typed(v) for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self.call("dynamodb", "UpdateItem", {"TableName": self.table, "Key": {"pk": {"S": pk}},
                                             "UpdateExpression": expr, "ExpressionAttributeNames": names,
                                             "ExpressionAttributeValues": values}, principal, world_made)
        self.items.setdefault(pk, {"pk": pk}).update(fields)

    def delete_item(self, pk: str, principal=WORLD_PRINCIPAL, world_made=True) -> None:
        self.call("dynamodb", "DeleteItem", {"TableName": self.table, "Key": {"pk": {"S": pk}}}, principal, world_made)
        self.items.pop(pk, None)

    def replace_records(self, fqdn: str, ttl: int, records: list[dict], comment: str,
                        principal=WORLD_PRINCIPAL, world_made=True) -> None:
        """UPSERT `records` ({set_id, ip, weight}) at `fqdn` and DELETE every other A record there."""
        keep = {r["set_id"] for r in records}
        changes = [{"Action": "DELETE", "ResourceRecordSet": {
            "Name": fqdn, "Type": "A", "SetIdentifier": r["SetIdentifier"], "Weight": r["Weight"], "TTL": r["TTL"],
            "ResourceRecords": [{"Value": r["Value"]}]}} for r in self.live(fqdn) if r["SetIdentifier"] not in keep]
        changes += [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": fqdn, "Type": "A", "SetIdentifier": r["set_id"], "Weight": r["weight"], "TTL": ttl,
            "ResourceRecords": [{"Value": r["ip"]}]}} for r in records]
        if not changes:
            return
        self.call("route53", "ChangeResourceRecordSets", {"HostedZoneId": self.zone_id, "ChangeBatch": {
            "Comment": comment, "Changes": changes}}, principal, world_made)
        self.records[fqdn] = [{"SetIdentifier": r["set_id"], "Value": r["ip"], "Weight": r["weight"], "TTL": ttl}
                              for r in records]
        if not self.records[fqdn]:
            del self.records[fqdn]

    def put_parameter(self, name: str, value: str, principal=WORLD_PRINCIPAL, world_made=True) -> None:
        self.call("ssm", "PutParameter", {"Name": name, "Value": value, "Type": "String", "Overwrite": True},
                  principal, world_made)
        self.ssm[name] = value
