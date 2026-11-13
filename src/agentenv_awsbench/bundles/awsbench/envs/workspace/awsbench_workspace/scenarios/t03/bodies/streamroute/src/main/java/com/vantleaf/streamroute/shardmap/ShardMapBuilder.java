package com.vantleaf.streamroute.shardmap;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class ShardMapBuilder {
    void build(long epoch) {
        ExecutorService pool = Executors.newFixedThreadPool(cfg.buildWorkers(), named("shardmap-build"));
        int started = 0;
        try {
            for (int i = 0; i < cfg.buildWorkers(); i++) {
                pool.execute(task(i));   // starts a platform thread; EAGAIN at RLIMIT_NPROC -> OutOfMemoryError
                started++;
            }
            awaitAll(pool);
        } catch (Throwable t) {
            log.error("build epoch={} failed after {}/{} workers started", epoch, started, cfg.buildWorkers(), t);
            throw t;
        } finally {
            pool.shutdownNow();
        }
    }
}
