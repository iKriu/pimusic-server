package io.github.ikriu.pimusic.player

import android.content.Context
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** 30-секундное превью из поиска (отдельный лёгкий плеер; основная очередь при этом встаёт на паузу). */
class PreviewPlayer(private val ctx: Context, private val hub: PlayerHub) {
    private var p: ExoPlayer? = null
    private val _playing = MutableStateFlow<String?>(null)
    /** Адрес превью, которое играет сейчас. */
    val playing: StateFlow<String?> = _playing.asStateFlow()

    fun toggle(url: String) {
        if (_playing.value == url) { stop(); return }
        val pl = p ?: ExoPlayer.Builder(ctx).build().also { e ->
            p = e
            e.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) { if (state == Player.STATE_ENDED) stop() }
                override fun onPlayerError(error: androidx.media3.common.PlaybackException) { stop() }
            })
        }
        hub.player.pause()
        pl.setMediaItem(MediaItem.fromUri(url))
        pl.prepare()
        pl.play()
        _playing.value = url
    }

    fun stop() {
        p?.stop()
        _playing.value = null
    }

    fun release() { p?.release(); p = null; _playing.value = null }
}
