package com.smartinspector.tracelib;

import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Trace;
import android.util.Log;
import android.util.Printer;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * BlockCanary-style main thread block detection.
 *
 * <p>Monitors every Message dispatched on the main thread Looper.
 * When a Message takes longer than the configured threshold:
 * <ul>
 *   <li>Emits a {@code SI$block#MsgClass#durationMs} trace section (captured by Perfetto atrace)</li>
 *   <li>Writes a {@code Log.w("SIBlock", ...)} with the main thread stack trace (captured by Perfetto android.log)</li>
 * </ul>
 *
 * <p>Stack capture mechanism:
 * On dispatch start, a delayed Runnable is posted to a background HandlerThread.
 * If the main thread finishes within the threshold, the Runnable is cancelled.
 * If the Runnable fires (threshold exceeded), it captures the main thread's stack trace
 * from the background thread — this gives the real blocking call, not BlockMonitor's own frame.
 *
 * <p>Two dispatch-monitoring strategies:
 * <ul>
 *   <li>Android 10+ (API 29+): Uses {@code Looper.Observer} via meta-reflection.
 *       Zero string allocation overhead.</li>
 *   <li>Android 9 and below: Uses the classic {@code Looper.setMessageLogging(Printer)} approach.</li>
 * </ul>
 */
public class BlockMonitor {

    private static final String TAG = "SmartInspector";
    private static final String LOG_TAG = "SIBlock";
    private static final String SI_PREFIX = "SI$";

    private static long thresholdMs = 100;
    private static volatile boolean started = false;

    // Background handler for delayed stack capture
    private static HandlerThread watchdogThread;
    private static Handler watchdogHandler;

    // State for the current dispatch (accessed only on main thread)
    private static long dispatchStartNs;
    private static String currentMsgClass;

    // The watchdog Runnable currently posted (null if none)
    private static Runnable pendingWatchdog;

    // Captured stack from watchdog thread (volatile: written by watchdog, read by main)
    private static volatile String capturedStack;

    // Observer reference (API 29+) — keep to avoid GC
    private static Object observer;
    private static Method observerMethod;

    // Printer reference (legacy) — keep to restore on stop
    private static Printer previousPrinter;

    // System class prefixes for stack filtering
    private static final String[] SYSTEM_PREFIXES = {
        "android.", "androidx.", "java.", "javax.", "kotlin.",
        "kotlinx.", "dalvik.", "libcore.", "com.android.", "com.google.",
        "sun.", "org.apache.",
    };

    // ── Block event cache (consumed by CLI via WS) ────────────

    /** A single block event with structured data. */
    public static class BlockEvent {
        public final String msgClass;
        public final long durationMs;
        public final List<String> stackTrace;
        public final long timestampMs;

        public BlockEvent(String msgClass, long durationMs, List<String> stackTrace) {
            this.msgClass = msgClass;
            this.durationMs = durationMs;
            this.stackTrace = Collections.unmodifiableList(new ArrayList<>(stackTrace));
            this.timestampMs = System.currentTimeMillis();
        }

        /** Serialize to JSON for WS transfer. */
        public JSONObject toJson() throws Exception {
            JSONObject o = new JSONObject();
            o.put("msgClass", msgClass);
            o.put("durationMs", durationMs);
            o.put("timestampMs", timestampMs);
            JSONArray arr = new JSONArray();
            for (String frame : stackTrace) {
                arr.put(frame);
            }
            o.put("stackTrace", arr);
            return o;
        }
    }

    // Thread-safe buffer: written on main thread, read/cleared via WS handler thread
    private static final List<BlockEvent> blockEvents = new ArrayList<>();

    /**
     * Return all cached block events as a JSON array and clear the buffer.
     * Called from WS handler thread.
     */
    public static synchronized String getAndClearEventsJson() {
        if (blockEvents.isEmpty()) return "[]";
        JSONArray arr = new JSONArray();
        try {
            for (BlockEvent ev : blockEvents) {
                arr.put(ev.toJson());
            }
        } catch (Exception ignored) {}
        blockEvents.clear();
        return arr.toString();
    }

    /** Start monitoring with the given threshold. */
    public static void start(long thresholdMsParam) {
        if (started) return;
        thresholdMs = thresholdMsParam;

        // Start background watchdog thread
        watchdogThread = new HandlerThread("SI-BlockWatchdog");
        watchdogThread.start();
        watchdogHandler = new Handler(watchdogThread.getLooper());

        if (Build.VERSION.SDK_INT >= 29) {
            startWithObserver();
        } else {
            startWithPrinter();
        }

        started = true;
        Log.i(TAG, "BlockMonitor started (threshold=" + thresholdMs + "ms, "
                + (Build.VERSION.SDK_INT >= 29 ? "Observer" : "Printer") + ")");
    }

    /** Stop monitoring and clean up. */
    public static void stop() {
        if (!started) return;

        if (Build.VERSION.SDK_INT >= 29 && observerMethod != null) {
            try {
                observerMethod.invoke(Looper.getMainLooper(), (Object) null);
            } catch (Exception ignored) {}
        } else {
            Looper.getMainLooper().setMessageLogging(previousPrinter);
        }

        if (watchdogThread != null) {
            watchdogThread.quitSafely();
            watchdogThread = null;
        }
        watchdogHandler = null;

        observer = null;
        observerMethod = null;
        previousPrinter = null;
        pendingWatchdog = null;
        started = false;
        Log.i(TAG, "BlockMonitor stopped");
    }

    // ═══════════════════════════════════════════════════════════
    // API 29+: Looper.Observer via meta-reflection
    // ═══════════════════════════════════════════════════════════

    private static void startWithObserver() {
        try {
            Class<?> observerClass = Class.forName("android.os.Looper$Observer");

            Object proxy = java.lang.reflect.Proxy.newProxyInstance(
                observerClass.getClassLoader(),
                new Class<?>[]{ observerClass },
                new java.lang.reflect.InvocationHandler() {
                    @Override
                    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                        String name = method.getName();
                        if ("messageDispatchStarting".equals(name)) {
                            onDispatchStart();
                            return null;
                        }
                        if ("messageDispatched".equals(name)) {
                            // args[1] is the Message object — extract msgClass from it
                            String msgClass = "Unknown";
                            if (args != null && args.length >= 2 && args[1] instanceof android.os.Message) {
                                android.os.Message msg = (android.os.Message) args[1];
                                msgClass = msg.getCallback() != null
                                    ? msg.getCallback().getClass().getName()
                                    : "what=" + msg.what;
                            }
                            onDispatchEnd(msgClass);
                            return null;
                        }
                        return null;
                    }
                }
            );

            Method setObserver = Looper.class.getDeclaredMethod("setObserver", observerClass);
            setObserver.setAccessible(true);
            setObserver.invoke(Looper.getMainLooper(), proxy);

            observer = proxy;
            observerMethod = setObserver;
        } catch (Exception e) {
            Log.w(TAG, "Looper.Observer failed, falling back to Printer: " + e.getMessage());
            startWithPrinter();
        }
    }

    // ═══════════════════════════════════════════════════════════
    // Legacy: Looper.setMessageLogging(Printer)
    // ═══════════════════════════════════════════════════════════

    private static void startWithPrinter() {
        try {
            java.lang.reflect.Field loggingField = Looper.class.getDeclaredField("mLogging");
            loggingField.setAccessible(true);
            previousPrinter = (Printer) loggingField.get(Looper.getMainLooper());
        } catch (Exception e) {
            Log.w(TAG, "Could not get previous Printer: " + e.getMessage());
            previousPrinter = null;
        }

        Looper.getMainLooper().setMessageLogging(new Printer() {
            private static final String DISPATCHING = ">>>>> Dispatching to ";
            private static final String FINISHED = "<<<<< Finished to ";

            @Override
            public void println(String x) {
                if (x == null) return;

                if (x.startsWith(DISPATCHING)) {
                    String msgClass = parseMsgClassFromLog(x);
                    onDispatchStart(msgClass);
                } else if (x.startsWith(FINISHED)) {
                    onDispatchEnd();
                }
            }
        });
    }

    /** Parse the message callback class from Looper's dispatch log string. */
    private static String parseMsgClassFromLog(String logLine) {
        int start = logLine.indexOf('(');
        if (start < 0) return "Unknown";
        int end = logLine.indexOf(')', start);
        if (end < 0) return "Unknown";
        String callbackClass = logLine.substring(start + 1, end).trim();

        if (callbackClass.isEmpty() || callbackClass.equals("null")) {
            int whatIdx = logLine.indexOf("what=");
            if (whatIdx >= 0) {
                int whatEnd = logLine.indexOf(' ', whatIdx);
                if (whatEnd < 0) whatEnd = logLine.length();
                return "what=" + logLine.substring(whatIdx + 5, whatEnd).trim();
            }
            return "Unknown";
        }
        return callbackClass;
    }

    // ═══════════════════════════════════════════════════════════
    // Dispatch callbacks (called on main thread)
    // ═══════════════════════════════════════════════════════════

    private static void onDispatchStart() {
        dispatchStartNs = System.nanoTime();
        currentMsgClass = null; // will be filled in onDispatchEnd from Message
        scheduleWatchdog();
    }

    private static void onDispatchStart(String msgClass) {
        dispatchStartNs = System.nanoTime();
        currentMsgClass = msgClass;
        scheduleWatchdog();
    }

    /**
     * Post a delayed watchdog Runnable to the background thread.
     * If the main thread finishes the current Message before the threshold,
     * {@link #cancelWatchdog()} will remove this Runnable.
     * If it fires, it captures the main thread stack trace.
     */
    private static void scheduleWatchdog() {
        capturedStack = null;

        final String msgClass = currentMsgClass;
        final long startNs = dispatchStartNs;

        pendingWatchdog = new Runnable() {
            @Override
            public void run() {
                // Called on watchdog thread — main thread is still blocked
                Thread mainThread = Looper.getMainLooper().getThread();
                StackTraceElement[] stack = mainThread.getStackTrace();
                capturedStack = formatStack(stack, 25);
            }
        };

        if (watchdogHandler != null) {
            watchdogHandler.postDelayed(pendingWatchdog, thresholdMs);
        }
    }

    /**
     * Cancel the pending watchdog Runnable.
     * Called on the main thread when the current Message finishes within the threshold.
     */
    private static void cancelWatchdog() {
        if (pendingWatchdog != null && watchdogHandler != null) {
            watchdogHandler.removeCallbacks(pendingWatchdog);
            pendingWatchdog = null;
        }
    }

    private static void onDispatchEnd() {
        onDispatchEnd(currentMsgClass);
    }

    /**
     * Called when a Message finishes dispatching on the main thread.
     * @param msgClass The callback/target class of the dispatched message.
     */
    private static void onDispatchEnd(String msgClass) {
        long elapsedNs = System.nanoTime() - dispatchStartNs;
        long elapsedMs = elapsedNs / 1_000_000;

        if (elapsedMs < thresholdMs) {
            // Finished within threshold — cancel the watchdog
            cancelWatchdog();
            currentMsgClass = null;
            return;
        }

        // Exceeded threshold — watchdog has already captured the stack
        pendingWatchdog = null;
        currentMsgClass = null;

        if (msgClass == null) {
            msgClass = "Unknown";
        }

        // 1. Emit SI$block# trace section (use shortened class name to avoid atrace 127-char limit)
        String shortClass = shortenClassName(msgClass);
        String sectionName = SI_PREFIX + "block#" + shortClass + "#" + elapsedMs + "ms";
        Trace.beginSection(sectionName);
        Trace.endSection();

        // 2. Get the captured stack (watchdog thread may still be writing it)
        String stackStr = capturedStack;
        if (stackStr == null) {
            // Watchdog hasn't fired yet (edge case: exactly at threshold boundary)
            try { Thread.sleep(10); } catch (InterruptedException ignored) {}
            stackStr = capturedStack;
        }

        // 3. Write to logcat
        if (stackStr != null && !stackStr.isEmpty()) {
            Log.w(LOG_TAG, msgClass + "|" + elapsedMs + "ms|" + stackStr);
        } else {
            Log.w(LOG_TAG, msgClass + "|" + elapsedMs + "ms|<no user frames>");
        }

        // 4. Cache structured BlockEvent for WS retrieval
        List<String> frames = new ArrayList<>();
        if (stackStr != null && !stackStr.isEmpty()) {
            String[] parts = stackStr.split("\\|");
            Collections.addAll(frames, parts);
        }
        synchronized (BlockMonitor.class) {
            // Prevent OOM on long-running sessions
            if (blockEvents.size() >= 500) {
                blockEvents.subList(0, 100).clear();
            }
            blockEvents.add(new BlockEvent(msgClass, elapsedMs, frames));
        }
    }

    // ═══════════════════════════════════════════════════════════
    // Class name shortening (avoid atrace 127-char section name limit)
    // ═══════════════════════════════════════════════════════════

    /**
     * Shorten a fully-qualified class name for use in trace section names.
     *
     * Keeps only the immediate parent package + simple class name:
     *   "com.smartinspector.hook.worker.CpuBurnWorker$1"  →  "worker.CpuBurnWorker$1"
     *   "android.view.Choreographer$FrameDisplayEventReceiver"  →  "Choreographer$FrameDisplayEventReceiver"
     *   "com.example.Adapter"  →  "example.Adapter"
     *   "what=123"  →  "what=123"  (no change, not a FQN)
     *   "Unknown"  →  "Unknown"  (no change)
     */
    public static String shortenClassName(String fqn) {
        if (fqn == null || !fqn.contains(".")) return fqn;

        // Handle inner classes: split at $ first
        String outer = fqn;
        String inner = "";
        int dollar = fqn.indexOf('$');
        if (dollar >= 0) {
            outer = fqn.substring(0, dollar);
            inner = fqn.substring(dollar);
        }

        // outer is "com.smartinspector.hook.worker.CpuBurnWorker"
        // Keep last two segments: "worker.CpuBurnWorker"
        int lastDot = outer.lastIndexOf('.');
        if (lastDot < 0) return fqn; // single segment, no change

        int prevDot = outer.lastIndexOf('.', lastDot - 1);
        if (prevDot < 0) {
            // Only one package segment, keep "ClassName"
            return outer.substring(lastDot + 1) + inner;
        }
        return outer.substring(prevDot + 1) + inner;
    }

    // ═══════════════════════════════════════════════════════════
    // Stack trace formatting
    // ═══════════════════════════════════════════════════════════

    /**
     * Format stack trace, filtering out system frames, keeping at most {@code maxFrames}
     * user-code frames.
     *
     * Also filters out BlockMonitor's own frames to avoid noise.
     */
    private static String formatStack(StackTraceElement[] stack, int maxFrames) {
        List<String> userFrames = new ArrayList<>();
        for (StackTraceElement frame : stack) {
            if (userFrames.size() >= maxFrames) break;

            String className = frame.getClassName();

            // Skip BlockMonitor's own frames
            if (className.startsWith("com.smartinspector.tracelib.BlockMonitor")) continue;

            // Skip system frames
            boolean isSystem = false;
            for (String prefix : SYSTEM_PREFIXES) {
                if (className.startsWith(prefix)) {
                    isSystem = true;
                    break;
                }
            }
            if (isSystem) continue;

            String frameStr = "at " + className + "." + frame.getMethodName();
            if (frame.getFileName() != null && frame.getLineNumber() > 0) {
                frameStr += "(" + frame.getFileName() + ":" + frame.getLineNumber() + ")";
            } else if (frame.isNativeMethod()) {
                frameStr += "(Native Method)";
            } else if (frame.getFileName() != null) {
                frameStr += "(" + frame.getFileName() + ")";
            }
            userFrames.add(frameStr);
        }

        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < userFrames.size(); i++) {
            if (i > 0) sb.append("|");
            sb.append(userFrames.get(i));
        }
        return sb.toString();
    }

}
