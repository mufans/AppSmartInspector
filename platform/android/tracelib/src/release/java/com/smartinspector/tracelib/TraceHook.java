package com.smartinspector.tracelib;

import android.app.Activity;
import android.app.Application;
import android.app.Fragment;
import android.content.Context;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Trace;
import android.util.Log;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import top.canyie.pine.Pine;
import top.canyie.pine.PineConfig;
import top.canyie.pine.callback.MethodHook;

/**
 * SmartInspector TraceHook — 全链路性能追踪。
 *
 * <p>追踪链路：Activity → Fragment → RecyclerView 管线 → LayoutInflate → View → Handler
 *
 * <p>Usage: call {@link #init(Context)} once in Application.onCreate().
 * All trace section names are prefixed with "SI$" for downstream filtering.
 */
public class TraceHook {
    private static final String TAG = "SmartInspector";
    private static final String SI_PREFIX = "SI$";
    private static volatile boolean initialized = false;

    private static final Set<Class<?>> hookedAdapters = new HashSet<>();
    private static final Set<Class<?>> hookedLMs = new HashSet<>();
    private static final Set<Class<?>> hookedFragments = new HashSet<>();
    private static final Set<Class<?>> hookedActivities = new HashSet<>();

    private static SIClient wsClient;

    /** Init without context — uses default config (all built-in hooks on). */
    public static void init() {
        init(null);
    }

    /** Init with context — loads saved config, installs hooks, starts WS client. */
    public static void init(Context context) {
        if (initialized) return;
        synchronized (TraceHook.class) {
            if (initialized) return;
            if (context != null) {
                HookConfigManager.init(context);
            }
            doInit();
            initialized = true;
            // Start WS client for config sync with CLI
            if (context != null) {
                wsClient = new SIClient(context);
                wsClient.connect();
            }
        }
    }

    /** Get the WS client instance (for HookConfigActivity reconnect button). */
    public static SIClient getWsClient() {
        return wsClient;
    }

    private static void doInit() {
        PineConfig.debug = true;
        PineConfig.debuggable = false;

        if (HookConfigManager.isEnabled("activity_lifecycle")) {
            try {
                hookActivityLifecycle();
            } catch (Exception e) {
                Log.e(TAG, "Activity hook failed", e);
            }
        }

        if (HookConfigManager.isEnabled("fragment_lifecycle")) {
            try {
                hookFragmentLifecycle();
            } catch (Exception e) {
                Log.e(TAG, "Fragment hook failed", e);
            }
        }

        if (HookConfigManager.isEnabled("rv_pipeline") || HookConfigManager.isEnabled("rv_adapter")) {
            try {
                hookRecyclerView();
            } catch (Exception e) {
                Log.e(TAG, "RecyclerView hook failed", e);
            }
        }

        if (HookConfigManager.isEnabled("layout_inflate")) {
            try {
                hookLayoutInflate();
            } catch (Exception e) {
                Log.e(TAG, "LayoutInflate hook failed", e);
            }
        }

        if (HookConfigManager.isEnabled("view_traverse")) {
            try {
                hookViewTraverse();
            } catch (Exception e) {
                Log.e(TAG, "View traverse hook failed", e);
            }
        }

        if (HookConfigManager.isEnabled("handler_dispatch")) {
            try {
                hookHandlerDispatch();
            } catch (Exception e) {
                Log.e(TAG, "Handler dispatch hook failed", e);
            }
        }

        try {
            hookExtraClasses();
        } catch (Exception e) {
            Log.e(TAG, "Extra hooks failed", e);
        }

        Log.i(TAG, "All trace hooks installed");
    }

    // ═══════════════════════════════════════════════════════════
    // Activity lifecycle
    // ═══════════════════════════════════════════════════════════

    private static void hookActivityLifecycle() throws Exception {
        // Use Application.ActivityLifecycleCallbacks to discover concrete Activity
        // subclasses and hook their methods directly, capturing full method duration.
        hookConcrete(Activity.class, "onCreate", new Class<?>[]{Bundle.class});
        hookConcrete(Activity.class, "onStart", new Class<?>[0]);
        hookConcrete(Activity.class, "onResume", new Class<?>[0]);
        hookConcrete(Activity.class, "onPause", new Class<?>[0]);
        hookConcrete(Activity.class, "onStop", new Class<?>[0]);
        hookConcrete(Activity.class, "onDestroy", new Class<?>[0]);

        Method onWFC = Activity.class.getDeclaredMethod("onWindowFocusChanged", boolean.class);
        Pine.hook(onWFC, new MethodHook() {
            @Override public void beforeCall(Pine.CallFrame cf) {
                if (!HookConfigManager.isEnabled("activity_lifecycle")) return;
                boolean focus = (boolean) cf.args[0];
                Trace.beginSection(SI_PREFIX + cls(cf) + ".windowFocus(" + focus + ")");
            }
            @Override public void afterCall(Pine.CallFrame cf) {
                Trace.endSection();
            }
        });

        // Register ActivityLifecycleCallbacks to dynamically hook concrete subclasses
        try {
            Class<?> alcClass = Class.forName("android.app.Application$ActivityLifecycleCallbacks");
            java.lang.reflect.Method registerMethod = Application.class.getDeclaredMethod(
                "registerActivityLifecycleCallbacks", alcClass);

            Object callback = java.lang.reflect.Proxy.newProxyInstance(
                alcClass.getClassLoader(),
                new Class<?>[]{ alcClass },
                new java.lang.reflect.InvocationHandler() {
                    @Override
                    public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                        if (args != null && args.length >= 1 && args[0] instanceof Activity) {
                            hookConcreteActivity(args[0].getClass());
                        }
                        return null;
                    }
                }
            );

            // Find Application context from init()
            if (wsClient != null) {
                // Use any available context to get Application
                // The callback is registered on the Application object
            }
            // Register via hooking Application.onCreate to get the app instance
            Method appOnCreate = Application.class.getDeclaredMethod("onCreate");
            Pine.hook(appOnCreate, new MethodHook() {
                boolean registered = false;
                @Override public void afterCall(Pine.CallFrame cf) {
                    if (registered) return;
                    registered = true;
                    try {
                        registerMethod.invoke(cf.thisObject, callback);
                        Log.d(TAG, "Registered ActivityLifecycleCallbacks");
                    } catch (Exception e) {
                        Log.w(TAG, "Failed to register ActivityLifecycleCallbacks: " + e.getMessage());
                    }
                }
            });
        } catch (Exception e) {
            Log.w(TAG, "ActivityLifecycleCallbacks setup failed: " + e.getMessage());
        }

        Log.d(TAG, "Hooked Activity lifecycle");
    }

    /** Dynamically hook a concrete Activity subclass. */
    private static void hookConcreteActivity(Class<?> activityClass) {
        synchronized (hookedActivities) {
            if (hookedActivities.contains(activityClass)) return;
            String name = activityClass.getName();
            if (name.startsWith("android.") || name.startsWith("androidx.")) return;
            hookedActivities.add(activityClass);
        }

        Log.d(TAG, "Hooking concrete activity: " + activityClass.getName());
        safeHookMethod(activityClass, "onCreate", new Class<?>[]{Bundle.class}, "activity_lifecycle");
        safeHookMethod(activityClass, "onResume", new Class<?>[0], "activity_lifecycle");
        safeHookMethod(activityClass, "onPause", new Class<?>[0], "activity_lifecycle");
        safeHookMethod(activityClass, "onDestroy", new Class<?>[0], "activity_lifecycle");
    }

    // ═══════════════════════════════════════════════════════════
    // Fragment lifecycle (AndroidX + android.app)
    // ═══════════════════════════════════════════════════════════

    private static void hookFragmentLifecycle() throws Exception {
        // AndroidX Fragment
        try {
            Class<?> xf = Class.forName("androidx.fragment.app.Fragment");
            hookFragmentClass(xf);
        } catch (Exception e) {
            Log.w(TAG, "AndroidX Fragment not found: " + e.getMessage());
        }

        // android.app.Fragment
        try {
            hookFragmentClass(Fragment.class);
        } catch (Exception e) {
            Log.w(TAG, "android.app.Fragment hook failed: " + e.getMessage());
        }

        // Dynamic hook: register FragmentLifecycleCallbacks to discover and hook
        // concrete Fragment subclasses at runtime (like RecyclerView setAdapter pattern).
        // This ensures we hook the actual override method on the concrete class,
        // capturing the full user method duration.
        try {
            hookFragmentManagerRegisterFragment();
        } catch (Exception e) {
            Log.w(TAG, "FragmentLifecycleCallbacks hook failed: " + e.getMessage());
        }

        // FragmentManager.beginTransaction
        try {
            Class<?> fm = Class.forName("androidx.fragment.app.FragmentManager");
            hookConcrete(fm, "beginTransaction", new Class<?>[0]);
        } catch (Exception ignored) {}

        Log.d(TAG, "Hooked Fragment lifecycle");
    }

    /**
     * Hook FragmentActivity.onCreate to register FragmentLifecycleCallbacks,
     * which discovers concrete Fragment subclasses and hooks their methods.
     * Similar to RecyclerView.setAdapter -> hookConcreteAdapter pattern.
     */
    private static void hookFragmentManagerRegisterFragment() throws Exception {
        Class<?> faClass = Class.forName("androidx.fragment.app.FragmentActivity");
        Class<?> fmcClass = Class.forName("androidx.fragment.app.FragmentManager$FragmentLifecycleCallbacks");

        // Track which Activity instances have registered callbacks
        Set<Object> registered = new java.util.Collections.synchronizedSet(new java.util.HashSet<>());

        Method onCreate = faClass.getDeclaredMethod("onCreate", Bundle.class);
        Pine.hook(onCreate, new MethodHook() {
            @Override public void afterCall(Pine.CallFrame cf) {
                if (registered.contains(cf.thisObject)) return;

                try {
                    Method getFM = cf.thisObject.getClass().getMethod("getSupportFragmentManager");
                    Object fm = getFM.invoke(cf.thisObject);
                    if (fm == null) return;

                    Method regMethod = fm.getClass().getMethod(
                        "registerFragmentLifecycleCallbacks", fmcClass, boolean.class);

                    Object callback = java.lang.reflect.Proxy.newProxyInstance(
                        fmcClass.getClassLoader(),
                        new Class<?>[]{ fmcClass },
                        new java.lang.reflect.InvocationHandler() {
                            @Override
                            public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                                if (args != null && args.length >= 2 && args[1] instanceof Fragment) {
                                    hookConcreteFragment(args[1].getClass());
                                }
                                return null;
                            }
                        }
                    );
                    regMethod.invoke(fm, callback, false);
                    registered.add(cf.thisObject);
                    Log.d(TAG, "Registered FragmentLifecycleCallbacks for " + cf.thisObject.getClass().getName());
                } catch (Exception e) {
                    Log.w(TAG, "Failed to register FragmentLifecycleCallbacks: " + e.getMessage());
                }
            }
        });
    }

    private static void hookFragmentClass(Class<?> fragClass) throws Exception {
        Log.d(TAG, "hookFragmentClass: " + fragClass.getName());

        // Hook base class methods for Fragments that DON'T override them.
        // For Fragments that DO override (e.g. DetailFragment.onCreateView),
        // we dynamically hook the concrete subclass via FragmentLifecycleCallbacks.
        safeHookMethod(fragClass, "onCreate", new Class<?>[]{Bundle.class}, "fragment_lifecycle");
        safeHookMethod(fragClass, "onCreateView",
            new Class<?>[]{LayoutInflater.class, ViewGroup.class, Bundle.class}, "fragment_lifecycle");
        safeHookMethod(fragClass, "onViewCreated", new Class<?>[]{View.class, Bundle.class}, "fragment_lifecycle");
        safeHookMethod(fragClass, "onResume", new Class<?>[0], "fragment_lifecycle");
        safeHookMethod(fragClass, "onPause", new Class<?>[0], "fragment_lifecycle");
        safeHookMethod(fragClass, "onDestroyView", new Class<?>[0], "fragment_lifecycle");
    }

    /**
     * Hook a concrete Fragment subclass's lifecycle methods.
     * Called when a Fragment is first seen via FragmentLifecycleCallbacks.
     * Directly hooks the override methods on the concrete class,
     * capturing the full user method duration regardless of super calls.
     */
    private static void hookConcreteFragment(Class<?> fragmentClass) {
        synchronized (hookedFragments) {
            if (hookedFragments.contains(fragmentClass)) return;
            // Skip framework Fragment classes — already hooked via hookFragmentClass
            String name = fragmentClass.getName();
            if (name.startsWith("androidx.") || name.startsWith("android.app.")) return;
            hookedFragments.add(fragmentClass);
        }

        Log.d(TAG, "Hooking concrete fragment: " + fragmentClass.getName());
        safeHookMethod(fragmentClass, "onCreateView",
            new Class<?>[]{LayoutInflater.class, ViewGroup.class, Bundle.class}, "fragment_lifecycle");
        safeHookMethod(fragmentClass, "onResume", new Class<?>[0], "fragment_lifecycle");
        safeHookMethod(fragmentClass, "onPause", new Class<?>[0], "fragment_lifecycle");
        safeHookMethod(fragmentClass, "onDestroyView", new Class<?>[0], "fragment_lifecycle");
        safeHookMethod(fragmentClass, "onCreate", new Class<?>[]{Bundle.class}, "fragment_lifecycle");
    }

    // ═══════════════════════════════════════════════════════════
    // RecyclerView
    // ═══════════════════════════════════════════════════════════

    private static void hookRecyclerView() throws Exception {
        Class<?> rvClass = Class.forName("androidx.recyclerview.widget.RecyclerView");
        Class<?> adapterClass = Class.forName("androidx.recyclerview.widget.RecyclerView$Adapter");
        Class<?> lmClass = Class.forName("androidx.recyclerview.widget.RecyclerView$LayoutManager");
        Class<?> vhClass = Class.forName("androidx.recyclerview.widget.RecyclerView$ViewHolder");
        Class<?> recyclerClass = Class.forName("androidx.recyclerview.widget.RecyclerView$Recycler");
        Class<?> stateClass = Class.forName("androidx.recyclerview.widget.RecyclerView$State");

        // Direct hooks on RV concrete methods (rv_pipeline category)
        hookConcrete(rvClass, "onDraw", new Class<?>[]{android.graphics.Canvas.class});
        hookConcrete(rvClass, "onScrollStateChanged", new Class<?>[]{int.class});

        // dispatchLayoutStep 1/2/3
        for (int i = 1; i <= 3; i++) {
            safeHookMethod(rvClass, "dispatchLayoutStep" + i, new Class<?>[0], "rv_pipeline");
        }

        // setAdapter → dynamic hook
        Method setAdapter = rvClass.getDeclaredMethod("setAdapter", adapterClass);
        Pine.hook(setAdapter, new MethodHook() {
            @Override public void afterCall(Pine.CallFrame cf) {
                Object adapter = cf.args[0];
                if (adapter != null) hookConcreteAdapter(adapter.getClass(), vhClass);
            }
        });

        // setLayoutManager → dynamic hook
        Method setLM = rvClass.getDeclaredMethod("setLayoutManager", lmClass);
        Pine.hook(setLM, new MethodHook() {
            @Override public void afterCall(Pine.CallFrame cf) {
                Object lm = cf.args[0];
                if (lm != null) hookConcreteLM(lm.getClass(), recyclerClass, stateClass);
            }
        });

        // GapWorker.prefetch
        try {
            Class<?> gw = Class.forName("androidx.recyclerview.widget.GapWorker");
            hookConcrete(gw, "prefetch", new Class<?>[]{long.class});
        } catch (Exception ignored) {}

        Log.d(TAG, "Hooked RecyclerView pipeline");
    }

    private static void hookConcreteAdapter(Class<?> adapter, Class<?> vhClass) {
        synchronized (hookedAdapters) {
            if (hookedAdapters.contains(adapter)) return;
            hookedAdapters.add(adapter);
        }

        safeHookMethod(adapter, "onCreateViewHolder", new Class<?>[]{ViewGroup.class, int.class}, "rv_adapter");
        safeHookMethod(adapter, "onBindViewHolder", new Class<?>[]{vhClass, int.class}, "rv_adapter");
        safeHookMethod(adapter, "onBindViewHolder", new Class<?>[]{vhClass, int.class, java.util.List.class}, "rv_adapter");
        safeHookMethod(adapter, "onViewRecycled", new Class<?>[]{vhClass}, "rv_adapter");
        safeHookMethod(adapter, "onViewAttachedToWindow", new Class<?>[]{vhClass}, "rv_adapter");
        safeHookMethod(adapter, "onViewDetachedFromWindow", new Class<?>[]{vhClass}, "rv_adapter");
    }

    private static void hookConcreteLM(Class<?> lm, Class<?> recyclerClass, Class<?> stateClass) {
        synchronized (hookedLMs) {
            if (hookedLMs.contains(lm)) return;
            hookedLMs.add(lm);
        }
        safeHookMethod(lm, "onLayoutChildren", new Class<?>[]{recyclerClass, stateClass}, "rv_pipeline");
    }

    // ═══════════════════════════════════════════════════════════
    // LayoutInflate hook
    // ═══════════════════════════════════════════════════════════

    private static void hookLayoutInflate() throws Exception {
        Method inflate = LayoutInflater.class.getDeclaredMethod(
                "inflate", int.class, ViewGroup.class, boolean.class);
        Pine.hook(inflate, new MethodHook() {
            @Override public void beforeCall(Pine.CallFrame cf) {
                if (!HookConfigManager.isEnabled("layout_inflate")) return;
                int layoutResId = (int) cf.args[0];
                ViewGroup parent = (ViewGroup) cf.args[1];
                String layoutName;
                try {
                    Context ctx = parent != null ? parent.getContext()
                            : (Context) cf.thisObject;
                    layoutName = ctx.getResources().getResourceEntryName(layoutResId);
                } catch (Exception e) {
                    layoutName = "0x" + Integer.toHexString(layoutResId);
                }
                String parentClass = parent != null ? parent.getClass().getSimpleName() : "null";
                Trace.beginSection(SI_PREFIX + "inflate#" + layoutName + "#" + parentClass);
            }
            @Override public void afterCall(Pine.CallFrame cf) {
                Trace.endSection();
            }
        });
        Log.d(TAG, "Hooked LayoutInflate");
    }

    // ═══════════════════════════════════════════════════════════
    // View traverse hook (measure/layout/draw, non-RV)
    // ═══════════════════════════════════════════════════════════

    private static void hookViewTraverse() throws Exception {
        Class<?> viewClass = View.class;
        String[] methods = {"measure", "layout", "draw"};
        for (String methodName : methods) {
            Class<?>[] params;
            switch (methodName) {
                case "measure":
                    params = new Class<?>[]{int.class, int.class};
                    break;
                case "layout":
                    params = new Class<?>[]{int.class, int.class, int.class, int.class};
                    break;
                case "draw":
                    params = new Class<?>[]{android.graphics.Canvas.class};
                    break;
                default:
                    continue;
            }
            try {
                Method m = viewClass.getDeclaredMethod(methodName, params);
                Pine.hook(m, new MethodHook() {
                    @Override public void beforeCall(Pine.CallFrame cf) {
                        if (!HookConfigManager.isEnabled("view_traverse")) return;
                        // Skip RecyclerView — already hooked by rv_pipeline
                        Object thiz = cf.thisObject;
                        if (thiz.getClass().getName().contains("RecyclerView")) return;
                        Trace.beginSection(SI_PREFIX + "view#" + thiz.getClass().getName() + "." + methodName);
                    }
                    @Override public void afterCall(Pine.CallFrame cf) {
                        Trace.endSection();
                    }
                });
            } catch (Exception e) {
                Log.w(TAG, "Failed to hook View." + methodName + ": " + e.getMessage());
            }
        }
        Log.d(TAG, "Hooked View traverse");
    }

    // ═══════════════════════════════════════════════════════════
    // Handler dispatch hook (main thread only)
    // ═══════════════════════════════════════════════════════════

    private static void hookHandlerDispatch() throws Exception {
        Method dispatch = Handler.class.getDeclaredMethod("dispatchMessage", android.os.Message.class);
        Pine.hook(dispatch, new MethodHook() {
            @Override public void beforeCall(Pine.CallFrame cf) {
                if (!HookConfigManager.isEnabled("handler_dispatch")) return;
                // Main thread only
                if (Looper.myLooper() != Looper.getMainLooper()) return;
                android.os.Message msg = (android.os.Message) cf.args[0];
                String msgClass = msg.getCallback() != null ? msg.getCallback().getClass().getName() : "what=" + msg.what;
                Trace.beginSection(SI_PREFIX + "handler#" + msgClass);
            }
            @Override public void afterCall(Pine.CallFrame cf) {
                Trace.endSection();
            }
        });
        Log.d(TAG, "Hooked Handler dispatch");
    }

    // ═══════════════════════════════════════════════════════════
    // Extra hooks (user-specified classes/methods)
    // ═══════════════════════════════════════════════════════════

    private static void hookExtraClasses() {
        List<HookConfig.ExtraHook> extras = HookConfigManager.getExtraHooks();
        for (HookConfig.ExtraHook eh : extras) {
            try {
                Class<?> clazz = Class.forName(eh.className);
                for (String methodName : eh.methods) {
                    // Try hooking with no-arg signature first, then with Bundle arg
                    try {
                        safeHookMethod(clazz, methodName, new Class<?>[0], null);
                    } catch (Exception ignored) {}
                    // Best-effort: we don't know the exact parameter types,
                    // so we try the most common signatures.
                }
                Log.d(TAG, "Hooked extra: " + eh.className);
            } catch (ClassNotFoundException e) {
                Log.w(TAG, "Extra hook class not found: " + eh.className);
            }
        }
    }

    // ═══════════════════════════════════════════════════════════
    // Generic hook helpers
    // ═══════════════════════════════════════════════════════════

    /**
     * Hook a concrete (non-abstract) method with a simple beginSection/endSection.
     * The section name is auto-generated from the thisObject class name + method name.
     */
    private static void hookConcrete(Class<?> clazz, String methodName, Class<?>[] paramTypes) {
        try {
            Method m = clazz.getDeclaredMethod(methodName, paramTypes);
            Pine.hook(m, new MethodHook() {
                @Override public void beforeCall(Pine.CallFrame cf) {
                    String tag = autoTag(cf, methodName);
                    Trace.beginSection(tag);
                    Log.d(TAG, "[hook-fire] " + tag + " this=" + cf.thisObject.getClass().getName());
                }
                @Override public void afterCall(Pine.CallFrame cf) { Trace.endSection(); }
            });
            Log.d(TAG, "[hook-ok] " + clazz.getSimpleName() + "." + methodName);
        } catch (Exception e) {
            Log.w(TAG, "Failed to hook " + clazz.getSimpleName() + "." + methodName + ": " + e.getMessage());
        }
    }

    /**
     * Safely hook a method — silently skip if it doesn't exist or is abstract.
     * @param hookId config key for runtime toggle, or null to always run.
     */
    private static void safeHookMethod(Class<?> clazz, String methodName, Class<?>[] paramTypes, String hookId) {
        try {
            Method m = clazz.getDeclaredMethod(methodName, paramTypes);
            Pine.hook(m, new MethodHook() {
                @Override public void beforeCall(Pine.CallFrame cf) {
                    if (hookId != null && !HookConfigManager.isEnabled(hookId)) return;
                    String tag = autoTag(cf, methodName);
                    Trace.beginSection(tag);
                    Log.d(TAG, "[hook-fire] " + tag + " this=" + cf.thisObject.getClass().getName());
                }
                @Override public void afterCall(Pine.CallFrame cf) {
                    Trace.endSection();
                }
            });
            Log.d(TAG, "[hook-ok] " + clazz.getSimpleName() + "." + methodName);
        } catch (Exception e) {
            Log.w(TAG, "[hook-fail] " + clazz.getSimpleName() + "." + methodName + ": " + e.getMessage());
        }
    }

    /**
     * Auto-generate a trace section name from the call frame.
     * For RV methods, includes view ID + adapter class.
     * For Fragment perform* methods, maps to on* name for downstream parsing.
     * All tags are prefixed with "SI$" for downstream filtering.
     */
    private static String autoTag(Pine.CallFrame cf, String method) {
        Object thiz = cf.thisObject;

        // Special handling for RecyclerView methods — include view ID + adapter
        if (thiz instanceof View) {
            View v = (View) thiz;
            String className = thiz.getClass().getName();
            if (className.contains("RecyclerView")) {
                return SI_PREFIX + rvTag(thiz) + "." + method;
            }
        }

        // Map perform* → on* (performCreateView → onCreateView, performResume → onResume)
        String tagName = method;
        if (method.startsWith("perform") && method.length() > 7) {
            tagName = "on" + method.substring(7);
        }

        return SI_PREFIX + thiz.getClass().getName() + "." + tagName;
    }

    // ═══════════════════════════════════════════════════════════
    // RV tag helpers
    // ═══════════════════════════════════════════════════════════

    private static String cls(Pine.CallFrame cf) {
        return cf.thisObject.getClass().getName();
    }

    private static String rvTag(Object rv) {
        if (!(rv instanceof View)) return "RV#unknown";
        View view = (View) rv;

        String idPart;
        int id = view.getId();
        if (id != View.NO_ID) {
            try {
                idPart = view.getContext().getResources().getResourceEntryName(id);
            } catch (Exception e) {
                idPart = "0x" + Integer.toHexString(id);
            }
        } else {
            idPart = "no_id";
        }

        return "RV#" + idPart + "#" + getAdapterName(rv);
    }

    private static String getAdapterName(Object rv) {
        try {
            Method getAdapter = rv.getClass().getMethod("getAdapter");
            Object adapter = getAdapter.invoke(rv);
            if (adapter != null) return adapter.getClass().getName();
        } catch (Exception ignored) {}
        return "null";
    }

    private static String lmTag(Object lm) {
        try {
            Field f = findField(lm.getClass(), "mRecyclerView");
            if (f != null) {
                f.setAccessible(true);
                Object rv = f.get(lm);
                if (rv != null) return rvTag(rv);
            }
        } catch (Exception ignored) {}
        return "RV#LM_unknown";
    }

    private static Field findField(Class<?> clazz, String... names) {
        Class<?> c = clazz;
        while (c != null && c != Object.class) {
            for (String n : names) {
                try { return c.getDeclaredField(n); }
                catch (NoSuchFieldException ignored) {}
            }
            c = c.getSuperclass();
        }
        return null;
    }
}
