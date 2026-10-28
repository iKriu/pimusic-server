package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.DeleteSweep
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material3.CircularWavyProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.fmtBytes
import io.github.ikriu.pimusic.core.nTracks
import io.github.ikriu.pimusic.ui.ConfirmDialog
import io.github.ikriu.pimusic.ui.EmptyState
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.SectionTitle
import io.github.ikriu.pimusic.ui.TrackMenu
import io.github.ikriu.pimusic.ui.TrackRow
import io.github.ikriu.pimusic.player.PlaySource

/**
 * «Скачанное»: треки на устройстве (не вытесняются, работают без сети) — размер, прогресс, удаление; группы — альбом/
 * плейлист, из которых скачивали. «Слушать» — всё скачанное очередью.
 */
@Composable
fun OfflineScreen() {
    val st = LocalAppState.current
    val items by st.app.offline.items.collectAsState()
    val ui by st.app.player.ui.collectAsState()
    var confirm by remember { mutableStateOf(false) }
    val all = items.values.sortedByDescending { it.updated }
    val done = all.filter { it.done }
    Column(Modifier.fillMaxSize()) {
        PageBar("Скачанное") {
            if (all.isNotEmpty()) IconButton(onClick = { confirm = true }) { Icon(Icons.Rounded.DeleteSweep, "Удалить всё") }
        }
        if (all.isEmpty()) {
            EmptyState(Icons.Rounded.DownloadForOffline, "Здесь пока пусто", "В меню трека, альбома или плейлиста — «Скачать на устройство». Скачанное играет без сети и хранится, пока вы его не удалите.")
            return@Column
        }
        LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
            item {
                Text("${nTracks(done.size.toLong())} · ${fmtBytes(done.sumOf { it.bytes })}" + (if (done.size < all.size) " · ещё ${all.size - done.size} в работе" else ""),
                    style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 16.dp))
            }
            item {
                val tracks = done.map { it.track }
                PlayButtons({ st.app.player.playList(tracks, 0, false, PlaySource.OFFLINE) }, { st.app.player.playList(tracks, 0, true, PlaySource.OFFLINE) })
            }
            for ((group, list) in all.groupBy { it.group ?: "" }.toSortedMap()) {
                item(key = "g$group") { SectionTitle(group.ifEmpty { "Отдельные треки" }) }
                items(list, key = { "o" + it.track.id }) { o ->
                    val tracks = done.map { it.track }
                    TrackRow(
                        o.track, playing = ui.current?.id == o.track.id, animating = ui.playing,
                        onClick = { if (o.done) st.app.player.playList(tracks, tracks.indexOfFirst { it.id == o.track.id }.coerceAtLeast(0), false, PlaySource.OFFLINE) },
                        onMore = { st.menu = TrackMenu(o.track) }, offline = o.done,
                        trailing = {
                            Box(contentAlignment = Alignment.Center) {
                                when {
                                    o.active -> CircularWavyProgressIndicator(progress = { (o.percent / 100f).coerceIn(0f, 1f) }, modifier = Modifier.size(28.dp))
                                    o.failed -> IconButton(onClick = { st.saveOffline(listOf(o.track), o.group) }) { Icon(Icons.Rounded.Refresh, "Повторить") }
                                    else -> Text(fmtBytes(o.bytes), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                            IconButton(onClick = { st.app.offline.remove(o.track.id) }) { Icon(Icons.Rounded.Close, "Удалить с устройства") }
                        },
                    )
                }
            }
        }
    }
    if (confirm) ConfirmDialog("Удалить всё скачанное?", "Треки останутся в медиатеке на сервере.", "Удалить", { confirm = false }) {
        confirm = false
        st.app.offline.removeAll()
    }
}
