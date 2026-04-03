package com.smartinspector.tracelib;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;

/**
 * Manages HookConfig lifecycle: load from SharedPreferences, query, update.
 *
 * <p>Must be initialized with {@link #init(Context)} before use.
 * Thread-safe for reads; writes are synchronized.
 */
public class HookConfigManager {

    private static final String TAG = "SmartInspector";
    private static final String SP_NAME = "smartinspector_hooks";
    private static final String KEY_CONFIG = "hook_config_json";

    private static volatile HookConfig config;
    private static SharedPreferences prefs;

    /** Initialize from application context. Loads saved config or defaults. */
    public static synchronized void init(Context context) {
        prefs = context.getApplicationContext().getSharedPreferences(SP_NAME, Context.MODE_PRIVATE);
        String json = prefs.getString(KEY_CONFIG, null);
        if (json != null) {
            config = HookConfig.fromJson(json);
            Log.d(TAG, "Loaded hook config from SP");
        } else {
            config = HookConfig.defaults();
            Log.d(TAG, "Using default hook config");
        }
    }

    /** Ensure config is available; fallback to defaults if not initialized. */
    private static HookConfig ensureConfig() {
        if (config == null) {
            config = HookConfig.defaults();
        }
        return config;
    }

    /** Check if a specific hook category is enabled. */
    public static boolean isEnabled(String hookId) {
        HookConfig c = ensureConfig();
        switch (hookId) {
            case "activity_lifecycle": return c.activityLifecycle;
            case "fragment_lifecycle": return c.fragmentLifecycle;
            case "rv_pipeline": return c.rvPipeline;
            case "rv_adapter": return c.rvAdapter;
            case "layout_inflate": return c.layoutInflate;
            case "view_traverse": return c.viewTraverse;
            case "handler_dispatch": return c.handlerDispatch;
            case "block_monitor": return c.blockMonitor;
            case "network_io": return c.networkIo;
            case "database_io": return c.databaseIo;
            case "image_load": return c.imageLoad;
            case "input_event": return c.inputEvent;
            // Perfetto boolean toggles
            case "cpu_callstacks": return c.collectCpuCallstacks;
            case "java_heap": return c.collectJavaHeap;
            case "gpu_mem": return c.collectGpuMem;
            case "logcat": return c.collectLogcat;
            case "frame_timeline": return c.collectFrameTimeline;
            case "focus_main_thread": return c.focusMainThread;
            default: return false;
        }
    }

    /**
     * Update config from a JSON string.
     * Saves to SP + updates in-memory + syncs via WS.
     */
    public static synchronized void updateFromJson(String json) {
        updateFromJson(json, true);
    }

    /**
     * Update config from a JSON string with optional WS sync.
     * @param syncToWs false when called from WS incoming message to avoid echo.
     */
    public static synchronized void updateFromJson(String json, boolean syncToWs) {
        HookConfig newConfig = HookConfig.fromJson(json);
        config = newConfig;
        if (prefs != null) {
            prefs.edit().putString(KEY_CONFIG, json).apply();
        }
        if (syncToWs) {
            SIClient ws = TraceHook.getWsClient();
            if (ws != null && ws.isConnected()) {
                ws.sendConfig(config.toJson());
            }
        }
        Log.i(TAG, "Hook config updated" + (syncToWs ? "" : " (from WS, skip sync)"));
    }

    /** Return current config as JSON string. */
    public static String getConfig() {
        return ensureConfig().toJson();
    }

    /** Return list of enabled extra hooks. */
    public static List<HookConfig.ExtraHook> getExtraHooks() {
        HookConfig c = ensureConfig();
        List<HookConfig.ExtraHook> enabled = new ArrayList<>();
        for (HookConfig.ExtraHook eh : c.extraHooks) {
            if (eh.enabled) {
                enabled.add(eh);
            }
        }
        return enabled;
    }

    /** Return block monitor threshold in ms. */
    public static long getBlockThresholdMs() {
        return ensureConfig().blockThresholdMs;
    }

    /** Reset config to defaults. */
    public static synchronized void resetDefaults() {
        config = HookConfig.defaults();
        if (prefs != null) {
            prefs.edit().remove(KEY_CONFIG).apply();
        }
        Log.i(TAG, "Hook config reset to defaults");
    }
}
