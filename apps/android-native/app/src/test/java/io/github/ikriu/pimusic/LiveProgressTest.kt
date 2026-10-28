package io.github.ikriu.pimusic

import android.app.Application
import android.app.Notification
import androidx.core.app.NotificationCompat
import org.robolectric.RuntimeEnvironment
import io.github.ikriu.pimusic.player.LiveProgress
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Уведомления прогресса в стиле Live Updates: ongoing, заголовок, прогресс, короткий текст, просьба о повышении. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = Application::class)
class LiveProgressTest {
    private val ctx: Application = RuntimeEnvironment.getApplication()

    @Test fun promotedHasRequestShortTextAndOngoing() {
        LiveProgress.ensureChannel(ctx, LiveProgress.CH_DOWNLOADS, "Скачивание")
        val n = LiveProgress.build(ctx, LiveProgress.CH_DOWNLOADS, "Скачивание на устройство", "Группа крови · осталось 9", 45, 100, "3/12", promote = true)
        assertTrue(n.flags and Notification.FLAG_ONGOING_EVENT != 0)
        assertTrue(NotificationCompat.isRequestPromotedOngoing(n))
        assertEquals("3/12", NotificationCompat.getShortCriticalText(n))
        assertEquals("Скачивание на устройство", n.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertFalse(n.extras.getBoolean(Notification.EXTRA_COLORIZED))
    }

    @Test fun fallbackIsPlainProgress() {
        val n = LiveProgress.build(ctx, LiveProgress.CH_UPDATES, "Обновление PiMusic 2.0.2", "Загрузка — 45 %", 45, 100, "45%", promote = false)
        assertTrue(n.flags and Notification.FLAG_ONGOING_EVENT != 0)
        assertFalse(NotificationCompat.isRequestPromotedOngoing(n))
        assertEquals(45, n.extras.getInt(Notification.EXTRA_PROGRESS))
        assertEquals(100, n.extras.getInt(Notification.EXTRA_PROGRESS_MAX))
        assertFalse(n.extras.getBoolean(Notification.EXTRA_PROGRESS_INDETERMINATE))
    }

    @Test fun androidBelow16NeverPromotes() {
        assertFalse(LiveProgress.canPromote(ctx)) // SDK 34
    }
}
