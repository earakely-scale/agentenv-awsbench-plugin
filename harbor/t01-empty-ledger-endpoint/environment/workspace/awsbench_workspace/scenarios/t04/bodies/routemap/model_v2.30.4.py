# routemap/model.py at tag v2.30.4 (also vendored in the workflow images)
class Route:
    @classmethod
    def from_item(cls, item):
        spec = item.get("route_spec") or {}    # missing spec -> defaults
        return cls(merchant_id=item["merchant_id"], route_id=item["route_id"], idem_ns=item["idem_ns"],
                   lifecycle=item.get("lifecycle", "ACTIVE"), version=int(item["route_version"]),
                   spec=RouteSpec.from_dict(spec),          # defaults: cells [], pool-default, fallback cell-drain
                   settlement=item.get("settlement_profile"))
