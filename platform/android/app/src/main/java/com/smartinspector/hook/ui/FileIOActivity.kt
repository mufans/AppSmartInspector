package com.smartinspector.hook.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile

/**
 * File I/O blocking scenario — main thread and background thread file operations.
 * Triggers __intrinsic_thread_state io_wait analysis.
 */
class FileIOActivity : AppCompatActivity() {

    private var running = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private var ioOpsCount = 0L
    private var mainThreadIOTime = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scrollView = ScrollView(this)
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        val title = TextView(this).apply {
            text = "File I/O Blocking"
            textSize = 24f
            setTextColor(android.graphics.Color.BLACK)
        }
        container.addView(title)

        val desc = TextView(this).apply {
            text = "Main thread file I/O operations blocking the UI.\n" +
                    "Reads/writes large files, random access, and directory scans.\n" +
                    "Triggers: __intrinsic_thread_state (io_wait), file_io dimension"
            textSize = 14f
            setTextColor(android.graphics.Color.GRAY)
            setPadding(0, 24, 0, 24)
        }
        container.addView(desc)

        val mainReadBtn = Button(this).apply {
            text = "Main Thread: Read 10MB File"
            setOnClickListener { mainThreadRead() }
        }
        container.addView(mainReadBtn)

        val mainWriteBtn = Button(this).apply {
            text = "Main Thread: Write 20MB File"
            setOnClickListener { mainThreadWrite() }
        }
        container.addView(mainWriteBtn)

        val mainRandomBtn = Button(this).apply {
            text = "Main Thread: Random Access I/O (3s)"
            setOnClickListener { mainThreadRandomAccess() }
        }
        container.addView(mainRandomBtn)

        val mainScanBtn = Button(this).apply {
            text = "Main Thread: Directory Scan (1000 files)"
            setOnClickListener { mainThreadDirectoryScan() }
        }
        container.addView(mainScanBtn)

        val bgBtn = Button(this).apply {
            text = "Start Background I/O Stress (4 threads)"
            setOnClickListener { startBackgroundIO() }
        }
        container.addView(bgBtn)

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

    private fun createLargeFile(name: String, sizeMB: Int): File {
        val file = File(cacheDir, name)
        if (!file.exists() || file.length() != sizeMB.toLong() * 1024 * 1024) {
            FileOutputStream(file).use { fos ->
                val buffer = ByteArray(1024 * 1024)
                buffer.fill(0xAB.toByte())
                repeat(sizeMB) {
                    fos.write(buffer)
                }
            }
        }
        return file
    }

    /**
     * Main thread blocking read — reads a 10MB file on the UI thread.
     * This will show up as io_wait in __intrinsic_thread_state.
     */
    private fun mainThreadRead() {
        val file = createLargeFile("io_test_10mb.dat", 10)
        val start = System.currentTimeMillis()

        try {
            val data = FileInputStream(file).use { fis ->
                val buf = ByteArray(file.length().toInt())
                fis.read(buf)
                buf
            }
            mainThreadIOTime = System.currentTimeMillis() - start
            ioOpsCount++
            Log.i("FileIO", "Main thread read ${data.size} bytes in ${mainThreadIOTime}ms")
            updateStatus("Main thread read completed in ${mainThreadIOTime}ms")
        } catch (e: Exception) {
            Log.e("FileIO", "Main thread read failed", e)
            updateStatus("Read failed: ${e.message}")
        }
    }

    /**
     * Main thread blocking write — writes 20MB to a file on the UI thread.
     * fsync will cause disk I/O wait.
     */
    private fun mainThreadWrite() {
        val file = File(cacheDir, "io_write_test.dat")
        val start = System.currentTimeMillis()

        try {
            FileOutputStream(file).use { fos ->
                val buffer = ByteArray(1024 * 1024)
                buffer.fill(0xCD.toByte())
                repeat(20) {
                    fos.write(buffer)
                }
                fos.fd.sync() // Force fsync to ensure disk I/O
            }
            mainThreadIOTime = System.currentTimeMillis() - start
            ioOpsCount++
            Log.i("FileIO", "Main thread write + fsync in ${mainThreadIOTime}ms")
            updateStatus("Main thread write completed in ${mainThreadIOTime}ms")
        } catch (e: Exception) {
            Log.e("FileIO", "Main thread write failed", e)
            updateStatus("Write failed: ${e.message}")
        }
    }

    /**
     * Main thread random access I/O — seeks and reads random positions for ~3 seconds.
     * Each individual read is small but causes many syscalls and I/O waits.
     */
    private fun mainThreadRandomAccess() {
        val file = createLargeFile("io_random_test.dat", 20)
        val raf = RandomAccessFile(file, "r")
        val start = System.currentTimeMillis()
        var ops = 0

        try {
            val fileSize = raf.length()
            val buffer = ByteArray(4096)
            while (System.currentTimeMillis() - start < 3000) {
                // Seek to random position and read 4KB
                val pos = (Math.random() * (fileSize - 4096)).toLong()
                raf.seek(pos)
                raf.read(buffer)
                ops++
            }
            mainThreadIOTime = System.currentTimeMillis() - start
            ioOpsCount += ops
            Log.i("FileIO", "Main thread random I/O: $ops ops in ${mainThreadIOTime}ms")
            updateStatus("Random access: $ops ops in ${mainThreadIOTime}ms")
        } catch (e: Exception) {
            Log.e("FileIO", "Random access failed", e)
        } finally {
            raf.close()
        }
    }

    /**
     * Main thread directory scan — creates 1000 small files then lists them.
     * File listing on a large directory causes main thread I/O wait.
     */
    private fun mainThreadDirectoryScan() {
        val dir = File(cacheDir, "io_scan_test")
        dir.mkdirs()

        // Create files if not already present
        if (dir.listFiles()?.size ?: 0 < 1000) {
            for (i in 0 until 1000) {
                val f = File(dir, "file_$i.dat")
                if (!f.exists()) {
                    f.writeText("data $i " + "x".repeat(100))
                }
            }
        }

        val start = System.currentTimeMillis()
        val files = dir.listFiles()
        // Force stat on each file (causes I/O syscalls)
        var totalSize = 0L
        files?.forEach { f ->
            totalSize += f.length()
            f.lastModified()
        }
        mainThreadIOTime = System.currentTimeMillis() - start
        ioOpsCount++
        Log.i("FileIO", "Main thread directory scan: ${files?.size} files in ${mainThreadIOTime}ms")
        updateStatus("Directory scan: ${files?.size} files, ${totalSize / 1024}KB in ${mainThreadIOTime}ms")
    }

    /**
     * Background I/O stress — 4 threads doing continuous file I/O.
     * While these run on background threads, they compete for disk I/O bandwidth
     * which amplifies main thread I/O blocking time.
     */
    private fun startBackgroundIO() {
        if (running) return
        running = true
        ioOpsCount = 0

        for (t in 0 until 4) {
            Thread({
                val threadFile = File(cacheDir, "io_bg_stress_$t.dat")
                var localOps = 0L
                while (running && !Thread.currentThread().isInterrupted) {
                    try {
                        // Write 2MB
                        FileOutputStream(threadFile).use { fos ->
                            val buf = ByteArray(512 * 1024)
                            buf.fill((t + localOps % 256).toByte())
                            repeat(4) { fos.write(buf) }
                            fos.fd.sync()
                        }
                        // Read it back
                        FileInputStream(threadFile).use { fis ->
                            val buf = ByteArray(4096)
                            while (fis.read(buf) != -1) { /* drain */ }
                        }
                        localOps++
                        ioOpsCount++
                    } catch (_: InterruptedException) {
                        return@Thread
                    } catch (e: Exception) {
                        Log.w("FileIO", "BG I/O error thread $t: ${e.message}")
                    }
                }
                Log.i("FileIO", "BG I/O thread $t stopped: $localOps ops")
            }, "FileIOWorker-$t").start()
        }

        updateStatus("Background I/O stress running (4 threads)")
        startStatusUpdates()
    }

    private fun updateStatus(msg: String) {
        handler.post {
            statusText.text = "Status: $msg\n" +
                    "  Total I/O ops: $ioOpsCount\n" +
                    "  Last main-thread I/O: ${mainThreadIOTime}ms"
        }
    }

    private fun startStatusUpdates() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!running) return
                statusText.text = "Status: running (4 I/O threads)\n" +
                        "  Total I/O ops: $ioOpsCount\n" +
                        "  Last main-thread I/O: ${mainThreadIOTime}ms"
                handler.postDelayed(this, 500)
            }
        }, 500)
    }

    private fun stop() {
        running = false
        handler.removeCallbacksAndMessages(null)
        statusText.text = "Status: stopped (total I/O ops: $ioOpsCount)"
    }

    override fun onDestroy() {
        super.onDestroy()
        stop()
        // Clean up test files
        File(cacheDir, "io_scan_test").deleteRecursively()
    }
}
