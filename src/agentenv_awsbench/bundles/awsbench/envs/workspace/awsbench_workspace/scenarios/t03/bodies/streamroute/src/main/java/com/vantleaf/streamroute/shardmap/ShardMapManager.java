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
        // TODO: move the reset into a finally block; a java.lang.Error escapes this catch
    }
}
