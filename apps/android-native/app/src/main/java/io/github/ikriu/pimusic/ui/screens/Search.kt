package io.github.ikriu.pimusic.ui.screens

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Album
import androidx.compose.material.icons.rounded.Download
import androidx.compose.material.icons.rounded.History
import androidx.compose.material.icons.rounded.Link
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.Person
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.CircularWavyProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.CTrack
import io.github.ikriu.pimusic.data.DownloadResp
import io.github.ikriu.pimusic.data.SearchResult
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.Cover
import io.github.ikriu.pimusic.ui.EmptyState
import io.github.ikriu.pimusic.ui.ErrorState
import io.github.ikriu.pimusic.ui.InLibraryIcon
import io.github.ikriu.pimusic.ui.LoadingBox
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.SectionTitle
import io.github.ikriu.pimusic.ui.Tab
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** История поиска (8 последних) — на устройстве. */
private fun history(ctx: Context): List<String> = ctx.getSharedPreferences("search", Context.MODE_PRIVATE).getString("h", "")!!.split("\n").filter { it.isNotBlank() }
private fun saveHistory(ctx: Context, q: String) {
    val h = (listOf(q) + history(ctx).filter { !it.equals(q, true) }).take(8)
    ctx.getSharedPreferences("search", Context.MODE_PRIVATE).edit().putString("h", h.joinToString("\n")).apply()
}

fun isUrl(q: String) = Regex("^https?://\\S+$", RegexOption.IGNORE_CASE).matches(q.trim())

/** Поставить в загрузку на сервер (тело как у /api/download); ключи — для прогресса в рядах. */
fun AppState.download(token: String, kind: String, keys: List<String?>, i: Int = 0, idx: List<Int>? = null, playlistId: Long? = null, j: Int = 0) = run {
    val r = app.api.post("/download", buildJsonObject {
        put("token", token); put("kind", kind); put("i", i); put("j", j)
        if (idx != null) put("idx", JsonArray(idx.map { JsonPrimitive(it) }))
        if (playlistId != null) put("playlist_id", playlistId)
    }, DownloadResp.serializer())
    markPending(keys.filterNotNull())
    msg(if (r.queued == 1) "В очереди: ${r.title}" else "В очереди: ${r.title} — ${r.queued} тр.", "Загрузки") { select(Tab.DOWNLOADS) }
}

/** Ряд трека каталога: обложка, название, кнопка состояния download → волнистый прогресс → ✓ (в медиатеке). */
@Composable
fun CTrackRow(st: AppState, t: CTrack, onDownload: () -> Unit, number: Int? = null) {
    val prev by st.preview.playing.collectAsState()
    val inLib = (t.key != null && t.key in st.libKeys) || (t.isrc != null && t.isrc in st.libKeys)
    val pending = t.key != null && t.key in st.pending
    Row(
        Modifier.fillMaxWidth().heightIn(min = 64.dp).clickable(enabled = t.preview != null) { t.preview?.let { st.preview.toggle(it) } }
            .padding(start = 16.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (number != null) Text(number.toString(), Modifier.width(32.dp), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        else Box(contentAlignment = Alignment.Center) {
            Cover(t.cover, 56.dp)
            if (prev != null && prev == t.preview) Icon(Icons.Rounded.Pause, "Остановить превью", tint = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(t.title, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis, color = if (prev == t.preview && prev != null) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface)
            val act = if (pending) st.activeFor(t.artist, t.title) else null
            Text(act?.let { "Качается" + (it.progress?.let { p -> " · $p" } ?: "…") }
                ?: listOfNotNull(t.artist.ifEmpty { null }, t.album?.ifEmpty { null }, t.duration.ifEmpty { null }).joinToString(" · "),
                style = MaterialTheme.typography.bodyMedium, color = if (act != null) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
            when {
                inLib -> InLibraryIcon()
                pending -> CircularWavyProgressIndicator(Modifier.size(28.dp))
                else -> IconButton(onClick = onDownload) { Icon(Icons.Rounded.Download, "Скачать «${t.title}»") }
            }
        }
    }
}

/** Поиск в каталоге: история, ссылка → «Скачать по ссылке», исполнитель, треки, альбомы, исполнители. */
@Composable
fun SearchScreen() {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var input by rememberSaveable { mutableStateOf("") }
    var res by remember { mutableStateOf<SearchResult?>(null) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var view by rememberSaveable { mutableStateOf("tracks") }
    var hist by remember { mutableStateOf(history(ctx)) }
    DisposableEffect(Unit) { onDispose { st.preview.stop() } }

    fun search(q: String) {
        val query = q.trim()
        if (query.isEmpty() || isUrl(query)) return
        input = query
        saveHistory(ctx, query); hist = history(ctx)
        loading = true; error = null
        scope.launch {
            try {
                res = st.app.api.get("/search?q=${enc(query)}", SearchResult.serializer())
                view = "tracks"
            } catch (e: ApiException) { error = e.message } finally { loading = false }
        }
    }

    Column(Modifier.fillMaxSize()) {
        TabHeader("Поиск")
        PillSearch(input, { input = it }, "Исполнитель, трек, альбом или ссылка", onSubmit = { search(input) })
        val r = res
        LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
            val joinToken = inviteToken(input)
            if (joinToken != null) item { JoinCard(st, joinToken) { input = "" } }
            else if (isUrl(input)) item {
                Surface(color = MaterialTheme.colorScheme.primaryContainer, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().padding(16.dp)) {
                    Column(Modifier.padding(20.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Rounded.Link, null); Spacer(Modifier.width(12.dp))
                            Text("Ссылка на трек, альбом или плейлист", style = MaterialTheme.typography.titleMedium)
                        }
                        Text("Spotify, YouTube, SoundCloud, Bandcamp, Яндекс Музыка… — как если бы её прислали боту.", style = MaterialTheme.typography.bodyMedium)
                        Spacer(Modifier.size(12.dp))
                        Button(onClick = {
                            st.run {
                                val d = st.app.api.post("/download/link", buildJsonObject { put("url", input.trim()) }, DownloadResp.serializer())
                                st.msg("В очереди: ${d.title} — ${d.queued} тр." + (d.note?.let { " ($it)" } ?: ""), "Загрузки") { st.select(Tab.DOWNLOADS) }
                                input = ""
                            }
                        }) { Icon(Icons.Rounded.Download, null); Spacer(Modifier.width(6.dp)); Text("Скачать по ссылке") }
                    }
                }
            }
            if (r == null && !loading && error == null && !isUrl(input)) {
                if (hist.isNotEmpty()) {
                    item { SectionTitle("Недавние запросы") }
                    item {
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp), contentPadding = PaddingValues(horizontal = 16.dp)) {
                            items(hist) { h -> AssistChip({ search(h) }, label = { Text(h) }, leadingIcon = { Icon(Icons.Rounded.History, null, Modifier.size(18.dp)) }) }
                        }
                    }
                }
                item { EmptyState(Icons.Rounded.Search, "Найдите музыку", "Нажмите «Скачать» — трек появится в медиатеке. Тап по треку — 30-секундное превью.") }
            }
            if (loading) item { LoadingBox(Modifier.fillMaxWidth()) }
            error?.let { e -> item { ErrorState(e) { search(input) } } }
            if (r != null && !loading) {
                if (r.artist != null) item {
                    Surface(
                        onClick = { r.artistId?.let { st.push(Screen.CatalogArtist(it, r.artist)) } },
                        color = MaterialTheme.colorScheme.surfaceContainerHigh, shape = MaterialTheme.shapes.extraLarge,
                        modifier = Modifier.fillMaxWidth().padding(16.dp),
                    ) {
                        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                            Cover(r.picture, 72.dp, CircleShape, icon = Icons.Rounded.Person)
                            Spacer(Modifier.width(16.dp))
                            Column {
                                Text(r.artist, style = MaterialTheme.typography.titleLarge)
                                if (r.fans > 0) Text("${r.fans} слушателей", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
                item {
                    ConnectedGroup(
                        listOf("tracks" to "Треки ${r.tracks.size}", "albums" to "Альбомы ${r.albums.size}", "artists" to "Исполнители ${r.artists.size}"),
                        view, { view = it }, Modifier.padding(horizontal = 16.dp, vertical = 4.dp), short = mapOf("artists" to "Артисты ${r.artists.size}"),
                    )
                }
                when (view) {
                    "tracks" -> {
                        if (r.tracks.isNotEmpty()) item {
                            Row(Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) {
                                Button(onClick = { st.download(r.token, "all", r.tracks.map { it.key }) }) { Icon(Icons.Rounded.Download, null); Spacer(Modifier.width(6.dp)); Text("Скачать все") }
                            }
                        }
                        items(r.tracks, key = { "t" + it.i }) { t -> CTrackRow(st, t, { st.download(r.token, "track", listOf(t.key), i = t.i) }) }
                        if (r.tracks.isEmpty()) item { EmptyState(Icons.Rounded.Search, "Треков не нашлось") }
                    }
                    "albums" -> items(r.albums, key = { "a" + it.i }) { a ->
                        Row(
                            Modifier.fillMaxWidth().clickable { st.push(Screen.CatalogAlbum(a.id, r.token, a.i, a.title)) }.padding(horizontal = 16.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Cover(a.cover, 64.dp, icon = Icons.Rounded.Album)
                            Spacer(Modifier.width(16.dp))
                            Column(Modifier.weight(1f)) {
                                Text(a.title, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text(listOfNotNull(a.artist, a.year, kindLabel(a.kind)).joinToString(" · "), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                            }
                        }
                    }
                    else -> items(r.artists, key = { "r" + (it.id ?: it.name) }) { a ->
                        Row(
                            Modifier.fillMaxWidth().clickable { a.id?.let { st.push(Screen.CatalogArtist(it, a.name)) } }.padding(horizontal = 16.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Cover(a.picture, 56.dp, CircleShape, icon = Icons.Rounded.Person)
                            Spacer(Modifier.width(16.dp))
                            Column {
                                Text(a.name, style = MaterialTheme.typography.titleMedium)
                                if (a.fans > 0) Text("${a.fans} слушателей", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }
    }
}

/** Токен приглашения в плейлист из ссылки (…/app/#join=<t> или t.me/<бот>?start=pl_<t>). */
fun inviteToken(text: String): String? =
    Regex("(?:#join=|[?&]start=pl_)([A-Za-z0-9_-]{6,64})").find(text.trim())?.groupValues?.get(1)

/** Вступление в общий плейлист по ссылке-приглашению: превью + «Присоединиться». */
@Composable
private fun JoinCard(st: AppState, token: String, done: () -> Unit) {
    val l = io.github.ikriu.pimusic.ui.rememberLoader("/playlists/join?token=${enc(token)}", io.github.ikriu.pimusic.data.JoinPreview.serializer(), cache = false)
    Surface(color = MaterialTheme.colorScheme.tertiaryContainer, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().padding(16.dp)) {
        Column(Modifier.padding(20.dp)) {
            val p = l.data
            when {
                p != null -> {
                    Text("Плейлист «${p.name}»", style = MaterialTheme.typography.titleMedium)
                    Text("от ${p.owner} · ${io.github.ikriu.pimusic.core.nTracks(p.count.toLong())}", style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.size(12.dp))
                    Button(onClick = {
                        if (p.already) { st.push(Screen.PlaylistPage(p.id, p.name)); done() }
                        else st.run {
                            val r = st.app.api.post("/playlists/join", buildJsonObject { put("token", token) }, io.github.ikriu.pimusic.data.Playlist.serializer())
                            st.libraryChanged(); done()
                            st.push(Screen.PlaylistPage(r.id, r.name))
                        }
                    }) { Text(if (p.already) "Открыть" else "Присоединиться") }
                }
                l.loading -> LoadingBox(Modifier.fillMaxWidth())
                else -> Text(if (l.notFound) "Приглашение недействительно" else (l.error ?: "Ошибка"), style = MaterialTheme.typography.bodyLarge)
            }
        }
    }
}

fun kindLabel(k: String) = when (k) { "single" -> "Сингл"; "ep" -> "EP"; "compile" -> "Сборник"; else -> "Альбом" }
