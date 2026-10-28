package io.github.ikriu.pimusic.ui.screens

import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ExitToApp
import androidx.compose.material.icons.automirrored.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Delete
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Edit
import androidx.compose.material.icons.rounded.Group
import androidx.compose.material.icons.rounded.KeyboardArrowDown
import androidx.compose.material.icons.rounded.KeyboardArrowUp
import androidx.compose.material.icons.rounded.LibraryAdd
import androidx.compose.material.icons.rounded.LinkOff
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material.icons.rounded.PersonRemove
import androidx.compose.material.icons.rounded.Share
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.Playlist
import io.github.ikriu.pimusic.data.SaveResult
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.ConfirmDialog
import io.github.ikriu.pimusic.ui.EmptyState
import io.github.ikriu.pimusic.ui.Loaded
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.MenuRow
import io.github.ikriu.pimusic.ui.NewPlaylistDialog
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.TrackMenu
import io.github.ikriu.pimusic.ui.TrackRow
import io.github.ikriu.pimusic.ui.ids
import io.github.ikriu.pimusic.ui.rememberCoverSource
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.ui.SelectionHeader
import io.github.ikriu.pimusic.ui.SelectionToolbar
import io.github.ikriu.pimusic.ui.playlistActions
import io.github.ikriu.pimusic.ui.rememberSelection
import io.github.ikriu.pimusic.ui.trackActions
import androidx.activity.compose.BackHandler
import io.github.ikriu.pimusic.ui.theme.ContentTheme
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

fun plSrc(p: Playlist) = io.github.ikriu.pimusic.player.PlaySource("playlist", p.name, id = p.id)

/** Аватар-инициал: 4 тональные роли по хэшу логина. */
@Composable
fun Avatar(login: String, size: Int = 32) {
    val cs = MaterialTheme.colorScheme
    val (bg, fg) = listOf(cs.primaryContainer to cs.onPrimaryContainer, cs.secondaryContainer to cs.onSecondaryContainer,
        cs.tertiaryContainer to cs.onTertiaryContainer, cs.surfaceContainerHighest to cs.onSurface)[(login.hashCode() and 0x7fffffff) % 4]
    Box(Modifier.size(size.dp).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
        Text(login.take(1).uppercase(), color = fg, style = MaterialTheme.typography.labelLarge)
    }
}

/**
 * Плейлист: мозаика, имя, описание, чип «Общий», участники, «Слушать/Перемешать», сортировка (сервер:
 * ?sort=&order=&seed=), режим правки (только «Порядок плейлиста»): Выше/Ниже/Убрать. Меню: переименовать,
 * поделиться, общий доступ, сохранить в медиатеку, скачать на устройство, удалить/выйти.
 */
@Composable
fun PlaylistScreen(s: Screen.PlaylistPage) {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    val store = remember { SortStore(ctx) }
    val view = "pl:${s.id}"
    var sort by remember { mutableStateOf(store.get(view, Sorts.PLTRACKS)) }
    val path = "/playlists/${s.id}?sort=${sort.sort}" + (if (Sorts.noDir(sort.sort)) "" else "&order=${sort.order}") + (if (sort.sort == "random") "&seed=${sort.seed}" else "")
    val l = rememberLoader(path, Playlist.serializer())
    val ui by st.app.player.ui.collectAsState()
    val offline by st.app.offline.items.collectAsState()
    val done = offline.filterValues { it.done }.keys
    var edit by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var dialog by remember { mutableStateOf<String?>(null) }
    val src = rememberCoverSource(l.data?.covers?.firstOrNull())

    fun share(p: Playlist) = st.run share@{
        var inv = p.invite
        if (inv == null) {
            val r = st.app.api.post("/playlists/${p.id}/share", buildJsonObject { put("enabled", true) }, Playlist.serializer())
            inv = r.invite
            l.reload()
        }
        val link = inv?.link ?: inv?.appLink
        if (link == null) { st.msg("Не удалось получить ссылку"); return@share }
        val i = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, "Плейлист «${p.name}» в PiMusic: $link")
        ctx.startActivity(Intent.createChooser(i, "Поделиться плейлистом").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    val sel = rememberSelection()
    BackHandler(sel.active) { sel.stop() }
    ContentTheme(src) {
        Surface(Modifier.fillMaxSize()) {
            Column {
                val p0 = l.data
                if (sel.active) SelectionHeader(sel, p0?.tracks.orEmpty()) else PageBar(p0?.name ?: s.name) {
                    if (p0 != null && !edit) SelectButton { sel.start() }
                    if (p0 != null && sort.sort == "manual") IconButton(onClick = { edit = !edit }) {
                        Icon(if (edit) Icons.Rounded.Check else Icons.Rounded.Edit, if (edit) "Готово" else "Править")
                    }
                    Box {
                        IconButton(onClick = { menu = true }) { Icon(Icons.Rounded.MoreVert, "Ещё") }
                        if (p0 != null) DropdownMenu(menu, { menu = false }) {
                            DropdownMenuItem(text = { Text("Переименовать") }, leadingIcon = { Icon(Icons.Rounded.Edit, null) }, onClick = { menu = false; dialog = "rename" })
                            DropdownMenuItem(text = { Text("Поделиться") }, leadingIcon = { Icon(Icons.Rounded.Share, null) }, onClick = { menu = false; share(p0) })
                            if (p0.role == "owner" && p0.inviteEnabled) DropdownMenuItem(text = { Text("Выключить ссылку-приглашение") }, leadingIcon = { Icon(Icons.Rounded.LinkOff, null) }, onClick = {
                                menu = false
                                st.run { st.app.api.post("/playlists/${p0.id}/share", buildJsonObject { put("enabled", false) }, JsonObject.serializer()); l.reload(); st.msg("Ссылка выключена") }
                            })
                            if (p0.members.isNotEmpty()) DropdownMenuItem(text = { Text("Участники") }, leadingIcon = { Icon(Icons.Rounded.Group, null) }, onClick = { menu = false; dialog = "members" })
                            DropdownMenuItem(text = { Text("Сохранить в медиатеку") }, leadingIcon = { Icon(Icons.Rounded.LibraryAdd, null) }, onClick = {
                                menu = false
                                st.run {
                                    val r = st.app.api.post("/playlists/${p0.id}/save", null, SaveResult.serializer())
                                    st.msg(if (r.linked > 0) "В медиатеку добавлено: ${r.linked}" else "Все треки уже в медиатеке"); st.libraryChanged()
                                }
                            })
                            DropdownMenuItem(text = { Text("Скачать на устройство") }, leadingIcon = { Icon(Icons.Rounded.DownloadForOffline, null) }, onClick = { menu = false; st.saveOffline(p0.tracks.orEmpty(), p0.name) })
                            if (p0.role == "owner") DropdownMenuItem(text = { Text("Удалить плейлист") }, leadingIcon = { Icon(Icons.Rounded.Delete, null) }, onClick = { menu = false; dialog = "delete" })
                            else DropdownMenuItem(text = { Text("Выйти из плейлиста") }, leadingIcon = { Icon(Icons.AutoMirrored.Rounded.ExitToApp, null) }, onClick = { menu = false; dialog = "leave" })
                        }
                    }
                }
                Loaded(l) { p ->
                    val tracks = p.tracks.orEmpty()
                    val list = rememberLazyListState()
                    val curIdx = ui.current?.id?.let { id -> tracks.indexOfFirst { it.id == id }.takeIf { it >= 0 } }
                    Box(Modifier.fillMaxSize()) {
                        LazyColumn(Modifier.fillMaxSize(), state = list, contentPadding = PaddingValues(bottom = 88.dp)) {
                            item {
                                PageHeader(null, p.name, null, null, coverContent = { Mosaic(p.covers, Modifier.fillMaxSize(), 28) })
                            }
                            item {
                                Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                                    if (p.description.isNotEmpty()) Text(p.description, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center)
                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        if (p.shared) AssistChip({ dialog = "members" }, label = { Text("Общий") }, leadingIcon = { Icon(Icons.Rounded.Group, null, Modifier.size(18.dp)) },
                                            colors = AssistChipDefaults.assistChipColors(containerColor = MaterialTheme.colorScheme.tertiaryContainer, labelColor = MaterialTheme.colorScheme.onTertiaryContainer))
                                        (listOf(p.owner) + p.members.map { it.login }).take(4).forEach { Avatar(it, 28) }
                                    }
                                    Text(listOfNotNull(if (p.role == "member") "от ${p.owner}" else null, metaOf(tracks)).joinToString(" · "),
                                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                            item {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    PlayButtons({ st.app.player.playList(tracks, 0, false, plSrc(p)) }, { st.app.player.playList(tracks, 0, true, plSrc(p)) }, Modifier.weight(1f))
                                }
                            }
                            item {
                                Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Spacer(Modifier.weight(1f))
                                    SortChip(view, Sorts.PLTRACKS, sort) { sort = it; if (it.sort != "manual") edit = false }
                                }
                            }
                            if (tracks.any { !it.owned }) item {
                                Surface(color = MaterialTheme.colorScheme.secondaryContainer, shape = MaterialTheme.shapes.large, modifier = Modifier.fillMaxWidth().padding(16.dp)) {
                                    Text("Часть треков не в вашей медиатеке — «Сохранить в медиатеку» в меню ⋮ добавит их без скачивания.",
                                        style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(16.dp))
                                }
                            }
                            if (tracks.isEmpty()) item { EmptyState(Icons.AutoMirrored.Rounded.QueueMusic, "В плейлисте пока пусто", "Добавляйте треки через ⋮ → «Добавить в плейлист…».", cookie = false) }
                            itemsIndexed(tracks, key = { i, t -> "${t.itemId ?: t.id}:$i" }) { i, t ->
                                TrackRow(
                                    t, playing = ui.current?.id == t.id, animating = ui.playing,
                                    onClick = {
                                        if (sel.active) sel.toggle(t)
                                        else if (edit) Unit else if (ui.current?.id == t.id) st.openPlayer() else st.app.player.playList(tracks, i, false, plSrc(p))
                                    },
                                    onMore = if (edit) null else ({ st.menu = TrackMenu(t, p.id, canRemoveFromPlaylist = true) { l.reload() } }),
                                    selected = if (sel.active) sel.isSelected(t) else null,
                                    offline = t.id in done,
                                    trailing = if (!edit) null else ({
                                        Row {
                                            if (i > 0) IconButton(onClick = { move(st, p, t, i - 1) { l.reload() } }) { Icon(Icons.Rounded.KeyboardArrowUp, "Выше") }
                                            if (i < tracks.size - 1) IconButton(onClick = { move(st, p, t, i + 1) { l.reload() } }) { Icon(Icons.Rounded.KeyboardArrowDown, "Ниже") }
                                            IconButton(onClick = {
                                                st.run {
                                                    st.app.api.post("/playlists/${p.id}/remove", buildJsonObject { put("item_ids", ids(listOfNotNull(t.itemId))) }, JsonObject.serializer())
                                                    l.reload()
                                                }
                                            }) { Icon(Icons.Rounded.Close, "Убрать") }
                                        }
                                    }),
                                )
                            }
                        }
                        if (!sel.active) LocateFab(list, curIdx?.let { it + 4 + (if (tracks.any { !it.owned }) 1 else 0) }, Modifier.align(Alignment.BottomEnd))
                        SelectionToolbar(
                            sel, tracks,
                            trackActions(st, sel, playlistActions(st, sel, p.id, manual = sort.sort == "manual") { l.reload() }),
                            Modifier.align(Alignment.BottomCenter),
                        )
                    }
                    when (dialog) {
                        "rename" -> NewPlaylistDialog(st, { dialog = null }, initial = p.name, title = "Переименовать", action = "Сохранить", showShared = false) { name, _ ->
                            dialog = null
                            st.run { st.app.api.post("/playlists/${p.id}/update", buildJsonObject { put("name", name) }, JsonObject.serializer()); l.reload() }
                        }
                        "delete" -> ConfirmDialog("Удалить плейлист?", "«${p.name}» удалится у вас и у участников. Треки останутся в медиатеке.", "Удалить", { dialog = null }) {
                            dialog = null
                            st.run { st.app.api.post("/playlists/${p.id}/delete", null, JsonObject.serializer()); st.back(); st.libraryChanged() }
                        }
                        "leave" -> ConfirmDialog("Выйти из плейлиста?", "Вернуться можно будет только по новой ссылке-приглашению.", "Выйти", { dialog = null }) {
                            dialog = null
                            st.run { st.app.api.post("/playlists/${p.id}/leave", null, JsonObject.serializer()); st.back(); st.libraryChanged() }
                        }
                        "members" -> MembersSheet(p, onDismiss = { dialog = null }) { login ->
                            st.run { st.app.api.post("/playlists/${p.id}/kick", buildJsonObject { put("login", login) }, JsonObject.serializer()); l.reload(); st.msg("$login больше не участник") }
                        }
                    }
                }
            }
        }
    }
}

private fun move(st: io.github.ikriu.pimusic.ui.AppState, p: Playlist, t: Track, to: Int, done: () -> Unit) = st.run {
    st.app.api.post("/playlists/${p.id}/move", buildJsonObject { put("item_id", t.itemId ?: return@run); put("to", to) }, JsonObject.serializer())
    done()
}

@Composable
private fun MembersSheet(p: Playlist, onDismiss: () -> Unit, onKick: (String) -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.navigationBarsPadding()) {
            Text("Участники · ${nTracks(p.count.toLong())}", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
            Row(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Avatar(p.owner, 40); Spacer(Modifier.width(16.dp)); Text("${p.owner} · владелец", style = MaterialTheme.typography.bodyLarge)
            }
            p.members.forEach { m ->
                Row(Modifier.fillMaxWidth().clickable(enabled = false) {}.padding(horizontal = 24.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Avatar(m.login, 40); Spacer(Modifier.width(16.dp))
                    Text(m.login, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
                    if (p.role == "owner") IconButton(onClick = { onKick(m.login); onDismiss() }) { Icon(Icons.Rounded.PersonRemove, "Убрать ${m.login}") }
                }
            }
            if (p.members.isEmpty()) MenuRow(Icons.Rounded.Group, "Пока никого", "Поделитесь ссылкой — друзья смогут присоединиться") {}
            Spacer(Modifier.size(16.dp))
        }
    }
}
