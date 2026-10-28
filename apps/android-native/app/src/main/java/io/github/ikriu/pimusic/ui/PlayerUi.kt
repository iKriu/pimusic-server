package io.github.ikriu.pimusic.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.PlaylistAdd
import androidx.compose.material.icons.automirrored.rounded.QueueMusic
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Equalizer
import androidx.compose.material.icons.rounded.KeyboardArrowDown
import androidx.compose.material.icons.rounded.Lyrics
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Repeat
import androidx.compose.material.icons.rounded.RepeatOne
import androidx.compose.material.icons.rounded.Shuffle
import androidx.compose.material.icons.rounded.SkipNext
import androidx.compose.material.icons.rounded.SkipPrevious
import androidx.compose.material.icons.rounded.Timer
import androidx.compose.material3.AssistChip
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Surface
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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.util.VelocityTracker
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.graphics.shapes.Morph
import io.github.ikriu.pimusic.core.RepeatMode
import io.github.ikriu.pimusic.core.fmtTime
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.data.WaveNow
import io.github.ikriu.pimusic.player.Mode
import io.github.ikriu.pimusic.player.PlayerHub
import io.github.ikriu.pimusic.ui.screens.EqScreen
import io.github.ikriu.pimusic.ui.theme.ContentTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.roundToInt

/** Позиция/длительность плеера для UI (опрос раз в 250 мс, пока экран виден). */
@Composable
fun rememberProgress(hub: PlayerHub): Pair<Long, Long> {
    var pos by remember { mutableLongStateOf(hub.position()) }
    var dur by remember { mutableLongStateOf(hub.duration()) }
    LaunchedEffect(hub) {
        while (true) {
            pos = hub.position(); dur = hub.duration()
            delay(250)
        }
    }
    return pos to dur
}

/** Кнопка ▶/❚❚ M3E: на паузе — cookie9, играет — круг (морфинг формы, пружина). */
@Composable
fun MorphPlayButton(playing: Boolean, size: Dp, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val morph = remember { Morph(MaterialShapes.Cookie9Sided, MaterialShapes.Circle) }
    val p by animateFloatAsState(if (playing) 1f else 0f, MaterialTheme.motionScheme.defaultSpatialSpec(), label = "morph")
    val cs = MaterialTheme.colorScheme
    Box(
        modifier.size(size).clip(MorphShape(morph, p)).background(cs.primary)
            .clickable(onClick = onClick)
            .semantics { contentDescription = if (playing) "Пауза" else "Играть" },
        contentAlignment = Alignment.Center,
    ) {
        Icon(if (playing) Icons.Rounded.Pause else Icons.Rounded.PlayArrow, null, tint = cs.onPrimary, modifier = Modifier.size(size * 0.45f))
    }
}

/**
 * Перемотка: активная часть — волнистая (M3E), на паузе — прямая; доступный слайдер поверх (role=slider,
 * подпись «1:23 из 4:46»).
 */
@Composable
fun SeekBar(hub: PlayerHub, playing: Boolean, modifier: Modifier = Modifier) {
    val (pos, dur) = rememberProgress(hub)
    var drag by remember { mutableStateOf<Float?>(null) }
    val frac = if (dur > 0) (drag ?: (pos.toFloat() / dur)).coerceIn(0f, 1f) else 0f
    val cs = MaterialTheme.colorScheme
    Column(modifier) {
        Box(Modifier.fillMaxWidth().height(40.dp), contentAlignment = Alignment.Center) {
            if (playing && drag == null) LinearWavyProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth().height(10.dp), color = cs.primary, trackColor = cs.surfaceContainerHighest)
            else LinearProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth().height(8.dp), color = cs.primary, trackColor = cs.surfaceContainerHighest)
            Slider(
                value = frac,
                onValueChange = { drag = it },
                onValueChangeFinished = { drag?.let { hub.seek((it * dur).toLong()) }; drag = null },
                colors = SliderDefaults.colors(activeTrackColor = androidx.compose.ui.graphics.Color.Transparent, inactiveTrackColor = androidx.compose.ui.graphics.Color.Transparent, thumbColor = cs.primary),
                modifier = Modifier.fillMaxWidth().semantics { stateDescription = "${fmtTime((frac * dur / 1000).toLong())} из ${fmtTime(dur / 1000)}" },
            )
        }
        Row(Modifier.fillMaxWidth()) {
            Text(fmtTime(((drag ?: frac) * dur / 1000).toLong()), style = MaterialTheme.typography.labelMedium, color = cs.onSurfaceVariant)
            Spacer(Modifier.weight(1f))
            Text(fmtTime(dur / 1000), style = MaterialTheme.typography.labelMedium, color = cs.onSurfaceVariant)
        }
    }
}

/** «Моя Волна» сейчас: опрос /api/wave/now раз в 15 с, пока виден. */
@Composable
fun rememberWaveNow(st: AppState, active: Boolean): WaveNow? {
    // пока играет Волна, её опрашивает PlayerHub (метаданные уведомления) — берём оттуда
    val hubNow by st.app.player.waveNow.collectAsState()
    var now by remember { mutableStateOf<WaveNow?>(null) }
    LaunchedEffect(active, hubNow == null) {
        while (active && hubNow == null) {
            runCatching { st.app.api.get("/wave/now", WaveNow.serializer()) }.onSuccess { now = it }
            delay(15_000)
        }
    }
    return hubNow ?: now
}

/**
 * Мини-плеер: обложка, название/исполнитель, ▶/❚❚, ⏭, линия прогресса 3dp. Смахивание влево — следующий, вправо —
 * предыдущий; тап — «Сейчас играет». Волна — «В эфире» и ✕.
 */
@Composable
fun MiniPlayer(st: AppState) {
    val hub = st.app.player
    val ui by hub.ui.collectAsState()
    val wave = ui.mode == Mode.WAVE
    val waveNow = rememberWaveNow(st, wave)
    val t = ui.current
    val coverUrl = if (wave) waveNow?.cover else t?.cover
    val src = rememberCoverSource(coverUrl)
    val scope = rememberCoroutineScope()
    val offset = remember { Animatable(0f) }
    val threshold = with(LocalDensity.current) { 96.dp.toPx() }
    ContentTheme(src) {
        val cs = MaterialTheme.colorScheme
        Surface(color = cs.surfaceContainerHigh, modifier = Modifier.fillMaxWidth()) {
            Column {
                Row(
                    Modifier.fillMaxWidth().height(64.dp)
                        .clickable { if (wave) st.select(Tab.WAVE) else st.openPlayer() }
                        .pointerInput(wave) {
                            if (wave) return@pointerInput
                            // ось выбирается по первому движению: по горизонтали — следующий/предыдущий,
                            // вверх — выдвинуть «Сейчас играет» (лист следует за пальцем)
                            var axis = 0
                            val vt = VelocityTracker()
                            detectDragGestures(
                                onDragStart = { axis = 0; vt.resetTracking() },
                                onDragEnd = {
                                    if (axis == 1) {
                                        val v = offset.value
                                        scope.launch {
                                            if (v < -threshold) hub.next() else if (v > threshold) hub.prev()
                                            offset.animateTo(0f)
                                        }
                                    } else if (axis == 2) st.sheet?.settle(vt.calculateVelocity().y)
                                },
                                onDragCancel = { scope.launch { offset.animateTo(0f) }; if (axis == 2) st.sheet?.settle(0f) },
                            ) { change, d ->
                                vt.addPosition(change.uptimeMillis, change.position)
                                if (axis == 0) axis = if (abs(d.x) > abs(d.y)) 1 else 2
                                change.consume()
                                if (axis == 1) scope.launch { offset.snapTo(offset.value + d.x) } else st.sheet?.drag(d.y)
                            }
                        }
                        .padding(horizontal = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Row(Modifier.weight(1f).offset { IntOffset(offset.value.roundToInt(), 0) }.graphicsLayer { alpha = 1f - (abs(offset.value) / (threshold * 2)).coerceIn(0f, 0.6f) }, verticalAlignment = Alignment.CenterVertically) {
                        Cover(coverUrl, 48.dp)
                        Spacer(Modifier.size(12.dp))
                        Column(Modifier.weight(1f)) {
                            Text(if (wave) (waveNow?.title ?: "Моя Волна") else t?.title.orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.titleSmall)
                            Text(if (wave) (waveNow?.artist?.let { "В эфире · $it" } ?: "В эфире") else t?.artist.orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis,
                                style = MaterialTheme.typography.bodySmall, color = cs.onSurfaceVariant)
                        }
                    }
                    MorphPlayButton(ui.playing, 44.dp, { hub.toggle() })
                    if (wave) IconButton(onClick = { hub.stopWave() }) { Icon(Icons.Rounded.Close, "Выключить волну") }
                    else IconButton(onClick = { hub.next() }) { Icon(Icons.Rounded.SkipNext, "Следующий трек") }
                }
                if (!wave) {
                    val (pos, dur) = rememberProgress(hub)
                    LinearProgressIndicator(
                        progress = { if (dur > 0) (pos.toFloat() / dur).coerceIn(0f, 1f) else 0f },
                        modifier = Modifier.fillMaxWidth().height(3.dp), color = cs.primary, trackColor = cs.surfaceContainerHighest,
                        drawStopIndicator = {},
                    )
                }
            }
        }
    }
}

/** Плитка инструментов «Сейчас играет»: иконка над подписью (tonal, 56dp). */
@Composable
private fun ToolTile(icon: androidx.compose.ui.graphics.vector.ImageVector, label: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Surface(onClick = onClick, shape = MaterialTheme.shapes.large, color = MaterialTheme.colorScheme.secondaryContainer, modifier = modifier.heightIn(min = 56.dp)) {
        Column(Modifier.padding(vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
            Icon(icon, null, Modifier.size(22.dp))
            FitText(label, MaterialTheme.typography.labelLarge, Modifier.padding(horizontal = 4.dp), minScale = 0.85f)
        }
    }
}

/**
 * «Сейчас играет»: compact — полноэкранный лист (свайп вниз — свернуть), expanded — панель справа.
 * Виды: главный (обложка → название → перемотка → управление → инструменты), очередь, текст, эквалайзер.
 */
@Composable
fun NowPlaying(st: AppState, panel: Boolean, sheet: PlayerSheet? = null) {
    val hub = st.app.player
    val ui by hub.ui.collectAsState()
    val t = ui.current
    val src = rememberCoverSource(t?.cover)
    ContentTheme(src) {
        val cs = MaterialTheme.colorScheme
        Surface(color = cs.surface, modifier = Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                var menu by remember { mutableStateOf(false) }
                val dragState = rememberDraggableState { d -> sheet?.drag(d) }
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp)
                        // свайп вниз по шапке — свернуть (лист следует за пальцем, отпускание — пружина)
                        .draggable(dragState, Orientation.Vertical, enabled = sheet != null, onDragStopped = { v -> sheet?.settle(v) }),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    IconButton(onClick = {
                        if (st.playerView != PlayerView.MAIN) st.playerView = PlayerView.MAIN
                        else { st.playerOpen = false; if (panel) st.panelDismissed = true }
                    }) {
                        Icon(Icons.Rounded.KeyboardArrowDown, "Свернуть")
                    }
                    Text(
                        when (st.playerView) { PlayerView.QUEUE -> "Очередь"; PlayerView.LYRICS -> "Текст"; PlayerView.EQ -> "Эквалайзер"; else -> "Сейчас играет" },
                        style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f), textAlign = TextAlign.Center,
                    )
                    Box {
                        IconButton(onClick = { menu = true }) { Icon(Icons.Rounded.MoreVert, "Ещё") }
                        PlayerMenu(st, t, menu) { menu = false }
                    }
                }
                when (st.playerView) {
                    PlayerView.MAIN -> PlayerMain(st, t, ui.playing, ui.shuffle, ui.repeat, ui.queue.size - ui.index - 1, panel)
                    PlayerView.QUEUE -> QueueView(st)
                    PlayerView.LYRICS -> LyricsView(st, t)
                    PlayerView.EQ -> EqScreen(null, embedded = true, onBack = { st.playerView = PlayerView.MAIN })
                }
            }
        }
    }
}

@Composable
private fun PlayerMenu(st: AppState, t: Track?, open: Boolean, close: () -> Unit) {
    var sheet by remember { mutableStateOf<String?>(null) }
    DropdownMenu(open, close) {
        DropdownMenuItem(text = { Text("Таймер сна") }, onClick = { close(); sheet = "sleep" })
        DropdownMenuItem(text = { Text("Скорость") }, onClick = { close(); sheet = "speed" })
        DropdownMenuItem(text = { Text("Плавный переход") }, onClick = { close(); sheet = "xf" })
        if (t != null) {
            DropdownMenuItem(text = { Text("Действия с треком…") }, onClick = { close(); st.menu = TrackMenu(t) })
        }
        DropdownMenuItem(text = { Text("Настройки воспроизведения") }, onClick = { close(); st.push(Screen.Settings) })
    }
    when (sheet) {
        "sleep" -> SleepSheet(st) { sheet = null }
        "speed" -> SpeedSheet(st) { sheet = null }
        "xf" -> CrossfadeSheet(st) { sheet = null }
    }
}

@Composable
private fun PlayerMain(st: AppState, t: Track?, playing: Boolean, shuffle: Boolean, repeat: RepeatMode, upcoming: Int, panel: Boolean) {
    val ui by st.app.player.ui.collectAsState()
    val hub = st.app.player
    val cs = MaterialTheme.colorScheme
    val sleep by hub.sleep.collectAsState()
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val landscape = maxWidth > maxHeight && maxHeight < 480.dp
        val coverSize = if (landscape) (maxHeight - 32.dp) else minOf(maxWidth - 48.dp, maxHeight * 0.45f, 420.dp)
        // обложка «дышит»: на паузе скругление 32 → 48dp и масштаб 0,9
        val radius by animateDpAsState(if (playing) 32.dp else 48.dp, MaterialTheme.motionScheme.slowSpatialSpec(), label = "r")
        val scale by animateFloatAsState(if (playing) 1f else 0.9f, MaterialTheme.motionScheme.slowSpatialSpec(), label = "s")
        val coverBox: @Composable () -> Unit = {
            if (ui.queue.size > 1 && ui.index >= 0) CoverCarousel(st, ui.queue.map { it.track }, ui.index, coverSize, radius, scale)
            else Cover(t?.cover, coverSize, RoundedCornerShape(radius), Modifier.scale(scale))
        }
        val info: @Composable (Modifier) -> Unit = { m ->
            Column(m.padding(horizontal = 24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                if (sleep != null) {
                    AssistChip(onClick = { hub.cancelSleep() }, label = {
                        Text(sleep?.untilMs?.let { "Сон · " + java.text.SimpleDateFormat("HH:mm", java.util.Locale.ROOT).format(java.util.Date(it)) } ?: "Сон · до конца трека")
                    }, leadingIcon = { Icon(Icons.Rounded.Timer, null, Modifier.size(18.dp)) })
                }
                Text(t?.title.orEmpty(), style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.SemiBold), textAlign = TextAlign.Center, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Row(horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                    if (!t?.artist.isNullOrEmpty()) TextButton(enabled = t?.owned == true, onClick = { t?.let { st.goArtist(it) } }) { Text(t?.artist.orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis) }
                    if (!t?.album.isNullOrEmpty()) TextButton(enabled = t?.owned == true, onClick = { t?.let { st.goAlbum(it) } }) { Text(t?.album.orEmpty(), maxLines = 1, overflow = TextOverflow.Ellipsis, color = cs.onSurfaceVariant) }
                }
                SeekBar(hub, playing, Modifier.fillMaxWidth())
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
                    ToggleIcon(shuffle, if (shuffle) "Перемешивание включено" else "Перемешать", Icons.Rounded.Shuffle) { hub.toggleShuffle() }
                    IconButton(onClick = { hub.prev() }, Modifier.size(56.dp)) { Icon(Icons.Rounded.SkipPrevious, "Предыдущий", Modifier.size(32.dp)) }
                    MorphPlayButton(playing, 80.dp, { hub.toggle() })
                    IconButton(onClick = { hub.next() }, Modifier.size(56.dp)) { Icon(Icons.Rounded.SkipNext, "Следующий", Modifier.size(32.dp)) }
                    ToggleIcon(repeat != RepeatMode.OFF, when (repeat) { RepeatMode.OFF -> "Повтор выключен"; RepeatMode.ALL -> "Повтор всего"; RepeatMode.ONE -> "Повтор трека" },
                        if (repeat == RepeatMode.ONE) Icons.Rounded.RepeatOne else Icons.Rounded.Repeat) { hub.cycleRepeat() }
                }
                Spacer(Modifier.height(12.dp))
                // ряд из 4 плиток; тесно (крупный шрифт, узкая панель) — сетка 2×2, подписи не мельчают
                BoxWithConstraints(Modifier.fillMaxWidth()) {
                    val fs = androidx.compose.ui.platform.LocalDensity.current.fontScale
                    val grid = maxWidth / 4 < 92.dp * fs
                    val tiles: List<Triple<androidx.compose.ui.graphics.vector.ImageVector, String, () -> Unit>> = listOf(
                        Triple(Icons.Rounded.Lyrics, "Текст") { st.playerView = PlayerView.LYRICS },
                        Triple(Icons.Rounded.Equalizer, "Эквалайзер") { st.playerView = PlayerView.EQ },
                        Triple(Icons.AutoMirrored.Rounded.PlaylistAdd, "В плейлист") { t?.let { st.addToPlaylist = listOf(it) } },
                        Triple(Icons.AutoMirrored.Rounded.QueueMusic, "Очередь · ${upcoming.coerceAtLeast(0)}") { st.playerView = PlayerView.QUEUE },
                    )
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        for (row in tiles.chunked(if (grid) 2 else 4)) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            row.forEach { (ic, label, go) -> ToolTile(ic, label, Modifier.weight(1f), onClick = go) }
                        }
                    }
                }
            }
        }
        if (landscape) {
            Row(Modifier.fillMaxSize().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(coverSize), contentAlignment = Alignment.Center) { coverBox() }
                info(Modifier.weight(1f).verticalScroll(rememberScrollState()))
            }
        } else {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(bottom = 16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                SourceLabel(st, ui.source)
                Spacer(Modifier.height(8.dp))
                Box(Modifier.fillMaxWidth().height(coverSize), contentAlignment = Alignment.Center) { coverBox() }
                Spacer(Modifier.height(16.dp))
                info(Modifier.fillMaxWidth().widthIn(max = 560.dp))
                // панель на широком экране: «Далее» — ближайшие треки прямо под управлением
                if (panel && ui.index >= 0 && ui.index + 1 < ui.queue.size) {
                    SectionTitle("Далее", Modifier.fillMaxWidth())
                    ui.queue.drop(ui.index + 1).take(5).forEachIndexed { k, q ->
                        TrackRow(q.track, playing = false, animating = false, onClick = { st.app.player.jumpTo(ui.index + 1 + k) }, onMore = { st.menu = TrackMenu(q.track) })
                    }
                    TextButton(onClick = { st.playerView = PlayerView.QUEUE }) { Text("Вся очередь · ${ui.queue.size - ui.index - 1}") }
                }
            }
        }
    }
}

/**
 * Карусель обложек очереди (hero, соседние видны по краям): смахивание — к соседнему треку без смены play/pause;
 * текущая «дышит» вместе с воспроизведением.
 */
@Composable
private fun CoverCarousel(st: AppState, tracks: List<Track>, index: Int, size: Dp, radius: Dp, scale: Float) {
    val pager = rememberPagerState(initialPage = index.coerceIn(0, tracks.size - 1)) { tracks.size }
    LaunchedEffect(index) { if (index in tracks.indices && pager.currentPage != index) pager.animateScrollToPage(index) }
    LaunchedEffect(pager) {
        snapshotFlow { pager.settledPage }.collect { p -> if (p != st.app.player.ui.value.index) st.app.player.seekToItem(p) }
    }
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        val side = ((maxWidth - size) / 2).coerceAtLeast(0.dp)
        HorizontalPager(
            pager, contentPadding = PaddingValues(horizontal = side), pageSpacing = 12.dp,
            modifier = Modifier.fillMaxWidth().semantics { contentDescription = "Обложки очереди: ${index + 1} из ${tracks.size}" },
        ) { p ->
            val cur = p == pager.currentPage
            val s by animateFloatAsState(if (cur) scale else 0.82f, label = "cs")
            Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                Cover(tracks[p].cover, size, RoundedCornerShape(if (cur) radius else 28.dp), Modifier.scale(s).graphicsLayer { alpha = if (cur) 1f else 0.6f })
            }
        }
    }
}

/** «Играет из …»: имя источника очереди; тап — к нему (плейлист, альбом, исполнитель, медиатека…). */
@Composable
fun SourceLabel(st: AppState, src: io.github.ikriu.pimusic.player.PlaySource?, modifier: Modifier = Modifier) {
    if (src == null) return
    val from = when (src.kind) {
        "playlist" -> "Играет из плейлиста"; "album" -> "Играет из альбома"; "artist" -> "Играет: исполнитель"
        "wave" -> "Играет"; else -> "Играет из"
    }
    TextButton(onClick = { st.goSource(src) }, modifier = modifier.heightIn(min = 48.dp).semantics { contentDescription = "$from «${src.title}». Открыть" }) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(from, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(src.title, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Включённое состояние — не только цвет: форма круг → скруглённый квадрат 12dp. */
@Composable
private fun ToggleIcon(on: Boolean, label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, onClick: () -> Unit) {
    val r by animateDpAsState(if (on) 12.dp else 24.dp, label = "t")
    FilledTonalIconButton(
        onClick = onClick, shape = RoundedCornerShape(r), modifier = Modifier.size(48.dp).semantics { contentDescription = label },
        colors = if (on) IconButtonDefaults.filledTonalIconButtonColors() else IconButtonDefaults.filledTonalIconButtonColors(containerColor = androidx.compose.ui.graphics.Color.Transparent),
    ) { Icon(icon, null) }
}
