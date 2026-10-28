package io.github.ikriu.pimusic

import android.os.Looper
import androidx.media3.common.Player
import androidx.media3.common.Timeline
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.shots.FakeServer
import io.github.ikriu.pimusic.shots.ShotApp
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Массовые изменения очереди — не больше двух изменений таймлайна: каждое уходит всем контроллерам MediaSession
 * (поштучные операции по тысячам треков роняли оболочку OneUI на S24 Ultra).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = ShotApp::class)
class QueueBatchTest {
    private fun idle() = shadowOf(Looper.getMainLooper()).idle()

    @Test fun dropAndRefreshChangeTimelineAtMostTwice() {
        val app = PiApp.get() as ShotApp
        val hub = app.player
        try {
            val list = (0 until 2000).map { PiJson.decodeFromString(Track.serializer(), FakeServer.track(it)) }
            hub.setQueuePaused(list, 10)
            idle()
            var changes = 0
            hub.player.addListener(object : Player.Listener {
                override fun onTimelineChanged(timeline: Timeline, reason: Int) {
                    if (reason == Player.TIMELINE_CHANGE_REASON_PLAYLIST_CHANGED) changes++
                }
            })
            hub.dropTracks((100L..600L).toSet()) // 501 трек недоступен совсем
            idle()
            assertTrue("dropTracks: $changes изменений", changes <= 2)
            assertEquals(2000 - 501, hub.player.mediaItemCount)
            assertEquals(11L, hub.ui.value.current?.id) // текущий трек на месте

            changes = 0
            runBlocking { hub.refreshLinks() } // id, кратные 7, сервер больше не отдаёт
            idle()
            assertTrue("refreshLinks: $changes изменений", changes <= 2)
            val left = (1L..2000L).filter { it !in 100L..600L && it % 7 != 0L }.size
            assertEquals(left, hub.player.mediaItemCount)
        } finally {
            app.shutdown()
        }
    }
}
