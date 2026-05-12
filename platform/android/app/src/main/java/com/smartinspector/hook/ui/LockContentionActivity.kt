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
 * Lock contention scenario — multiple threads competing for a synchronized lock.
 * Triggers android.monitor_contention analysis.
 */
class LockContentionActivity : AppCompatActivity() {

    private val lock = Object()
    private val threads = mutableListOf<Thread>()
    private var running = false
    private var contentionCount = 0
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Lock Contention"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Multiple threads compete for a synchronized lock.\n" +
                    "Main thread waits while workers hold the lock.\n" +
                    "Triggers: android.monitor_contention"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val startBtn = Button(this).apply {
            text = "Start Contention (4 workers)"
            setOnClickListener { startContention() }
        }
        container.addView(startBtn)

        val stopBtn = Button(this).apply {
            text = "Stop"
            setOnClickListener { stopContention() }
        }
        container.addView(stopBtn)

        val mainBlockBtn = Button(this).apply {
            text = "Block Main Thread (3s)"
            setOnClickListener { blockMainThread() }
        }
        container.addView(mainBlockBtn)

        statusText = TextView(this).apply {
            text = "Status: idle"
            textSize = 14f
            setPadding(0, 24, 0, 0)
        }
        container.addView(statusText)

        scrollView.addView(container)
        setContentView(scrollView)
    }

    private fun startContention() {
        if (running) return
        running = true
        contentionCount = 0

        for (i in 0 until 4) {
            val thread = Thread({
                while (running && !Thread.currentThread().isInterrupted) {
                    synchronized(lock) {
                        try {
                            Log.i("LockWorker", "LockWorker-$i acquired lock, doing work...")
                            // Hold lock for 200ms to create contention window
                            Thread.sleep(200)
                            contentionCount++
                        } catch (_: InterruptedException) {
                            return@Thread
                        }
                    }
                    // Brief pause between acquisitions
                    try {
                        Thread.sleep(50)
                    } catch (_: InterruptedException) {
                        return@Thread
                    }
                }
                Log.i("LockWorker", "LockWorker-$i exiting")
            }, "LockWorker-$i")
            thread.start()
            threads.add(thread)
        }

        statusText.text = "Status: running (4 workers competing for lock)"
        startStatusUpdates()
    }

    private fun stopContention() {
        running = false
        threads.forEach { it.interrupt() }
        threads.clear()
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (contentions: $contentionCount)"
    }

    private fun blockMainThread() {
        Log.i("LockWorker", "Main thread attempting to acquire lock...")
        synchronized(lock) {
            try {
                // Main thread holds lock for 3 seconds — other workers will block
                Log.i("LockWorker", "Main thread acquired lock, blocking for 3s")
                Thread.sleep(3000)
            } catch (_: InterruptedException) {}
        }
        Log.i("LockWorker", "Main thread released lock")
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                statusText.text = "Status: running (contentions: $contentionCount)"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    override fun onDestroy() {
        super.onDestroy()
        stopContention()
    }
}
