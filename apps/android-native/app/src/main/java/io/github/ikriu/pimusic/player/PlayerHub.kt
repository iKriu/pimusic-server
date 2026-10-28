package io.github.ikriu.pimusic.player

import android.content.Context
import android.os.Bundle
import android.util.Log
import androidx.core.net.toUri
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.Renderer
import androidx.media3.exoplayer.RenderersFactory
import androidx.media3.exoplayer.audio.DefaultAudioSink
import androidx.media3.exoplayer.audio.MediaCodecAudioRenderer
import androidx.media3.exoplayer.mediacodec.MediaCodecSelector
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import io.github.ikriu.pimusic.core.Crossfade
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqState
import io.github.ikriu.pimusic.core.QueueModel
import io.github.ikriu.pimusic.core.RepeatMode
import io.github.ikriu.pimusic.core.ServerUrl
import io.github.ikriu.pimusic.data.Api
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Session
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.data.TrackList
import io.github.ikriu.pimusic.data.WaveNow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import java.io.File
import java.util.concurrent.atomic.AtomicLong

/** Элемент очереди: uid уникален (один трек может стоять дважды). */
data class QItem(val uid: String, val track: Track)

enum class Mode { QUEUE, WAVE }

/**
 * Откуда играет очередь («Играет из …» над обложкой): kind — library | playlist | album | artist | search | wave |
 * offline | upload; id — плейлист; a/b — ключи папок исполнителя/альбома (переход по тапу).
 */
@Serializable
data class PlaySource(val kind: String, val title: String, val id: Long? = null, val a: String? = null, val b: String? = null) {
    companion object {
        val LIBRARY = PlaySource("library", "Медиатека")
        val OFFLINE = PlaySource("offline", "Скачанное")
        val UPLOADS = PlaySource("upload", "Свои файлы")
        val WAVE = PlaySource("wave", "Моя Волна")
    }
}

/** Таймер сна: до времени (мс) или до конца трека. */
data class Sleep(val untilMs: Long? = null, val endOfTrack: Boolean = false)

data class PlayerUi(
    val mode: Mode = Mode.QUEUE,
    val queue: List<QItem> = emptyList(),
    val index: Int = -1,
    val playing: Boolean = false,
    val buffering: Boolean = false,
    val shuffle: Boolean = false,
    val repeat: RepeatMode = RepeatMode.OFF,
    val speed: Float = 1f,
    val source: PlaySource? = null,
) {
    val current: Track? get() = queue.getOrNull(index)?.track
    val currentUid: String? get() = queue.getOrNull(index)?.uid
}

/**
 * Плеер приложения: один ExoPlayer (главный поток) для очереди и «Моей Волны», очередь, shuffle/repeat, нормализация,
 * EQ, скорость, таймер сна, предзагрузка, восстановление очереди на паузе. PlaybackService публикует его в MediaSession.
 */
class PlayerHub(
    private val ctx: Context,
    private val session: Session,
    private val api: Api,
    private val caches: MediaCaches,
    private val prefetcher: Prefetcher,
    private val scope: CoroutineScope,
) {
    val eq = EqAudioProcessor()
    val player: ExoPlayer
    private val uidSeq = AtomicLong(System.currentTimeMillis())
    private val tracks = HashMap<String, Track>()
    private var original: List<String>? = null // порядок до перемешивания (uid)
    private var linksAt = 0L // когда получены ссылки очереди
    private var retried = HashSet<String>()
    private var normalize = true
    private var sleepJob: Job? = null

    // плавный переход: второй ExoPlayer доигрывает хвост уходящего трека (без аудиофокуса и MediaSession),
    // главный (его видит MediaSession) сразу переходит к следующему и нарастает
    private val faderEq = EqAudioProcessor()
    private var fader: ExoPlayer? = null
    private var crossfadeSec = 0.0
    private var fadeJob: Job? = null
    private var sleepVol = 1f
    private var fadeInVol = 1f
    private var fadeOutVol = 0f
    private var waveJob: Job? = null
    private val _waveNow = MutableStateFlow<WaveNow?>(null)
    /** «Сейчас в эфире» Волны (опрос, пока играет Волна) — для уведомления и мини-плеера. */
    val waveNow: StateFlow<WaveNow?> = _waveNow.asStateFlow()

    private val _ui = MutableStateFlow(PlayerUi())
    val ui: StateFlow<PlayerUi> = _ui.asStateFlow()
    private val _sleep = MutableStateFlow<Sleep?>(null)
    val sleep: StateFlow<Sleep?> = _sleep.asStateFlow()
    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 8)
    /** Короткие сообщения для снекбара. */
    val messages: SharedFlow<String> = _messages

    private fun build(proc: EqAudioProcessor, main: Boolean): ExoPlayer {
        val sink = DefaultAudioSink.Builder(ctx).setAudioProcessors(arrayOf(proc)).build()
        val renderers = RenderersFactory { handler, _, audioListener, _, _ ->
            arrayOf<Renderer>(MediaCodecAudioRenderer(ctx, MediaCodecSelector.DEFAULT, handler, audioListener, sink))
        }
        val load = DefaultLoadControl.Builder()
            // главный: буфер до 10 минут вперёд (трек целиком в памяти, перемотка и смена сети без пауз)
            .setBufferDurationsMs(if (main) 60_000 else 5_000, if (main) 600_000 else 30_000, 500, 1_000)
            .setPrioritizeTimeOverSizeThresholds(true)
            .build()
        return ExoPlayer.Builder(ctx, renderers)
            .setMediaSourceFactory(DefaultMediaSourceFactory(caches.playbackFactory))
            .setLoadControl(load)
            // аудиофокус и «шум» ведёт только главный плеер; хвост перехода следует за ним
            .setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), main)
            .setHandleAudioBecomingNoisy(main)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
    }

    init {
        player = build(eq, main = true)
        player.addListener(object : Player.Listener {
            override fun onEvents(p: Player, events: Player.Events) = publish()
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
                applyGain()
                schedulePrefetch()
                saveSoon()
            }
            override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
                // таймер «до конца трека»: плеер сам встал на паузу в конце трека
                if (reason == Player.PLAY_WHEN_READY_CHANGE_REASON_END_OF_MEDIA_ITEM && _sleep.value?.endOfTrack == true) cancelSleep()
            }
            override fun onPlayerError(error: PlaybackException) = onError(error)
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (!isPlaying) { saveSoon(); if (!player.playWhenReady) stopFade() }
            }
            override fun onPositionDiscontinuity(oldPosition: Player.PositionInfo, newPosition: Player.PositionInfo, reason: Int) {
                // перемотка во время перехода — хвост больше не нужен
                if (reason == Player.DISCONTINUITY_REASON_SEEK && fadeJob?.isActive == true && newPosition.mediaItemIndex == oldPosition.mediaItemIndex) stopFade()
            }
        })
        scope.launch { restore() }
        scope.launch {
            while (true) {
                delay(150)
                checkCrossfade()
            }
        }
    }

    // ---------------------------------------------------------------- плавный переход

    fun setCrossfade(sec: Double) { crossfadeSec = sec.coerceIn(0.0, 12.0); if (crossfadeSec == 0.0) stopFade() }

    private fun applyVolumes() {
        player.volume = sleepVol * fadeInVol
        fader?.volume = sleepVol * fadeOutVol
    }

    private fun checkCrossfade() {
        if (fadeJob?.isActive == true || _ui.value.mode != Mode.QUEUE || !player.isPlaying) return
        val dur = player.duration.takeIf { it != C.TIME_UNSET } ?: return
        if (!Crossfade.shouldStart(crossfadeSec, dur - player.currentPosition, dur, player.playbackParameters.speed,
                player.repeatMode == Player.REPEAT_MODE_ONE, player.hasNextMediaItem(), _sleep.value?.endOfTrack == true)) return
        startFade()
    }

    private fun startFade() {
        val cur = player.currentMediaItem ?: return
        val f = fader ?: build(faderEq, main = false).also { fader = it }
        val t = tracks[cur.mediaId]
        faderEq.setTrackGain(Eq.normalizationGain(t?.gain, t?.peak, normalize))
        f.playbackParameters = player.playbackParameters
        f.setMediaItem(cur, player.currentPosition)
        f.prepare()
        fadeOutVol = 1f
        fadeInVol = 0f
        applyVolumes()
        f.play()
        player.seekToNextMediaItem() // MediaSession сразу видит следующий трек
        val ms = (crossfadeSec * 1000).toLong().coerceAtLeast(1)
        fadeJob = scope.launch {
            val start = System.currentTimeMillis()
            while (true) {
                val x = (System.currentTimeMillis() - start).toDouble() / ms
                fadeInVol = Crossfade.inGain(x).toFloat()
                fadeOutVol = Crossfade.outGain(x).toFloat()
                applyVolumes()
                if (x >= 1) break
                delay(40)
            }
            finishFade()
        }
    }

    private fun finishFade() {
        fader?.stop()
        fader?.clearMediaItems()
        fadeInVol = 1f
        fadeOutVol = 0f
        applyVolumes()
    }

    private fun stopFade() {
        fadeJob?.cancel()
        fadeJob = null
        finishFade()
    }

    /** Эквалайзер — на оба плеера (главный и хвост перехода). */
    fun applyEq(state: EqState, bypass: Boolean) {
        eq.setEq(state); eq.setBypass(bypass)
        faderEq.setEq(state); faderEq.setBypass(bypass)
    }

    // ---------------------------------------------------------------- MediaItem

    private fun newUid() = "q" + uidSeq.incrementAndGet().toString(36)

    private fun item(uid: String, t: Track): MediaItem {
        val base = session.server
        val extras = Bundle().apply { putLong("track_id", t.id) }
        val md = MediaMetadata.Builder()
            .setTitle(t.title)
            .setArtist(t.artist)
            .setAlbumTitle(t.album.ifEmpty { null })
            .setSubtitle(_ui.value.source?.takeIf { it.kind != "library" }?.let { "Играет из «${it.title}»" })
            .setArtworkUri(ServerUrl.absolute(base, t.cover)?.toUri())
            .setDurationMs(if (t.seconds > 0) t.seconds * 1000 else null)
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .setMediaType(MediaMetadata.MEDIA_TYPE_MUSIC)
            .setExtras(extras)
            .build()
        return MediaItem.Builder()
            .setMediaId(uid)
            .setUri(ServerUrl.absolute(base, t.stream) ?: "https://invalid.local/")
            .setCustomCacheKey(MediaCaches.key(t.id))
            .setMediaMetadata(md)
            .build()
    }

    private fun queueNow(): List<QItem> = (0 until player.mediaItemCount).mapNotNull { i ->
        val uid = player.getMediaItemAt(i).mediaId
        tracks[uid]?.let { QItem(uid, it) }
    }

    private fun publish() {
        val mode = _ui.value.mode
        _ui.value = _ui.value.copy(
            queue = if (mode == Mode.QUEUE) queueNow() else emptyList(),
            index = if (mode == Mode.QUEUE) player.currentMediaItemIndex else -1,
            playing = player.isPlaying || (player.playWhenReady && player.playbackState == Player.STATE_BUFFERING),
            buffering = player.playbackState == Player.STATE_BUFFERING,
            repeat = when (player.repeatMode) { Player.REPEAT_MODE_ALL -> RepeatMode.ALL; Player.REPEAT_MODE_ONE -> RepeatMode.ONE; else -> RepeatMode.OFF },
            speed = player.playbackParameters.speed,
        )
    }

    // ---------------------------------------------------------------- воспроизведение очереди

    /** Играть список с позиции [start]; shuffle — перемешать (стартовый — первым). */
    fun playList(list: List<Track>, start: Int = 0, shuffle: Boolean = false, source: PlaySource? = null) {
        val playable = list.filter { it.stream != null }
        if (playable.isEmpty()) return
        val startTrack = list.getOrNull(start)
        var st = playable.indexOfFirst { it === startTrack }.coerceAtLeast(0)
        var entries = playable.map { QItem(newUid(), it) }
        tracks.clear()
        entries.forEach { tracks[it.uid] = it.track }
        original = null
        if (shuffle) {
            original = entries.map { it.uid }
            // «Перемешать»: первым — случайный трек (тап по треку всегда без перемешивания)
            entries = QueueModel.shuffled(entries, entries.indices.random())
            st = 0
        }
        linksAt = System.currentTimeMillis()
        retried.clear()
        stopFade()
        stopWavePoll()
        _ui.value = _ui.value.copy(mode = Mode.QUEUE, shuffle = shuffle, source = source)
        player.setMediaItems(entries.map { item(it.uid, it.track) }, st, 0)
        player.prepare()
        player.play()
        applyGain()
        schedulePrefetch()
        publish()
        saveSoon()
    }

    fun toggle() {
        if (player.mediaItemCount == 0) return
        if (player.isPlaying) player.pause() else {
            if (player.playbackState == Player.STATE_IDLE) player.prepare()
            if (player.playbackState == Player.STATE_ENDED) player.seekTo(0, 0)
            if (_ui.value.mode == Mode.QUEUE && System.currentTimeMillis() - linksAt > 6 * 3600_000L) scope.launch { refreshLinks(); player.play() }
            else player.play()
        }
    }

    fun next() { if (player.hasNextMediaItem()) player.seekToNextMediaItem() else if (_ui.value.repeat == RepeatMode.ALL) player.seekTo(0, 0) }
    fun prev() {
        if (QueueModel.prevGoesToStart(player.currentPosition) || !player.hasPreviousMediaItem()) player.seekTo(0) else player.seekToPreviousMediaItem()
    }
    fun seek(ms: Long) = player.seekTo(ms.coerceAtLeast(0))
    /** Перейти к треку очереди, не меняя play/pause (карусель обложек). */
    fun seekToItem(index: Int) { if (index in 0 until player.mediaItemCount && index != player.currentMediaItemIndex) player.seekTo(index, 0) }
    fun jumpTo(index: Int) { if (index in 0 until player.mediaItemCount) { player.seekTo(index, 0); player.play() } }

    fun cycleRepeat() {
        player.repeatMode = when (_ui.value.repeat.next()) { RepeatMode.ALL -> Player.REPEAT_MODE_ALL; RepeatMode.ONE -> Player.REPEAT_MODE_ONE; else -> Player.REPEAT_MODE_OFF }
        publish(); schedulePrefetch()
    }

    /** Перемешать/вернуть порядок: текущий трек играет дальше без перезапуска. */
    fun toggleShuffle() {
        val n = player.mediaItemCount
        if (n < 2 || _ui.value.mode != Mode.QUEUE) { _ui.value = _ui.value.copy(shuffle = !_ui.value.shuffle); return }
        val cur = player.currentMediaItemIndex
        val now = queueNow().map { it.uid }
        val target = if (!_ui.value.shuffle) {
            original = now
            QueueModel.shuffled(now, cur)
        } else {
            QueueModel.unshuffled(original ?: now, now).also { original = null }
        }
        reorderAroundCurrent(target)
        _ui.value = _ui.value.copy(shuffle = !_ui.value.shuffle)
        publish(); schedulePrefetch(); saveSoon()
    }

    /** Переставить очередь в порядок [target] (uid), не трогая текущий элемент (без перезапуска звука). */
    private fun reorderAroundCurrent(target: List<String>) {
        // как и остальные массовые изменения — не больше двух изменений таймлайна (см. replaceQueue)
        replaceQueue(target.mapNotNull { u -> tracks[u]?.let { QItem(u, it) } }, replaceCurrent = false)
    }

    fun addNext(list: List<Track>) = insert(list, next = true)
    fun addToQueue(list: List<Track>) = insert(list, next = false)

    private fun insert(list: List<Track>, next: Boolean) {
        val items = list.filter { it.stream != null }.map { QItem(newUid(), it) }
        if (items.isEmpty()) return
        if (_ui.value.mode != Mode.QUEUE || player.mediaItemCount == 0) { playList(list); return }
        items.forEach { tracks[it.uid] = it.track }
        val at = if (next) player.currentMediaItemIndex + 1 else player.mediaItemCount
        player.addMediaItems(at, items.map { item(it.uid, it.track) })
        publish(); schedulePrefetch(); saveSoon()
        _messages.tryEmit(if (next) "Играет следующим" else "Добавлено в очередь")
    }

    fun removeAt(i: Int) {
        if (i !in 0 until player.mediaItemCount) return
        tracks.remove(player.getMediaItemAt(i).mediaId)
        player.removeMediaItem(i)
        publish(); schedulePrefetch(); saveSoon()
    }

    fun move(from: Int, to: Int) {
        if (from == to || from !in 0 until player.mediaItemCount) return
        player.moveMediaItem(from, to.coerceIn(0, player.mediaItemCount - 1))
        publish(); schedulePrefetch(); saveSoon()
    }

    /** Очистить всё, кроме текущего. */
    fun clearUpcoming() {
        val cur = player.currentMediaItemIndex
        if (cur + 1 < player.mediaItemCount) player.removeMediaItems(cur + 1, player.mediaItemCount)
        if (cur > 0) player.removeMediaItems(0, cur)
        publish(); saveSoon()
    }

    /** Треки удалены из медиатеки и недоступны (gone_ids) — убрать из очереди. */
    fun dropTracks(ids: Set<Long>) {
        val q = queueNow()
        val keep = q.filter { it.track.id !in ids }
        if (keep.size == q.size) return
        q.forEach { if (it.track.id in ids) tracks.remove(it.uid) }
        replaceQueue(keep, replaceCurrent = false) // одним изменением таймлайна (см. replaceQueue)
        publish(); saveSoon()
    }

    // ---------------------------------------------------------------- Моя Волна (живой поток)

    fun playWave(url: String) {
        tracks.clear()
        original = null
        val md = MediaMetadata.Builder().setTitle("Моя Волна").setArtist("В эфире").setIsPlayable(true).build()
        _ui.value = _ui.value.copy(mode = Mode.WAVE, shuffle = false, source = PlaySource.WAVE)
        player.setMediaItem(MediaItem.Builder().setMediaId("wave").setUri(url).setMediaMetadata(md).build())
        player.repeatMode = Player.REPEAT_MODE_OFF
        player.setPlaybackSpeed(1f) // волна — всегда 1×
        stopFade()
        player.prepare()
        player.play()
        eq.setTrackGain(1.0)
        prefetcher.update(emptyList())
        publish()
        startWavePoll()
    }

    /** Метаданные эфира для уведомления/экрана блокировки: /api/wave/now раз в 15 с (track_id → обложка трека). */
    private fun startWavePoll() {
        waveJob?.cancel()
        waveJob = scope.launch {
            var shown: String? = null
            while (_ui.value.mode == Mode.WAVE) {
                val now = runCatching { api.get("/wave/now", WaveNow.serializer()) }.getOrNull()
                if (now != null) {
                    _waveNow.value = now
                    var cover = now.cover
                    if (cover == null && now.trackId != null) {
                        cover = runCatching { api.get("/tracks?ids=${now.trackId}", TrackList.serializer()).items.firstOrNull()?.cover }.getOrNull()
                    }
                    val sig = "${now.trackId}|${now.title}|${now.artist}"
                    val cur = player.currentMediaItem
                    if (sig != shown && cur != null && cur.mediaId == "wave" && now.title != null) {
                        shown = sig
                        val md = MediaMetadata.Builder()
                            .setTitle(now.title).setArtist(now.artist ?: "Моя Волна").setAlbumTitle(now.album ?: "Моя Волна")
                            .setArtworkUri(ServerUrl.absolute(session.server, cover)?.toUri()).setIsPlayable(true).build()
                        // тот же адрес → ExoPlayer обновляет метаданные без перезапуска потока
                        player.replaceMediaItem(0, cur.buildUpon().setMediaMetadata(md).build())
                    }
                }
                delay(15_000)
            }
        }
    }

    private fun stopWavePoll() { waveJob?.cancel(); waveJob = null; _waveNow.value = null }

    fun stopWave() {
        if (_ui.value.mode != Mode.WAVE) return
        stopWavePoll()
        player.stop()
        player.clearMediaItems()
        _ui.value = _ui.value.copy(mode = Mode.QUEUE)
        publish()
    }

    // ---------------------------------------------------------------- опции

    fun setNormalize(on: Boolean) { normalize = on; applyGain() }

    fun setSpeed(v: Float) {
        if (_ui.value.mode == Mode.WAVE) return
        player.playbackParameters = PlaybackParameters(v.coerceIn(0.5f, 2f)) // Sonic: высота голоса сохраняется
        publish()
    }

    private fun applyGain() {
        val t = tracks[player.currentMediaItem?.mediaId ?: ""]
        eq.setTrackGain(if (_ui.value.mode == Mode.WAVE) 1.0 else Eq.normalizationGain(t?.gain, t?.peak, normalize))
    }

    // ---------------------------------------------------------------- таймер сна

    fun setSleep(minutes: Int?) {
        cancelSleep()
        if (minutes == null) return
        if (minutes == 0) { player.pauseAtEndOfMediaItems = true; _sleep.value = Sleep(endOfTrack = true); return }
        val until = System.currentTimeMillis() + minutes * 60_000L
        _sleep.value = Sleep(untilMs = until)
        sleepJob = scope.launch {
            delay(until - System.currentTimeMillis() - 8000)
            // затухание 8 с, затем пауза и возврат громкости
            val steps = 40
            for (i in steps downTo 0) { sleepVol = i / steps.toFloat(); applyVolumes(); delay(200) }
            player.pause()
            sleepVol = 1f
            applyVolumes()
            _sleep.value = null
        }
    }

    fun cancelSleep() {
        sleepJob?.cancel()
        sleepJob = null
        player.pauseAtEndOfMediaItems = false
        sleepVol = 1f
        applyVolumes()
        _sleep.value = null
    }

    // ---------------------------------------------------------------- ошибки и свежие ссылки

    private fun onError(error: PlaybackException) {
        Log.w("PiMusic", "player error ${error.errorCodeName}: ${error.message}")
        if (_ui.value.mode == Mode.WAVE) { _messages.tryEmit("Эфир недоступен — попробуйте позже"); return }
        val uid = player.currentMediaItem?.mediaId ?: return
        val t = tracks[uid] ?: return
        if (uid !in retried) {
            retried += uid
            scope.launch {
                refreshLinks()
                player.prepare()
                player.play()
            }
        } else {
            _messages.tryEmit("Не удалось включить «${t.title}» — пропускаю")
            if (player.hasNextMediaItem()) { player.seekToNextMediaItem(); player.prepare(); player.play() }
        }
    }

    /** Подписанные ссылки живут 12 ч: обновить пачками по 500 через /api/tracks (недоступные — убрать). */
    suspend fun refreshLinks() {
        val q = queueNow()
        if (q.isEmpty()) return
        val ids = q.map { it.track.id }.distinct()
        val fresh = HashMap<Long, Track>()
        try {
            for (chunk in ids.chunked(500)) {
                api.get("/tracks?ids=" + chunk.joinToString(","), TrackList.serializer()).items.forEach { fresh[it.id] = it }
            }
        } catch (e: Exception) {
            Log.w("PiMusic", "refresh links: $e")
            return // офлайн: играем из кэша по старым ссылкам
        }
        linksAt = System.currentTimeMillis()
        val curUid = player.currentMediaItem?.mediaId
        val keep = ArrayList<QItem>(q.size)
        for (qi in q) {
            val nt = fresh[qi.track.id]
            if (nt == null) {
                if (qi.uid == curUid || caches.isOffline(qi.track.id)) keep += qi else tracks.remove(qi.uid)
                continue
            }
            tracks[qi.uid] = nt
            keep += QItem(qi.uid, nt)
        }
        replaceQueue(keep, replaceCurrent = player.playbackState == Player.STATE_IDLE)
        publish(); schedulePrefetch(); saveSoon()
    }

    /**
     * Заменить очередь не более чем двумя изменениями таймлайна. Каждое изменение MediaSession рассылает всем
     * контроллерам (системная шторка, OneUI Now Bar, часы): поштучные replace/remove по тысячам треков вешали
     * system_server и роняли оболочку (S24 Ultra, OneUI 9). Играющий текущий элемент не трогаем — звук не прерывается.
     */
    private fun replaceQueue(items: List<QItem>, replaceCurrent: Boolean) {
        val mediaItems = items.map { item(it.uid, it.track) }
        val curUid = player.currentMediaItem?.mediaId
        val pos = items.indexOfFirst { it.uid == curUid }
        if (pos < 0 || replaceCurrent) {
            if (mediaItems.isEmpty()) { player.clearMediaItems(); return }
            player.setMediaItems(mediaItems, pos.coerceAtLeast(0), if (pos < 0) 0 else player.currentPosition)
            return
        }
        val cur = player.currentMediaItemIndex
        player.replaceMediaItems(cur + 1, player.mediaItemCount, mediaItems.subList(pos + 1, mediaItems.size))
        player.replaceMediaItems(0, cur, mediaItems.subList(0, pos))
    }

    /** Свежие данные треков (ссылки) — для скачивания офлайн. */
    suspend fun fresh(list: List<Track>): List<Track> {
        val out = ArrayList<Track>()
        for (chunk in list.map { it.id }.distinct().chunked(500)) {
            out += api.get("/tracks?ids=" + chunk.joinToString(","), TrackList.serializer()).items
        }
        return out
    }

    // ---------------------------------------------------------------- предзагрузка

    fun schedulePrefetch() {
        if (_ui.value.mode != Mode.QUEUE) { prefetcher.update(emptyList()); return }
        val idx = player.currentMediaItemIndex
        val n = player.mediaItemCount
        val list = QueueModel.prefetchIndices(idx, n, _ui.value.repeat, 2).mapNotNull { i ->
            val t = tracks[player.getMediaItemAt(i).mediaId] ?: return@mapNotNull null
            val url = ServerUrl.absolute(session.server, t.stream) ?: return@mapNotNull null
            Prefetcher.Item(t.id, url)
        }
        prefetcher.update(list)
    }

    // ---------------------------------------------------------------- сохранение очереди

    @Serializable
    data class Saved(
        val tracks: List<Track>, val index: Int, val positionMs: Long, val shuffle: Boolean, val repeat: Int, val linksAt: Long,
        val source: String? = null, // 2.0.0–2.0.1 (строка) — не используется
        val src: PlaySource? = null,
    )

    private fun file() = File(ctx.filesDir, "queue-${session.login ?: "anon"}.json")
    private var saveJob: Job? = null

    private fun saveSoon() {
        saveJob?.cancel()
        saveJob = scope.launch {
            delay(800)
            if (_ui.value.mode != Mode.QUEUE) return@launch
            val q = queueNow()
            val s = Saved(q.map { it.track }.take(3000), player.currentMediaItemIndex, player.currentPosition, _ui.value.shuffle, player.repeatMode, linksAt, src = _ui.value.source)
            val text = PiJson.encodeToString(Saved.serializer(), s)
            withContext(Dispatchers.IO) { runCatching { file().writeText(text) } }
        }
    }

    fun savePosition() = saveSoon()

    /** Восстановить очередь на паузе (без автозапуска). */
    suspend fun restore() {
        if (player.mediaItemCount > 0) return
        val text = withContext(Dispatchers.IO) { runCatching { file().takeIf { it.isFile }?.readText() }.getOrNull() } ?: return
        val s = runCatching { PiJson.decodeFromString(Saved.serializer(), text) }.getOrNull() ?: return
        if (s.tracks.isEmpty()) return
        val entries = s.tracks.map { QItem(newUid(), it) }
        tracks.clear()
        entries.forEach { tracks[it.uid] = it.track }
        linksAt = s.linksAt
        player.setMediaItems(entries.map { item(it.uid, it.track) }, s.index.coerceIn(0, entries.size - 1), s.positionMs)
        player.repeatMode = s.repeat
        _ui.value = _ui.value.copy(mode = Mode.QUEUE, shuffle = s.shuffle, source = s.src)
        player.prepare()
        applyGain()
        publish()
        if (System.currentTimeMillis() - linksAt > 6 * 3600_000L) refreshLinks()
    }

    /** Очередь на паузе без подготовки плеера (скриншот-тесты: мини-плеер, «Сейчас играет», очередь). */
    @androidx.annotation.VisibleForTesting
    fun setQueuePaused(list: List<Track>, index: Int) {
        val entries = list.map { QItem(newUid(), it) }
        tracks.clear()
        entries.forEach { tracks[it.uid] = it.track }
        _ui.value = _ui.value.copy(mode = Mode.QUEUE, shuffle = false, source = PlaySource("playlist", "Дорога", id = 1))
        player.setMediaItems(entries.map { item(it.uid, it.track) }, index, 0)
        publish()
    }

    /** Выход из аккаунта: остановить и забыть очередь. */
    fun reset() {
        stopFade()
        stopWavePoll()
        player.stop()
        player.clearMediaItems()
        tracks.clear()
        prefetcher.update(emptyList())
        _ui.value = PlayerUi()
    }

    @Suppress("unused")
    private val listSer = ListSerializer(Track.serializer())

    /** Позиция для UI (вызывать из главного потока). */
    fun position(): Long = player.currentPosition
    fun duration(): Long = player.duration.takeIf { it != C.TIME_UNSET && it > 0 } ?: ((_ui.value.current?.seconds ?: 0) * 1000)
    fun buffered(): Long = player.bufferedPosition

    @Suppress("unused")
    private fun isActive() = scope.isActive
}
