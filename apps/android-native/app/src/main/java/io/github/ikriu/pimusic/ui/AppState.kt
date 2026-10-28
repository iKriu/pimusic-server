package io.github.ikriu.pimusic.ui

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.Uri
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshots.SnapshotStateList
import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.LibKeys
import io.github.ikriu.pimusic.data.Me
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.theme.ThemePref
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement

enum class Tab(val label: String) { SEARCH("Поиск"), LIBRARY("Медиатека"), DOWNLOADS("Загрузки"), WAVE("Волна") }

/** Экраны стека (у каждой вкладки свой стек). */
sealed interface Screen {
    data class Root(val tab: Tab) : Screen
    data class CatalogAlbum(val id: String?, val token: String? = null, val i: Int = 0, val title: String = "") : Screen
    data class CatalogArtist(val id: String, val name: String = "") : Screen
    data class LibArtist(val key: String, val name: String) : Screen
    data class LibAlbum(val a: String, val b: String, val title: String, val cover: String? = null) : Screen
    data class PlaylistPage(val id: Long, val name: String) : Screen
    data object Settings : Screen
    data object Offline : Screen
    data object Equalizer : Screen
    data class RemoteEq(val deviceId: String, val name: String) : Screen
}

enum class PlayerView { MAIN, QUEUE, LYRICS, EQ }

/** Лист действий трека (⋮ / долгое нажатие). */
data class TrackMenu(val track: Track, val playlistId: Long? = null, val canRemoveFromPlaylist: Boolean = false, val onRemoved: (() -> Unit)? = null)

/** Состояние интерфейса: вкладки и стеки, плеер, листы, снекбар, ключи медиатеки. */
class AppState(val app: PiApp, val scope: CoroutineScope) {
    var tab by mutableStateOf(Tab.LIBRARY)
    private val stacks: Map<Tab, SnapshotStateList<Screen>> = Tab.entries.associateWith { mutableStateListOf<Screen>(Screen.Root(it)) }
    var playerOpen by mutableStateOf(false)
    /** Пользователь закрыл закреплённую панель «Сейчас играет» (≥1200dp) — больше не открывать её самой. */
    var panelDismissed by mutableStateOf(false)
    /** Лист «Сейчас играет» (жесты мини-плеера; задаёт App на compact/medium). */
    var sheet: PlayerSheet? = null

    /** «Играет из …»: переход к источнику очереди (плейлист, альбом, исполнитель, медиатека, скачанное, Волна). */
    fun goSource(s: io.github.ikriu.pimusic.player.PlaySource) {
        playerOpen = false
        when (s.kind) {
            "playlist" -> s.id?.let { push(Screen.PlaylistPage(it, s.title)) }
            "album" -> if (s.a != null && s.b != null) push(Screen.LibAlbum(s.a, s.b, s.title))
            "artist" -> s.a?.let { push(Screen.LibArtist(it, s.title)) }
            "offline" -> push(Screen.Offline)
            "wave" -> select(Tab.WAVE)
            "upload" -> select(Tab.DOWNLOADS)
            else -> select(Tab.LIBRARY)
        }
    }
    var playerView by mutableStateOf(PlayerView.MAIN)
    var menu by mutableStateOf<TrackMenu?>(null)
    var addToPlaylist by mutableStateOf<List<Track>?>(null)
    var bulkDelete by mutableStateOf<BulkDelete?>(null)
    var me by mutableStateOf<Me?>(null)
    var libKeys by mutableStateOf<Set<String>>(emptySet())
    var online by mutableStateOf(true)
    private var offlinePref by mutableStateOf(app.prefs.offline.value)
    var theme by mutableStateOf(readTheme())
    /** Счётчик «медиатека изменилась» — экраны перезагружают списки. */
    var libraryVersion by mutableStateOf(0)
    val snackbar = SnackbarHostState()
    val preview by lazy { io.github.ikriu.pimusic.player.PreviewPlayer(app, app.player) }

    init {
        val cm = app.getSystemService(ConnectivityManager::class.java)
        online = cm?.getNetworkCapabilities(cm.activeNetwork)?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true
        cm?.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) { scope.launch { online = true } }
            override fun onLost(network: Network) { scope.launch { online = false } }
        })
        scope.launch { app.prefs.offline.collect { offlinePref = it } }
        scope.launch { app.player.messages.collect { msg(it) } }
        scope.launch {
            app.settings.changes.collect { (key, _) -> if (key == "ui.theme") theme = readTheme() }
        }
        scope.launch { app.api.authLost.collect { reason -> msg(reason) } }
        if (app.session.loggedIn) afterLogin()
    }

    val offlineMode get() = offlinePref || !online

    // ---------------------------------------------------------------- навигация

    fun stack(t: Tab = tab): SnapshotStateList<Screen> = stacks.getValue(t)
    val current: Screen get() = stack().last()

    fun push(s: Screen) {
        playerOpen = false
        if (stack().last() != s) stack().add(s)
    }

    fun select(t: Tab) {
        if (t == tab && stack().size > 1) { val st = stack(); while (st.size > 1) st.removeAt(st.size - 1) }
        tab = t
    }

    /** «Назад»: лист → плеер → стек вкладки → вкладка «Медиатека». false — выйти из приложения. */
    fun back(): Boolean {
        when {
            bulkDelete != null -> bulkDelete = null
            menu != null -> menu = null
            addToPlaylist != null -> addToPlaylist = null
            playerOpen && playerView != PlayerView.MAIN -> playerView = PlayerView.MAIN
            playerOpen -> playerOpen = false
            stack().size > 1 -> stack().removeAt(stack().size - 1)
            tab != Tab.LIBRARY -> tab = Tab.LIBRARY
            else -> return false
        }
        return true
    }

    fun openPlayer(view: PlayerView = PlayerView.MAIN) { playerView = view; playerOpen = true }

    // ---------------------------------------------------------------- сообщения и ошибки

    fun msg(text: String, action: String? = null, onAction: (() -> Unit)? = null) {
        scope.launch {
            val r = snackbar.showSnackbar(text, actionLabel = action, withDismissAction = action == null, duration = SnackbarDuration.Short)
            if (r == SnackbarResult.ActionPerformed) onAction?.invoke()
        }
    }

    /** Выполнить действие API; ошибка — снекбар с текстом сервера. */
    fun run(block: suspend () -> Unit) {
        scope.launch {
            try { block() } catch (e: ApiException) { msg(e.message) } catch (e: Exception) { msg(e.message ?: "Ошибка") }
        }
    }

    // ---------------------------------------------------------------- вход

    fun afterLogin() {
        app.settings.setLogin(app.session.login)
        app.eqc.ensureDevice()
        app.eqc.refreshAll()
        theme = readTheme()
        scope.launch {
            app.settings.sync()
            theme = readTheme()
            app.eqc.refreshAll()
        }
        refreshMe()
        refreshKeys()
    }

    fun refreshMe() = scope.launch {
        runCatching { app.api.get("/me", Me.serializer()) }.onSuccess { me = it }
    }

    fun refreshKeys() = scope.launch {
        runCatching { app.api.get("/library/keys", LibKeys.serializer()) }.onSuccess { libKeys = (it.keys + it.isrc).toHashSet() }
    }

    fun libraryChanged() { libraryVersion++; refreshKeys() }

    fun logout(all: Boolean = false) {
        scope.launch {
            runCatching { app.api.post("/auth/logout", if (all) JsonObject(mapOf("all" to kotlinx.serialization.json.JsonPrimitive(true))) else null, JsonObject.serializer()) }
            app.player.reset()
            app.session.signOut()
            me = null
        }
    }

    // ---------------------------------------------------------------- тема

    private fun readTheme(): ThemePref {
        val s = app.settings.get("ui.theme")
        val fromSettings = s?.let { runCatching { PiJson.decodeFromJsonElement<ThemePref>(it) }.getOrNull() }
        val local = app.prefs.themeLocal?.let { runCatching { PiJson.decodeFromString(ThemePref.serializer(), it) }.getOrNull() }
        return (fromSettings ?: local ?: ThemePref()).clean()
    }

    fun changeTheme(p: ThemePref) {
        val c = p.clean()
        theme = c
        app.prefs.themeLocal = PiJson.encodeToString(ThemePref.serializer(), c)
        if (app.session.loggedIn) app.settings.set("ui.theme", PiJson.encodeToJsonElement(ThemePref.serializer(), c))
    }

    // ---------------------------------------------------------------- переходы к исполнителю/альбому

    /** Сервер не отдаёт ключи папок у трека — ищем исполнителя по имени в /api/library/artists (как libtrack.js). */
    /** «К исполнителю/К альбому» есть только у своих треков (у чужого из общего плейлиста страница ответит 404). */
    fun canGoLibrary(t: Track) = t.owned

    fun goArtist(t: Track) = run {
        if (!t.owned) { msg("Этого трека нет в вашей медиатеке — сохраните плейлист в медиатеку"); return@run }
        t.artistKey?.let { playerOpen = false; push(Screen.LibArtist(it, t.artist)); return@run }
        val list = app.api.get("/library/artists", io.github.ikriu.pimusic.data.LibArtists.serializer()).items
        val a = list.firstOrNull { it.name.equals(t.artist, true) } ?: list.firstOrNull { t.artist.startsWith(it.name, true) }
        if (a != null) { playerOpen = false; push(Screen.LibArtist(a.key, a.name)) } else msg("Исполнителя «${t.artist}» нет в медиатеке")
    }

    fun goAlbum(t: Track) = run {
        if (!t.owned) { msg("Этого трека нет в вашей медиатеке — сохраните плейлист в медиатеку"); return@run }
        if (t.artistKey != null && t.albumKey != null) { playerOpen = false; push(Screen.LibAlbum(t.artistKey, t.albumKey, t.album, t.cover)); return@run }
        val list = app.api.get("/library/albums", io.github.ikriu.pimusic.data.LibAlbums.serializer()).items
        val a = list.firstOrNull { it.title.equals(t.album, true) && it.artist.equals(t.artist, true) } ?: list.firstOrNull { it.title.equals(t.album, true) }
        if (a != null) { playerOpen = false; push(Screen.LibAlbum(a.artistKey, a.key, a.title, a.cover)) } else msg("Альбома «${t.album}» нет в медиатеке")
    }

    // ---------------------------------------------------------------- загрузки на сервер (✓ в поиске)

    /** Ключи «артист|название», поставленные в загрузку: прогресс в ряду, пока ключ не появится в медиатеке. */
    var pending by mutableStateOf<Set<String>>(emptySet())
    private var pendingPoll: kotlinx.coroutines.Job? = null

    /** Что качается на сервере сейчас (/api/activity active_items) — прогресс в рядах поиска. */
    var activeItems by mutableStateOf<List<io.github.ikriu.pimusic.data.ActiveItem>>(emptyList())

    /**
     * Опрос /api/activity (дёшево) раз в 4 с, пока есть ожидаемые загрузки; ключи медиатеки (/library/keys)
     * перечитываются только когда что-то закончилось (список active_items изменился).
     */
    fun markPending(keys: Collection<String>) {
        pending = pending + keys
        if (pendingPoll?.isActive == true) return
        pendingPoll = scope.launch {
            var n = 0
            var last: Set<String>? = null
            while (pending.isNotEmpty() && n++ < 150) {
                kotlinx.coroutines.delay(4000)
                val act = runCatching { app.api.get("/activity", io.github.ikriu.pimusic.data.Activity.serializer()) }.getOrNull() ?: continue
                activeItems = act.activeItems
                val now = act.activeItems.map { it.text }.toSet()
                val finished = last != null && (last - now).isNotEmpty()
                last = now
                if (finished || (now.isEmpty() && act.queue == 0 && act.jobs.isEmpty()) || n % 8 == 0) {
                    runCatching { app.api.get("/library/keys", LibKeys.serializer()) }.onSuccess {
                        val k = (it.keys + it.isrc).toHashSet()
                        libKeys = k
                        if (pending.any { p -> p in k }) libraryVersion++
                        pending = pending.filter { p -> p !in k }.toSet()
                    }
                    if (now.isEmpty() && act.queue == 0 && act.jobs.isEmpty()) pending = emptySet() // всё закончилось (часть могла не найтись)
                }
            }
            pending = emptySet()
            activeItems = emptyList()
        }
    }

    /** Ход загрузки трека из поиска: active_items по исполнителю и названию. */
    fun activeFor(artist: String, title: String): io.github.ikriu.pimusic.data.ActiveItem? =
        activeItems.firstOrNull { it.title.equals(title, true) && (it.artist == null || it.artist.equals(artist, true)) }

    // ---------------------------------------------------------------- офлайн

    /** Скачать треки на устройство (ссылки обновляются перед постановкой). */
    fun saveOffline(tracks: List<Track>, group: String?) = run {
        val list = tracks.filter { it.stream != null || it.id > 0 }
        if (list.isEmpty()) return@run
        val fresh = app.player.fresh(list)
        app.offline.add(fresh, group)
        val size = estimateSize(fresh)
        msg((if (fresh.size == 1) "Скачиваю «${fresh[0].title}»" else "Скачиваю: ${fresh.size} тр.") + (size?.let { " · ≈ ${io.github.ikriu.pimusic.core.fmtBytes(it)}" } ?: ""))
    }

    /** Оценка места для «Скачать на устройство» по полю size (null — размер неизвестен ни у одного трека). */
    fun estimateSize(tracks: List<Track>): Long? = tracks.mapNotNull { it.size }.takeIf { it.isNotEmpty() }?.sum()

    fun openUrl(ctx: Context, url: String) {
        val i = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { ctx.startActivity(i) }.onFailure { msg("Не удалось открыть ссылку") }
    }

    /** Ссылка t.me/<бот>?start=… — приложением Telegram (tg://resolve), если установлено, иначе в браузере. */
    fun openTelegram(ctx: Context, link: String) {
        val u = Uri.parse(link)
        if (u.host == "t.me" || u.host == "telegram.me") {
            val domain = u.pathSegments.firstOrNull()
            if (domain != null) {
                val tg = Uri.parse("tg://resolve?domain=$domain" + (u.getQueryParameter("start")?.let { "&start=$it" } ?: ""))
                val i = Intent(Intent.ACTION_VIEW, tg).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (runCatching { ctx.startActivity(i) }.isSuccess) return
            }
        }
        openUrl(ctx, link)
    }
}
