package io.github.ikriu.pimusic.player

import android.app.PendingIntent
import android.content.Intent
import androidx.media3.common.Player
import androidx.media3.session.DefaultMediaNotificationProvider
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import io.github.ikriu.pimusic.MainActivity
import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.R

/**
 * Фоновое воспроизведение: MediaSession над ExoPlayer из PlayerHub. Media3 сама ведёт уведомление (обложка, ⏮ ⏯ ⏭,
 * перемотка), foreground service на время игры, кнопки гарнитуры/Bluetooth, экран блокировки, аудиофокус
 * (ExoPlayer: handleAudioFocus) и «шум» при отключении наушников. Смахивание из «Недавних» музыку не останавливает
 * (если играет).
 */
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val hub = PiApp.get().player
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP).putExtra("open_player", true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        setMediaNotificationProvider(
            DefaultMediaNotificationProvider.Builder(this).setChannelName(R.string.channel_playback).build().apply {
                setSmallIcon(R.drawable.ic_stat_pimusic)
            }
        )
        // обложки для шторки/экрана блокировки/Now Bar — не больше 512 px (огромные битмапы в SystemUI не шлём)
        val bitmaps = androidx.media3.session.CacheBitmapLoader(
            androidx.media3.datasource.DataSourceBitmapLoader(
                com.google.common.util.concurrent.MoreExecutors.listeningDecorator(java.util.concurrent.Executors.newSingleThreadExecutor()),
                PiApp.get().caches.http, null, 512,
            )
        )
        session = MediaSession.Builder(this, hub.player).setSessionActivity(open).setCallback(callback).setBitmapLoader(bitmaps).build()
    }

    /**
     * Системной сессии (шторка, экран блокировки, OneUI Now Bar, Bluetooth) очередь не отдаём: без COMMAND_GET_TIMELINE
     * у контроллера уведомления Media3 не вызывает setQueue платформенной сессии. Очередь в тысячи треков при каждом
     * изменении уходила в system_server и SystemUI — на S24 Ultra (OneUI 9) это вешало всю оболочку.
     * Текущий трек, обложка, перемотка и ⏮ ⏭ работают как прежде.
     */
    private val callback = object : MediaSession.Callback {
        override fun onConnect(session: MediaSession, controller: MediaSession.ControllerInfo): MediaSession.ConnectionResult {
            if (!session.isMediaNotificationController(controller)) return super.onConnect(session, controller)
            return MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                .setAvailablePlayerCommands(
                    MediaSession.ConnectionResult.DEFAULT_PLAYER_COMMANDS.buildUpon().remove(Player.COMMAND_GET_TIMELINE).build()
                )
                .build()
        }
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        val p = session?.player
        if (p == null || !p.playWhenReady || p.mediaItemCount == 0 || p.playbackState == Player.STATE_ENDED) {
            PiApp.get().player.savePosition()
            stopSelf()
        }
    }

    override fun onDestroy() {
        // плеер живёт в PlayerHub (приложение) — здесь освобождаем только сессию
        session?.release()
        session = null
        super.onDestroy()
    }
}
