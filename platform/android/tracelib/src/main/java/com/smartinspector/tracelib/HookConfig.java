package com.smartinspector.tracelib;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Configuration model for SmartInspector trace hooks + perfetto parameters.
 *
 * <p>Controls which hook categories are enabled, perfetto collection/analysis params,
 * plus optional per-class/method hooks.
 * Serialized to/from JSON for persistence (SharedPreferences) and WS sync.
 */
public class HookConfig {

    // ── Hook toggles ──────────────────────────────────────────
    public boolean activityLifecycle = true;
    public boolean fragmentLifecycle = true;
    public boolean rvPipeline = true;
    public boolean rvAdapter = true;
    public boolean layoutInflate = false;
    public boolean viewTraverse = false;
    public boolean handlerDispatch = false;
    public boolean blockMonitor = true;
    public boolean networkIo = false;
    public boolean databaseIo = false;
    public boolean imageLoad = false;
    public boolean inputEvent = true;           // dispatchTouchEvent tracing

    // ── Block monitor params ──────────────────────────────────
    public long blockThresholdMs = 100;

    // ── Perfetto collection params ────────────────────────────
    public int traceDurationMs = 10000;           // trace 采集时长
    public String targetProcess = "";              // 目标进程包名 (空=自动)
    public int bufferSizeKb = 65536;               // 主 buffer 大小
    public boolean collectCpuCallstacks = true;     // CPU 调用栈采样
    public int cpuSamplingIntervalMs = 1;          // CPU 采样间隔 (ms)
    public boolean collectJavaHeap = true;         // Java heap profiling
    public boolean collectGpuMem = false;          // GPU 内存计数器
    public boolean collectLogcat = true;           // logcat 采集
    public boolean collectFrameTimeline = true;    // SurfaceFlinger frame timeline

    // ── Perfetto analysis params ──────────────────────────────
    public int topThreadsCount = 10;               // top N 热线程
    public int hotspotsCount = 15;                 // top N CPU 热点
    public int slowSlicesCount = 30;               // top N 最慢 slice
    public boolean focusMainThread = true;         // 只关注主线程
    public double jankThresholdMs = 16.0;          // 卡顿阈值 (ms)

    // ── Extra hooks for user-specified classes/methods ─────────
    public List<ExtraHook> extraHooks = new ArrayList<>();

    public static class ExtraHook {
        public String className;
        public List<String> methods;
        public boolean enabled;

        public ExtraHook(String className, List<String> methods, boolean enabled) {
            this.className = className;
            this.methods = methods;
            this.enabled = enabled;
        }
    }

    // ── Serialization ──────────────────────────────────────────

    public String toJson() {
        try {
            JSONObject root = new JSONObject();

            // Hook toggles
            root.put("activity_lifecycle", activityLifecycle);
            root.put("fragment_lifecycle", fragmentLifecycle);
            root.put("rv_pipeline", rvPipeline);
            root.put("rv_adapter", rvAdapter);
            root.put("layout_inflate", layoutInflate);
            root.put("view_traverse", viewTraverse);
            root.put("handler_dispatch", handlerDispatch);
            root.put("block_monitor", blockMonitor);
            root.put("network_io", networkIo);
            root.put("database_io", databaseIo);
            root.put("image_load", imageLoad);
            root.put("input_event", inputEvent);

            // Block monitor params
            root.put("block_threshold_ms", blockThresholdMs);

            // Perfetto collection params
            JSONObject collection = new JSONObject();
            collection.put("trace_duration_ms", traceDurationMs);
            collection.put("target_process", targetProcess);
            collection.put("buffer_size_kb", bufferSizeKb);
            collection.put("cpu_callstacks", collectCpuCallstacks);
            collection.put("cpu_sampling_interval_ms", cpuSamplingIntervalMs);
            collection.put("java_heap", collectJavaHeap);
            collection.put("gpu_mem", collectGpuMem);
            collection.put("logcat", collectLogcat);
            collection.put("frame_timeline", collectFrameTimeline);
            root.put("perfetto_collection", collection);

            // Perfetto analysis params
            JSONObject analysis = new JSONObject();
            analysis.put("top_threads_count", topThreadsCount);
            analysis.put("hotspots_count", hotspotsCount);
            analysis.put("slow_slices_count", slowSlicesCount);
            analysis.put("focus_main_thread", focusMainThread);
            analysis.put("jank_threshold_ms", jankThresholdMs);
            root.put("perfetto_analysis", analysis);

            // Extra hooks
            JSONArray extras = new JSONArray();
            for (ExtraHook eh : extraHooks) {
                JSONObject obj = new JSONObject();
                obj.put("class_name", eh.className);
                obj.put("enabled", eh.enabled);
                JSONArray methods = new JSONArray();
                for (String m : eh.methods) {
                    methods.put(m);
                }
                obj.put("methods", methods);
                extras.put(obj);
            }
            root.put("extra_hooks", extras);

            return root.toString(2);
        } catch (JSONException e) {
            return "{}";
        }
    }

    public static HookConfig fromJson(String json) {
        HookConfig config = new HookConfig();
        if (json == null || json.isEmpty()) return config;

        try {
            JSONObject root = new JSONObject(json);

            // Hook toggles
            config.activityLifecycle = root.optBoolean("activity_lifecycle", true);
            config.fragmentLifecycle = root.optBoolean("fragment_lifecycle", true);
            config.rvPipeline = root.optBoolean("rv_pipeline", true);
            config.rvAdapter = root.optBoolean("rv_adapter", true);
            config.layoutInflate = root.optBoolean("layout_inflate", false);
            config.viewTraverse = root.optBoolean("view_traverse", false);
            config.handlerDispatch = root.optBoolean("handler_dispatch", false);
            config.blockMonitor = root.optBoolean("block_monitor", true);
            config.networkIo = root.optBoolean("network_io", false);
            config.databaseIo = root.optBoolean("database_io", false);
            config.imageLoad = root.optBoolean("image_load", false);
            config.inputEvent = root.optBoolean("input_event", true);

            // Block monitor params
            config.blockThresholdMs = root.optLong("block_threshold_ms", 100);

            // Perfetto collection params
            JSONObject collection = root.optJSONObject("perfetto_collection");
            if (collection != null) {
                config.traceDurationMs = collection.optInt("trace_duration_ms", 10000);
                config.targetProcess = collection.optString("target_process", "");
                config.bufferSizeKb = collection.optInt("buffer_size_kb", 65536);
                config.collectCpuCallstacks = collection.optBoolean("cpu_callstacks", true);
                config.cpuSamplingIntervalMs = collection.optInt("cpu_sampling_interval_ms", 1);
                config.collectJavaHeap = collection.optBoolean("java_heap", true);
                config.collectGpuMem = collection.optBoolean("gpu_mem", false);
                config.collectLogcat = collection.optBoolean("logcat", true);
                config.collectFrameTimeline = collection.optBoolean("frame_timeline", true);
            }

            // Perfetto analysis params
            JSONObject analysis = root.optJSONObject("perfetto_analysis");
            if (analysis != null) {
                config.topThreadsCount = analysis.optInt("top_threads_count", 10);
                config.hotspotsCount = analysis.optInt("hotspots_count", 15);
                config.slowSlicesCount = analysis.optInt("slow_slices_count", 30);
                config.focusMainThread = analysis.optBoolean("focus_main_thread", true);
                config.jankThresholdMs = analysis.optDouble("jank_threshold_ms", 16.0);
            }

            // Extra hooks
            JSONArray extras = root.optJSONArray("extra_hooks");
            if (extras != null) {
                for (int i = 0; i < extras.length(); i++) {
                    JSONObject obj = extras.getJSONObject(i);
                    String className = obj.getString("class_name");
                    boolean enabled = obj.optBoolean("enabled", true);
                    JSONArray methodsArr = obj.optJSONArray("methods");
                    List<String> methods = new ArrayList<>();
                    if (methodsArr != null) {
                        for (int j = 0; j < methodsArr.length(); j++) {
                            methods.add(methodsArr.getString(j));
                        }
                    }
                    config.extraHooks.add(new ExtraHook(className, methods, enabled));
                }
            }
        } catch (JSONException e) {
            // Return defaults on parse error
        }

        return config;
    }

    public static HookConfig defaults() {
        return new HookConfig();
    }
}
