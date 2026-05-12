package com.smartinspector.hook.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import kotlin.math.sin

/**
 * Input latency scenario — heavy drawing/computation causing slow touch response.
 * Triggers android.input analysis.
 */
class InputLatencyActivity : AppCompatActivity() {

    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private lateinit var heavyTouchView: HeavyTouchView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "Input Latency"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Heavy computation on touch events causes input delay.\n" +
                    "Touch the area below — each touch triggers expensive work.\n" +
                    "Triggers: android.input"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val startBtn = Button(this).apply {
            text = "Start Continuous Draw"
            setOnClickListener { startContinuousDraw() }
        }
        container.addView(startBtn)

        val stopBtn = Button(this).apply {
            text = "Stop"
            setOnClickListener { stop() }
        }
        container.addView(stopBtn)

        statusText = TextView(this).apply {
            text = "Status: idle. Touch the canvas below."
            textSize = 14f
            setPadding(0, 16, 0, 16)
        }
        container.addView(statusText)

        // Heavy touch-responsive view
        heavyTouchView = HeavyTouchView(this).apply {
            layoutParams = android.widget.LinearLayout.LayoutParams(
                android.widget.LinearLayout.LayoutParams.MATCH_PARENT,
                600
            )
        }
        container.addView(heavyTouchView)

        scrollView.addView(container)
        setContentView(scrollView)
    }

    private fun startContinuousDraw() {
        running = true
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                heavyTouchView.tick++
                heavyTouchView.invalidate()
                handler.postDelayed(this, 16) // 60fps target
            }
        }, 16)
        statusText.text = "Status: continuous draw active. Touch the canvas!"
    }

    private fun stop() {
        running = false
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
    }

    /**
     * Custom view that does heavy work in onTouchEvent and onDraw.
     */
    class HeavyTouchView(context: Context) : View(context) {

        var tick = 0
        private var lastTouchTime = 0L
        private var lastDrawTime = 0L
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        private var touchCount = 0

        override fun onTouchEvent(event: MotionEvent): Boolean {
            if (event.action == MotionEvent.ACTION_DOWN ||
                event.action == MotionEvent.ACTION_MOVE) {

                val start = System.nanoTime()
                touchCount++

                // Heavy computation on every touch event — simulates input processing
                // This blocks input dispatch and causes latency
                var sum = 0.0
                for (i in 0 until 500_000) {
                    sum += Math.sqrt(i.toDouble()) * Math.sin(i.toDouble())
                }

                // Simulate layout request during touch (triggers requestLayout)
                if (touchCount % 5 == 0) {
                    requestLayout()
                }

                lastTouchTime = (System.nanoTime() - start) / 1_000_000
                Log.i("InputLatency", "Touch #$touchCount processed in ${lastTouchTime}ms")
                invalidate()
                return true
            }
            return super.onTouchEvent(event)
        }

        override fun onDraw(canvas: Canvas) {
            super.onDraw(canvas)
            val start = System.nanoTime()

            val w = width.toFloat()
            val h = height.toFloat()

            // Heavy drawing — many paths and shapes
            // Background gradient simulation
            for (y in 0 until h.toInt() step 4) {
                val ratio = y.toFloat() / h
                val r = (50 + ratio * 100).toInt()
                val g = (50 + ratio * 50).toInt()
                val b = (150 + ratio * 105).toInt()
                paint.color = Color.argb(255, r, g, b)
                canvas.drawRect(0f, y.toFloat(), w, y + 4f, paint)
            }

            // Wavy lines
            for (line in 0 until 15) {
                val path = Path()
                path.moveTo(0f, h / 2f)
                for (x in 0 until w.toInt() step 2) {
                    val y = h / 2f +
                            sin((x + line * 30 + tick * 5) * 0.03f) * h * 0.3f
                    path.lineTo(x.toFloat(), y)
                }
                paint.color = Color.argb(80, 255, 255, 255)
                paint.style = Paint.Style.STROKE
                paint.strokeWidth = 2f
                canvas.drawPath(path, paint)
            }

            // Concentric circles
            paint.style = Paint.Style.FILL
            for (i in 0 until 30) {
                val radius = w.coerceAtMost(h) / 2f - i * 6f
                if (radius <= 0) break
                paint.color = Color.argb(
                    (30 + i * 3).coerceAtMost(255),
                    255, 200, 100
                )
                canvas.drawCircle(w / 2f, h / 2f, radius, paint)
            }

            lastDrawTime = (System.nanoTime() - start) / 1_000_000
        }
    }
}
