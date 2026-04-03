package com.smartinspector.hook.ui

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View


class HeavyDrawView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private var frameCount = 0

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        val bgPaint = Paint().apply {
            shader = LinearGradient(
                0f, 0f, width.toFloat(), height.toFloat(),
                Color.parseColor("#1a1a2e"),
                Color.parseColor("#16213e"),
                Shader.TileMode.CLAMP
            )
        }
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), bgPaint)

        drawDecorativePaths(canvas)

        drawActivityIndicator(canvas)

        frameCount++
        if (frameCount % 3 == 0) {
            invalidate()
        }
    }

    private fun drawDecorativePaths(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()

        for (line in 0 until 5) {
            val paint = Paint().apply {
                color = Color.argb(60, 100 + line * 30, 150, 255)
                strokeWidth = 2f + line
                style = Paint.Style.STROKE
                isAntiAlias = true
            }

            val path = Path()
            val baseY = h * (line + 1) / 6f
            path.moveTo(0f, baseY)

            for (x in 0..width step 4) {
                val y = baseY + Math.sin((x + frameCount * 2 + line * 50) * 0.02) * 15f
                path.lineTo(x.toFloat(), y.toFloat())
            }
            canvas.drawPath(path, paint)
        }
    }

    private fun drawActivityIndicator(canvas: Canvas) {
        val cx = width / 2f
        val cy = height / 2f

        val radius = 8f + Math.sin(frameCount * 0.1) * 4f
        val paint = Paint().apply {
            color = Color.argb(180, 0, 200, 255)
            isAntiAlias = true
            setShadowLayer(8f, 0f, 0f, Color.argb(100, 0, 150, 255))
        }
        canvas.drawCircle(cx, cy, radius.toFloat(), paint)
    }
}
