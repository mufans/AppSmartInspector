package com.smartinspector.hook.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * CPU throttling scenario — sustained high CPU load causing thermal throttling.
 * Triggers cpu_throttling dimension analysis (frequency drop below 50% max).
 */
class CpuThrottlingActivity : AppCompatActivity() {

    private val threads = mutableListOf<Thread>()
    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var totalIterations = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "CPU Throttling"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Sustained high CPU load to trigger thermal throttling.\n" +
                    "Runs CPU-intensive loops on all cores for extended duration.\n" +
                    "Watch CPU frequency drop as thermal limits are hit.\n" +
                    "Triggers: cpu_throttling dimension, cpu_counter_track"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val numCores = Runtime.getRuntime().availableProcessors()
        val coreInfo = TextView(this).apply {
            text = "Device cores: $numCores"
            textSize = 13f
            setPadding(0, 0, 0, 16)
        }
        container.addView(coreInfo)

        val startAllBtn = Button(this).apply {
            text = "Start Full Load (all $numCores cores)"
            setOnClickListener { startFullLoad() }
        }
        container.addView(startAllBtn)

        val start8Btn = Button(this).apply {
            text = "Start 8-Core Sustained Load (30s)"
            setOnClickListener { startTimedLoad(8, 30) }
        }
        container.addView(start8Btn)

        val start60Btn = Button(this).apply {
            text = "Start Extended Load (60s — deep throttle)"
            setOnClickListener { startTimedLoad(numCores, 60) }
        }
        container.addView(start60Btn)

        val mixedBtn = Button(this).apply {
            text = "Mixed Load (CPU + Memory pressure)"
            setOnClickListener { startMixedLoad() }
        }
        container.addView(mixedBtn)

        val stopBtn = Button(this).apply {
            text = "Stop All"
            setOnClickListener { stop() }
        }
        container.addView(stopBtn)

        statusText = TextView(this).apply {
            text = "Status: idle"
            textSize = 14f
            setPadding(0, 24, 0, 0)
        }
        container.addView(statusText)

        scrollView.addView(container)
        setContentView(scrollView)
    }

    /**
     * Full load on all cores — indefinite until stopped.
     * Each thread runs a tight math loop without any sleep or yield.
     */
    private fun startFullLoad() {
        if (running) stop()
        running = true
        totalIterations = 0

        val numCores = Runtime.getRuntime().availableProcessors()
        for (t in 0 until numCores) {
            spawnBurnThread(t, null)
        }

        statusText.text = "Status: full load ($numCores threads)"
        startStatusUpdates()
        Log.i("CpuThrottle", "Started full load on $numCores cores")
    }

    /**
     * Timed load — runs for a specified duration then stops.
     * This is useful for producing a clear throttling signature in traces.
     */
    private fun startTimedLoad(threadCount: Int, durationSeconds: Int) {
        if (running) stop()
        running = true
        totalIterations = 0

        for (t in 0 until threadCount) {
            spawnBurnThread(t, durationSeconds * 1000L)
        }

        statusText.text = "Status: timed load ($threadCount threads, ${durationSeconds}s)"
        startStatusUpdates()
        Log.i("CpuThrottle", "Started $threadCount threads for ${durationSeconds}s")
    }

    /**
     * Mixed load — CPU intensive + memory pressure to maximize heat generation.
     * CPU-heavy math combined with large memory accesses stresses both compute and memory bus.
     */
    private fun startMixedLoad() {
        if (running) stop()
        running = true
        totalIterations = 0

        val numCores = Runtime.getRuntime().availableProcessors()

        // Half the threads: pure CPU burn
        for (t in 0 until numCores / 2) {
            spawnBurnThread(t, null)
        }

        // Other half: CPU + memory pressure
        for (t in 0 until numCores - numCores / 2) {
            val thread = Thread({
                val bigArray = DoubleArray(1024 * 1024) // 8MB working set
                var iter = 0L
                while (running && !Thread.currentThread().isInterrupted) {
                    // Matrix-like operations: access memory in a pattern that defeats prefetch
                    var sum = 0.0
                    for (i in bigArray.indices) {
                        bigArray[i] = Math.sqrt(bigArray[i] + i)
                        sum += bigArray[i]
                    }
                    // Don't let the compiler optimize away
                    if (sum.isNaN()) break
                    iter++
                    totalIterations++
                }
                Log.d("CpuThrottle", "MemBurnThread-$t done: $iter iterations")
            }, "MemBurnThread-$t")
            thread.priority = Thread.NORM_PRIORITY
            thread.start()
            threads.add(thread)
        }

        statusText.text = "Status: mixed load ($numCores threads)"
        startStatusUpdates()
        Log.i("CpuThrottle", "Started mixed load on $numCores cores")
    }

    private fun spawnBurnThread(id: Int, durationMs: Long?) {
        val thread = Thread({
            val startTime = System.currentTimeMillis()
            var localIter = 0L
            while (running && !Thread.currentThread().isInterrupted) {
                // Tight CPU loop — pure math, no I/O, no allocation
                var x = 1.0
                for (i in 0 until 1_000_000) {
                    x = Math.sin(x) * Math.cos(x) + Math.sqrt(Math.abs(x) + 1.0)
                }
                // Prevent dead code elimination
                if (x.isNaN()) break
                localIter++
                totalIterations++

                // Check duration limit
                if (durationMs != null && System.currentTimeMillis() - startTime >= durationMs) {
                    Log.d("CpuThrottle", "BurnThread-$id reached duration limit")
                    break
                }
            }
            Log.d("CpuThrottle", "BurnThread-$id done: $localIter iterations")
        }, "BurnThread-$id")
        thread.priority = Thread.NORM_PRIORITY
        thread.start()
        threads.add(thread)
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                val activeThreads = threads.count { it.isAlive }
                statusText.text = "Status: running\n" +
                        "  Active threads: $activeThreads\n" +
                        "  Total iterations: $totalIterations\n" +
                        "  Hint: watch CPU freq drop via Perfetto cpu_counter_track"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun stop() {
        running = false
        threads.forEach { it.interrupt() }
        threads.clear()
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (total iterations: $totalIterations)"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
    }
}
