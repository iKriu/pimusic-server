package io.github.ikriu.pimusic

import android.app.Application
import android.content.Context
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.disk.DiskCache
import coil3.memory.MemoryCache
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import io.github.ikriu.pimusic.data.Api
import io.github.ikriu.pimusic.data.ApiCache
import io.github.ikriu.pimusic.data.Session
import io.github.ikriu.pimusic.data.SettingsStore
import io.github.ikriu.pimusic.player.EqController
import io.github.ikriu.pimusic.player.MediaCaches
import io.github.ikriu.pimusic.player.OfflineDownloads
import io.github.ikriu.pimusic.player.PlayerHub
import io.github.ikriu.pimusic.player.Prefetcher
import io.github.ikriu.pimusic.update.Updater
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okio.Path.Companion.toOkioPath

/** Локальные настройки устройства (не синхронизируются): кэш, скачивание, офлайн. */
class AppPrefs(ctx: Context) {
    private val p = ctx.getSharedPreferences("device", Context.MODE_PRIVATE)
    private val _offline = MutableStateFlow(p.getBoolean("offline", false))
    val offline: StateFlow<Boolean> = _offline.asStateFlow()

    var cacheLimitMb: Int
        get() = p.getInt("cache_limit_mb", 2048)
        set(v) { p.edit().putInt("cache_limit_mb", v).apply() }
    var wifiOnly: Boolean
        get() = p.getBoolean("wifi_only", false)
        set(v) { p.edit().putBoolean("wifi_only", v).apply() }
    var themeLocal: String?
        get() = p.getString("theme", null)
        set(v) { p.edit().putString("theme", v).apply() }

    fun setOffline(on: Boolean) { p.edit().putBoolean("offline", on).apply(); _offline.value = on }
}

/** Приложение: общие объекты (сессия, API, кэши, плеер, скачивание, обновления, загрузчик обложек). */
open class PiApp : Application(), SingletonImageLoader.Factory {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    lateinit var prefs: AppPrefs
    lateinit var session: Session
    lateinit var api: Api
    lateinit var apiCache: ApiCache
    lateinit var settings: SettingsStore
    lateinit var caches: MediaCaches
    lateinit var offline: OfflineDownloads
    lateinit var prefetcher: Prefetcher
    lateinit var player: PlayerHub
    lateinit var updater: Updater
    lateinit var eqc: EqController

    companion object {
        private lateinit var instance: PiApp
        fun get() = instance
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
        prefs = AppPrefs(this)
        session = Session(this)
        api = Api(session, Api.client())
        apiCache = ApiCache(cacheDir.resolve("api"), session)
        settings = SettingsStore.android(this, api, session, scope)
        settings.setLogin(session.login)
        caches = MediaCaches(this, api.http, prefs.cacheLimitMb * 1024L * 1024L)
        offline = OfflineDownloads(this, caches, scope)
        offline.setWifiOnly(prefs.wifiOnly)
        prefetcher = Prefetcher(this, caches)
        player = PlayerHub(this, session, api, caches, prefetcher, scope)
        updater = Updater(this, scope)
        eqc = EqController(settings, session, player, scope)
        settings.startPeriodic { ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED) }
        scope.launch { api.authLost.collect { player.reset() } }
    }

    val foreground get() = ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)

    override fun newImageLoader(context: PlatformContext): ImageLoader = ImageLoader.Builder(context)
        .components { add(OkHttpNetworkFetcherFactory(callFactory = { api.http })) }
        .memoryCache { MemoryCache.Builder().maxSizePercent(context, 0.2).build() }
        .diskCache { DiskCache.Builder().directory(cacheDir.resolve("covers").toOkioPath()).maxSizeBytes(300L * 1024 * 1024).build() }
        .crossfade(true)
        .build()
}
