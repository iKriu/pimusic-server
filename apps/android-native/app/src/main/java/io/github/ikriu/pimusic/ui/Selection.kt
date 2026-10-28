package io.github.ikriu.pimusic.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.PlaylistAdd
import androidx.compose.material.icons.automirrored.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Delete
import androidx.compose.material.icons.rounded.DoneAll
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.RemoveCircleOutline
import androidx.compose.material.icons.rounded.SkipNext
import androidx.compose.material.icons.rounded.VerticalAlignBottom
import androidx.compose.material.icons.rounded.VerticalAlignTop
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.fmtBytes
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.LibDeleteResp
import io.github.ikriu.pimusic.data.Track
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Режим выбора треков (медиатека, альбом, исполнитель, плейлист): ключ — item_id (плейлист) или id трека.
 * Порядок выбранного — как в списке (для переноса пачкой и очереди).
 */
class Selection {
    val items = mutableStateMapOf<String, Track>()
    var active by mutableStateOf(false)
    val count get() = items.size

    fun key(t: Track) = "${t.itemId ?: "t"}:${t.id}"
    fun isSelected(t: Track) = key(t) in items
    fun start(t: Track? = null) { active = true; if (t != null) items[key(t)] = t }
    fun toggle(t: Track) { val k = key(t); if (k in items) items.remove(k) else items[k] = t }
    fun selectAll(list: List<Track>) { list.forEach { items[key(it)] = it } }
    fun stop() { items.clear(); active = false }

    /** Выбранное в порядке списка [order]. */
    fun ordered(order: List<Track>): List<Track> = order.filter { isSelected(it) }
}

/** Подтверждение удаления пачкой (диалог в App). */
data class BulkDelete(val tracks: List<Track>, val done: () -> Unit)

data class SelAction(val icon: ImageVector, val label: String, val run: (List<Track>) -> Unit)

@Composable
fun rememberSelection() = remember { Selection() }

/** Шапка режима выбора: ✕ · «N выбрано» · «Выбрать все» / «Снять выбор». */
@Composable
fun SelectionHeader(sel: Selection, all: List<Track>) {
    val allSelected = all.isNotEmpty() && all.all { sel.isSelected(it) }
    Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = { sel.stop() }) { Icon(Icons.Rounded.Close, "Закончить выбор") }
        FitText(
            "Выбрано: ${sel.count}",
            MaterialTheme.typography.titleLarge, Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite }, minScale = 0.75f,
        )
        // «Выбрать все» / «Снять выбор» — тональная кнопка-иконка 48dp (подпись — для TalkBack): заголовок не обрезается
        androidx.compose.material3.FilledTonalIconToggleButton(
            checked = allSelected, onCheckedChange = { if (allSelected) sel.items.clear() else sel.selectAll(all) },
            modifier = Modifier.size(48.dp).semantics { contentDescription = if (allSelected) "Снять выбор" else "Выбрать все" },
        ) { Icon(Icons.Rounded.DoneAll, null) }
    }
}

/** Плавающая панель действий M3E (floating toolbar): иконка над подписью, прокрутка по горизонтали. */
@Composable
fun SelectionToolbar(sel: Selection, order: List<Track>, actions: List<SelAction>, modifier: Modifier = Modifier) {
    AnimatedVisibility(sel.active && sel.count > 0, modifier, enter = slideInVertically { it }, exit = slideOutVertically { it }) {
        Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.BottomCenter) {
            Surface(shape = CircleShape, color = MaterialTheme.colorScheme.primaryContainer, shadowElevation = 6.dp, tonalElevation = 3.dp, modifier = Modifier.widthIn(max = 640.dp)) {
                Row(Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 8.dp, vertical = 4.dp), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                    actions.forEach { a ->
                        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.widthIn(min = 64.dp)) {
                            IconButton(onClick = { a.run(sel.ordered(order)) }) { Icon(a.icon, a.label) }
                            Text(a.label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onPrimaryContainer, maxLines = 1)
                        }
                    }
                }
            }
        }
    }
}

/** Общие действия над выбранными треками; [extra] — действия конкретного экрана (плейлист). */
fun trackActions(st: AppState, sel: Selection, extra: List<SelAction> = emptyList(), canDelete: Boolean = true): List<SelAction> = buildList {
    add(SelAction(Icons.Rounded.SkipNext, "Следующими") { st.app.player.addNext(it); sel.stop() })
    add(SelAction(Icons.AutoMirrored.Rounded.QueueMusic, "В очередь") { st.app.player.addToQueue(it); sel.stop() })
    add(SelAction(Icons.AutoMirrored.Rounded.PlaylistAdd, "В плейлист") { st.addToPlaylist = it; sel.stop() })
    add(SelAction(Icons.Rounded.DownloadForOffline, "На устройство") { st.saveOffline(it, null); sel.stop() })
    addAll(extra)
    if (canDelete) add(SelAction(Icons.Rounded.Delete, "Удалить") { list ->
        val own = list.filter { t -> t.owned }
        if (own.isEmpty()) st.msg("Среди выбранных нет треков из вашей медиатеки") else st.bulkDelete = BulkDelete(own) { sel.stop() }
    })
}

/** Действия плейлиста: убрать, перенести пачкой в начало/конец (только в «Порядке плейлиста»). */
fun playlistActions(st: AppState, sel: Selection, playlistId: Long, manual: Boolean, changed: () -> Unit): List<SelAction> = buildList {
    add(SelAction(Icons.Rounded.RemoveCircleOutline, "Убрать") { list ->
        st.run {
            for (chunk in list.mapNotNull { it.itemId }.chunked(5000)) {
                st.app.api.post("/playlists/$playlistId/remove", buildJsonObject { put("item_ids", ids(chunk)) }, JsonObject.serializer())
            }
            st.msg("Убрано из плейлиста: ${list.size}")
            sel.stop(); changed()
        }
    })
    if (manual) {
        fun move(to: Int, label: String) = SelAction(if (to == 0) Icons.Rounded.VerticalAlignTop else Icons.Rounded.VerticalAlignBottom, label) { list ->
            st.run {
                st.app.api.post("/playlists/$playlistId/move-many", buildJsonObject { put("item_ids", ids(list.mapNotNull { it.itemId })); put("to", to) }, JsonObject.serializer())
                st.msg("Перенесено: ${list.size}")
                sel.stop(); changed()
            }
        }
        add(move(0, "В начало"))
        add(move(Int.MAX_VALUE / 2, "В конец"))
    }
}

/** Удаление пачкой из медиатеки: /api/library/delete по 500; недоступные совсем — убрать из очереди. */
fun AppState.deleteFromLibrary(list: List<Track>) = run {
    var deleted = 0
    val gone = HashSet<Long>()
    for (chunk in list.map { it.id }.distinct().chunked(500)) {
        val r = app.api.post("/library/delete", buildJsonObject { put("track_ids", ids(chunk)) }, LibDeleteResp.serializer())
        deleted += r.deleted
        gone += r.goneIds
    }
    if (gone.isNotEmpty()) app.player.dropTracks(gone)
    msg("Удалено из медиатеки: ${nTracks(deleted.toLong())}")
    libraryChanged()
}

/** Подпись оценки размера: «≈ 120 МБ» или null. */
fun sizeNote(st: AppState, list: List<Track>) = st.estimateSize(list)?.let { "≈ ${fmtBytes(it)}" }
