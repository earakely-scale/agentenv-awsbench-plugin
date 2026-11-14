Alarm routemap-api-5xx-high has been in ALARM since 09:14 ET. About 9% of UpdateRoute and DescribeRoute calls from merchant-dashboard return 500 (merchant settings saves failing). CreateRoute is fine.

Trace from /ecs/routemap-api:

```
InternalError: route reconcile failed
  File "routemap/model.py", line 211, in from_item
    spec = item['route_spec']
KeyError: 'route_spec'
```

Possibly PR #612 (v2.31.0, deployed Fri 16:05), which refactored Route.from_item. Leaving at P3 for Monday unless it grows.
