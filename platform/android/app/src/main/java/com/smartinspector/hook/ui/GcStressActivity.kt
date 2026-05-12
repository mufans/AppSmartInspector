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
 * GC stress scenario — frequent large object allocations triggering garbage collection.
 * Triggers android.garbage_collection analysis.
 */
class GcStressActivity : AppCompatActivity() {

    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var gcCount = 0
    private var allocatedMB = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "GC Stress"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Frequent large object allocations triggering GC.\n" +
                    "Allocates arrays, bitmaps, and string builders rapidly.\n" +
                    "Triggers: android.garbage_collection"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val startBtn = Button(this).apply {
            text = "Start GC Stress (4 threads)"
            setOnClickListener { startGcStress() }
        }
        container.addView(startBtn)

        val triggerBtn = Button(this).apply {
            text = "Trigger Burst Allocation (100MB)"
            setOnClickListener { burstAllocation() }
        }
        container.addView(triggerBtn)

        val stopBtn = Button(this).apply {
            text = "Stop"
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

    private fun startGcStress() {
        if (running) return
        running = true
        gcCount = 0
        allocatedMB = 0

        // Thread 1: Large byte array allocations
        Thread({
            while (running && !Thread.currentThread().isInterrupted) {
                try {
                    // Allocate 1MB array, hold briefly, discard
                    val array = ByteArray(1024 * 1024) // 1MB
                    // Touch the array to ensure allocation
                    array[0] = 1
                    array[array.size - 1] = 1
                    allocatedMB += 1
                    Thread.sleep(100)
                    // array is now eligible for GC
                } catch (_: InterruptedException) {
                    return@Thread
                } catch (_: OutOfMemoryError) {
                    gcCount++
                    Log.w("GcStress", "OOM caught in array allocator, GC triggered")
                    try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
                }
            }
        }, "GcStress-ArrayAlloc").start()

        // Thread 2: String builder churn
        Thread({
            while (running && !Thread.currentThread().isInterrupted) {
                try {
                    val sb = StringBuilder()
                    for (i in 0 until 100_000) {
                        sb.append("data-$i,")
                    }
                    allocatedMB += sb.length / 1024 / 1024
                    Thread.sleep(50)
                } catch (_: InterruptedException) {
                    return@Thread
                }
            }
        }, "GcStress-StringChurn").start()

        // Thread 3: Short-lived object storms
        Thread({
            while (running && !Thread.currentThread().isInterrupted) {
                try {
                    val list = mutableListOf<ByteArray>()
                    for (i in 0 until 50) {
                        list.add(ByteArray(1024 * 64)) // 64KB each = 3.2MB total
                    }
                    allocatedMB += 3
                    Thread.sleep(80)
                } catch (_: InterruptedException) {
                    return@Thread
                } catch (_: OutOfMemoryError) {
                    gcCount++
                    try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
                }
            }
        }, "GcStress-ObjectStorm").start()

        // Thread 4: Main-thread allocation pressure
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                try {
                    // Allocate on main thread to trigger GC on UI thread
                    val data = ByteArray(512 * 1024) // 512KB
                    data[0] = 1
                    allocatedMB += 1
                    gcCount++
                    Log.i("GcStress", "Main thread allocation, total MB: $allocatedMB")
                } catch (_: OutOfMemoryError) {
                    gcCount++
                }
                handler.postDelayed(this, 300)
            }
        }, 300)

        statusText.text = "Status: running (4 stress threads)"
        startStatusUpdates()
    }

    private fun burstAllocation() {
        Thread({
            try {
                Log.i("GcStress", "Starting burst allocation...")
                val chunks = mutableListOf<ByteArray>()
                for (i in 0 until 100) {
                    chunks.add(ByteArray(1024 * 1024)) // 1MB each = 100MB total
                    allocatedMB += 1
                    if (i % 10 == 0) {
                        Log.i("GcStress", "Burst progress: ${i}MB allocated")
                    }
                }
                // Hold briefly then release
                Thread.sleep(500)
                chunks.clear()
                gcCount++
                Log.i("GcStress", "Burst allocation done, released 100MB")
            } catch (e: OutOfMemoryError) {
                gcCount++
                Log.w("GcStress", "Burst OOM: ${e.message}")
                System.gc()
            }
        }, "GcStress-Burst").start()
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                val runtime = Runtime.getRuntime()
                val usedMB = (runtime.totalMemory() - runtime.freeMemory()) / 1024 / 1024
                val maxMB = runtime.maxMemory() / 1024 / 1024
                statusText.text = "Status: running\n" +
                        "  GC triggers: $gcCount\n" +
                        "  Allocated total: ${allocatedMB}MB\n" +
                        "  Heap used: ${usedMB}MB / ${maxMB}MB"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun stop() {
        running = false
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (GC triggers: $gcCount, allocated: ${allocatedMB}MB)"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
    }
}
