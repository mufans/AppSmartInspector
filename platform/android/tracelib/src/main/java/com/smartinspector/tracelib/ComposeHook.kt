package com.smartinspector.tracelib

import android.os.Trace
import android.util.Log
import top.canyie.pine.Pine
import top.canyie.pine.PineConfig
import top.canyie.pine.callback.MethodHook
import java.lang.reflect.Method
import java.util.concurrent.atomic.AtomicLong

/**
 * Jetpack Compose recomposition tracking hook.
 *
 * Tracks Compose recompositions by hooking into the Compose runtime's internal
 * tracing mechanism. Emits SI$compose# prefixed slices into Perfetto traces for
 * downstream analysis.
 *
 * Tag format:
 *   SI$compose#ComposableName#first     — first composition
 *   SI$compose#ComposableName#recompose — recomposition
 *
 * Usage: called from [TraceHook.doInit] when compose_tracking hook is enabled.
 */
object ComposeHook {
    private const val TAG = "SmartInspector"
    private const val SI_PREFIX = "SI$"
    private const val COMPOSE_PREFIX = "compose#"

    private val recomposeCounters = java.util.concurrent.ConcurrentHashMap<String, AtomicLong>()

    /** Install Compose recomposition hooks. */
    fun hook() {
        hookTracerImpl()
    }

    /**
     * Strategy A: Hook Compose Runtime's TracerImpl.
     *
     * androidx.compose.runtime.internal.TracerImpl is used when
     * compose runtime tracing is enabled. It wraps beginSection/endSection
     * with composable function names. We intercept these to emit SI$ tags.
     */
    private fun hookTracerImpl() {
        try {
            val tracerClass = Class.forName(
                "androidx.compose.runtime.internal.TracerImpl"
            )

            // Hook beginSection — called at the start of each composable
            val beginMethod = tracerClass.getDeclaredMethod("beginSection", String::class.java)
            Pine.hook(beginMethod, object : MethodHook() {
                override fun beforeCall(cf: Pine.CallFrame) {
                    if (!HookConfigManager.isEnabled("compose_tracking")) return
                    val name = cf.args[0] as? String ?: return
                    val key = name.replace("#", "_").takeIf {
                        it.isNotBlank()
                    } ?: return

                    // Track recomposition count
                    val counter = recomposeCounters.computeIfAbsent(key) {
                        AtomicLong(0)
                    }
                    val count = counter.incrementAndGet()
                    val suffix = if (count == 1L) "first" else "recompose"

                    // atrace section name limit: 127 bytes
                    var tag = "$SI_PREFIX$COMPOSE_PREFIX${name.take(80)}#$suffix"
                    if (tag.length > 127) {
                        val maxNameLen = 127 - "$SI_PREFIX$COMPOSE_PREFIX".length - 1 - suffix.length
                        tag = "$SI_PREFIX$COMPOSE_PREFIX${name.take(maxNameLen)}#$suffix"
                    }
                    Trace.beginSection(tag)
                }

                override fun afterCall(cf: Pine.CallFrame) {
                    Trace.endSection()
                }
            })

            // Hook endSection — called at the end of each composable
            val endMethod = tracerClass.getDeclaredMethod("endSection")
            Pine.hook(endMethod, object : MethodHook() {
                override fun beforeCall(cf: Pine.CallFrame) {
                    // endSection is already handled by afterCall of beginSection hook
                }

                override fun afterCall(cf: Pine.CallFrame) {
                    // No-op: the endSection in TracerImpl is a no-op by default,
                    // our afterCall in beginSection hook handles the Trace.endSection
                }
            })

            Log.d(TAG, "Hooked Compose TracerImpl")
        } catch (e: ClassNotFoundException) {
            Log.w(TAG, "Compose TracerImpl not found (Compose not in classpath): ${e.message}")
            // Fallback to Strategy B
            hookComposerImpl()
        } catch (e: Exception) {
            Log.w(TAG, "Compose TracerImpl hook failed: ${e.message}")
            hookComposerImpl()
        }
    }

    /**
     * Strategy B: Hook ComposerImpl's restart group methods.
     *
     * startRestartGroup / endRestartGroup are called for each composable
     * that participates in recomposition. We intercept these to emit
     * SI$compose# tags with the composable's key/name.
     *
     * This is a fallback when TracerImpl is not available (older Compose versions
     * or when runtime tracing is not enabled).
     */
    private fun hookComposerImpl() {
        try {
            val composerClass = Class.forName(
                "androidx.compose.runtime.ComposerImpl"
            )

            // startRestartGroup(key: Int) — called at start of a restartable composable
            try {
                val startMethod = composerClass.getDeclaredMethod("startRestartGroup", Int::class.java)
                Pine.hook(startMethod, object : MethodHook() {
                    override fun beforeCall(cf: Pine.CallFrame) {
                        if (!HookConfigManager.isEnabled("compose_tracking")) return
                        val key = cf.args[0] as? Int ?: return
                        val label = "restartGroup_$key"
                        val suffix = "recompose"
                        var tag = "$SI_PREFIX$COMPOSE_PREFIX${label}#$suffix"
                        if (tag.length > 127) {
                            tag = tag.take(127)
                        }
                        Trace.beginSection(tag)
                    }

                    override fun afterCall(cf: Pine.CallFrame) {
                        Trace.endSection()
                    }
                })
                Log.d(TAG, "Hooked ComposerImpl.startRestartGroup")
            } catch (e: Exception) {
                Log.w(TAG, "startRestartGroup hook failed: ${e.message}")
            }

            // startReusableNode — called for layout nodes
            try {
                val nodeMethod = composerClass.getDeclaredMethod(
                    "startReusableNode", Int::class.java
                )
                Pine.hook(nodeMethod, object : MethodHook() {
                    override fun beforeCall(cf: Pine.CallFrame) {
                        if (!HookConfigManager.isEnabled("compose_tracking")) return
                        val key = cf.args[0] as? Int ?: return
                        var tag = "$SI_PREFIX${COMPOSE_PREFIX}node_$key#layout"
                        if (tag.length > 127) {
                            tag = tag.take(127)
                        }
                        Trace.beginSection(tag)
                    }

                    override fun afterCall(cf: Pine.CallFrame) {
                        Trace.endSection()
                    }
                })
            } catch (e: Exception) {
                // startReusableNode may not exist in all Compose versions
                Log.d(TAG, "startReusableNode not available: ${e.message}")
            }

            Log.d(TAG, "Hooked Compose ComposerImpl (fallback)")
        } catch (e: ClassNotFoundException) {
            Log.w(TAG, "Compose runtime not found — skipping Compose hooks: ${e.message}")
        } catch (e: Exception) {
            Log.w(TAG, "Compose ComposerImpl hook failed: ${e.message}")
        }
    }

    /** Reset recomposition counters (called when a new trace starts). */
    fun resetCounters() {
        recomposeCounters.clear()
    }

    /** Get recomposition counts snapshot. */
    fun getRecompositionCounts(): Map<String, Long> {
        return recomposeCounters.mapValues { it.value.get() }
    }
}
