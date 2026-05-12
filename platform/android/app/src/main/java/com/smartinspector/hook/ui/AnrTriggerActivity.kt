package com.smartinspector.hook.ui

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * ANR trigger scenario — main thread blocking operations.
 * Triggers android.anrs analysis.
 */
class AnrTriggerActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "ANR Trigger"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Triggers ANR by blocking the main thread.\n" +
                    "WARNING: These will make the app unresponsive!\n" +
                    "Triggers: android.anrs"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val warning = TextView(this).apply {
            text = "Each button will block the main thread for the\nduration shown. The app will freeze until complete."
            textSize = 13f
            setTextColor(android.graphics.Color.parseColor("#D32F2F"))
            setPadding(0, 0, 0, 24)
        }
        container.addView(warning)

        val sleep5Btn = Button(this).apply {
            text = "Sleep Main Thread (5s)"
            setOnClickListener { triggerSleepAnr(5000) }
        }
        container.addView(sleep5Btn)

        val sleep10Btn = Button(this).apply {
            text = "Sleep Main Thread (10s)"
            setOnClickListener { triggerSleepAnr(10000) }
        }
        container.addView(sleep10Btn)

        val computeBtn = Button(this).apply {
            text = "Heavy Computation (8s)"
            setOnClickListener { triggerComputeAnr() }
        }
        container.addView(computeBtn)

        val broadcastBtn = Button(this).apply {
            text = "Broadcast ANR (ordered broadcast)"
            setOnClickListener { triggerBroadcastAnr() }
        }
        container.addView(broadcastBtn)

        val lockBtn = Button(this).apply {
            text = "Lock Contention ANR (6s)"
            setOnClickListener { triggerLockAnr() }
        }
        container.addView(lockBtn)

        scrollView.addView(container)
        setContentView(scrollView)
    }

    /**
     * Type 1: Thread.sleep on main thread — simplest ANR trigger.
     */
    private fun triggerSleepAnr(durationMs: Int) {
        Log.i("AnrTrigger", "Triggering sleep ANR: ${durationMs}ms on main thread")
        try {
            Thread.sleep(durationMs.toLong())
        } catch (_: InterruptedException) {}
        Log.i("AnrTrigger", "Sleep ANR completed")
    }

    /**
     * Type 2: CPU-heavy computation — no sleep, pure CPU work.
     */
    private fun triggerComputeAnr() {
        Log.i("AnrTrigger", "Triggering compute ANR: heavy math for ~8s")
        val start = System.currentTimeMillis()
        var result = 0.0
        while (System.currentTimeMillis() - start < 8000) {
            for (i in 0 until 1_000_000) {
                result += Math.sqrt(i.toDouble()) * Math.sin(i.toDouble())
            }
            if (result.isNaN()) break
        }
        Log.i("AnrTrigger", "Compute ANR completed, result=$result")
    }

    /**
     * Type 3: Ordered broadcast with slow receiver.
     * Registers a receiver that blocks in onReceive for 6 seconds.
     */
    private fun triggerBroadcastAnr() {
        Log.i("AnrTrigger", "Triggering broadcast ANR")

        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                Log.i("AnrTrigger", "BroadcastReceiver.onReceive called, sleeping 6s")
                // Block the receiver — ANR for broadcast receivers is 10s foreground
                try {
                    Thread.sleep(6000)
                } catch (_: InterruptedException) {}
                Log.i("AnrTrigger", "BroadcastReceiver.onReceive done")
            }
        }

        // Register and send a custom broadcast
        val action = "com.smartinspector.hook.ACTION_SLOW_BROADCAST"
        registerReceiver(receiver, IntentFilter(action))

        val intent = Intent(action)
        intent.setPackage(packageName)
        sendOrderedBroadcast(intent, null)

        // Unregister after delay
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            try {
                unregisterReceiver(receiver)
            } catch (_: Exception) {}
        }, 8000)
    }

    /**
     * Type 4: Lock contention on main thread — synchronized block held by background thread.
     */
    private fun triggerLockAnr() {
        val lock = Object()

        // Background thread holds lock for 6 seconds
        Thread({
            synchronized(lock) {
                Log.i("AnrTrigger", "Background thread holding lock for 6s")
                try {
                    Thread.sleep(6000)
                } catch (_: InterruptedException) {}
            }
            Log.i("AnrTrigger", "Background thread released lock")
        }, "AnrLockHolder").start()

        // Give background thread time to acquire lock
        try { Thread.sleep(100) } catch (_: InterruptedException) {}

        // Main thread tries to acquire — blocks for 6 seconds
        Log.i("AnrTrigger", "Main thread waiting for lock...")
        synchronized(lock) {
            Log.i("AnrTrigger", "Main thread acquired lock after waiting")
        }
    }
}
