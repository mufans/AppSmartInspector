package com.smartinspector.hook.worker

import android.os.Handler
import android.os.Looper
import android.util.Log


class CpuBurnWorker {

    private val threads = mutableListOf<Thread>()
    private var running = false

    fun start(threadCount: Int = 4) {
        if (running) return
        running = true

        for (t in 0 until threadCount) {
            val thread = Thread({
                var result = 0.0
                while (running && !Thread.currentThread().isInterrupted) {
                    // Tight math loop — pure CPU workload
                    for (i in 0 until 1_000_000) {
                        result += Math.sqrt(i.toDouble()) * Math.sin(i.toDouble())
                    }
                    // Prevent compiler from optimizing away
                    if (result.isNaN()) break
                }
                Log.d("CpuBurnWorker", "Thread $t finished")
            }, "CpuBurnThread-$t")
            thread.priority = Thread.NORM_PRIORITY - 1
            thread.start()
            threads.add(thread)
        }

        Log.d("CpuBurnWorker", "Started $threadCount CPU burn threads")
    }

    /** Start periodic main-thread work that contributes to jank. */
    fun startMainThreadWork(handler: Handler) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                // P1: ~5ms of computation on main thread every 200ms
                var sum = 0.0
                for (i in 0 until 100_000) {
                    sum += Math.sqrt(i.toDouble())
                }
                handler.postDelayed(this, 200)
            }
        }, 200)
    }

    /** Stop all background threads. */
    fun stop() {
        running = false
        threads.forEach { it.interrupt() }
        threads.clear()
    }
}
