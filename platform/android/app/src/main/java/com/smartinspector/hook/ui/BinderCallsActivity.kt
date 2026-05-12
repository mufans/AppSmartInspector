package com.smartinspector.hook.ui

import android.content.ContentValues
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Binder IPC scenario — frequent ContentProvider queries and cross-process calls.
 * Triggers android.binder and android.binder_breakdown analysis.
 */
class BinderCallsActivity : AppCompatActivity() {

    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var queryCount = 0
    private var insertCount = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Binder IPC Calls"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Frequent ContentProvider queries and batch inserts.\n" +
                    "Simulates cross-process IPC traffic.\n" +
                    "Triggers: android.binder, android.binder_breakdown"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val startQueryBtn = Button(this).apply {
            text = "Start Rapid Queries"
            setOnClickListener { startRapidQueries() }
        }
        container.addView(startQueryBtn)

        val startInsertBtn = Button(this).apply {
            text = "Batch Insert (100 rows)"
            setOnClickListener { batchInsert() }
        }
        container.addView(startInsertBtn)

        val startBothBtn = Button(this).apply {
            text = "Start Both (heavy IPC)"
            setOnClickListener {
                startRapidQueries()
                startBatchInserts()
            }
        }
        container.addView(startBothBtn)

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

    private fun startRapidQueries() {
        if (running) return
        running = true
        queryCount = 0

        Thread({
            while (running && !Thread.currentThread().isInterrupted) {
                try {
                    // Query Settings provider — standard cross-process ContentProvider call
                    val cursor = contentResolver.query(
                        android.provider.Settings.System.CONTENT_URI,
                        arrayOf("name", "value"),
                        null, null, null
                    )
                    cursor?.close()
                    queryCount++
                    Log.d("BinderWorker", "Query #$queryCount completed")

                    // Also query Contacts for additional IPC variety
                    val cursor2 = contentResolver.query(
                        android.provider.ContactsContract.Settings.CONTENT_URI,
                        null, null, null, null
                    )
                    cursor2?.close()
                    queryCount++

                    Thread.sleep(50)
                } catch (e: Exception) {
                    Log.w("BinderWorker", "Query error: ${e.message}")
                    try { Thread.sleep(100) } catch (_: InterruptedException) { return@Thread }
                }
            }
        }, "BinderQueryWorker").start()

        startStatusUpdates()
    }

    private fun batchInsert() {
        Thread({
            try {
                val values = ContentValues()
                for (i in 0 until 100) {
                    values.clear()
                    values.put("name", "si_test_$i")
                    values.put("value", "test_value_$i")

                    // This will fail (no writable provider) but generates Binder traffic
                    try {
                        contentResolver.insert(
                            android.provider.Settings.System.CONTENT_URI,
                            values
                        )
                    } catch (_: Exception) {}
                    insertCount++
                }
                Log.i("BinderWorker", "Batch insert done: $insertCount attempts")
            } catch (_: InterruptedException) {}
        }, "BinderInsertWorker").start()
    }

    private fun startBatchInserts() {
        insertCount = 0
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                batchInsert()
                handler.postDelayed(this, 2000)
            }
        }, 0)
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                statusText.text = "Status: running (queries=$queryCount, inserts=$insertCount)"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun stop() {
        running = false
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (queries=$queryCount, inserts=$insertCount)"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
    }
}
