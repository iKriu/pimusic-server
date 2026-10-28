package io.github.ikriu.pimusic.player

import android.app.Notification
import android.content.Context
import androidx.core.net.toUri
import androidx.media3.exoplayer.offline.Download
import androidx.media3.exoplayer.offline.DownloadManager
import androidx.media3.exoplayer.offline.DownloadNotificationHelper
import androidx.media3.exoplayer.offline.DownloadRequest
import androidx.media3.exoplayer.offline.DownloadService
import androidx.media3.exoplayer.scheduler.PlatformScheduler
import androidx.media3.exoplayer.scheduler.Requirements
import androidx.media3.exoplayer.scheduler.Scheduler
import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.R
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Track
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.Executors

/** Скачанный трек: состояние Media3 Download + метаданные трека (JSON в DownloadRequest.data). */
data class OfflineItem(val track: Track, val state: Int, val bytes: Long, val percent: Float, val group: String?, val updated: Long) {
    val done get() = state == Download.STATE_COMPLETED
    val failed get() = state == Download.STATE_FAILED
    val active get() = state == Download.STATE_DOWNLOADING || state == Download.STATE_QUEUED || state == Download.STATE_RESTARTING
}

/**
 * «Скачать» трек/альбом/плейлист: Media3 DownloadManager в отдельный кэш без вытеснения (MediaCaches.offline).
 * Хранится до ручного удаления. Состояние — [items] (id трека → элемент), обновляется слушателем и опросом прогресса.
 */
class OfflineDownloads(private val ctx: Context, caches: MediaCaches, private val scope: CoroutineScope) {
    val manager = DownloadManager(ctx, caches.db, caches.offline, caches.downloadFactory, Executors.newFixedThreadPool(2)).apply {
        maxParallelDownloads = 2
    }
    private val _items = MutableStateFlow<Map<Long, OfflineItem>>(emptyMap())
    val items: StateFlow<Map<Long, OfflineItem>> = _items.asStateFlow()
    private var poll: Job? = null

    init {
        manager.addListener(object : DownloadManager.Listener {
            override fun onInitialized(downloadManager: DownloadManager) = refresh()
            override fun onDownloadChanged(downloadManager: DownloadManager, download: Download, finalException: Exception?) = refresh()
            override fun onDownloadRemoved(downloadManager: DownloadManager, download: Download) = refresh()
        })
        refresh()
    }

    fun setWifiOnly(on: Boolean) {
        manager.requirements = Requirements(if (on) Requirements.NETWORK_UNMETERED else Requirements.NETWORK)
    }

    private fun parse(d: Download): OfflineItem? {
        val meta = runCatching { PiJson.decodeFromString(Meta.serializer(), String(d.request.data, Charsets.UTF_8)) }.getOrNull() ?: return null
        return OfflineItem(meta.track, d.state, d.bytesDownloaded, d.percentDownloaded, meta.group, d.updateTimeMs)
    }

    fun refresh() {
        scope.launch(Dispatchers.IO) {
            val out = LinkedHashMap<Long, OfflineItem>()
            runCatching {
                manager.downloadIndex.getDownloads().use { c ->
                    while (c.moveToNext()) parse(c.download)?.let { out[it.track.id] = it }
                }
            }
            _items.value = out
            if (out.values.any { it.active }) startPolling()
        }
    }

    private fun startPolling() {
        if (poll?.isActive == true) return
        poll = scope.launch {
            while (isActive) {
                delay(1000)
                val cur = manager.currentDownloads
                if (cur.isEmpty()) { refresh(); break }
                val m = _items.value.toMutableMap()
                for (d in cur) parse(d)?.let { m[it.track.id] = it }
                _items.value = m
            }
        }
    }

    @kotlinx.serialization.Serializable
    data class Meta(val track: Track, val group: String? = null)

    /** Поставить треки в скачивание (ссылки должны быть свежими — PlayerHub обновляет их перед вызовом). */
    fun add(tracks: List<Track>, group: String?) {
        val base = PiApp.get().session.server
        for (t in tracks) {
            val url = io.github.ikriu.pimusic.core.ServerUrl.absolute(base, t.stream) ?: continue
            val req = DownloadRequest.Builder(MediaCaches.key(t.id), url.toUri())
                .setCustomCacheKey(MediaCaches.key(t.id))
                .setData(PiJson.encodeToString(Meta.serializer(), Meta(t, group)).toByteArray(Charsets.UTF_8))
                .build()
            DownloadService.sendAddDownload(ctx, DownloadsService::class.java, req, false)
        }
        refresh()
    }

    fun remove(trackId: Long) {
        DownloadService.sendRemoveDownload(ctx, DownloadsService::class.java, MediaCaches.key(trackId), false)
    }

    fun removeAll() {
        DownloadService.sendRemoveAllDownloads(ctx, DownloadsService::class.java, false)
    }
}

/** Сводка скачивания для уведомления: «3/12», общий процент, текущий трек. */
data class DownloadProgress(val percent: Int?, val short: String, val text: String)

fun progressOf(downloads: List<Download>): DownloadProgress {
    val active = downloads.filter { it.state == Download.STATE_DOWNLOADING || it.state == Download.STATE_QUEUED || it.state == Download.STATE_RESTARTING }
    val total = downloads.size.coerceAtLeast(1)
    val done = downloads.count { it.state == Download.STATE_COMPLETED }
    val pcts = downloads.map { if (it.state == Download.STATE_COMPLETED) 100f else it.percentDownloaded.takeIf { p -> p >= 0 } ?: 0f }
    val percent = if (downloads.isEmpty()) null else (pcts.sum() / total).toInt().coerceIn(0, 100)
    val cur = active.firstOrNull { it.state == Download.STATE_DOWNLOADING }?.let {
        runCatching { PiJson.decodeFromString(OfflineDownloads.Meta.serializer(), String(it.request.data, Charsets.UTF_8)).track.title }.getOrNull()
    }
    val left = active.size
    return DownloadProgress(percent, "${(total - left).coerceAtLeast(done)}/$total",
        listOfNotNull(cur, if (left > 0) "осталось $left" else null).joinToString(" · ").ifEmpty { "Готовлю…" })
}

/** Служба скачивания (уведомление с прогрессом; продолжение по сети через PlatformScheduler). */
class DownloadsService : DownloadService(
    // обновление прогресса не чаще раза в секунду (Live Updates: не заваливать SystemUI)
    NOTIFICATION_ID, 1000L, CHANNEL, R.string.channel_downloads, R.string.channel_downloads_desc,
) {
    companion object {
        const val NOTIFICATION_ID = 0x5049
        const val CHANNEL = "downloads"
        private const val JOB_ID = 0x504A
    }

    override fun getDownloadManager(): DownloadManager = PiApp.get().offline.manager

    override fun getScheduler(): Scheduler = PlatformScheduler(this, JOB_ID)

    override fun getForegroundNotification(downloads: MutableList<Download>, notMetRequirements: Int): Notification {
        val p = progressOf(downloads)
        val open = android.app.PendingIntent.getActivity(
            this, 2, android.content.Intent(this, io.github.ikriu.pimusic.MainActivity::class.java), android.app.PendingIntent.FLAG_IMMUTABLE,
        )
        return LiveProgress.build(
            this, CHANNEL, "Скачивание на устройство",
            if (notMetRequirements != 0) "Ждёт подходящей сети" else p.text,
            p.percent, 100, p.short, open,
        )
    }
}
