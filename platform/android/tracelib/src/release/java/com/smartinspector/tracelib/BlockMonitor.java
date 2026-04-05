package com.smartinspector.tracelib;

/** Release variant: no-op BlockMonitor stub. */
public class BlockMonitor {
    public static void start(long thresholdMs) {}
    public static void stop() {}
    public static synchronized String getAndClearEventsJson() { return "[]"; }
}
