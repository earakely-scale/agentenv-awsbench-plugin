package com.vantleaf.streamroute.shardmap;

final class RebuildLoop implements Runnable {
    public void run() {
        while (running) {
            try {
                manager.rebuild(nextEpoch());
            } catch (Throwable t) {
                /* already logged by the builder; keep looping */
            }
            sleep(rebuildInterval);
        }
    }
}
