package com.smartinspector.tracelib;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * Receives config update broadcasts and applies them to HookConfigManager.
 *
 * <p>Action: {@code com.smartinspector.HOOK_CONFIG}
 * <p>Extra: {@code config} — JSON string with hook configuration
 */
public class HookConfigReceiver extends BroadcastReceiver {

    private static final String TAG = "SmartInspector";
    public static final String ACTION_CONFIG = "com.smartinspector.HOOK_CONFIG";
    public static final String EXTRA_CONFIG = "config";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!ACTION_CONFIG.equals(intent.getAction())) return;

        String json = intent.getStringExtra(EXTRA_CONFIG);
        if (json == null || json.isEmpty()) {
            Log.w(TAG, "Received empty config broadcast");
            return;
        }

        Log.i(TAG, "Received config update broadcast");
        HookConfigManager.updateFromJson(json);
    }
}
