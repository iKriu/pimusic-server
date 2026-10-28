package io.github.ikriu.pimusic.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.nestedscroll.NestedScrollConnection
import androidx.compose.ui.input.nestedscroll.NestedScrollSource
import androidx.compose.ui.unit.Velocity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/**
 * Полноэкранный «Сейчас играет» как лист, который тянется пальцем: смещение 0 — открыт, [height] — закрыт.
 * Свайп вверх по мини-плееру выдвигает лист, свайп вниз по листу (шапка или прокрутка, упёршаяся в верх) — сворачивает.
 * Отпускание — пружина M3E (spatial) к ближайшему состоянию с учётом скорости.
 */
class PlayerSheet(private val st: AppState, private val scope: CoroutineScope) {
    val offset = Animatable(Float.MAX_VALUE)
    var height = 1f
        private set

    private val springSpec = spring<Float>(dampingRatio = 0.85f, stiffness = Spring.StiffnessMediumLow)

    /** Высота области (px); пока лист закрыт, смещение следует за ней. */
    fun layout(h: Float) {
        height = h.coerceAtLeast(1f)
        if (!st.playerOpen && !dragging) scope.launch { offset.snapTo(height) }
    }

    var dragging = false
        private set

    val visible get() = st.playerOpen || offset.value < height - 0.5f

    fun drag(dy: Float) {
        dragging = true
        scope.launch { offset.snapTo((offset.value + dy).coerceIn(0f, height)) }
    }

    /** Отпустили: открыть или закрыть по положению и скорости (px/с, вниз — положительная). */
    fun settle(velocity: Float) {
        dragging = false
        val open = when {
            velocity < -1200f -> true
            velocity > 1200f -> false
            else -> offset.value < height * 0.5f
        }
        animate(open, velocity)
    }

    fun animate(open: Boolean, velocity: Float = 0f) {
        if (open && !st.playerOpen) st.openPlayer()
        if (!open && st.playerOpen) st.playerOpen = false
        scope.launch { offset.animateTo(if (open) 0f else height, springSpec, initialVelocity = velocity) }
    }

    /** Вложенная прокрутка листа: прокрутка упёрлась в верх и тянут вниз — тянется лист; вверх — сначала лист. */
    val nested = object : NestedScrollConnection {
        override fun onPreScroll(available: Offset, source: NestedScrollSource): Offset {
            if (available.y < 0 && offset.value > 0f && source == NestedScrollSource.UserInput) {
                drag(available.y); return Offset(0f, available.y)
            }
            return Offset.Zero
        }
        override fun onPostScroll(consumed: Offset, available: Offset, source: NestedScrollSource): Offset {
            if (available.y > 0 && source == NestedScrollSource.UserInput) { drag(available.y); return Offset(0f, available.y) }
            return Offset.Zero
        }
        override suspend fun onPreFling(available: Velocity): Velocity {
            if (dragging || offset.value > 0f) { settle(available.y); return available }
            return Velocity.Zero
        }
    }
}
