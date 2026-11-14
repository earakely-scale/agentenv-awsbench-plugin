package com.vantleaf.streamroute.shardmap;

final class ShardMapManager {
    private volatile boolean buildInProgress;
    private volatile long inFlight;

    void rebuild(long epoch) {
        if (buildInProgress) {
            log.info("rebuild skipped: build already in progress (epoch {})", inFlight);
            return;
        }
        buildInProgress = true;
        inFlight = epoch;
        try {
            builder.build(epoch);
            buildInProgress = false;
        } catch (Exception e) {
            buildInProgress = false;
            log.error("build epoch={} failed", epoch, e);
        }
        // no finally: a java.lang.Error (e.g. OutOfMemoryError) escapes the catch and leaves
        // buildInProgress == true until the process restarts
    }
}
