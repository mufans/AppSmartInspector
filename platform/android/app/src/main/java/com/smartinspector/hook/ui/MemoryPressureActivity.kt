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
 * Memory pressure scenario — large allocations triggering OOM adj changes and potential LMK.
 * Triggers android.memory.process analysis.
 */
class MemoryPressureActivity : AppCompatActivity() {

    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var allocatedMB = 0L
    private val heldBuffers = mutableListOf<ByteArray>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Memory Pressure"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Allocates large chunks of memory to trigger OOM adj changes.\n" +
                    "May trigger LMK (Low Memory Killer) on other processes.\n" +
                    "Triggers: android.memory.process, OOM score transitions"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val alloc50Btn = Button(this).apply {
            text = "Allocate 50MB (hold)"
            setOnClickListener { allocateAndHold(50) }
        }
        container.addView(alloc50Btn)

        val alloc100Btn = Button(this).apply {
            text = "Allocate 100MB (hold)"
            setOnClickListener { allocateAndHold(100) }
        }
        container.addView(alloc100Btn)

        val alloc200Btn = Button(this).apply {
            text = "Allocate 200MB (hold)"
            setOnClickListener { allocateAndHold(200) }
        }
        container.addView(alloc200Btn)

        val churnBtn = Button(this).apply {
            text = "Start Memory Churn (continuous)"
            setOnClickListener { startMemoryChurn() }
        }
        container.addView(churnBtn)

        val releaseBtn = Button(this).apply {
            text = "Release All"
            setOnClickListener { releaseAll() }
        }
        container.addView(releaseBtn)

        statusText = TextView(this).apply {
            text = "Status: idle"
            textSize = 14f
            setPadding(0, 24, 0, 0)
        }
        container.addView(statusText)

        scrollView.addView(container)
        setContentView(scrollView)

        updateHeapStatus()
    }

    private fun allocateAndHold(mb: Int) {
        Thread({
            try {
                Log.i("MemoryPressure", "Allocating ${mb}MB...")
                val chunk = ByteArray(mb * 1024 * 1024)
                // Touch all pages to ensure physical allocation
                for (i in chunk.indices step 4096) {
                    chunk[i] = 1
                }
                heldBuffers.add(chunk)
                allocatedMB += mb
                Log.i("MemoryPressure", "Allocated ${mb}MB, total held: ${allocatedMB}MB")

                handler.post { updateHeapStatus() }
            } catch (e: OutOfMemoryError) {
                Log.w("MemoryPressure", "OOM while allocating ${mb}MB: ${e.message}")
                handler.post {
                    statusText.text = "Status: OOM! Cannot allocate ${mb}MB.\n" +
                            "Held: ${allocatedMB}MB. Try releasing first."
                }
                System.gc()
            }
        }, "MemAllocWorker").start()
    }

    private fun startMemoryChurn() {
        if (running) return
        running = true

        // Background thread: allocate and free in cycles
        Thread({
            var cycle = 0
            while (running && !Thread.currentThread().isInterrupted) {
                try {
                    // Allocate 10MB
                    val buffer = ByteArray(10 * 1024 * 1024)
                    for (i in buffer.indices step 4096) {
                        buffer[i] = 1
                    }
                    cycle++
                    Log.d("MemoryPressure", "Churn cycle $cycle: allocated 10MB")
                    Thread.sleep(200)
                    // Buffer goes out of scope, eligible for GC
                } catch (_: InterruptedException) {
                    return@Thread
                } catch (e: OutOfMemoryError) {
                    Log.w("MemoryPressure", "Churn OOM: ${e.message}")
                    System.gc()
                    try { Thread.sleep(500) } catch (_: InterruptedException) { return@Thread }
                }
            }
            Log.i("MemoryPressure", "Memory churn stopped after $cycle cycles")
        }, "MemChurnWorker").start()

        // Periodically update status
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                updateHeapStatus()
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun releaseAll() {
        running = false
        heldBuffers.clear()
        allocatedMB = 0
        handler.removeCallbacksAndMessages(null)
        System.gc()
        Log.i("MemoryPressure", "Released all buffers")
        updateHeapStatus()
    }

    private fun updateHeapStatus() {
        val runtime = Runtime.getRuntime()
        val usedMB = (runtime.totalMemory() - runtime.freeMemory()) / 1024 / 1024
        val totalMB = runtime.totalMemory() / 1024 / 1024
        val maxMB = runtime.maxMemory() / 1024 / 1024

        statusText.text = "Heap: ${usedMB}MB used / ${totalMB}MB total / ${maxMB}MB max\n" +
                "Held buffers: ${allocatedMB}MB (${heldBuffers.size} chunks)\n" +
                "Churn: ${if (running) "active" else "stopped"}"
    }

    override fun onDestroy() {
        super.onDestroy()
        running = false
        heldBuffers.clear()
        handler.removeCallbacksAndMessages(null)
    }
}
