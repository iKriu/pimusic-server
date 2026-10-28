package io.github.ikriu.pimusic.player

import android.content.Context
import android.net.Uri
import androidx.media3.common.C
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.cache.Cache
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.CacheEvictor
import androidx.media3.datasource.cache.CacheKeyFactory
import androidx.media3.datasource.cache.CacheSpan
import androidx.media3.datasource.cache.ContentMetadata
import androidx.media3.datasource.cache.NoOpCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.datasource.okhttp.OkHttpDataSource
import okhttp3.OkHttpClient
import java.io.File
import java.util.TreeSet

/**
 * Два кэша Media3:
 *  * поток (cacheDir/media) — LRU с лимитом из настроек; сюда пишет ExoPlayer и Prefetcher (файл целиком);
 *  * скачанное (filesDir/offline) — без вытеснения, только DownloadManager; удаляет пользователь.
 * Ключ — «track:<id>» (подписанная ссылка меняется каждый час, ключ — нет).
 */
class MediaCaches(ctx: Context, okHttp: OkHttpClient, limitBytes: Long) {
    val db = StandaloneDatabaseProvider(ctx)
    val evictor = MutableLruEvictor(limitBytes)
    val stream = SimpleCache(File(ctx.cacheDir, "media"), evictor, db)
    val offline = SimpleCache(File(ctx.filesDir, "offline"), NoOpCacheEvictor(), db)

    val keyFactory = CacheKeyFactory { spec -> spec.key ?: spec.uri.toString() }
    val http: DataSource.Factory = OkHttpDataSource.Factory(okHttp)

    /** Кэш потока: чтение/запись, сеть — OkHttp. */
    val streamFactory: CacheDataSource.Factory = CacheDataSource.Factory()
        .setCache(stream)
        .setUpstreamDataSourceFactory(http)
        .setCacheKeyFactory(keyFactory)
        .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)

    /** Воспроизведение трека: скачанное (только чтение) → кэш потока → сеть. */
    private val trackFactory: CacheDataSource.Factory = CacheDataSource.Factory()
        .setCache(offline)
        .setUpstreamDataSourceFactory(streamFactory)
        .setCacheWriteDataSinkFactory(null)
        .setCacheKeyFactory(keyFactory)

    /** Для DownloadManager: пишет в «скачанное», читает уже закэшированное из кэша потока (без повторной загрузки). */
    val downloadFactory: CacheDataSource.Factory = CacheDataSource.Factory()
        .setCache(offline)
        .setUpstreamDataSourceFactory(
            CacheDataSource.Factory().setCache(stream).setUpstreamDataSourceFactory(http)
                .setCacheWriteDataSinkFactory(null).setCacheKeyFactory(keyFactory)
        )
        .setCacheKeyFactory(keyFactory)

    /** Треки (ключ track:…) — через кэши, остальное (живой поток Волны) — напрямую. */
    val playbackFactory = DataSource.Factory { RoutingDataSource(trackFactory.createDataSource(), http.createDataSource()) }

    private fun fully(cache: Cache, key: String): Boolean {
        val len = ContentMetadata.getContentLength(cache.getContentMetadata(key))
        return len > 0 && cache.getCachedBytes(key, 0, len) >= len
    }

    fun isOffline(trackId: Long) = fully(offline, key(trackId))
    fun isCached(trackId: Long) = fully(offline, key(trackId)) || fully(stream, key(trackId))

    fun streamBytes(): Long = stream.cacheSpace
    fun offlineBytes(): Long = offline.cacheSpace

    fun clearStream() {
        for (k in stream.keys.toList()) runCatching { stream.removeResource(k) }
    }

    companion object {
        fun key(trackId: Long) = "track:$trackId"
    }
}

/** Выбор источника по ключу: track:… — через кэш, иначе — напрямую (живой поток не кэшируется). */
private class RoutingDataSource(private val cached: DataSource, private val plain: DataSource) : DataSource {
    private var cur: DataSource? = null
    override fun addTransferListener(transferListener: TransferListener) {
        cached.addTransferListener(transferListener)
        plain.addTransferListener(transferListener)
    }
    override fun open(dataSpec: DataSpec): Long {
        val d = if (dataSpec.key?.startsWith("track:") == true) cached else plain
        cur = d
        return d.open(dataSpec)
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int): Int = cur?.read(buffer, offset, length) ?: C.RESULT_END_OF_INPUT
    override fun getUri(): Uri? = cur?.uri
    override fun getResponseHeaders(): Map<String, List<String>> = cur?.responseHeaders ?: emptyMap()
    override fun close() {
        try { cur?.close() } finally { cur = null }
    }
}

/**
 * LRU-вытеснение с изменяемым лимитом (у LeastRecentlyUsedCacheEvictor лимит задаётся раз и навсегда):
 * лимит меняется в настройках без перезапуска.
 */
class MutableLruEvictor(@Volatile var maxBytes: Long) : CacheEvictor {
    private val spans = TreeSet<CacheSpan> { a, b ->
        val d = a.lastTouchTimestamp - b.lastTouchTimestamp
        if (d != 0L) (if (d < 0) -1 else 1) else a.compareTo(b)
    }
    private var size = 0L
    @Volatile private var cache: Cache? = null

    override fun requiresCacheSpanTouches() = true
    override fun onCacheInitialized() {}

    // Колбэки вызывает SimpleCache под своей блокировкой — своя не нужна (иначе возможна взаимоблокировка).
    override fun onStartFile(cache: Cache, key: String, position: Long, length: Long) {
        this.cache = cache
        if (length != C.LENGTH_UNSET.toLong()) evict(cache, length)
    }

    override fun onSpanAdded(cache: Cache, span: CacheSpan) {
        this.cache = cache
        spans.add(span)
        size += span.length
        evict(cache, 0)
    }

    override fun onSpanRemoved(cache: Cache, span: CacheSpan) {
        if (spans.remove(span)) size -= span.length
    }

    override fun onSpanTouched(cache: Cache, oldSpan: CacheSpan, newSpan: CacheSpan) {
        onSpanRemoved(cache, oldSpan)
        onSpanAdded(cache, newSpan)
    }

    private fun evict(cache: Cache, required: Long) {
        while (size + required > maxBytes && spans.isNotEmpty()) {
            cache.removeSpan(spans.first())
        }
    }

    /** Новый лимит: лишнее вытесняется сразу (вызывать не из потока плеера). */
    fun applyLimit(bytes: Long) {
        maxBytes = bytes
        val c = cache ?: return
        synchronized(c) { evict(c, 0) } // та же блокировка, что у методов SimpleCache
    }
}
