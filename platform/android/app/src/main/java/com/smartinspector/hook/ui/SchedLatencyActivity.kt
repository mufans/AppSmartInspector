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
 * Scheduling latency scenario — many CPU-intensive threads competing for cores.
 * Triggers sched.latency analysis.
 */
class SchedLatencyActivity : AppCompatActivity() {

    private val threads = mutableListOf<Thread>()
    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var totalCycles = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Sched Latency"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Creates many CPU-intensive threads competing for cores.\n" +
                    "Causes scheduling delays (runnable→running latency).\n" +
                    "Triggers: sched.latency"
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

        val start8Btn = Button(this).apply {
            text = "Start 8 CPU Threads"
            setOnClickListener { startSchedStress(8) }
        }
        container.addView(start8Btn)

        val start16Btn = Button(this).apply {
            text = "Start 16 CPU Threads (over-subscribe)"
            setOnClickListener { startSchedStress(16) }
        }
        container.addView(start16Btn)

        val start32Btn = Button(this).apply {
            text = "Start 32 CPU Threads (heavy)"
            setOnClickListener { startSchedStress(32) }
        }
        container.addView(start32Btn)

        val yieldBtn = Button(this).apply {
            text = "Add Yielding Thread (causes context switches)"
            setOnClickListener { startYieldingThread() }
        }
        container.addView(yieldBtn)

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

    private fun startSchedStress(threadCount: Int) {
        if (running) stop()
        running = true
        totalCycles = 0

        for (t in 0 until threadCount) {
            val thread = Thread({
                var localCycles = 0L
                while (running && !Thread.currentThread().isInterrupted) {
                    // Pure CPU work — no sleep, no I/O
                    var result = 0.0
                    for (i in 0 until 500_000) {
                        result += Math.sqrt(i.toDouble()) * Math.sin(i.toDouble())
                    }
                    if (result.isNaN()) break
                    localCycles++
                    totalCycles++

                    // Occasional yield to increase context switching
                    if (localCycles % 10 == 0L) {
                        Thread.yield()
                    }
                }
                Log.d("SchedLatency", "SchedWorker-$t done: $localCycles cycles")
            }, "SchedWorker-$t")
            // Mix priorities to create scheduling contention
            thread.priority = if (t % 3 == 0) Thread.NORM_PRIORITY
            else Thread.NORM_PRIORITY - 1
            thread.start()
            threads.add(thread)
        }

        statusText.text = "Status: running ($threadCount CPU threads)"
        startStatusUpdates()
        Log.i("SchedLatency", "Started $threadCount CPU stress threads")
    }

    private fun startYieldingThread() {
        val thread = Thread({
            var yieldCount = 0L
            while (running && !Thread.currentThread().isInterrupted) {
                // Rapid yield loop — causes excessive context switches
                Thread.yield()
                yieldCount++
                if (yieldCount % 10000 == 0L) {
                    Log.d("SchedLatency", "YieldThread: $yieldCount yields")
                }
            }
            Log.d("SchedLatency", "YieldThread done: $yieldCount yields")
        }, "SchedYieldThread")
        thread.priority = Thread.MIN_PRIORITY
        thread.start()
        threads.add(thread)
        Log.i("SchedLatency", "Added yielding thread")
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                statusText.text = "Status: running\n" +
                        "  Active threads: ${threads.count { it.isAlive }}\n" +
                        "  Total cycles: $totalCycles"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun stop() {
        running = false
        threads.forEach { it.interrupt() }
        threads.clear()
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (total cycles: $totalCycles)"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
    }
}
