package com.smartinspector.tracelib;

import android.app.Activity;
import android.content.Context;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/**
 * Debug-only configuration UI for SmartInspector.
 *
 * <p>Displays:
 * - WS connection status + reconnect button
 * - Hook toggles
 * - Perfetto collection params
 * - Perfetto analysis params
 * - Extra hooks info
 * - Reset / Save+Sync buttons
 */
public class HookConfigActivity extends Activity {

    private static final String[][] HOOK_TOGGLES = {
            {"Activity Lifecycle", "activity_lifecycle"},
            {"Fragment Lifecycle", "fragment_lifecycle"},
            {"RV Pipeline", "rv_pipeline"},
            {"RV Adapter", "rv_adapter"},
            {"Layout Inflate", "layout_inflate"},
            {"View Traverse", "view_traverse"},
            {"Handler Dispatch", "handler_dispatch"},
            {"Block Monitor", "block_monitor"},
            {"Network IO", "network_io"},
            {"Database IO", "database_io"},
            {"Image Load", "image_load"},
    };

    private TextView statusText;

    /** Tracked EditText fields for flushing before sync. */
    private final List<EditableField> editFields = new ArrayList<>();

    private static class EditableField {
        final EditText editText;
        final String configKey;
        final int type; // 0 = number, 1 = text

        EditableField(EditText editText, String configKey, int type) {
            this.editText = editText;
            this.configKey = configKey;
            this.type = type;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(32, 32, 32, 32);

        // ── Title ──
        TextView title = new TextView(this);
        title.setText("SmartInspector Config");
        title.setTextSize(20);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, 0, 0, 16);
        root.addView(title);

        // ── WS connection status ──
        LinearLayout wsRow = new LinearLayout(this);
        wsRow.setOrientation(LinearLayout.HORIZONTAL);
        wsRow.setGravity(Gravity.CENTER_VERTICAL);
        wsRow.setPadding(0, 4, 0, 16);

        statusText = new TextView(this);
        statusText.setLayoutParams(new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        updateConnectionStatus();

        Button reconnectBtn = new Button(this);
        reconnectBtn.setText("Reconnect");
        reconnectBtn.setOnClickListener(v -> {
            SIClient ws = TraceHook.getWsClient();
            if (ws != null) {
                ws.disconnect();
                ws.connect();
                updateConnectionStatus();
            }
        });

        wsRow.addView(statusText);
        wsRow.addView(reconnectBtn);
        root.addView(wsRow);

        // ── Hook Toggles ──
        addSectionHeader(root, "Hook Toggles");
        for (String[] hook : HOOK_TOGGLES) {
            addToggleRow(root, hook[0], hook[1]);
        }

        // ── Block Monitor params ──
        addSectionHeader(root, "Block Monitor");
        addNumberRow(root, "Threshold (ms)", "block_threshold_ms", 100);

        // ── Perfetto Collection ──
        addSectionHeader(root, "Perfetto Collection");
        addNumberRow(root, "Duration (ms)", "trace_duration_ms", 10000);
        addTextRow(root, "Target Process", "target_process", "");
        addNumberRow(root, "Buffer (KB)", "buffer_size_kb", 65536);
        addToggleRow(root, "CPU Callstacks", "cpu_callstacks");
        addNumberRow(root, "Sampling (ms)", "cpu_sampling_interval_ms", 1);
        addToggleRow(root, "Java Heap", "java_heap");
        addToggleRow(root, "GPU Mem", "gpu_mem");
        addToggleRow(root, "Logcat", "logcat");
        addToggleRow(root, "Frame Timeline", "frame_timeline");

        // ── Perfetto Analysis ──
        addSectionHeader(root, "Perfetto Analysis");
        addNumberRow(root, "Top Threads", "top_threads_count", 10);
        addNumberRow(root, "Hotspots", "hotspots_count", 15);
        addNumberRow(root, "Slow Slices", "slow_slices_count", 30);
        addToggleRow(root, "Focus Main Thread", "focus_main_thread");
        addNumberRow(root, "Jank Threshold (ms)", "jank_threshold_ms", 16);

        // ── Extra Hooks ──
        addSectionHeader(root, "Extra Hooks");
        TextView extraStatus = new TextView(this);
        extraStatus.setPadding(0, 0, 0, 8);
        updateExtraStatus(extraStatus);
        root.addView(extraStatus);

        // ── Buttons ──
        LinearLayout buttonRow = new LinearLayout(this);
        buttonRow.setOrientation(LinearLayout.HORIZONTAL);
        buttonRow.setPadding(0, 16, 0, 0);

        Button resetBtn = new Button(this);
        resetBtn.setText("Reset");
        resetBtn.setOnClickListener(v -> {
            HookConfigManager.resetDefaults();
            recreate();
        });
        buttonRow.addView(resetBtn);

        Button syncBtn = new Button(this);
        syncBtn.setText("Sync to CLI");
        syncBtn.setOnClickListener(v -> {
            // Flush all EditText fields to config before sending
            flushEditTexts();

            SIClient ws = TraceHook.getWsClient();
            if (ws != null && ws.isConnected()) {
                ws.sendConfig(HookConfigManager.getConfig());
                statusText.setText("WS: synced");
                statusText.setTextColor(0xFF4CAF50);
            } else {
                statusText.setText("WS: not connected");
                statusText.setTextColor(0xFFF44336);
            }
        });
        buttonRow.addView(syncBtn);

        Button closeBtn = new Button(this);
        closeBtn.setText("Close");
        closeBtn.setOnClickListener(v -> finish());
        buttonRow.addView(closeBtn);

        root.addView(buttonRow);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(root);
        setContentView(scroll);

        // Register WS state listener
        SIClient ws = TraceHook.getWsClient();
        if (ws != null) {
            ws.setStateListener(new SIClient.ConnectionStateListener() {
                @Override public void onConnected() {
                    runOnUiThread(() -> updateConnectionStatus());
                }
                @Override public void onDisconnected() {
                    runOnUiThread(() -> updateConnectionStatus());
                }
            });
        }
    }

    // ── Flush all EditText values to HookConfigManager ──────────

    private void flushEditTexts() {
        // Clear focus to trigger any pending onFocusChange callbacks
        View focused = getCurrentFocus();
        if (focused != null) {
            focused.clearFocus();
        }

        // Explicitly write each EditText value to config
        HookConfig config = HookConfig.fromJson(HookConfigManager.getConfig());
        boolean changed = false;

        for (EditableField field : editFields) {
            String text = field.editText.getText().toString().trim();
            if (field.type == 0) {
                // Number field
                try {
                    int newVal = Integer.parseInt(text);
                    int current = getConfigNumber(config, field.configKey, 0);
                    if (newVal != current) {
                        setConfigNumber(config, field.configKey, newVal);
                        changed = true;
                    }
                } catch (NumberFormatException ignored) {}
            } else {
                // Text field
                String current = getConfigText(config, field.configKey, "");
                if (!text.equals(current)) {
                    setConfigText(config, field.configKey, text);
                    changed = true;
                }
            }
        }

        if (changed) {
            HookConfigManager.updateFromJson(config.toJson());
        }
    }

    // ── UI helpers ──────────────────────────────────────────────

    private void addSectionHeader(LinearLayout parent, String text) {
        View divider = new View(this);
        divider.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 2));
        divider.setPadding(0, 16, 0, 8);
        parent.addView(divider);

        TextView header = new TextView(this);
        header.setText(text);
        header.setTextSize(16);
        header.setPadding(0, 0, 0, 8);
        parent.addView(header);
    }

    private void addToggleRow(LinearLayout parent, String label, String configKey) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, 4, 0, 4);

        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        Switch sw = new Switch(this);
        sw.setChecked(HookConfigManager.isEnabled(configKey));
        sw.setOnCheckedChangeListener((buttonView, isChecked) -> {
            HookConfig config = HookConfig.fromJson(HookConfigManager.getConfig());
            setConfigField(config, configKey, isChecked);
            HookConfigManager.updateFromJson(config.toJson());
        });

        row.addView(tv);
        row.addView(sw);
        parent.addView(row);
    }

    private void addNumberRow(LinearLayout parent, String label, String configKey, int defaultValue) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, 4, 0, 4);

        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        EditText et = new EditText(this);
        HookConfig current = HookConfig.fromJson(HookConfigManager.getConfig());
        int value = getConfigNumber(current, configKey, defaultValue);
        et.setText(String.valueOf(value));
        et.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        et.setEms(8);
        et.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) {
                try {
                    int newVal = Integer.parseInt(et.getText().toString().trim());
                    HookConfig config = HookConfig.fromJson(HookConfigManager.getConfig());
                    setConfigNumber(config, configKey, newVal);
                    HookConfigManager.updateFromJson(config.toJson());
                } catch (NumberFormatException ignored) {}
            }
        });

        editFields.add(new EditableField(et, configKey, 0));

        row.addView(tv);
        row.addView(et);
        parent.addView(row);
    }

    private void addTextRow(LinearLayout parent, String label, String configKey, String defaultValue) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, 4, 0, 4);

        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        EditText et = new EditText(this);
        HookConfig current = HookConfig.fromJson(HookConfigManager.getConfig());
        String value = getConfigText(current, configKey, defaultValue);
        et.setText(value);
        et.setEms(12);
        et.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) {
                HookConfig config = HookConfig.fromJson(HookConfigManager.getConfig());
                setConfigText(config, configKey, et.getText().toString().trim());
                HookConfigManager.updateFromJson(config.toJson());
            }
        });

        editFields.add(new EditableField(et, configKey, 1));

        row.addView(tv);
        row.addView(et);
        parent.addView(row);
    }

    // ── Config field helpers ────────────────────────────────────

    private void setConfigField(HookConfig config, String key, boolean value) {
        switch (key) {
            case "activity_lifecycle": config.activityLifecycle = value; break;
            case "fragment_lifecycle": config.fragmentLifecycle = value; break;
            case "rv_pipeline": config.rvPipeline = value; break;
            case "rv_adapter": config.rvAdapter = value; break;
            case "layout_inflate": config.layoutInflate = value; break;
            case "view_traverse": config.viewTraverse = value; break;
            case "handler_dispatch": config.handlerDispatch = value; break;
            case "block_monitor": config.blockMonitor = value; break;
            case "network_io": config.networkIo = value; break;
            case "database_io": config.databaseIo = value; break;
            case "image_load": config.imageLoad = value; break;
            case "cpu_callstacks": config.collectCpuCallstacks = value; break;
            case "java_heap": config.collectJavaHeap = value; break;
            case "gpu_mem": config.collectGpuMem = value; break;
            case "logcat": config.collectLogcat = value; break;
            case "frame_timeline": config.collectFrameTimeline = value; break;
            case "focus_main_thread": config.focusMainThread = value; break;
        }
    }

    private int getConfigNumber(HookConfig c, String key, int def) {
        switch (key) {
            case "trace_duration_ms": return c.traceDurationMs;
            case "buffer_size_kb": return c.bufferSizeKb;
            case "cpu_sampling_interval_ms": return c.cpuSamplingIntervalMs;
            case "block_threshold_ms": return (int) c.blockThresholdMs;
            case "top_threads_count": return c.topThreadsCount;
            case "hotspots_count": return c.hotspotsCount;
            case "slow_slices_count": return c.slowSlicesCount;
            case "jank_threshold_ms": return (int) c.jankThresholdMs;
            default: return def;
        }
    }

    private void setConfigNumber(HookConfig c, String key, int value) {
        switch (key) {
            case "trace_duration_ms": c.traceDurationMs = value; break;
            case "buffer_size_kb": c.bufferSizeKb = value; break;
            case "cpu_sampling_interval_ms": c.cpuSamplingIntervalMs = value; break;
            case "block_threshold_ms": c.blockThresholdMs = (long) value; break;
            case "top_threads_count": c.topThreadsCount = value; break;
            case "hotspots_count": c.hotspotsCount = value; break;
            case "slow_slices_count": c.slowSlicesCount = value; break;
            case "jank_threshold_ms": c.jankThresholdMs = (double) value; break;
        }
    }

    private String getConfigText(HookConfig c, String key, String def) {
        switch (key) {
            case "target_process": return c.targetProcess;
            default: return def;
        }
    }

    private void setConfigText(HookConfig c, String key, String value) {
        switch (key) {
            case "target_process": c.targetProcess = value; break;
        }
    }

    private void updateConnectionStatus() {
        SIClient ws = TraceHook.getWsClient();
        if (ws != null && ws.isConnected()) {
            statusText.setText("WS: connected");
            statusText.setTextColor(0xFF4CAF50);
        } else {
            statusText.setText("WS: disconnected");
            statusText.setTextColor(0xFFF44336);
        }
    }

    private void updateExtraStatus(TextView tv) {
        List<HookConfig.ExtraHook> extras = HookConfigManager.getExtraHooks();
        int methods = 0;
        for (HookConfig.ExtraHook eh : extras) {
            methods += eh.methods.size();
        }
        tv.setText(extras.size() + " classes, " + methods + " methods\n(use /hook add <class> <method> from CLI)");
    }
}
