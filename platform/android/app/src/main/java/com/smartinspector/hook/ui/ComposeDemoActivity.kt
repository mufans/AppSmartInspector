package com.smartinspector.hook.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.sin

/**
 * Compose demo page for testing TraceHook's Compose recomposition tracking.
 *
 * Contains intentional performance anti-patterns:
 * - Unnecessary recomposition via unstable lambdas
 * - Heavy Canvas drawing in composable
 * - LazyColumn with expensive item composables
 * - State-driven animations triggering recomposition
 * - Eager computation in composable body
 */
class ComposeDemoActivity : ComponentActivity() {

    private val handler = Handler(Looper.getMainLooper())
    private var destroyed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    ComposeDemoScreen()
                }
            }
        }

        // Periodic state change to trigger recomposition
        startPeriodicRecomposition()
    }

    override fun onDestroy() {
        super.onDestroy()
        destroyed = true
        handler.removeCallbacksAndMessages(null)
    }

    private fun startPeriodicRecomposition() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (destroyed) return
                // Trigger a global recomposition via tick counter
                recompositionTick++
                handler.postDelayed(this, 200)
            }
        }, 200)
    }

    companion object {
        @Volatile
        var recompositionTick: Int = 0
    }
}

// ═══════════════════════════════════════════════════════════
// Main screen composable
// ═══════════════════════════════════════════════════════════

@Composable
fun ComposeDemoScreen() {
    var counter by remember { mutableIntStateOf(0) }
    var showHeavyList by remember { mutableStateOf(false) }
    var animationProgress by remember { mutableStateOf(0f) }

    // Read tick to force recomposition from external source
    val tick = ComposeDemoActivity.recompositionTick

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        // Header
        Text(
            text = "Compose Demo (tick=$tick)",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text(
            text = "Testing ComposeHook recomposition tracking",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Spacer(modifier = Modifier.height(16.dp))

        // Counter section — triggers recomposition on each click
        CounterSection(counter = counter, onIncrement = { counter++ })

        Spacer(modifier = Modifier.height(16.dp))

        // Animation section — continuous recomposition
        AnimatedSection(onProgressChange = { animationProgress = it })

        Spacer(modifier = Modifier.height(16.dp))

        // Heavy Canvas drawing
        HeavyCanvasSection(progress = animationProgress)

        Spacer(modifier = Modifier.height(16.dp))

        // Toggle to show expensive LazyColumn
        Button(onClick = { showHeavyList = !showHeavyList }) {
            Text(if (showHeavyList) "Hide Heavy List" else "Show Heavy List")
        }

        if (showHeavyList) {
            Spacer(modifier = Modifier.height(8.dp))
            HeavyLazyColumn(itemCount = 50)
        }

        Spacer(modifier = Modifier.height(16.dp))

        // Unstable lambda pattern — causes unnecessary recomposition
        UnstableLambdaSection(tick = tick)
    }
}

// ═══════════════════════════════════════════════════════════
// Counter — simple state-driven recomposition
// ═══════════════════════════════════════════════════════════

@Composable
fun CounterSection(counter: Int, onIncrement: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text("Counter: $counter", style = MaterialTheme.typography.titleLarge)
            Spacer(modifier = Modifier.height(8.dp))
            Button(onClick = onIncrement) {
                Text("Increment")
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════
// Animated section — continuous recomposition via animateFloatAsState
// ═══════════════════════════════════════════════════════════

@Composable
fun AnimatedSection(onProgressChange: (Float) -> Unit) {
    var target by remember { mutableStateOf(0f) }
    val progress by animateFloatAsState(
        targetValue = target,
        animationSpec = tween(durationMillis = 1000, easing = LinearEasing),
        label = "progress"
    )

    onProgressChange(progress)

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text("Animation", style = MaterialTheme.typography.titleMedium)
            Spacer(modifier = Modifier.height(8.dp))

            // Progress bar
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(8.dp)
                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(4.dp))
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth(progress)
                        .height(8.dp)
                        .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(4.dp))
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            Button(onClick = {
                target = if (target >= 1f) 0f else 1f
            }) {
                Text(if (target >= 1f) "Reset Animation" else "Start Animation")
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════
// Heavy Canvas — expensive drawing in Compose
// ═══════════════════════════════════════════════════════════

@Composable
fun HeavyCanvasSection(progress: Float) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.tertiaryContainer
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text("Heavy Canvas Drawing", style = MaterialTheme.typography.titleMedium)
            Spacer(modifier = Modifier.height(8.dp))

            Canvas(modifier = Modifier.fillMaxWidth().height(200.dp)) {
                val w = size.width
                val h = size.height

                // Concentric circles
                for (i in 0 until 20) {
                    val radius = (w.coerceAtMost(h) / 2f) - i * 8f
                    if (radius <= 0) break
                    drawCircle(
                        color = Color.White.copy(alpha = 0.3f + (i * 0.02f)),
                        radius = radius,
                        center = center
                    )
                }

                // Wavy lines
                for (line in 0 until 5) {
                    val path = Path()
                    path.moveTo(0f, h / 2f)
                    for (x in 0 until w.toInt() step 4) {
                        val y = h / 2f +
                                sin((x + line * 50 + progress * 360) * 0.02f) * h * 0.3f
                        path.lineTo(x.toFloat(), y)
                    }
                    drawPath(
                        path = path,
                        color = Color.White.copy(alpha = 0.5f),
                        style = Stroke(width = 2f)
                    )
                }

                // Gradient overlay
                drawRect(
                    brush = Brush.linearGradient(
                        colors = listOf(
                            Color(0xFF6200EE).copy(alpha = 0.3f),
                            Color(0xFF03DAC5).copy(alpha = 0.3f)
                        ),
                        start = Offset.Zero,
                        end = Offset(w, h)
                    )
                )
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════
// Heavy LazyColumn — expensive item composables
// ═══════════════════════════════════════════════════════════

@Composable
fun HeavyLazyColumn(itemCount: Int) {
    val items = remember(itemCount) {
        List(itemCount) { index ->
            "Item #$index" to "Category ${(index % 5)} — payload data for stress testing"
        }
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxWidth()
            .height(400.dp)
    ) {
        items(items, key = { it.first }) { (title, subtitle) ->
            HeavyListItem(title = title, subtitle = subtitle)
        }
    }
}

@Composable
fun HeavyListItem(title: String, subtitle: String) {
    // Simulate expensive computation during composition
    val computedValue = remember(title) {
        var sum = 0.0
        for (i in 0 until 10_000) {
            sum += sin(i.toDouble()) * Math.sqrt(i.toDouble())
        }
        sum
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Row(
            modifier = Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Colored box — simulates image placeholder
            Box(
                modifier = Modifier
                    .size(48.dp)
                    .background(
                        Color(
                            red = (title.hashCode() % 128 + 128),
                            green = (title.hashCode() * 37 % 128 + 128),
                            blue = (title.hashCode() * 71 % 128 + 128),
                            alpha = 255
                        ),
                        RoundedCornerShape(8.dp)
                    )
            )

            Spacer(modifier = Modifier.width(12.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(title, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Text(
                    subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Text(
                    "computed=$computedValue",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline
                )
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════
// Unstable lambda — causes unnecessary recomposition of children
// ═══════════════════════════════════════════════════════════

@Composable
fun UnstableLambdaSection(tick: Int) {
    // This lambda is recreated on every recomposition (unstable),
    // causing all children that receive it to recompose unnecessarily.
    val onClick: () -> Unit = {
        // no-op — but the lambda reference changes every time
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.errorContainer
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text("Unstable Lambda Section", style = MaterialTheme.typography.titleMedium)
            Text(
                "tick=$tick — each tick causes full recomposition",
                style = MaterialTheme.typography.bodySmall
            )
            Spacer(modifier = Modifier.height(8.dp))

            // 10 items each receiving unstable lambda — all recompose every tick
            for (i in 0 until 10) {
                UnstableItem(index = i, onClick = onClick)
            }
        }
    }
}

@Composable
fun UnstableItem(index: Int, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text("Unstable Item #$index")
        Text(
            "→ recomposes",
            color = MaterialTheme.colorScheme.error,
            style = MaterialTheme.typography.labelSmall
        )
    }
}
