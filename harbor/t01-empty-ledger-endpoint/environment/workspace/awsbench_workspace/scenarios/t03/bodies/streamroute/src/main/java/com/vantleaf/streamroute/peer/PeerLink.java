package com.vantleaf.streamroute.peer;

public final class PeerLink {
    private final Peer peer;
    private final Thread inbound;
    private final Thread outbound;

    PeerLink(Peer peer) {
        this.peer = peer;
        this.inbound = new Thread(this::readLoop, "peerlink-in-" + peer.id());
        this.outbound = new Thread(this::writeLoop, "peerlink-out-" + peer.id());
    }

    void start() {
        inbound.start();   // may throw OutOfMemoryError("unable to create native thread") at the user's RLIMIT_NPROC
        outbound.start();
    }

    private void readLoop() { /* gossip frames, lease hints */ }
    private void writeLoop() { /* heartbeats, map digests */ }
}
