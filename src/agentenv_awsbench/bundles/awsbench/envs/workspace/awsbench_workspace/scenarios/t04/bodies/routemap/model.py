# routemap/model.py at v2.31.0 (PR #612)
class Route:
    @classmethod
    def from_item(cls, item):
        labels = item.get("labels", {})
        spec = item["route_spec"]            # line 211: strict since PR #612
        return cls(merchant_id=item["merchant_id"], route_id=item["route_id"], idem_ns=item["idem_ns"],
                   lifecycle=item["lifecycle"], version=int(item["route_version"]),
                   spec=RouteSpec.from_dict(spec), settlement=item.get("settlement_profile"), labels=labels)
