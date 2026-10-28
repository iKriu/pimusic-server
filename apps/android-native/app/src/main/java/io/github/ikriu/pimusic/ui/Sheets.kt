package io.github.ikriu.pimusic.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.selection.toggleable
import androidx.compose.material3.Button
import androidx.compose.material3.TriStateCheckbox
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.state.ToggleableState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.PlaylistAdd
import androidx.compose.material.icons.automirrored.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Add
import androidx.compose.material.icons.rounded.Album
import androidx.compose.material.icons.rounded.Autorenew
import androidx.compose.material.icons.rounded.CheckBox
import androidx.compose.material.icons.rounded.CheckBoxOutlineBlank
import androidx.compose.material.icons.rounded.Delete
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Person
import androidx.compose.material.icons.rounded.RemoveCircleOutline
import androidx.compose.material.icons.rounded.SkipNext
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.data.AddResult
import io.github.ikriu.pimusic.data.ForTrack
import io.github.ikriu.pimusic.data.LibDeleteResp
import io.github.ikriu.pimusic.data.Playlist
import io.github.ikriu.pimusic.data.Playlists
import io.github.ikriu.pimusic.data.Track
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.math.roundToInt

fun ids(list: List<Long>) = JsonArray(list.map { JsonPrimitive(it) })

/** Лист действий трека: играть следующим, в очередь, в плейлист, скачать, к исполнителю/альбому, удалить. */
@Composable
fun TrackMenuSheet(st: AppState, m: TrackMenu) {
    val t = m.track
    val offline by st.app.offline.items.collectAsState()
    val off = offline[t.id]
    var confirmDelete by remember { mutableStateOf(false) }
    val close = { st.menu = null }
    ModalBottomSheet(onDismissRequest = close, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.navigationBarsPadding().verticalScroll(rememberScrollState())) {
            Row(Modifier.padding(horizontal = 24.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Cover(t.cover, 56.dp)
                Spacer(Modifier.width(16.dp))
                Column(Modifier.weight(1f)) {
                    Text(t.title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    Text(listOf(t.artist, t.album).filter { it.isNotEmpty() }.joinToString(" · "), style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            MenuRow(Icons.Rounded.SkipNext, "Играть следующим") { st.app.player.addNext(listOf(t)); close() }
            MenuRow(Icons.AutoMirrored.Rounded.QueueMusic, "В очередь") { st.app.player.addToQueue(listOf(t)); close() }
            MenuRow(Icons.AutoMirrored.Rounded.PlaylistAdd, "Добавить в плейлист…") { close(); st.addToPlaylist = listOf(t) }
            when {
                off?.done == true -> MenuRow(Icons.Rounded.DownloadForOffline, "Удалить с устройства", "Скачан для прослушивания без сети") { st.app.offline.remove(t.id); close() }
                off?.active == true -> MenuRow(Icons.Rounded.DownloadForOffline, "Отменить скачивание") { st.app.offline.remove(t.id); close() }
                else -> MenuRow(Icons.Rounded.DownloadForOffline, "Скачать на устройство", t.size?.let { "≈ " + io.github.ikriu.pimusic.core.fmtBytes(it) }) { st.saveOffline(listOf(t), null); close() }
            }
            // у чужого трека (общий плейлист) страниц исполнителя/альбома в своей медиатеке нет — пункты скрыты
            if (st.canGoLibrary(t) && t.artist.isNotEmpty()) MenuRow(Icons.Rounded.Person, "К исполнителю") { close(); st.goArtist(t) }
            if (st.canGoLibrary(t) && t.album.isNotEmpty()) MenuRow(Icons.Rounded.Album, "К альбому") { close(); st.goAlbum(t) }
            if (m.canRemoveFromPlaylist && m.playlistId != null && t.itemId != null) {
                MenuRow(Icons.Rounded.RemoveCircleOutline, "Убрать из плейлиста") {
                    close()
                    st.run {
                        st.app.api.post("/playlists/${m.playlistId}/remove", buildJsonObject { put("item_ids", ids(listOf(t.itemId))) }, JsonObject.serializer())
                        st.msg("Убрано из плейлиста")
                        m.onRemoved?.invoke()
                    }
                }
            }
            if (t.owned) MenuRow(Icons.Rounded.Autorenew, "Перекачать трек", if (t.broken) "Файл помечен проблемным" else "Если файл битый или не тот") {
                close()
                st.run {
                    val r = st.app.api.post("/tracks/${t.id}/redownload", null, io.github.ikriu.pimusic.data.Ok.serializer())
                    st.msg(r.text ?: "Перекачиваю «${t.title}»", "Загрузки") { st.select(Tab.DOWNLOADS) }
                }
            }
            if (t.owned) MenuRow(Icons.Rounded.Delete, "Удалить из медиатеки", color = MaterialTheme.colorScheme.error) { confirmDelete = true }
            Spacer(Modifier.size(16.dp))
        }
    }
    if (confirmDelete) ConfirmDialog(
        "Удалить из медиатеки?", "«${t.title}» исчезнет из медиатеки. Из плейлистов трек не удаляется.", "Удалить",
        onDismiss = { confirmDelete = false },
    ) {
        confirmDelete = false
        close()
        st.run {
            val r = st.app.api.post("/library/delete", buildJsonObject { put("track_ids", ids(listOf(t.id))) }, LibDeleteResp.serializer())
            if (r.goneIds.isNotEmpty()) st.app.player.dropTracks(r.goneIds.toSet())
            st.msg(if (r.deleted > 0) "Удалено из медиатеки" else "Не удалось удалить")
            st.libraryChanged()
        }
    }
}

/** Отметка плейлиста в листе «В плейлисты»: сколько выбранных треков уже в нём. */
private data class PlMark(val id: Long, val name: String, val have: Int)

/**
 * Лист «В плейлисты» (как в Mini App/десктопе): чекбоксы всех плейлистов сразу, «Готово» применяет изменения разом —
 * добавить в отмеченные, убрать из снятых. Для одного трека начальные отметки — /api/playlists/for-track (has);
 * для выделения (до 50 треков) — по каждому треку, отметка «частично» (TriState); больше 50 — только добавление.
 */
@Composable
fun AddToPlaylistSheet(st: AppState, tracks: List<Track>) {
    val tids = tracks.map { it.id }.distinct()
    var marks by remember { mutableStateOf<List<PlMark>?>(null) }
    val want = remember { mutableStateMapOf<Long, Boolean>() } // id плейлиста → отмечен (только изменённые)
    var creating by remember { mutableStateOf(false) }
    var applying by remember { mutableStateOf(false) }
    LaunchedEffect(tracks) {
        marks = runCatching {
            val all = st.app.api.get("/playlists", Playlists.serializer()).items.filter { it.role == "owner" || it.role == "member" }
            val have = HashMap<Long, Int>()
            if (tids.size <= 50) for (tid in tids) {
                st.app.api.get("/playlists/for-track?track_id=$tid", ForTrack.serializer()).items.forEach { if (it.has) have[it.id] = (have[it.id] ?: 0) + 1 }
            }
            all.map { PlMark(it.id, it.name, have[it.id] ?: 0) }
        }.getOrElse { st.msg((it as? Exception)?.message ?: "Ошибка"); emptyList() }
    }
    fun stateOf(m: PlMark): ToggleableState = when (want[m.id]) {
        true -> ToggleableState.On
        false -> ToggleableState.Off
        null -> when { m.have == 0 -> ToggleableState.Off; m.have >= tids.size -> ToggleableState.On; else -> ToggleableState.Indeterminate }
    }
    val close = { st.addToPlaylist = null }
    fun apply() {
        val list = marks ?: return
        applying = true
        st.run {
            var added = 0; var removed = 0
            try {
                for (m in list) {
                    val w = want[m.id] ?: continue
                    if (w && m.have < tids.size) {
                        added += st.app.api.post("/playlists/${m.id}/add", buildJsonObject { put("track_ids", ids(tids)) }, AddResult.serializer()).added
                    } else if (!w && m.have > 0) {
                        val items = st.app.api.get("/playlists/${m.id}", Playlist.serializer()).tracks.orEmpty()
                            .filter { it.id in tids }.mapNotNull { it.itemId }
                        if (items.isNotEmpty()) {
                            st.app.api.post("/playlists/${m.id}/remove", buildJsonObject { put("item_ids", ids(items)) }, JsonObject.serializer())
                            removed += items.size
                        }
                    }
                }
                st.msg(listOfNotNull(
                    if (added > 0) "Добавлено: $added" else null,
                    if (removed > 0) "убрано: $removed" else null,
                ).joinToString(", ").ifEmpty { "Без изменений" })
            } finally { applying = false; close() }
        }
    }
    ModalBottomSheet(onDismissRequest = close) {
        Column(Modifier.navigationBarsPadding()) {
            // «Готово» — в шапке листа: видно сразу, даже когда лист раскрыт наполовину
            Row(Modifier.fillMaxWidth().padding(start = 24.dp, end = 16.dp, top = 4.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                FitText(if (tids.size == 1) "В плейлисты" else "В плейлисты · ${tids.size} тр.", MaterialTheme.typography.titleLarge, Modifier.weight(1f), minScale = 0.75f)
                Button(onClick = { apply() }, enabled = !applying && want.isNotEmpty(), modifier = Modifier.heightIn(min = 48.dp)) { Text("Готово") }
            }
            MenuRow(Icons.Rounded.Add, "Новый плейлист") { creating = true }
            val list = marks
            if (list == null) LoadingBox(Modifier.fillMaxWidth())
            else LazyColumn(Modifier.heightIn(max = 420.dp)) {
                items(list, key = { it.id }) { m ->
                    val state = stateOf(m)
                    Row(
                        Modifier.fillMaxWidth().heightIn(min = 56.dp)
                            .toggleable(value = state == ToggleableState.On, onValueChange = { want[m.id] = state != ToggleableState.On }, role = Role.Checkbox)
                            .padding(horizontal = 24.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        TriStateCheckbox(state, null)
                        Spacer(Modifier.width(16.dp))
                        Text(m.name, style = MaterialTheme.typography.bodyLarge, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                    }
                }
            }
            Spacer(Modifier.size(16.dp))
        }
    }
    if (creating) NewPlaylistDialog(st, onDismiss = { creating = false }) { name, shared ->
        creating = false
        close()
        st.run {
            val p = st.app.api.post("/playlists", buildJsonObject {
                put("name", name); put("shared", shared); put("track_ids", ids(tracks.map { it.id }))
            }, Playlist.serializer())
            st.msg("Создан плейлист «${p.name}»", "Открыть") { st.push(Screen.PlaylistPage(p.id, p.name)) }
            st.libraryChanged()
        }
    }
}

/** Диалог-форма «Новый плейлист»: имя (счётчик 0/100), «Общий плейлист»; «Создать» неактивна, пока имя пустое. */
@Composable
fun NewPlaylistDialog(st: AppState, onDismiss: () -> Unit, initial: String = "", title: String = "Новый плейлист", action: String = "Создать", showShared: Boolean = true, onDone: (String, Boolean) -> Unit) {
    var name by remember { mutableStateOf(initial) }
    var shared by remember { mutableStateOf(false) }
    val fr = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { fr.requestFocus() } }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column {
                OutlinedTextField(
                    name, { if (it.length <= 100) name = it }, label = { Text("Название") }, singleLine = true,
                    supportingText = { Text("${name.length}/100") }, modifier = Modifier.fillMaxWidth().focusRequester(fr),
                )
                if (showShared) Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth().clickable { shared = !shared }.padding(vertical = 8.dp)) {
                    Column(Modifier.weight(1f)) {
                        Text("Общий плейлист", style = MaterialTheme.typography.bodyLarge)
                        Text("Друзья смогут добавлять треки по ссылке", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Switch(shared, { shared = it })
                }
            }
        },
        confirmButton = { TextButton(enabled = name.isNotBlank(), onClick = { onDone(name.trim(), shared) }) { Text(action) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Отмена") } },
    )
}

@Composable
fun ConfirmDialog(title: String, text: String?, action: String, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = text?.let { { Text(it) } },
        confirmButton = { TextButton(onClick = onConfirm) { Text(action) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Отмена") } },
    )
}

/** Таймер сна: 15/30/45/60 мин, «До конца трека», выключить. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SleepSheet(st: AppState, onDismiss: () -> Unit) {
    val hub = st.app.player
    val sleep by hub.sleep.collectAsState()
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.navigationBarsPadding().padding(horizontal = 24.dp)) {
            Text("Таймер сна", style = MaterialTheme.typography.titleLarge)
            Text("Музыка плавно затихнет и встанет на паузу", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.size(16.dp))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(15, 30, 45, 60).forEach { m -> FilterChip(false, { hub.setSleep(m); onDismiss(); st.msg("Таймер сна: $m мин") }, label = { Text("$m мин") }) }
                FilterChip(sleep?.endOfTrack == true, { hub.setSleep(0); onDismiss(); st.msg("Остановлю после этого трека") }, label = { Text("До конца трека") })
                if (sleep != null) FilterChip(false, { hub.cancelSleep(); onDismiss() }, label = { Text("Выключить") })
            }
            Spacer(Modifier.size(24.dp))
        }
    }
}

/** Скорость 0,5–2× (высота голоса сохраняется); волна — всегда 1×. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SpeedSheet(st: AppState, onDismiss: () -> Unit) {
    val eqc = st.app.eqc
    val opts by eqc.opts.collectAsState()
    var v by remember(opts.speed) { mutableFloatStateOf(opts.speed.toFloat()) }
    fun set(x: Float) { v = x; eqc.setOpts(opts.copy(speed = ((x * 20).roundToInt() / 20.0))) }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.navigationBarsPadding().padding(horizontal = 24.dp)) {
            Text("Скорость · ${fmtSpeed(v)}", style = MaterialTheme.typography.titleLarge)
            Slider(v, { v = it }, valueRange = 0.5f..2f, steps = 29, onValueChangeFinished = { set(v) })
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(0.75f, 1f, 1.25f, 1.5f, 2f).forEach { x -> FilterChip(kotlin.math.abs(v - x) < 0.01f, { set(x) }, label = { Text(fmtSpeed(x)) }) }
            }
            Spacer(Modifier.size(24.dp))
        }
    }
}

/** Плавный переход 0–12 с (0 — без паузы, gapless), с нормализацией громкости; волна — без перехода. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CrossfadeSheet(st: AppState, onDismiss: () -> Unit) {
    val eqc = st.app.eqc
    val opts by eqc.opts.collectAsState()
    var v by remember(opts.crossfade) { mutableFloatStateOf(opts.crossfade.toFloat()) }
    fun set(x: Float) { v = x; eqc.setOpts(opts.copy(crossfade = x.roundToInt().toDouble())) }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.navigationBarsPadding().padding(horizontal = 24.dp)) {
            Text("Плавный переход · ${fmtXf(v.toDouble())}", style = MaterialTheme.typography.titleLarge)
            Text("Конец трека затихает, следующий нарастает. 0 — без паузы между треками.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Slider(v, { v = it }, valueRange = 0f..12f, steps = 11, onValueChangeFinished = { set(v) })
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(0f, 3f, 6f, 9f, 12f).forEach { x -> FilterChip(v.roundToInt() == x.roundToInt(), { set(x) }, label = { Text(fmtXf(x.toDouble())) }) }
            }
            Spacer(Modifier.size(24.dp))
        }
    }
}

fun fmtXf(v: Double) = if (v >= 0.5) "${v.roundToInt()} с" else "Без паузы"

fun fmtSpeed(v: Float) = ((v * 100).roundToInt() / 100.0).toString().removeSuffix(".0").replace('.', ',') + "×"
