package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.automirrored.rounded.PlaylistAdd
import androidx.compose.material.icons.rounded.Album
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Person
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.fmtDuration
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.LibAlbumPage
import io.github.ikriu.pimusic.data.LibArtistPage
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.Cover
import io.github.ikriu.pimusic.ui.Loaded
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.SectionTitle
import io.github.ikriu.pimusic.ui.TrackMenu
import io.github.ikriu.pimusic.ui.TrackRow
import io.github.ikriu.pimusic.ui.rememberCoverSource
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.player.PlaySource
import io.github.ikriu.pimusic.ui.Selection
import io.github.ikriu.pimusic.ui.SelectionHeader
import io.github.ikriu.pimusic.ui.SelectionToolbar
import io.github.ikriu.pimusic.ui.rememberSelection
import io.github.ikriu.pimusic.ui.trackActions
import androidx.activity.compose.BackHandler
import io.github.ikriu.pimusic.ui.theme.ContentTheme

/** Верхняя панель страницы: «Назад» + заголовок (появляется при прокрутке) + действия. */
@Composable
fun PageBar(title: String, actions: @Composable () -> Unit = {}) {
    val st = LocalAppState.current
    Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = { st.back() }) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Назад") }
        Text(title, style = MaterialTheme.typography.titleLarge, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        actions()
    }
}

/** Шапка страницы (large flexible app bar): обложка 240dp, заголовок headline emphasized, подзаголовок, кнопки. */
@Composable
fun PageHeader(cover: String?, title: String, subtitle: String?, meta: String?, shape: Shape = RoundedCornerShape(28.dp), icon: ImageVector = Icons.Rounded.Album, onSubtitle: (() -> Unit)? = null, coverContent: (@Composable () -> Unit)? = null) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        if (coverContent != null) Box(Modifier.size(240.dp)) { coverContent() } else Cover(cover, 240.dp, shape, icon = icon)
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.SemiBold), textAlign = TextAlign.Center)
        if (subtitle != null) {
            if (onSubtitle != null) androidx.compose.material3.TextButton(onClick = onSubtitle) { Text(subtitle) }
            else Text(subtitle, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        }
        if (meta != null) Text(meta, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

fun metaOf(tracks: List<Track>, year: String? = null) =
    listOfNotNull(year, nTracks(tracks.size.toLong()), fmtDuration(tracks.sumOf { it.seconds }).takeIf { tracks.isNotEmpty() }).joinToString(" · ")

/** Список треков с подсветкой играющего; тап — играть этот список с трека. */
fun LazyListScope.trackItems(st: AppState, tracks: List<Track>, source: PlaySource, playingId: Long?, playing: Boolean, offline: Set<Long>, numbered: Boolean = false, showAlbum: Boolean = true, sel: Selection? = null, playQueue: List<Track> = tracks, menu: (Track) -> TrackMenu = { TrackMenu(it) }) {
    itemsIndexed(tracks, key = { i, t -> "${t.itemId ?: t.id}:$i" }) { i, t ->
        val selecting = sel?.active == true
        TrackRow(
            t, playing = playingId == t.id, animating = playing,
            onClick = {
                if (selecting) sel!!.toggle(t)
                else if (playingId == t.id) st.openPlayer() else st.app.player.playList(playQueue, playQueue.indexOf(t).coerceAtLeast(0), false, source)
            },
            onMore = { st.menu = menu(t) }, number = if (numbered) i + 1 else null, showAlbum = showAlbum, offline = t.id in offline,
            selected = if (selecting) sel!!.isSelected(t) else null,
        )
    }
}

fun albumSrc(a: LibAlbumPage) = PlaySource("album", a.title, a = a.artistKey, b = a.key)

/** Свой исполнитель: альбомы с треками, «Слушать/Перемешать», скачать всё на устройство. */
@Composable
fun LibArtistScreen(s: Screen.LibArtist) {
    val st = LocalAppState.current
    val l = rememberLoader("/library/artist?a=${enc(s.key)}", LibArtistPage.serializer())
    val ui by st.app.player.ui.collectAsState()
    val offline by st.app.offline.items.collectAsState()
    val done = offline.filterValues { it.done }.keys
    val src = rememberCoverSource(l.data?.albums?.firstOrNull()?.cover)
    val sel = rememberSelection()
    BackHandler(sel.active) { sel.stop() }
    ContentTheme(src) {
        Surface(Modifier.fillMaxSize()) {
            Column {
                val all0 = l.data?.albums?.flatMap { it.tracks }.orEmpty()
                if (sel.active) SelectionHeader(sel, all0) else PageBar(l.data?.name ?: s.name) { if (l.data != null) SelectButton { sel.start() } }
                Loaded(l) { a ->
                    val all = a.albums.flatMap { it.tracks }
                    val artistSrc = PlaySource("artist", a.name, a = a.key)
                    val list = rememberLazyListState()
                    Box(Modifier.fillMaxSize()) {
                    LazyColumn(Modifier.fillMaxSize(), state = list, contentPadding = PaddingValues(bottom = 88.dp)) {
                        item { PageHeader(a.image ?: a.albums.firstOrNull()?.cover, a.name, null, "${a.albums.size} альб. · ${metaOf(all)}", CircleShape, Icons.Rounded.Person) }
                        item {
                            PlayButtons({ st.app.player.playList(all, 0, false, artistSrc) }, { st.app.player.playList(all, 0, true, artistSrc) }, Modifier.fillMaxWidth(), extraCount = 2) {
                                FilledTonalIconButton(onClick = { st.saveOffline(all, a.name) }) { Icon(Icons.Rounded.DownloadForOffline, "Скачать на устройство") }
                                FilledTonalIconButton(onClick = { st.addToPlaylist = all }) { Icon(Icons.AutoMirrored.Rounded.PlaylistAdd, "В плейлист") }
                            }
                        }
                        for (al in a.albums) {
                            item(key = "h" + al.key) {
                                Row(Modifier.padding(start = 16.dp, top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Cover(al.cover, 40.dp)
                                    SectionTitle(al.title.ifEmpty { "Без альбома" }, Modifier.weight(1f))
                                }
                            }
                            trackItems(st, al.tracks, artistSrc, ui.current?.id, ui.playing, done, numbered = true, showAlbum = false, sel = sel, playQueue = all)
                        }
                    }
                    SelectionToolbar(sel, all, trackActions(st, sel), Modifier.align(Alignment.BottomCenter))
                    }
                }
            }
        }
    }
}

/** Свой альбом: шапка в контент-цвете обложки, треклист с номерами. */
@Composable
fun LibAlbumScreen(s: Screen.LibAlbum) {
    val st = LocalAppState.current
    val l = rememberLoader("/library/album?a=${enc(s.a)}&b=${enc(s.b)}", LibAlbumPage.serializer())
    val ui by st.app.player.ui.collectAsState()
    val offline by st.app.offline.items.collectAsState()
    val done = offline.filterValues { it.done }.keys
    val src = rememberCoverSource(l.data?.cover ?: s.cover)
    val sel = rememberSelection()
    BackHandler(sel.active) { sel.stop() }
    ContentTheme(src) {
        Surface(Modifier.fillMaxSize()) {
            Column {
                if (sel.active) SelectionHeader(sel, l.data?.tracks.orEmpty()) else PageBar(l.data?.title ?: s.title) { if (l.data != null) SelectButton { sel.start() } }
                Loaded(l) { a ->
                    Box(Modifier.fillMaxSize()) {
                    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
                        item {
                            PageHeader(a.cover, a.title, a.artist, metaOf(a.tracks, a.tracks.firstNotNullOfOrNull { it.year }?.toString()),
                                onSubtitle = { st.push(Screen.LibArtist(a.artistKey, a.artist)) })
                        }
                        item {
                            PlayButtons({ st.app.player.playList(a.tracks, 0, false, albumSrc(a)) }, { st.app.player.playList(a.tracks, 0, true, albumSrc(a)) }, Modifier.fillMaxWidth(), extraCount = 2) {
                                FilledTonalIconButton(onClick = { st.saveOffline(a.tracks, a.title) }) { Icon(Icons.Rounded.DownloadForOffline, "Скачать на устройство") }
                                FilledTonalIconButton(onClick = { st.addToPlaylist = a.tracks }) { Icon(Icons.AutoMirrored.Rounded.PlaylistAdd, "В плейлист") }
                            }
                        }
                        trackItems(st, a.tracks, albumSrc(a), ui.current?.id, ui.playing, done, numbered = true, showAlbum = false, sel = sel)
                    }
                    SelectionToolbar(sel, a.tracks, trackActions(st, sel), Modifier.align(Alignment.BottomCenter))
                    }
                }
            }
        }
    }
}
