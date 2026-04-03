package com.smartinspector.hook;

import android.app.Application;
import android.util.Log;

import com.smartinspector.tracelib.TraceHook;

public class App extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        Log.i("SmartInspector", "Initializing TraceHook...");
        TraceHook.init(this);
        Log.i("SmartInspector", "TraceHook initialized");
    }
}
