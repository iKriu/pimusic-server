package io.github.ikriu.pimusic.ui.screens

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Add
import androidx.compose.material.icons.rounded.Checklist
import androidx.compose.material.icons.rounded.Album
import androidx.compose.material.icons.rounded.CloudOff
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Group
import androidx.compose.material.icons.rounded.LibraryMusic
import androidx.compose.material.icons.rounded.MyLocation
import androidx.compose.material.icons.rounded.Person
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material.icons.rounded.Settings
import androidx.compose.material.icons.rounded.Shuffle
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.fmtDuration
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.Genres
import io.github.ikriu.pimusic.data.LibAlbums
import io.github.ikriu.pimusic.data.LibArtists
import io.github.ikriu.pimusic.data.LibraryPage
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Playlist
import io.github.ikriu.pimusic.data.Playlists
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.data.TrackList
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.Cover
import io.github.ikriu.pimusic.ui.EmptyState
import io.github.ikriu.pimusic.ui.ErrorState
import io.github.ikriu.pimusic.ui.LoadingBox
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.NewPlaylistDialog
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.Tab
import io.github.ikriu.pimusic.ui.TrackMenu
import io.github.ikriu.pimusic.ui.TrackRow
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.player.PlaySource
import io.github.ikriu.pimusic.ui.SelectionHeader
import io.github.ikriu.pimusic.ui.SelectionToolbar
import io.github.ikriu.pimusic.ui.rememberSelection
import io.github.ikriu.pimusic.ui.trackActions
import androidx.activity.compose.BackHandler
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URLEncoder

fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

/** Шапка вкладки: крупный заголовок, офлайн-метка, «Скачанное», «Настройки». */
@Composable
fun TabHeader(title: String, extra: @Composable () -> Unit = {}) {
    val st = LocalAppState.current
    Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 4.dp, top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.SemiBold), modifier = Modifier.weight(1f))
        if (st.offlineMode) Icon(Icons.Rounded.CloudOff, "Офлайн", tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(end = 8.dp))
        extra()
        IconButton(onClick = { st.push(Screen.Offline) }) { Icon(Icons.Rounded.DownloadForOffline, "Скачанное") }
        IconButton(onClick = { st.push(Screen.Settings) }) { Icon(Icons.Rounded.Settings, "Настройки") }
    }
}

/** Поле поиска-«пилюля». */
@Composable
fun PillSearch(value: String, onChange: (String) -> Unit, placeholder: String, modifier: Modifier = Modifier, onSubmit: (() -> Unit)? = null) {
    OutlinedTextField(
        value, onChange, modifier = modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        placeholder = { Text(placeholder) }, singleLine = true, shape = RoundedCornerShape(28.dp),
        leadingIcon = { Icon(Icons.Rounded.Search, null) },
        keyboardActions = androidx.compose.foundation.text.KeyboardActions(onSearch = { onSubmit?.invoke() }),
        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(imeAction = androidx.compose.ui.text.input.ImeAction.Search),
    )
}

/** Кнопка «Выбрать» (режим выбора треков). */
@Composable
fun SelectButton(onClick: () -> Unit) {
    IconButton(onClick = onClick) { Icon(Icons.Rounded.Checklist, "Выбрать треки") }
}

/** Кнопки «Слушать» (filled) и «Перемешать» (tonal). */
@Composable
fun PlayButtons(onPlay: () -> Unit, onShuffle: () -> Unit, modifier: Modifier = Modifier, extraCount: Int = 0, extra: @Composable () -> Unit = {}) {
    // M3E medium buttons (56dp, как в Mini App) на всю ширину с равными весами; доп. действия — в той же строке
    val measurer = androidx.compose.ui.text.rememberTextMeasurer()
    val density = androidx.compose.ui.platform.LocalDensity.current
    val style = MaterialTheme.typography.titleMedium
    BoxWithConstraints(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
        val extras = extraCount
        val textW = with(density) { measurer.measure("Перемешать", style, maxLines = 1).size.width.toDp() }
        val per = (maxWidth - (56.dp + 8.dp) * extras - 8.dp) / 2
        val tight = textW + 24.dp + 8.dp + 24.dp > per
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = onPlay, modifier = Modifier.weight(1f).heightIn(min = 56.dp), contentPadding = PaddingValues(horizontal = 12.dp)) {
                Icon(Icons.Rounded.PlayArrow, null); Spacer(Modifier.width(8.dp))
                io.github.ikriu.pimusic.ui.FitText("Слушать", style, minScale = 0.75f)
            }
            if (tight) androidx.compose.material3.FilledTonalIconButton(onClick = onShuffle, modifier = Modifier.size(56.dp)) { Icon(Icons.Rounded.Shuffle, "Перемешать") }
            else FilledTonalButton(onClick = onShuffle, modifier = Modifier.weight(1f).heightIn(min = 56.dp), contentPadding = PaddingValues(horizontal = 12.dp)) {
                Icon(Icons.Rounded.Shuffle, null); Spacer(Modifier.width(8.dp))
                io.github.ikriu.pimusic.ui.FitText("Перемешать", style, minScale = 0.75f)
            }
            extra()
        }
    }
}

/** Строка над списком: счётчик слева (по центру строки), справа — чипы сортировки/выбора. Высота 48dp. */
@Composable
fun ListToolbar(count: String, modifier: Modifier = Modifier, actions: @Composable () -> Unit) {
    Row(modifier.fillMaxWidth().heightIn(min = 48.dp).padding(start = 16.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(count, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f), maxLines = 2)
        actions()
    }
}

/** Кнопка «к текущему треку»: видна, когда играющий трек есть в списке и он вне экрана. */
@Composable
fun LocateFab(list: LazyListState, index: Int?, modifier: Modifier = Modifier) {
    val scope = rememberCoroutineScope()
    val visible by remember(index) {
        derivedStateOf { index != null && list.layoutInfo.visibleItemsInfo.none { it.index == index } }
    }
    if (visible && index != null) SmallFloatingActionButton(onClick = { scope.launch { list.animateScrollToItem(index) } }, modifier = modifier.padding(16.dp)) {
        Icon(Icons.Rounded.MyLocation, "К текущему треку")
    }
}

/** Медиатека: Треки · Исполнители · Альбомы · Плейлисты, поиск, сортировки и жанры. */
@Composable
fun LibraryScreen() {
    val st = LocalAppState.current
    var view by rememberSaveable { mutableStateOf("tracks") }
    var q by rememberSaveable { mutableStateOf("") }
    val views = listOf("tracks" to "Треки", "artists" to "Исполнители", "albums" to "Альбомы", "playlists" to "Плейлисты")
    val order = views.map { it.first }
    val motion = MaterialTheme.motionScheme
    val shift = with(androidx.compose.ui.platform.LocalDensity.current) { 32.dp.roundToPx() }
    Column(Modifier.fillMaxSize()) {
        TabHeader("Медиатека")
        // поиск у плейлистов не нужен — сворачивается, а не пропадает рывком (содержимое ниже не прыгает)
        AnimatedVisibility(
            view != "playlists",
            enter = expandVertically(motion.defaultSpatialSpec()) + fadeIn(motion.defaultEffectsSpec()),
            exit = shrinkVertically(motion.defaultSpatialSpec()) + fadeOut(motion.fastEffectsSpec()),
        ) { PillSearch(q, { q = it }, "Поиск в медиатеке") }
        ConnectedGroup(
            views, view, { view = it },
            Modifier.padding(horizontal = 16.dp, vertical = 4.dp), compact = true, short = mapOf("artists" to "Артисты"),
            icons = mapOf("tracks" to Icons.Rounded.LibraryMusic, "artists" to Icons.Rounded.Person, "albums" to Icons.Rounded.Album, "playlists" to Icons.AutoMirrored.Rounded.QueueMusic),
        )
        // M3 shared axis X: прежний вид быстро гаснет со сдвигом против направления, новый въезжает пружиной
        AnimatedContent(
            view, Modifier.fillMaxSize(), label = "libView",
            transitionSpec = {
                val dir = if (order.indexOf(targetState) > order.indexOf(initialState)) 1 else -1
                (slideInHorizontally(motion.defaultSpatialSpec()) { dir * shift } + fadeIn(motion.defaultEffectsSpec())) togetherWith
                    (slideOutHorizontally(motion.fastSpatialSpec()) { -dir * shift } + fadeOut(motion.fastEffectsSpec()))
            },
        ) { v ->
            when (v) {
                "tracks" -> LibTracks(st, q)
                "artists" -> LibArtistsGrid(st, q)
                "albums" -> LibAlbumsGrid(st, q)
                else -> PlaylistsGrid(st)
            }
        }
    }
}

@Composable
private fun LibTracks(st: AppState, qRaw: String) {
    val ctx = LocalContext.current
    val store = remember { SortStore(ctx) }
    var sort by remember { mutableStateOf(store.get("tracks", Sorts.TRACKS)) }
    var genre by remember { mutableStateOf(store.genre("tracks")) }
    var q by remember { mutableStateOf(qRaw) }
    LaunchedEffect(qRaw) { delay(300); q = qRaw }
    val genres = rememberLoader("/library/genres", Genres.serializer())
    val items = remember { mutableStateListOf<Track>() }
    var total by remember { mutableIntStateOf(0) }
    var libTotal by remember { mutableIntStateOf(0) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var end by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val base = "/library?sort=${sort.sort}&order=${sort.order}&genre=${enc(genre)}&q=${enc(q)}" + (if (sort.sort == "random") "&seed=${sort.seed}" else "")
    suspend fun page(offset: Int) {
        val path = "$base&offset=$offset&limit=200"
        loading = true
        try {
            if (offset == 0) {
                st.app.apiCache.read(path)?.let { txt ->
                    runCatching { PiJson.decodeFromString(LibraryPage.serializer(), txt) }.getOrNull()?.let { p ->
                        items.clear(); items.addAll(p.items); total = p.total; libTotal = p.libraryTotal
                    }
                }
            }
            if (st.offlineMode) { end = true; if (items.isEmpty()) error = "Нет сети — откройте «Скачанное»"; return }
            val txt = st.app.api.getText(path)
            val p = PiJson.decodeFromString(LibraryPage.serializer(), txt)
            if (offset == 0) { items.clear(); st.app.apiCache.write(path, txt) }
            items.addAll(p.items)
            total = p.total; libTotal = p.libraryTotal
            end = items.size >= p.total || p.items.isEmpty()
            error = null
        } catch (e: ApiException) {
            if (items.isEmpty()) error = e.message
            end = true
        } finally { loading = false }
    }
    LaunchedEffect(base, st.libraryVersion) { end = false; page(0) }
    val list = rememberLazyListState()
    val ui by st.app.player.ui.collectAsState()
    val offline by st.app.offline.items.collectAsState()
    val curIdx = ui.current?.id?.let { id -> items.indexOfFirst { it.id == id }.takeIf { it >= 0 } }
    LaunchedEffect(list, items.size, end) {
        snapshotFlowLast(list) { last -> if (!end && !loading && last >= items.size - 30 && items.isNotEmpty()) page(items.size) }
    }
    fun playAll(shuffle: Boolean) = st.run {
        if (st.offlineMode) { st.app.player.playList(items.toList(), 0, shuffle, PlaySource.LIBRARY); return@run }
        val s = if (shuffle) "random&seed=${System.currentTimeMillis() % 1_000_000}" else "${sort.sort}&order=${sort.order}" + (if (sort.sort == "random") "&seed=${sort.seed}" else "")
        val r = st.app.api.get("/library/play?sort=$s&genre=${enc(genre)}&q=${enc(q)}&limit=3000", TrackList.serializer())
        st.app.player.playList(r.items, 0, false, PlaySource.LIBRARY)
    }
    val sel = rememberSelection()
    BackHandler(sel.active) { sel.stop() }
    val fadeSpec = MaterialTheme.motionScheme.defaultEffectsSpec<Float>() // загруженное проявляется, а не появляется рывком
    Box(Modifier.fillMaxSize()) {
        LazyColumn(Modifier.fillMaxSize(), state = list, contentPadding = PaddingValues(bottom = 88.dp)) {
            item {
                if (sel.active) SelectionHeader(sel, items)
                else ListToolbar(if (q.isNotEmpty() || genre.isNotEmpty()) "Найдено: ${nTracks(total.toLong())} из $libTotal" else nTracks(libTotal.toLong())) {
                    SortChip("tracks", Sorts.TRACKS, sort) { sort = it }
                    SelectButton { sel.start() }
                }
            }
            item { GenreRow(genres.data?.items.orEmpty(), genre) { genre = it; store.setGenre("tracks", it) } }
            item { PlayButtons({ playAll(false) }, { playAll(true) }) }
            if (items.isEmpty()) item {
                when {
                    loading -> LoadingBox(Modifier.fillMaxWidth())
                    error != null -> ErrorState(error!!) { scope.launch { page(0) } }
                    q.isNotEmpty() || genre.isNotEmpty() -> EmptyState(Icons.Rounded.Search, "Ничего не нашлось", "Измените запрос или жанр.")
                    else -> EmptyState(Icons.Rounded.LibraryMusic, "Медиатека пуста", "Найдите музыку в поиске и нажмите «Скачать».", "К поиску", onAction = { st.select(Tab.SEARCH) })
                }
            }
            itemsIndexed(items, key = { i, t -> "${t.id}:$i" }) { i, t ->
                Box(Modifier.animateItem(fadeInSpec = fadeSpec, placementSpec = null, fadeOutSpec = null)) {
                TrackRow(
                    t, playing = ui.current?.id == t.id, animating = ui.playing,
                    onClick = {
                        if (sel.active) sel.toggle(t)
                        else if (ui.current?.id == t.id) st.openPlayer() else st.app.player.playList(items.toList(), i, false, PlaySource.LIBRARY)
                    },
                    onMore = { st.menu = TrackMenu(t) }, offline = offline[t.id]?.done == true,
                    selected = if (sel.active) sel.isSelected(t) else null,
                )
                }
            }
            if (loading && items.isNotEmpty()) item { LoadingBox(Modifier.fillMaxWidth()) }
        }
        if (!sel.active) LocateFab(list, curIdx?.let { it + 3 }, Modifier.align(Alignment.BottomEnd))
        SelectionToolbar(sel, items, trackActions(st, sel), Modifier.align(Alignment.BottomCenter))
    }
}

/** Вызов [onLast] с индексом последнего видимого элемента при прокрутке. */
suspend fun snapshotFlowLast(list: LazyListState, onLast: suspend (Int) -> Unit) {
    androidx.compose.runtime.snapshotFlow { list.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0 }.collect { onLast(it) }
}

@Composable
private fun LibArtistsGrid(st: AppState, q: String) {
    val ctx = LocalContext.current
    val store = remember { SortStore(ctx) }
    var sort by remember { mutableStateOf(store.get("artists", Sorts.ARTISTS)) }
    var genre by remember { mutableStateOf(store.genre("artists")) }
    val l = rememberLoader("/library/artists?sort=${sort.sort}&order=${sort.order}&genre=${enc(genre)}", LibArtists.serializer())
    val genres = rememberLoader("/library/genres", Genres.serializer())
    val items = l.data?.items.orEmpty().filter { q.isBlank() || it.name.contains(q.trim(), true) }
    LazyVerticalGrid(GridCells.Adaptive(120.dp), Modifier.fillMaxSize(), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 88.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            Column {
                ListToolbar("${items.size} исп.", Modifier.padding(start = 0.dp)) { SortChip("artists", Sorts.ARTISTS, sort) { sort = it } }
                Box(Modifier.padding(vertical = 4.dp)) { GenreRow(genres.data?.items.orEmpty(), genre) { genre = it; store.setGenre("artists", it) } }
            }
        }
        if (l.data == null) item(span = { GridItemSpan(maxLineSpan) }) { if (l.loading) LoadingBox(Modifier.fillMaxWidth()) else ErrorState(l.error ?: "Ошибка") { l.reload() } }
        items(items, key = { it.key }) { a ->
            Column(Modifier.animateItem(fadeInSpec = MaterialTheme.motionScheme.defaultEffectsSpec(), placementSpec = MaterialTheme.motionScheme.defaultSpatialSpec(), fadeOutSpec = null)
                .clickable { st.push(Screen.LibArtist(a.key, a.name)) }, horizontalAlignment = Alignment.CenterHorizontally) {
                Cover(a.image, 112.dp, CircleShape, icon = Icons.Rounded.Person)
                Spacer(Modifier.height(8.dp))
                Text(a.name, style = MaterialTheme.typography.titleSmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Text(nTracks(a.count.toLong()), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun LibAlbumsGrid(st: AppState, q: String) {
    val ctx = LocalContext.current
    val store = remember { SortStore(ctx) }
    var sort by remember { mutableStateOf(store.get("albums", Sorts.ALBUMS)) }
    var genre by remember { mutableStateOf(store.genre("albums")) }
    val l = rememberLoader("/library/albums?sort=${sort.sort}&order=${sort.order}&genre=${enc(genre)}", LibAlbums.serializer())
    val genres = rememberLoader("/library/genres", Genres.serializer())
    val items = l.data?.items.orEmpty().filter { q.isBlank() || it.title.contains(q.trim(), true) || it.artist.contains(q.trim(), true) }
    LazyVerticalGrid(GridCells.Adaptive(150.dp), Modifier.fillMaxSize(), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 88.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            Column {
                ListToolbar("${items.size} альб.") { SortChip("albums", Sorts.ALBUMS, sort) { sort = it } }
                Box(Modifier.padding(vertical = 4.dp)) { GenreRow(genres.data?.items.orEmpty(), genre) { genre = it; store.setGenre("albums", it) } }
            }
        }
        if (l.data == null) item(span = { GridItemSpan(maxLineSpan) }) { if (l.loading) LoadingBox(Modifier.fillMaxWidth()) else ErrorState(l.error ?: "Ошибка") { l.reload() } }
        items(items, key = { it.artistKey + "/" + it.key }) { a ->
            Column(Modifier.animateItem(fadeInSpec = MaterialTheme.motionScheme.defaultEffectsSpec(), placementSpec = MaterialTheme.motionScheme.defaultSpatialSpec(), fadeOutSpec = null)
                .clickable { st.push(Screen.LibAlbum(a.artistKey, a.key, a.title, a.cover)) }) {
                Cover(a.cover, 400.dp, RoundedCornerShape(28.dp), Modifier.fillMaxWidth().aspectRatio(1f), icon = Icons.Rounded.Album)
                Spacer(Modifier.height(8.dp))
                Text(a.title, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(listOfNotNull(a.artist, a.year?.toString()).joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

/** Мозаика обложек плейлиста: 4 → 2×2, 1–3 → одна во весь квадрат, 0 — иконка на primary-container. */
@Composable
fun Mosaic(covers: List<String>, modifier: Modifier = Modifier, radius: Int = 28) {
    val shape = RoundedCornerShape(radius.dp)
    val cs = MaterialTheme.colorScheme
    Box(modifier.aspectRatio(1f).clip(shape).background(cs.primaryContainer), contentAlignment = Alignment.Center) {
        when {
            covers.size >= 4 -> Column(Modifier.fillMaxSize()) {
                for (r in 0..1) Row(Modifier.weight(1f)) {
                    for (c in 0..1) Cover(covers[r * 2 + c], 400.dp, RoundedCornerShape(0.dp), Modifier.weight(1f).fillMaxSize())
                }
            }
            covers.isNotEmpty() -> Cover(covers[0], 400.dp, RoundedCornerShape(0.dp), Modifier.fillMaxSize())
            else -> Icon(Icons.AutoMirrored.Rounded.QueueMusic, null, tint = cs.onPrimaryContainer, modifier = Modifier.size(48.dp))
        }
    }
}

@Composable
private fun PlaylistsGrid(st: AppState) {
    val ctx = LocalContext.current
    val store = remember { SortStore(ctx) }
    var sort by remember { mutableStateOf(store.get("playlists", Sorts.PLAYLISTS)) }
    var creating by remember { mutableStateOf(false) }
    val l = rememberLoader("/playlists", Playlists.serializer())
    val items = l.data?.items.orEmpty().let { list ->
        val s = when (sort.sort) {
            "name" -> list.sortedBy { it.name.lowercase() }
            "count" -> list.sortedBy { it.count }
            else -> list.sortedBy { it.updatedAt }
        }
        if (sort.order == "desc") s.reversed() else s
    }
    LazyVerticalGrid(GridCells.Adaptive(150.dp), Modifier.fillMaxSize(), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 88.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            ListToolbar("${items.size} плейл.") { SortChip("playlists", Sorts.PLAYLISTS, sort) { sort = it } }
        }
        item {
            Column(Modifier.clickable { creating = true }) {
                Surface(shape = RoundedCornerShape(28.dp), border = BorderStroke(2.dp, MaterialTheme.colorScheme.outlineVariant), color = MaterialTheme.colorScheme.surface, modifier = Modifier.fillMaxWidth().aspectRatio(1f)) {
                    Box(contentAlignment = Alignment.Center) { Icon(Icons.Rounded.Add, null, Modifier.size(40.dp), tint = MaterialTheme.colorScheme.primary) }
                }
                Spacer(Modifier.height(8.dp))
                Text("Новый плейлист", style = MaterialTheme.typography.titleSmall)
            }
        }
        if (l.data == null) item(span = { GridItemSpan(maxLineSpan) }) { if (l.loading) LoadingBox(Modifier.fillMaxWidth()) else ErrorState(l.error ?: "Ошибка") { l.reload() } }
        else if (items.isEmpty()) item(span = { GridItemSpan(maxLineSpan) }) { EmptyState(Icons.AutoMirrored.Rounded.QueueMusic, "Создайте первый плейлист", "Собирайте треки в подборки и делитесь ими с друзьями.") }
        items(items, key = { it.id }) { p ->
            Box(Modifier.animateItem(fadeInSpec = MaterialTheme.motionScheme.defaultEffectsSpec(), placementSpec = MaterialTheme.motionScheme.defaultSpatialSpec(), fadeOutSpec = null)) {
                PlaylistCard(p) { st.push(Screen.PlaylistPage(p.id, p.name)) }
            }
        }
    }
    if (creating) NewPlaylistDialog(st, onDismiss = { creating = false }) { name, shared ->
        creating = false
        st.run {
            val p = st.app.api.post("/playlists", buildJsonObject { put("name", name); put("shared", shared) }, Playlist.serializer())
            l.reload()
            st.push(Screen.PlaylistPage(p.id, p.name))
        }
    }
}

@Composable
private fun PlaylistCard(p: Playlist, onClick: () -> Unit) {
    Column(Modifier.clickable(onClick = onClick)) {
        Box {
            Mosaic(p.covers, Modifier.fillMaxWidth())
            if (p.shared) Box(Modifier.align(Alignment.TopEnd).padding(8.dp).size(32.dp).clip(CircleShape).background(MaterialTheme.colorScheme.tertiary), contentAlignment = Alignment.Center) {
                Icon(Icons.Rounded.Group, "Общий", tint = MaterialTheme.colorScheme.onTertiary, modifier = Modifier.size(18.dp))
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(p.name, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(listOf(nTracks(p.count.toLong()), if (p.seconds > 0) fmtDuration(p.seconds) else null, if (p.role == "member") "от ${p.owner}" else null).filterNotNull().joinToString(" · "),
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}
