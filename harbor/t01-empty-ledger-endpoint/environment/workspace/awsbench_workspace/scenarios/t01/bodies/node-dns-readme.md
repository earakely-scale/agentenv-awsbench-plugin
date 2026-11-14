# terraform/modules/node-dns

Node-local unbound cache on vl-core and endpointd hosts (since 2026-06), forwarding to the VPC resolver (base+2), to stay
under the resolver's 1,024 packets/s per ENI.

- Positive answers are cached for the record TTL; negative answers (NXDOMAIN/NODATA) for the zone's SOA negative TTL
  (RFC 2308: min(SOA TTL, SOA minimum)). `internal.vantleaf.io` uses the default SOA (TTL 900, minimum 86400), so 900 s.
- Flush one name on a host: `unbound-control flush <name>`.
