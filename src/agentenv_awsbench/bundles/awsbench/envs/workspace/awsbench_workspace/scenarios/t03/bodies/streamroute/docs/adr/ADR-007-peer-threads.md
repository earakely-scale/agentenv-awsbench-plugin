# ADR-007: one reader and one writer thread per peer link

Status: accepted (2024-02)

Each streamroute host keeps a PeerLink to every other member for shard-map gossip and lease hints. Each link uses a
dedicated inbound reader thread and a dedicated outbound writer thread (blocking I/O). This is simple and easy to
debug. Cost: 2 threads per peer, so per-host thread count grows with fleet size. Revisit above 200 hosts (options:
NIO selector, peer sampling, a dedicated shard-map fleet).
