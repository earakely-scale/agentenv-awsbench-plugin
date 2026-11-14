# RB-SR-04 streamroute: shard-map build failures

**Symptom.** `ShardMapBuilder` logs `build ... failed: java.lang.OutOfMemoryError`. Hosts drop to STALE, then BROKEN
when the map expires.

**Cause (PLAT-1874).** Heap exhaustion during the build.

**Fix.** Raise `/streamroute/prod/jvm/xmx` by 2g and rolling-restart the fleet one AZ at a time.

Last edited 2024-08-20 (Elena Sokolova). Related: POST-2024-031.
