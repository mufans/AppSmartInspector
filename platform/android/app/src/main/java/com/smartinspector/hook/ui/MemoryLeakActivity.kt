package com.smartinspector.hook.ui

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Memory leak scenario — demonstrates classic Android leak patterns.
 *
 * Provides 4 leak scenarios that can be toggled independently:
 * 1. Static reference leak — static field holds Activity reference
 * 2. Anonymous inner class leak — Runnable captures outer Activity
 * 3. Unregistered BroadcastReceiver — registerReceiver without unregister
 * 4. Singleton callback leak — singleton holds Activity callback
 *
 * Each scenario creates real leaks that will be visible in heap dump
 * and detectable by SmartInspector's lifecycle-aware leak detection.
 *
 * Triggers: heap_graph_object leak_suspects + SI$Activity lifecycle
 */
class MemoryLeakActivity : AppCompatActivity() {

    companion object {
        // Leak 1: Static reference holds Activity
        private var leakedActivity: MemoryLeakActivity? = null

        // Leak 4: Singleton callback holds Activity
        private var callback: (() -> Unit)? = null
    }

    private lateinit var statusText: TextView
    private val handler = Handler(Looper.getMainLooper())

    // Leak 2: Anonymous inner class Runnable captures outer Activity
    private val anonymousRunnable: Runnable = object : Runnable {
        override fun run() {
            // This anonymous Runnable holds implicit reference to the outer Activity.
            // Even after Activity.onDestroy(), handler keeps it alive.
            Log.i("MemoryLeak", "Anonymous runnable tick on ${this@MemoryLeakActivity}")
            handler.postDelayed(this, 1000)
        }
    }

    // Leak 3: BroadcastReceiver registered but never unregistered
    private var registeredReceiver = false

    // Leak tracking counts for display
    private var leakCount = 0
    private var navigateCount = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Memory Leak"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Demonstrates classic Android memory leak patterns.\n" +
                    "Enable leak types, then press 'Simulate Navigate Back'\n" +
                    "multiple times to accumulate leaked instances.\n\n" +
                    "After 5+ navigations, take a heap dump to see\n" +
                    "multiple MemoryLeakActivity instances in heap.\n\n" +
                    "Triggers: heap_graph_object, leak_suspects"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        // --- Leak 1: Static reference ---
        val leak1Btn = Button(this).apply {
            text = "Leak 1: Static Reference"
            setOnClickListener { enableStaticLeak() }
        }
        container.addView(leak1Btn)

        val leak1Desc = TextView(this).apply {
            text = "Sets Companion.leakedActivity = this.\n" +
                    "Static field outlives Activity instance."
            textSize = 12f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 4, 0, 16)
        }
        container.addView(leak1Desc)

        // --- Leak 2: Anonymous inner class ---
        val leak2Btn = Button(this).apply {
            text = "Leak 2: Anonymous Runnable (Handler)"
            setOnClickListener { enableAnonymousLeak() }
        }
        container.addView(leak2Btn)

        val leak2Desc = TextView(this).apply {
            text = "Posts anonymous Runnable to Handler every 1s.\n" +
                    "Runnable captures outer Activity, never removed."
            textSize = 12f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 4, 0, 16)
        }
        container.addView(leak2Desc)

        // --- Leak 3: Unregistered BroadcastReceiver ---
        val leak3Btn = Button(this).apply {
            text = "Leak 3: Unregistered BroadcastReceiver"
            setOnClickListener { enableReceiverLeak() }
        }
        container.addView(leak3Btn)

        val leak3Desc = TextView(this).apply {
            text = "Registers BroadcastReceiver dynamically.\n" +
                    "Never calls unregisterReceiver() in onDestroy()."
            textSize = 12f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 4, 0, 16)
        }
        container.addView(leak3Desc)

        // --- Leak 4: Singleton callback ---
        val leak4Btn = Button(this).apply {
            text = "Leak 4: Singleton Callback"
            setOnClickListener { enableSingletonLeak() }
        }
        container.addView(leak4Btn)

        val leak4Desc = TextView(this).apply {
            text = "Stores Activity lambda in static callback field.\n" +
                    "Lambda captures Activity reference."
            textSize = 12f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 4, 0, 16)
        }
        container.addView(leak4Desc)

        // --- Navigate simulation ---
        val navigateBtn = Button(this).apply {
            text = "Simulate Navigate Back + Re-enter"
            setOnClickListener { simulateNavigateAndReenter() }
        }
        container.addView(navigateBtn)

        val navigateDesc = TextView(this).apply {
            text = "Finishes this Activity and immediately relaunches it.\n" +
                    "Leaked references prevent GC from reclaiming old instances.\n" +
                    "Press 5+ times, then analyze heap to see multiple instances."
            textSize = 12f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 4, 0, 16)
        }
        container.addView(navigateDesc)

        // --- Enable all leaks ---
        val allBtn = Button(this).apply {
            text = "Enable All 4 Leaks"
            setOnClickListener {
                enableStaticLeak()
                enableAnonymousLeak()
                enableReceiverLeak()
                enableSingletonLeak()
            }
        }
        container.addView(allBtn)

        statusText = TextView(this).apply {
            text = "Leaks: 0 | Navigations: 0"
            textSize = 14f
            setTextColor(android.graphics.Color.parseColor("#D32F2F"))
            setPadding(0, 24, 0, 0)
        }
        container.addView(statusText)

        scrollView.addView(container)
        setContentView(scrollView)
    }

    // ─── Leak implementations ──────────────────────────────────────

    private fun enableStaticLeak() {
        leakedActivity = this
        leakCount++
        updateStatus("Static reference leak enabled")
    }

    private fun enableAnonymousLeak() {
        // Start the repeating runnable — it captures 'this' Activity
        handler.postDelayed(anonymousRunnable, 1000)
        leakCount++
        updateStatus("Anonymous Runnable leak enabled")
    }

    private fun enableReceiverLeak() {
        if (registeredReceiver) return
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                // This receiver captures the outer Activity
                Log.i("MemoryLeak", "Receiver triggered on ${this@MemoryLeakActivity}")
            }
        }
        registerReceiver(receiver, IntentFilter("com.smartinspector.LEAK_TEST"))
        registeredReceiver = true
        leakCount++
        updateStatus("BroadcastReceiver leak enabled (never unregistered)")
    }

    private fun enableSingletonLeak() {
        callback = {
            // Lambda captures 'this' Activity
            Log.i("MemoryLeak", "Singleton callback on $this")
        }
        leakCount++
        updateStatus("Singleton callback leak enabled")
    }

    // ─── Navigate simulation ──────────────────────────────────────

    private fun simulateNavigateAndReenter() {
        navigateCount++
        // Finish current Activity, then relaunch after a brief delay
        // The leaked references prevent GC from reclaiming the old instance
        val intent = Intent(this, MemoryLeakActivity::class.java)
        startActivity(intent)
        finish()
    }

    // ─── Helpers ──────────────────────────────────────────────────

    private fun updateStatus(msg: String) {
        statusText.text = "Leaks: $leakCount | Navigations: $navigateCount\n$msg"
        Log.w("MemoryLeak", "[$msg] Activity=$this")
    }

    // Intentionally NOT cleaning up leaks in onDestroy — that's the point.
    // A well-behaved Activity would clear static refs, remove callbacks,
    // and unregister receivers here.
    override fun onDestroy() {
        super.onDestroy()
        // Only remove the handler callbacks — the static/anonymous/singleton
        // leaks remain, which is what we want to test detection for.
        handler.removeCallbacksAndMessages(null)
    }
}
