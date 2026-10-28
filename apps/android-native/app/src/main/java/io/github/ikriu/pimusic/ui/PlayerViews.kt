package io.github.ikriu.pimusic.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Clear
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.DragHandle
import androidx.compose.material.icons.rounded.KeyboardArrowDown
import androidx.compose.material.icons.rounded.KeyboardArrowUp
import androidx.compose.material.icons.rounded.Lyrics
import androidx.compose.material.icons.rounded.Shuffle
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.rounded.Refresh
import kotlinx.coroutines.launch
import kotlinx.serialization.json.put
import io.github.ikriu.pimusic.core.lyricIndex
import io.github.ikriu.pimusic.data.Lyrics
import io.github.ikriu.pimusic.data.Track
import kotlinx.coroutines.delay
import kotlin.math.roundToInt

/**
 * Очередь: играет сейчас + далее; ручка — перетаскивание (отпустить — переставить), «Выше/Ниже» — альтернатива
 * для TalkBack; тап — перейти; ✕ — убрать. Вверху — перемешать и «Очистить» (с подтверждением).
 */
@Composable
fun QueueView(st: AppState) {
    val hub = st.app.player
    val ui by hub.ui.collectAsState()
    val list = rememberLazyListState(initialFirstVisibleItemIndex = ui.index.coerceAtLeast(0))
    var dragIndex by remember { mutableStateOf<Int?>(null) }
    var dragOffset by remember { mutableFloatStateOf(0f) }
    var confirmClear by remember { mutableStateOf(false) }
    val rowPx = with(LocalDensity.current) { 72.dp.toPx() }
    val offline by st.app.offline.items.collectAsState()
    Column(Modifier.fillMaxSize()) {
        SourceLabel(st, ui.source, Modifier.padding(horizontal = 8.dp))
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Далее · ${(ui.queue.size - ui.index - 1).coerceAtLeast(0)}", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            IconButton(onClick = { hub.toggleShuffle() }) { Icon(Icons.Rounded.Shuffle, if (ui.shuffle) "Вернуть порядок" else "Перемешать") }
            IconButton(onClick = { confirmClear = true }) { Icon(Icons.Rounded.Clear, "Очистить очередь") }
        }
        LazyColumn(Modifier.fillMaxSize(), state = list) {
            itemsIndexed(ui.queue, key = { _, q -> q.uid }) { i, q ->
                val dragging = dragIndex == i
                Box(
                    Modifier.zIndex(if (dragging) 1f else 0f).graphicsLayer { translationY = if (dragging) dragOffset else 0f; shadowElevation = if (dragging) 8f else 0f }
                        // «Выше/Ниже» — действия TalkBack (видимые кнопки съедали ширину названия)
                        .semantics {
                            customActions = buildList {
                                if (i > 0) add(CustomAccessibilityAction("Выше") { hub.move(i, i - 1); true })
                                if (i < ui.queue.size - 1) add(CustomAccessibilityAction("Ниже") { hub.move(i, i + 1); true })
                            }
                        },
                ) {
                    TrackRow(
                        q.track, playing = i == ui.index, animating = ui.playing, onClick = { hub.jumpTo(i) }, onMore = null,
                        offline = offline[q.track.id]?.done == true,
                        leading = {
                            Icon(
                                Icons.Rounded.DragHandle, "Перетащить",
                                Modifier.padding(end = 8.dp).pointerInput(q.uid) {
                                    detectDragGestures(
                                        onDragStart = { dragIndex = i; dragOffset = 0f },
                                        onDragEnd = {
                                            val from = dragIndex
                                            if (from != null) hub.move(from, (from + (dragOffset / rowPx).roundToInt()).coerceIn(0, ui.queue.size - 1))
                                            dragIndex = null; dragOffset = 0f
                                        },
                                        onDragCancel = { dragIndex = null; dragOffset = 0f },
                                    ) { change, amount -> change.consume(); dragOffset += amount.y }
                                },
                                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        },
                        trailing = {
                            if (i != ui.index) IconButton(onClick = { hub.removeAt(i) }) { Icon(Icons.Rounded.Close, "Убрать из очереди") }
                        },
                    )
                }
            }
        }
    }
    if (confirmClear) AlertDialog(
        onDismissRequest = { confirmClear = false },
        title = { Text("Очистить очередь?") },
        text = { Text("Останется только текущий трек.") },
        confirmButton = { TextButton(onClick = { hub.clearUpcoming(); confirmClear = false }) { Text("Очистить") } },
        dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Отмена") } },
    )
}

/**
 * Текст песни: синхронный (.lrc) — подсветка текущей строки (primary), прошедшие 62 %, будущие 42 %, автопрокрутка
 * (ручная прокрутка ставит её на паузу на 4 с), тап по строке — перемотка. Без меток — обычный текст.
 */
@Composable
fun LyricsView(st: AppState, t: Track?) {
    if (t == null) { EmptyState(Icons.Rounded.Lyrics, "Ничего не играет", cookie = false); return }
    val l = rememberLoader("/lyrics/${t.id}", Lyrics.serializer())
    val hub = st.app.player
    Column(Modifier.fillMaxSize()) {
    LyricsActions(st, t, l)
    Loaded(l, empty = { it.synced.isNullOrEmpty() && it.plain.isNullOrBlank() }, emptyContent = {
        EmptyState(Icons.Rounded.Lyrics, "Текста нет", "Для этого трека не нашлось текста. «Найти текст заново» — поискать ещё раз.", cookie = false)
    }) { lyr ->
        val synced = lyr.synced
        val cs = MaterialTheme.colorScheme
        if (synced.isNullOrEmpty()) {
            LazyColumn(Modifier.fillMaxSize().padding(horizontal = 24.dp)) {
                item { Text(lyr.plain.orEmpty(), style = MaterialTheme.typography.titleLarge, color = cs.onSurface) }
                item { Spacer(Modifier.height(96.dp)) }
            }
            return@Loaded
        }
        val times = remember(synced) { synced.map { it.t } }
        var pos by remember { mutableLongStateOf(hub.position()) }
        LaunchedEffect(Unit) { while (true) { pos = hub.position(); delay(200) } }
        val cur = lyricIndex(times, pos / 1000.0)
        val list = rememberLazyListState()
        var userScrollAt by remember { mutableLongStateOf(0L) }
        LaunchedEffect(list.isScrollInProgress) { if (list.isScrollInProgress) userScrollAt = System.currentTimeMillis() }
        LaunchedEffect(cur) {
            if (cur >= 0 && System.currentTimeMillis() - userScrollAt > 4000) {
                val h = list.layoutInfo.viewportSize.height
                list.animateScrollToItem(cur, -(h * 0.38f).toInt())
            }
        }
        LazyColumn(Modifier.fillMaxSize(), state = list) {
            item { Spacer(Modifier.height(24.dp)) }
            itemsIndexed(synced) { i, line ->
                val alpha = when { i == cur -> 1f; i < cur -> 0.62f; else -> 0.42f }
                Text(
                    line.text.ifBlank { "♪" },
                    style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
                    color = if (i == cur) cs.primary else cs.onSurface.copy(alpha = alpha),
                    modifier = Modifier.fillMaxWidth().clickable { hub.seek((line.t * 1000).toLong()) }.padding(horizontal = 24.dp, vertical = 8.dp)
                        .graphicsLayer { val s = if (i == cur) 1.03f else 1f; scaleX = s; scaleY = s },
                )
            }
            item { Spacer(Modifier.height(240.dp)) }
        }
    }
    }
}

/**
 * «Найти текст заново» (POST /api/tracks/{id}/lyrics/refetch, до 100 с) и «Убрать текст» (clear — только для своей
 * медиатеки: файлы текста общие у всех, у кого есть трек). Показанный текст заменяется ответом, итог — снекбар.
 */
@Composable
private fun LyricsActions(st: AppState, t: Track, l: Loader<Lyrics>) {
    var busy by remember(t.id) { mutableStateOf(false) }
    fun refetch(clear: Boolean) {
        busy = true
        st.scope.launch {
            try {
                val body = if (clear) kotlinx.serialization.json.buildJsonObject { put("clear", true) } else null
                val r = st.app.api.post("/tracks/${t.id}/lyrics/refetch", body, Lyrics.serializer())
                l.data = r
                l.error = null
                st.msg(when (r.status) {
                    "updated" -> "Текст обновлён"
                    "same" -> "Нашёлся тот же текст"
                    "cleared" -> "Текст убран"
                    "not_found" -> if (clear) "Другого текста нет — текст оставлен" else "Лучшего текста не нашлось — оставлен прежний"
                    else -> "Готово"
                })
            } catch (e: io.github.ikriu.pimusic.data.ApiException) {
                st.msg(when (e.status) {
                    409 -> "Текст этого трека уже ищется"
                    429 -> "Слишком часто: не больше 20 поисков текста в час"
                    504 -> "Сервисы текстов не ответили — ничего не изменено"
                    else -> e.message
                })
            } finally { busy = false }
        }
    }
    Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), horizontalArrangement = Arrangement.End, verticalAlignment = Alignment.CenterVertically) {
        if (busy) {
            androidx.compose.material3.CircularWavyProgressIndicator(Modifier.size(24.dp))
            Spacer(Modifier.width(8.dp))
            Text("Ищу текст…", style = MaterialTheme.typography.labelLarge)
        } else {
            TextButton(onClick = { refetch(false) }, modifier = Modifier.heightIn(min = 48.dp)) {
                Icon(Icons.Rounded.Refresh, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Найти текст заново")
            }
            if (t.owned && l.data?.let { !it.synced.isNullOrEmpty() || !it.plain.isNullOrBlank() } == true) {
                TextButton(onClick = { refetch(true) }, modifier = Modifier.heightIn(min = 48.dp)) { Text("Убрать текст") }
            }
        }
    }
}
