package com.smartinspector.tracelib;

import android.content.Context;

/**
 * Release variant: pure no-op stub. Zero overhead.
 * Pine framework is not included in release builds.
 */
public class TraceHook {

    private TraceHook() {}

    public static void init() {}

    public static void init(Context context) {}

    public static SIClient getWsClient() {
        return null;
    }
}
