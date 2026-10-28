package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.PlaylistAdd
import androidx.compose.material.icons.rounded.Album
import androidx.compose.material.icons.rounded.Download
import androidx.compose.material.icons.rounded.Person
import androidx.compose.material3.Button
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.CAlbum
import io.github.ikriu.pimusic.data.CTrack
import io.github.ikriu.pimusic.data.CatalogAlbum
import io.github.ikriu.pimusic.data.CatalogArtist
import io.github.ikriu.pimusic.data.Playlist
import io.github.ikriu.pimusic.data.SearchAlbum
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.ConfirmDialog
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.Cover
import io.github.ikriu.pimusic.ui.Loaded
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.NewPlaylistDialog
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.rememberCoverSource
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.ui.theme.ContentTheme
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** Новый плейлист из поиска/каталога: наполняется по мере загрузки (POST /api/playlists с catalog). */
fun AppState.playlistFromCatalog(name: String, shared: Boolean, token: String, kind: String, i: Int = 0) = run {
    val p = app.api.post("/playlists", buildJsonObject {
        put("name", name); put("shared", shared)
        put("catalog", buildJsonObject { put("token", token); put("kind", kind); put("i", i) })
    }, Playlist.serializer())
    markPending(emptyList())
    msg("Плейлист «${p.name}» создан — треки добавятся по мере загрузки", "Открыть") { push(Screen.PlaylistPage(p.id, p.name)) }
    libraryChanged()
}

/** Альбом каталога: шапка в цвете обложки, «Скачать альбом» / «Только недостающие», новый плейлист, треклист. */
@Composable
fun CatalogAlbumScreen(s: Screen.CatalogAlbum) {
    val st = LocalAppState.current
    // по id — /api/catalog/album (свой token); без id — альбом из результата поиска (/api/album?token&i)
    val byId = rememberLoader(s.id?.let { "/catalog/album?id=${enc(it)}" }, CatalogAlbum.serializer(), cache = false)
    val byToken = rememberLoader(if (s.id == null && s.token != null) "/album?token=${enc(s.token)}&i=${s.i}" else null, SearchAlbum.serializer(), cache = false)
    var newPl by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize()) {
        if (s.id != null) {
            Loaded(byId) { a -> AlbumBody(st, a.title, a.artist, a.artistId, a.year, a.cover, a.tracks, a.token, false) { newPl = true } }
            byId.data?.let { a -> if (newPl) NewPlaylistDialog(st, { newPl = false }, initial = "${a.artist} — ${a.title}") { n, sh -> newPl = false; st.playlistFromCatalog(n, sh, a.token, "all") } }
        } else {
            Loaded(byToken) { a -> AlbumBody(st, a.title, a.artist, null, a.year, a.tracks.firstOrNull()?.cover, a.tracks, s.token!!, true, s.i) { newPl = true } }
            byToken.data?.let { a -> if (newPl) NewPlaylistDialog(st, { newPl = false }, initial = "${a.artist} — ${a.title}") { n, sh -> newPl = false; st.playlistFromCatalog(n, sh, s.token!!, "album", s.i) } }
        }
    }
}

@Composable
private fun AlbumBody(st: AppState, title: String, artist: String, artistId: String?, year: String?, cover: String?, tracks: List<CTrack>, token: String, fromSearch: Boolean, albumIndex: Int = 0, onNewPlaylist: () -> Unit) {
    val src = rememberCoverSource(cover)
    val missing = tracks.filter { t -> !((t.key != null && t.key in st.libKeys) || (t.isrc != null && t.isrc in st.libKeys)) }
    ContentTheme(src) {
        Surface(Modifier.fillMaxSize()) {
            Column {
                PageBar(title)
                LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
                    item {
                        PageHeader(cover, title, artist, listOfNotNull(year, nTracks(tracks.size.toLong())).joinToString(" · "),
                            onSubtitle = artistId?.let { id -> { st.push(Screen.CatalogArtist(id, artist)) } })
                    }
                    item {
                        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Button(onClick = {
                                if (fromSearch) st.download(token, "album", tracks.map { it.key }, i = albumIndex) else st.download(token, "all", tracks.map { it.key })
                            }, modifier = Modifier.weight(1f).heightIn(min = 56.dp), contentPadding = PaddingValues(horizontal = 12.dp)) {
                                Icon(Icons.Rounded.Download, null); Spacer(Modifier.width(8.dp))
                                io.github.ikriu.pimusic.ui.FitText("Скачать альбом", MaterialTheme.typography.titleMedium, minScale = 0.7f)
                            }
                            FilledTonalIconButton(onClick = onNewPlaylist, modifier = Modifier.size(56.dp)) { Icon(Icons.AutoMirrored.Rounded.PlaylistAdd, "Новый плейлист из альбома") }
                        }
                        // «Только недостающие» — отдельной строкой: на узком экране с крупным шрифтом не режется
                        if (!fromSearch && missing.isNotEmpty() && missing.size < tracks.size) FilledTonalButton(onClick = {
                            st.download(token, "tracks", missing.map { it.key }, idx = missing.map { it.i })
                        }, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp).heightIn(min = 48.dp)) {
                            Text("Скачать только недостающие · ${missing.size}", maxLines = 2, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
                        }
                    }
                    items(tracks, key = { "t" + it.i }) { t ->
                        CTrackRow(st, t, {
                            if (fromSearch) st.download(token, "album_track", listOf(t.key), i = albumIndex, j = t.i) else st.download(token, "track", listOf(t.key), i = t.i)
                        }, number = (t.no ?: (t.i + 1)))
                    }
                }
            }
        }
    }
}

/** Исполнитель каталога: фото, «N слушателей», Популярное | Дискография | Все треки, «Скачать всё». */
@Composable
fun CatalogArtistScreen(s: Screen.CatalogArtist) {
    val st = LocalAppState.current
    val l = rememberLoader("/catalog/artist?id=${enc(s.id)}", CatalogArtist.serializer(), cache = false)
    var tab by rememberSaveable { mutableStateOf("top") }
    var confirmAll by remember { mutableStateOf(false) }
    val all = rememberLoader(if (tab == "all" || confirmAll) "/catalog/artist/tracks?id=${enc(s.id)}" else null, AllTracks.serializer(), cache = false)
    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            PageBar(l.data?.name ?: s.name)
            Loaded(l) { a ->
                LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 96.dp)) {
                    item { PageHeader(a.picture, a.name, null, if (a.fans > 0) "${a.fans} слушателей" else null, CircleShape, Icons.Rounded.Person) }
                    item {
                        ConnectedGroup(listOf("top" to "Популярное", "albums" to "Дискография", "all" to "Все треки"), tab, { tab = it }, Modifier.padding(16.dp))
                    }
                    when (tab) {
                        "top" -> items(a.tracks, key = { "t" + it.i }) { t -> CTrackRow(st, t, { st.download(a.token, "track", listOf(t.key), i = t.i) }) }
                        "albums" -> {
                            item { DiscoCarousel(st, a.albums.filter { it.kind == "album" }, "Альбомы") }
                            item { DiscoCarousel(st, a.albums.filter { it.kind != "album" }, "Синглы и EP") }
                        }
                        else -> {
                            val d = all.data
                            if (d == null) item { if (all.loading) io.github.ikriu.pimusic.ui.LoadingBox(Modifier.fillMaxWidth()) else io.github.ikriu.pimusic.ui.ErrorState(all.error ?: "Ошибка") { all.reload() } }
                            else {
                                item {
                                    Text("${nTracks(d.tracks.size.toLong())} из ${d.albums} альб." + if (d.capped) " (показаны не все)" else "",
                                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 16.dp))
                                }
                                items(d.tracks, key = { "a" + it.i }) { t -> CTrackRow(st, t, { st.download(d.token, "track", listOf(t.key), i = t.i) }) }
                            }
                        }
                    }
                }
            }
        }
        if (l.data != null) ExtendedFloatingActionButton(
            onClick = { confirmAll = true }, icon = { Icon(Icons.Rounded.Download, null) }, text = { Text("Скачать всё") },
            modifier = Modifier.align(Alignment.BottomEnd).padding(16.dp),
        )
    }
    if (confirmAll) {
        val d = all.data
        ConfirmDialog(
            "Скачать всё?",
            if (d == null) "Собираю дискографию…" else "${nTracks(d.tracks.size.toLong())} исполнителя «${d.name}» встанут в очередь загрузки. Уже скачанные пропускаются.",
            "Скачать", { confirmAll = false },
        ) {
            confirmAll = false
            if (d != null) st.download(d.token, "all", d.tracks.map { it.key })
        }
    }
}

@kotlinx.serialization.Serializable
data class AllTracks(val token: String, val name: String = "", val albums: Int = 0, val capped: Boolean = false, val tracks: List<CTrack> = emptyList())

/** Карусель дискографии (multi-browse): карточки 160dp. */
@Composable
private fun DiscoCarousel(st: AppState, albums: List<CAlbum>, title: String) {
    if (albums.isEmpty()) return
    Column {
        io.github.ikriu.pimusic.ui.SectionTitle(title)
        LazyRow(horizontalArrangement = Arrangement.spacedBy(12.dp), contentPadding = PaddingValues(horizontal = 16.dp)) {
            items(albums, key = { it.id ?: it.title }) { a ->
                Column(Modifier.width(160.dp).clickable { st.push(Screen.CatalogAlbum(a.id, null, 0, a.title)) }) {
                    Cover(a.cover, 160.dp, RoundedCornerShape(28.dp), icon = Icons.Rounded.Album)
                    Spacer(Modifier.height(8.dp))
                    Text(a.title, style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    Text(listOfNotNull(a.year, kindLabel(a.kind)).joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}
