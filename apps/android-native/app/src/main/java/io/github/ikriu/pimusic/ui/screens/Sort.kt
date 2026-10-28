package io.github.ikriu.pimusic.ui.screens

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.Sort
import androidx.compose.material.icons.rounded.ArrowDownward
import androidx.compose.material.icons.rounded.ArrowUpward
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material.icons.rounded.RadioButtonChecked
import androidx.compose.material.icons.rounded.RadioButtonUnchecked
import androidx.compose.material3.AssistChip
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.widthIn
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.data.Genre
import io.github.ikriu.pimusic.ui.ConnectedGroup

/** Варианты сортировки (как SORTS в libsort.js): ключ, подпись, направление по умолчанию. */
object Sorts {
    val TRACKS = listOf(
        Triple("added", "По дате добавления", "desc"), Triple("title", "По названию", "asc"), Triple("artist", "По исполнителю", "asc"),
        Triple("album", "По альбому", "asc"), Triple("genre", "По жанру", "asc"), Triple("year", "По году", "desc"),
        Triple("duration", "По длительности", "asc"), Triple("random", "В случайном порядке", "asc"),
    )
    val ARTISTS = listOf(Triple("name", "По имени", "asc"), Triple("count", "По числу треков", "desc"), Triple("added", "По дате добавления", "desc"), Triple("year", "По году", "desc"))
    val ALBUMS = listOf(Triple("name", "По названию", "asc"), Triple("count", "По числу треков", "desc"), Triple("added", "По дате добавления", "desc"), Triple("year", "По году", "desc"))
    val PLAYLISTS = listOf(Triple("updated", "По изменению", "desc"), Triple("name", "По названию", "asc"), Triple("count", "По числу треков", "desc"))
    val PLTRACKS = listOf(
        Triple("manual", "Порядок плейлиста", "asc"), Triple("added", "По дате добавления", "desc"), Triple("title", "По названию", "asc"),
        Triple("artist", "По исполнителю", "asc"), Triple("album", "По альбому", "asc"), Triple("genre", "По жанру", "asc"),
        Triple("duration", "По длительности", "asc"), Triple("year", "По году", "desc"), Triple("random", "В случайном порядке", "asc"),
    )
    fun noDir(sort: String) = sort == "random" || sort == "manual"
}

data class SortState(val sort: String, val order: String, val seed: Long = System.currentTimeMillis() % 1_000_000)

/** Сортировка и жанр — свои у каждого вида, хранятся на устройстве (как localStorage у Mini App). */
class SortStore(ctx: Context) {
    private val p = ctx.getSharedPreferences("libsort", Context.MODE_PRIVATE)
    fun get(view: String, opts: List<Triple<String, String, String>>): SortState {
        val s = p.getString("s:$view", null)?.split(":")
        val def = opts.first()
        val sort = s?.getOrNull(0)?.takeIf { k -> opts.any { it.first == k } } ?: def.first
        val order = s?.getOrNull(1)?.takeIf { it == "asc" || it == "desc" } ?: opts.first { it.first == sort }.third
        return SortState(sort, order)
    }
    fun set(view: String, s: SortState) { p.edit().putString("s:$view", "${s.sort}:${s.order}").apply() }
    fun genre(view: String): String = p.getString("g:$view", "") ?: ""
    fun setGenre(view: String, g: String) { p.edit().putString("g:$view", g).apply() }
}

fun sortLabel(s: SortState, opts: List<Triple<String, String, String>>): String {
    val label = opts.firstOrNull { it.first == s.sort }?.second ?: s.sort
    return if (Sorts.noDir(s.sort)) label else label + if (s.order == "asc") " ↑" else " ↓"
}

/** Короткая подпись для чипа (полная — в листе и в подписи для TalkBack). */
private val SHORT = mapOf(
    "added" to "Добавлены", "title" to "Название", "artist" to "Исполнитель", "album" to "Альбом", "genre" to "Жанр",
    "year" to "Год", "duration" to "Длительность", "random" to "Случайно", "name" to "Название", "count" to "Треков",
    "updated" to "Изменены", "manual" to "Порядок",
)
fun sortChipLabel(s: SortState) = (SHORT[s.sort] ?: s.sort) + if (Sorts.noDir(s.sort)) "" else if (s.order == "asc") " ↑" else " ↓"

/** Чип «Сортировка» → лист: направление (connected group) и варианты (радио). */
@Composable
fun SortChip(view: String, opts: List<Triple<String, String, String>>, state: SortState, onChange: (SortState) -> Unit) {
    val ctx = LocalContext.current
    var open by remember { mutableStateOf(false) }
    AssistChip(
        onClick = { open = true },
        label = { Text(sortChipLabel(state), maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis) },
        leadingIcon = { Icon(Icons.AutoMirrored.Rounded.Sort, null, Modifier.size(18.dp)) },
        modifier = Modifier.widthIn(max = 200.dp).semantics { contentDescription = "Сортировка: " + sortLabel(state, opts).replace(" ↑", ", по возрастанию").replace(" ↓", ", по убыванию") },
    )
    if (open) ModalBottomSheet(onDismissRequest = { open = false }) {
        Column(Modifier.navigationBarsPadding()) {
            Text("Сортировка", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(horizontal = 24.dp))
            if (!Sorts.noDir(state.sort)) {
                ConnectedGroup(
                    listOf("asc" to "По возрастанию", "desc" to "По убыванию"), state.order,
                    { o -> val n = state.copy(order = o); SortStore(ctx).set(view, n); onChange(n) },
                    Modifier.padding(horizontal = 24.dp, vertical = 12.dp),
                    icons = mapOf("asc" to Icons.Rounded.ArrowUpward, "desc" to Icons.Rounded.ArrowDownward),
                )
            }
            opts.forEach { (k, label, def) ->
                Row(
                    Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable {
                        val n = SortState(k, if (k == state.sort) state.order else def, System.currentTimeMillis() % 1_000_000)
                        SortStore(ctx).set(view, n); onChange(n); open = false
                    }.padding(horizontal = 24.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(if (k == state.sort) Icons.Rounded.RadioButtonChecked else Icons.Rounded.RadioButtonUnchecked, null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(20.dp))
                    Text(label, style = MaterialTheme.typography.bodyLarge)
                }
            }
            Spacer(Modifier.size(16.dp))
        }
    }
}

/** Ряд жанров: «Все жанры» + частые + «Ещё…» (лист со всеми). */
@Composable
fun GenreRow(genres: List<Genre>, current: String, onPick: (String) -> Unit) {
    if (genres.isEmpty()) return
    var all by remember { mutableStateOf(false) }
    val top = genres.take(8)
    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp), contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 16.dp)) {
        item { FilterChip(current.isEmpty(), { onPick("") }, label = { Text("Все жанры") }) }
        if (current.isNotEmpty() && top.none { it.name == current }) item { FilterChip(true, { onPick("") }, label = { Text("$current ✕") }) }
        items(top, key = { it.name }) { g -> FilterChip(current == g.name, { onPick(if (current == g.name) "" else g.name) }, label = { Text(g.name) }) }
        if (genres.size > top.size) item { AssistChip({ all = true }, label = { Text("Ещё…") }, trailingIcon = { Icon(Icons.Rounded.ExpandMore, null, Modifier.size(18.dp)) }) }
    }
    if (all) ModalBottomSheet(onDismissRequest = { all = false }) {
        LazyColumn(Modifier.navigationBarsPadding()) {
            item { Text("Жанры", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp)) }
            items(listOf(Genre("", 0)) + genres, key = { "g" + it.name }) { g ->
                Row(
                    Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable { onPick(g.name); all = false }.padding(horizontal = 24.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(if (current == g.name) Icons.Rounded.RadioButtonChecked else Icons.Rounded.RadioButtonUnchecked, null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(20.dp))
                    Column {
                        Text(g.name.ifEmpty { "Все жанры" }, style = MaterialTheme.typography.bodyLarge)
                        if (g.count > 0) Text(nTracks(g.count.toLong()), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}
