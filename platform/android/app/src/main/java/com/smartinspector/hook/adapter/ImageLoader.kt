package com.smartinspector.hook.adapter

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader


object ImageLoader {

    private val paintCache = HashMap<Int, Paint>()


    fun decodeBitmap(width: Int, height: Int, seed: Int): Bitmap {
        val bmp = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)

        // P2: Expensive gradient background
        drawGradientBackground(canvas, width, height, seed)

        // P2: Concentric circles with alpha
        drawConcentricCircles(canvas, width, height, seed)

        // P2: Wavy path overlay
        drawWavyPath(canvas, width, height, seed)

        // Text label
        drawLabel(canvas, width, height, seed)

        return bmp
    }

    private fun drawGradientBackground(canvas: Canvas, w: Int, h: Int, seed: Int) {
        val r = (seed * 37) % 256
        val g = (seed * 71) % 256
        val b = (seed * 113) % 256

        val paint = getPaint(seed) {
            shader = LinearGradient(
                0f, 0f, w.toFloat(), h.toFloat(),
                Color.rgb(r, g, b),
                Color.rgb(255 - r, 255 - g, 255 - b),
                Shader.TileMode.CLAMP
            )
        }
        canvas.drawRect(0f, 0f, w.toFloat(), h.toFloat(), paint)
    }

    private fun drawConcentricCircles(canvas: Canvas, w: Int, h: Int, seed: Int) {
        val cx = w / 2f
        val cy = h / 2f
        val maxRadius = Math.min(w, h) / 2f

        for (i in 0 until 15) {
            val radius = maxRadius - i * 6f
            if (radius <= 0) break

            val alpha = 30 + (i * 8) % 60
            val paint = getPaint(seed + i * 1000) {
                color = Color.argb(alpha, 255, 255, 255)
                isAntiAlias = true
            }
            canvas.drawCircle(cx, cy, radius, paint)
        }
    }

    private fun drawWavyPath(canvas: Canvas, w: Int, h: Int, seed: Int) {
        val path = Path()
        path.moveTo(0f, h / 2f)

        val segments = 20
        val dx = w.toFloat() / segments
        for (i in 1..segments) {
            val x = i * dx
            val y = h / 2f + Math.sin((i + seed) * 0.8).toFloat() * h * 0.3f
            path.lineTo(x, y)
        }

        val paint = getPaint(seed + 5000) {
            color = Color.argb(80, 255, 255, 255)
            strokeWidth = 3f
            style = Paint.Style.STROKE
            isAntiAlias = true
        }
        canvas.drawPath(path, paint)
    }

    private fun drawLabel(canvas: Canvas, w: Int, h: Int, seed: Int) {
        val paint = getPaint(seed + 9000) {
            color = Color.WHITE
            textSize = 22f
            isAntiAlias = true
            setShadowLayer(4f, 2f, 2f, Color.BLACK)
        }
        canvas.drawText("#$seed", 16f, h / 2f + 8f, paint)
    }

    private fun getPaint(key: Int, configure: Paint.() -> Unit): Paint {
        val paint = Paint()
        paint.configure()
        paintCache[key] = paint
        return paint
    }
}
