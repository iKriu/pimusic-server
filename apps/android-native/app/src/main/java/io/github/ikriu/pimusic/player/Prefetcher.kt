package io.github.ikriu.pimusic.player

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import androidx.core.net.toUri
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheWriter
import java.util.concurrent.Executors
import java.util.concurrent.Future

/**
 * Предзагрузка ЦЕЛЫХ файлов в кэш потока: текущий и 1–2 следующих трека очереди (CacheWriter), по одному за раз,
 * текущий — первым. Скачанные и уже закэшированные пропускаются; без сети ничего не делаем.
 */
class Prefetcher(private val ctx: Context, private val caches: MediaCaches) {
    data class Item(val trackId: Long, val url: String)

    private val exec = Executors.newSingleThreadExecutor { r -> Thread(r, "pimusic-prefetch").apply { priority = Thread.MIN_PRIORITY } }
    private var task: Future<*>? = null
    @Volatile private var writer: CacheWriter? = null
    @Volatile private var wanted: List<Item> = emptyList()

    private fun online(): Boolean {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    /** Новый список (порядок = приоритет). Текущая загрузка прерывается, если её трека больше нет в списке. */
    @Synchronized
    fun update(items: List<Item>) {
        if (items == wanted && task?.isDone == false) return
        wanted = items
        writer?.cancel()
        task?.cancel(true)
        if (items.isEmpty()) return
        task = exec.submit { run(items) }
    }

    private fun run(items: List<Item>) {
        for (it in items) {
            if (Thread.currentThread().isInterrupted || wanted !== items) return
            if (!online()) return
            if (caches.isCached(it.trackId)) continue
            val spec = DataSpec.Builder().setUri(it.url.toUri()).setKey(MediaCaches.key(it.trackId)).build()
            val w = CacheWriter(caches.streamFactory.createDataSource(), spec, null, null)
            writer = w
            try {
                w.cache()
                Log.i("PiMusic", "prefetch: track ${it.trackId} cached")
            } catch (e: Exception) {
                if (e is InterruptedException || Thread.currentThread().isInterrupted) return
                Log.w("PiMusic", "prefetch ${it.trackId}: $e")
            } finally {
                writer = null
            }
        }
    }
}
