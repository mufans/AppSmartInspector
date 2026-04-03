package com.smartinspector.hook.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView
import androidx.fragment.app.Fragment
import com.smartinspector.hook.adapter.ImageLoader
import com.smartinspector.hook.repository.DataRepository


class DetailFragment : Fragment() {

    private val repo = DataRepository()
    private val handler = Handler(Looper.getMainLooper())
    private var running = false

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        simulateHeavyInit()

        val root = FrameLayout(requireContext()).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        // Add a text view
        val title = TextView(requireContext()).apply {
            text = "Detail Fragment"
            textSize = 20f
            setTextColor(Color.BLACK)
            setPadding(32, 32, 32, 16)
        }
        root.addView(title)

        // Add image with heavy bitmap
        val imageView = ImageView(requireContext()).apply {
            layoutParams = FrameLayout.LayoutParams(400, 400).apply {
                topMargin = 80
            }
            // P1: Decode bitmap during inflate
            setImageBitmap(ImageLoader.decodeBitmap(400, 400, 42))
        }
        root.addView(imageView)

        // Add custom heavy draw view
        val heavyView = HeavyDrawView(requireContext()).apply {
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                200
            ).apply {
                topMargin = 500
            }
        }
        root.addView(heavyView)

        return root
    }

    override fun onResume() {
        super.onResume()
        running = true

        val items = repo.loadItemsJson(20)
        // Items loaded but not used — simulates wasteful eager loading

        startPeriodicUpdate()
    }

    override fun onPause() {
        super.onPause()
        running = false
        handler.removeCallbacksAndMessages(null)
    }

    override fun onDestroyView() {
        super.onDestroyView()
        handler.removeCallbacksAndMessages(null)
    }

    private fun simulateHeavyInit() {
        try {
            Thread.sleep(30)
        } catch (_: InterruptedException) {}

        var sum = 0.0
        for (i in 0 until 50_000) {
            sum += Math.sqrt(i.toDouble()) * Math.sin(i.toDouble())
        }
    }

    private fun startPeriodicUpdate() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                view?.requestLayout()
                handler.postDelayed(this, 300)
            }
        }, 300)
    }
}
