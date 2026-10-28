package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.AutoAwesome
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.LibAlbums
import io.github.ikriu.pimusic.data.WaveDiscover
import io.github.ikriu.pimusic.data.WaveRefresh
import io.github.ikriu.pimusic.player.Mode
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.MorphPlayButton
import io.github.ikriu.pimusic.ui.SectionTitle
import io.github.ikriu.pimusic.ui.rememberCoverSource
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.ui.rememberWaveNow
import io.github.ikriu.pimusic.ui.theme.ContentTheme
import kotlinx.coroutines.launch

/** «Моя Волна»: hero-карточка (коллаж обложек, контент-цвет), cookie-кнопка «Слушать», «Обновить плейлист», «Найти новое». */
@Composable
fun WaveScreen() {
    val st = LocalAppState.current
    val hub = st.app.player
    val ui by hub.ui.collectAsState()
    val onAir = ui.mode == Mode.WAVE
    val now = rememberWaveNow(st, true)
    val albums = rememberLoader("/library/albums?sort=added&order=desc", LibAlbums.serializer())
    val covers = albums.data?.items.orEmpty().mapNotNull { it.cover }.take(4)
    val src = rememberCoverSource(now?.cover ?: covers.firstOrNull())
    var busy by remember { mutableStateOf<String?>(null) }
    var found by remember { mutableStateOf<List<String>>(emptyList()) }
    ContentTheme(src) {
        LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
            item { TabHeader("Моя Волна") }
            item {
                Surface(
                    color = MaterialTheme.colorScheme.primaryContainer, shape = RoundedCornerShape(48.dp),
                    modifier = Modifier.fillMaxWidth().padding(16.dp),
                ) {
                    Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                        Box(Modifier.widthIn(max = 280.dp).fillMaxWidth().aspectRatio(1f)) { Mosaic(if (now?.cover != null) listOf(now.cover) else covers, Modifier.fillMaxSize(), 32) }
                        Spacer(Modifier.size(16.dp))
                        Text(
                            if (onAir) (now?.title ?: "В эфире") else "Бесконечный эфир из вашей медиатеки",
                            style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.SemiBold), color = MaterialTheme.colorScheme.onPrimaryContainer,
                        )
                        if (onAir && now?.artist != null) Text(now.artist, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onPrimaryContainer)
                        if (now != null && now.listeners > 0) Text("Слушают: ${now.listeners}", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onPrimaryContainer)
                        Spacer(Modifier.size(16.dp))
                        MorphPlayButton(onAir && ui.playing, 96.dp, {
                            if (onAir) hub.toggle()
                            else {
                                val url = st.me?.streamUrl
                                if (url == null) { st.refreshMe(); st.msg("Эфир пока недоступен — попробуйте через минуту") } else hub.playWave(url)
                            }
                        })
                    }
                }
            }
            item {
                Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilledTonalButton(enabled = busy == null, onClick = {
                        busy = "refresh"
                        st.scope.launch {
                            try {
                                val r = st.app.api.post("/wave/refresh", null, WaveRefresh.serializer())
                                st.msg("Плейлист Волны обновлён: ${r.tracks} тр.")
                            } catch (e: ApiException) { st.msg(e.message) } finally { busy = null }
                        }
                    }, modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) { Icon(Icons.Rounded.Refresh, null); Spacer(Modifier.width(8.dp)); Text("Обновить плейлист", style = MaterialTheme.typography.titleMedium) }
                    FilledTonalButton(enabled = busy == null, onClick = {
                        busy = "discover"
                        st.scope.launch {
                            try {
                                val r = st.app.api.post("/wave/discover", null, WaveDiscover.serializer())
                                found = r.queued
                                st.msg(if (r.queued.isEmpty()) "Новых треков не нашлось" else "Качаю новое: ${r.queued.size} тр.")
                            } catch (e: ApiException) { st.msg(e.message) } finally { busy = null }
                        }
                    }, modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) { Icon(Icons.Rounded.AutoAwesome, null); Spacer(Modifier.width(8.dp)); Text("Найти новое", style = MaterialTheme.typography.titleMedium) }
                }
            }
            if (busy != null) item { Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) { LoadingIndicator() } }
            if (found.isNotEmpty()) {
                item { SectionTitle("Добавляется в медиатеку") }
                items(found) { Text(it, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp)) }
            }
        }
    }
}
