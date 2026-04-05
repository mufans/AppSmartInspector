package com.smartinspector.tracelib;

import android.content.Context;

/** Release variant: no-op WS client stub. */
public class SIClient {
    public SIClient(Context context) {}
    public void connect() {}
    public void disconnect() {}
    public void sendConfig(String configJson) {}
    public void requestConfig() {}
    public boolean isConnected() { return false; }
}
