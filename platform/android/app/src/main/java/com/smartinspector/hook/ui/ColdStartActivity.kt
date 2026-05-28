package com.smartinspector.hook.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Cold start scenario — simulates slow app startup with delayed initialization.
 * Can be triggered via: adb shell am start com.smartinspector.hook/.ui.ColdStartActivity
 * Triggers android.startup analysis.
 */
class ColdStartActivity : AppCompatActivity() {

    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        val startTime = SystemClock.elapsedRealtime()
        Log.i("ColdStart", "ColdStartActivity.onCreate started")

        // Phase 1: Simulate slow Application.onCreate work
        simulateSlowInit()

        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Cold Start"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val elapsed = SystemClock.elapsedRealtime() - startTime
        val desc = TextView(this).apply {
            text = "Simulates slow cold start with heavy initialization.\n" +
                    "Launch via: adb shell am start\n" +
                    "  com.smartinspector.hook/.ui.ColdStartActivity\n\n" +
                    "Init took: ${elapsed}ms\n" +
                    "Triggers: android.startup"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val statusText = TextView(this).apply {
            text = "Startup phases:\n"  +
                    "  [done] Application init (simulated)\n" +
                    "  [done] Activity.onCreate (heavy)\n" +
                    "  [pending] Layout inflation\n" +
                    "  [pending] Data loading\n"
            textSize = 14f
            setPadding(0, 0, 0, 24)
        }
        container.addView(statusText)

        val restartBtn = Button(this).apply {
            text = "Simulate Restart (re-run init)"
            setOnClickListener {
                Thread {
                    Log.i("ColdStart", "Re-running slow init...")
                    simulateSlowInit()
                    handler.post {
                        statusText.text = "Restart complete.\nInit re-ran successfully."
                    }
                }.start()
            }
        }
        container.addView(restartBtn)

        scrollView.addView(container)
        setContentView(scrollView)

        // Phase 2: Post-create deferred work
        handler.postDelayed({
            Log.i("ColdStart", "Phase 2: Deferred initialization starting")
            simulateDeferredInit()
            Log.i("ColdStart", "Phase 2: Deferred initialization complete")
        }, 500)

        Log.i("ColdStart", "ColdStartActivity.onCreate done in ${SystemClock.elapsedRealtime() - startTime}ms")
    }

    /**
     * Simulate heavy Application.onCreate work.
     * This runs BEFORE super.onCreate() to be part of the cold start path.
     */
    private fun simulateSlowInit() {
        // Simulate SDK initialization (analytics, crash reporting, etc.)
        Log.i("ColdStart", "Phase 1a: SDK init simulation (200ms)")
        try { Thread.sleep(200) } catch (_: InterruptedException) {}

        // Simulate database migration check
        Log.i("ColdStart", "Phase 1b: Database pre-check (150ms)")
        var hash = 0
        for (i in 0 until 500_000) {
            hash = hash * 31 + i
        }
        // Use hash to prevent optimization
        Log.d("ColdStart", "Hash check: $hash")

        // Simulate config loading
        Log.i("ColdStart", "Phase 1c: Config loading (100ms)")
        try { Thread.sleep(100) } catch (_: InterruptedException) {}

        // Simulate DI setup
        Log.i("ColdStart", "Phase 1d: Dependency injection setup (150ms)")
        val modules = listOf("NetworkModule", "DatabaseModule", "RepositoryModule", "ViewModelModule")
        for (module in modules) {
            Log.d("ColdStart", "Initializing $module...")
            var sum = 0.0
            for (i in 0 until 100_000) {
                sum += Math.sqrt(i.toDouble())
            }
        }
    }

    /**
     * Simulate deferred initialization that happens after first frame.
     */
    private fun simulateDeferredInit() {
        // Simulate cache warming
        Log.i("ColdStart", "Deferred: Cache warming (100ms)")
        try { Thread.sleep(100) } catch (_: InterruptedException) {}

        // Simulate feature flag fetch
        Log.i("ColdStart", "Deferred: Feature flags (50ms)")
        try { Thread.sleep(50) } catch (_: InterruptedException) {}

        // Simulate analytics event batching
        Log.i("ColdStart", "Deferred: Analytics setup (50ms)")
        var sum = 0.0
        for (i in 0 until 200_000) {
            sum += Math.sin(i.toDouble())
        }
    }
}
