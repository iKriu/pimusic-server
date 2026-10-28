package io.github.ikriu.pimusic.ui

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Download
import androidx.compose.material.icons.rounded.LibraryMusic
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material.icons.rounded.Waves
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.VerticalDivider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.unit.IntOffset
import androidx.compose.foundation.layout.offset
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import kotlin.math.roundToInt
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.player.Mode
import io.github.ikriu.pimusic.ui.screens.CatalogAlbumScreen
import io.github.ikriu.pimusic.ui.screens.CatalogArtistScreen
import io.github.ikriu.pimusic.ui.screens.DownloadsScreen
import io.github.ikriu.pimusic.ui.screens.EqScreen
import io.github.ikriu.pimusic.ui.screens.LibAlbumScreen
import io.github.ikriu.pimusic.ui.screens.LibArtistScreen
import io.github.ikriu.pimusic.ui.screens.LibraryScreen
import io.github.ikriu.pimusic.ui.screens.LoginScreen
import io.github.ikriu.pimusic.ui.screens.OfflineScreen
import io.github.ikriu.pimusic.ui.screens.PlaylistScreen
import io.github.ikriu.pimusic.ui.screens.SearchScreen
import io.github.ikriu.pimusic.ui.screens.SettingsScreen
import io.github.ikriu.pimusic.ui.screens.WaveScreen
import io.github.ikriu.pimusic.ui.theme.PiTheme
import io.github.ikriu.pimusic.update.Updater

private fun icon(t: Tab): ImageVector = when (t) {
    Tab.SEARCH -> Icons.Rounded.Search
    Tab.LIBRARY -> Icons.Rounded.LibraryMusic
    Tab.DOWNLOADS -> Icons.Rounded.Download
    Tab.WAVE -> Icons.Rounded.Waves
}

/** Корень интерфейса: вход или оболочка с навигацией (bar на compact, rail на medium+), мини-плеер, листы. */
@Composable
fun App(state: AppState) {
    val session by state.app.session.state.collectAsState()
    CompositionLocalProvider(LocalAppState provides state) {
        PiTheme(state.theme) {
            Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
                if (session.token.isNullOrEmpty()) LoginScreen()
                else Shell(state)
                UpdateDialog(state)
            }
        }
    }
}

@Composable
private fun Shell(st: AppState) {
    val ctx = LocalContext.current
    BackHandler { if (!st.back()) (ctx as? Activity)?.moveTaskToBack(true) }
    val ui by st.app.player.ui.collectAsState()
    val hasPlayer = ui.mode == Mode.WAVE || ui.queue.isNotEmpty()
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val compact = maxWidth < 600.dp
        val expanded = maxWidth >= 840.dp
        val landscapeCompact = maxHeight < 480.dp
        val large = maxWidth >= 1200.dp
        val panelWidth = if (maxWidth >= 1600.dp) 480.dp else 440.dp
        // large/xl: «Сейчас играет» — закреплённая панель, открывается сама при старте очереди, пока её не закрыли
        LaunchedEffect(large, hasPlayer) { if (large && hasPlayer && !st.panelDismissed) st.playerOpen = true }
        if (compact && !landscapeCompact) {
            Column(Modifier.fillMaxSize()) {
                Box(Modifier.weight(1f).windowInsetsPadding(WindowInsets.systemBars.only(WindowInsetsSides.Top))) { Content(st) }
                if (hasPlayer) MiniPlayer(st)
                NavigationBar {
                    Tab.entries.forEach { t ->
                        NavigationBarItem(selected = st.tab == t, onClick = { st.select(t) }, icon = { Icon(icon(t), null) },
                            label = { FitText(t.label, MaterialTheme.typography.labelMedium, minScale = 0.75f) })
                    }
                }
            }
        } else {
            Row(Modifier.fillMaxSize()) {
                NavigationRail(Modifier.windowInsetsPadding(WindowInsets.systemBars)) {
                    Spacer(Modifier.height(12.dp))
                    Tab.entries.forEach { t ->
                        NavigationRailItem(
                            selected = st.tab == t, onClick = { st.select(t) }, icon = { Icon(icon(t), null) },
                            label = if (landscapeCompact) null else ({ FitText(t.label, MaterialTheme.typography.labelMedium, minScale = 0.75f) }),
                        )
                    }
                }
                Column(Modifier.weight(1f).windowInsetsPadding(WindowInsets.systemBars)) {
                    Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.TopCenter) {
                        Box(Modifier.fillMaxHeight().widthIn(max = 840.dp)) { Content(st) }
                    }
                    if (hasPlayer && !(expanded && st.playerOpen)) MiniPlayer(st)
                }
                if (expanded && st.playerOpen) {
                    VerticalDivider()
                    Box(Modifier.width(panelWidth).fillMaxHeight().windowInsetsPadding(WindowInsets.systemBars)) { NowPlaying(st, panel = true) }
                }
            }
        }
        // «Сейчас играет»: на compact и medium — полноэкранный лист поверх; тянется пальцем (PlayerSheet)
        val scope = rememberCoroutineScope()
        val sheet = remember { PlayerSheet(st, scope) }
        st.sheet = sheet
        val heightPx = constraints.maxHeight.toFloat()
        LaunchedEffect(heightPx) { sheet.layout(heightPx) }
        LaunchedEffect(st.playerOpen, expanded) { if (!expanded && !sheet.dragging) sheet.animate(st.playerOpen) }
        if (!expanded && sheet.visible) {
            Box(
                Modifier.fillMaxSize()
                    .offset { IntOffset(0, sheet.offset.value.coerceIn(0f, heightPx).roundToInt()) }
                    .background(MaterialTheme.colorScheme.surface)
                    .windowInsetsPadding(WindowInsets.systemBars)
                    .nestedScroll(sheet.nested),
            ) { NowPlaying(st, panel = false, sheet = sheet) }
        }
        SnackbarHost(st.snackbar, Modifier.align(Alignment.BottomCenter).navigationBarsPadding().padding(bottom = if (compact) 140.dp else 72.dp))
    }
    st.menu?.let { TrackMenuSheet(st, it) }
    st.addToPlaylist?.let { AddToPlaylistSheet(st, it) }
    st.bulkDelete?.let { b ->
        ConfirmDialog(
            "Удалить из медиатеки ${io.github.ikriu.pimusic.core.nTracks(b.tracks.size.toLong())}?",
            "Из плейлистов треки не удаляются. Скачанное на устройство остаётся.", "Удалить", { st.bulkDelete = null },
        ) {
            st.bulkDelete = null
            st.deleteFromLibrary(b.tracks)
            b.done()
        }
    }
}

@Composable
private fun Content(st: AppState) {
    val screen = st.current
    AnimatedContent(screen, transitionSpec = { fadeIn() togetherWith fadeOut() }, label = "screen") { s ->
        when (s) {
            is Screen.Root -> when (s.tab) {
                Tab.SEARCH -> SearchScreen()
                Tab.LIBRARY -> LibraryScreen()
                Tab.DOWNLOADS -> DownloadsScreen()
                Tab.WAVE -> WaveScreen()
            }
            is Screen.CatalogAlbum -> CatalogAlbumScreen(s)
            is Screen.CatalogArtist -> CatalogArtistScreen(s)
            is Screen.LibArtist -> LibArtistScreen(s)
            is Screen.LibAlbum -> LibAlbumScreen(s)
            is Screen.PlaylistPage -> PlaylistScreen(s)
            Screen.Settings -> SettingsScreen()
            Screen.Offline -> OfflineScreen()
            Screen.Equalizer -> EqScreen(null, onBack = { st.back() })
            is Screen.RemoteEq -> EqScreen(s.deviceId to s.name, onBack = { st.back() })
        }
    }
}

/** Диалог обновления M3 (схема приложения): доступна версия → загрузка с прогрессом → установка. */
@Composable
private fun UpdateDialog(st: AppState) {
    val upd = st.app.updater
    val s by upd.state.collectAsState()
    val ctx = LocalContext.current
    when (val v = s) {
        is Updater.State.Available -> AlertDialog(
            onDismissRequest = { upd.dismiss() },
            title = { Text("Доступно обновление PiMusic") },
            text = {
                Text("Версия ${v.info.versionName} (установлена ${io.github.ikriu.pimusic.BuildConfig.VERSION_NAME})." +
                    (if (v.info.notes.isNotEmpty()) "\n\n" + v.info.notes.take(600) else ""))
            },
            confirmButton = { TextButton(onClick = { upd.start(v.info) }) { Text("Обновить") } },
            dismissButton = { TextButton(onClick = { upd.dismiss() }) { Text("Позже") } },
        )
        is Updater.State.NeedPermission -> AlertDialog(
            onDismissRequest = { upd.dismiss() },
            title = { Text("Нужно разрешение") },
            text = { Text("Чтобы установить обновление, разрешите PiMusic устанавливать приложения: включите «Разрешить установку из этого источника» и вернитесь назад.") },
            confirmButton = { TextButton(onClick = { runCatching { ctx.startActivity(upd.openPermissionSettings(v.info)) } }) { Text("Открыть настройки") } },
            dismissButton = { TextButton(onClick = { upd.dismiss() }) { Text("Позже") } },
        )
        is Updater.State.Downloading -> AlertDialog(
            onDismissRequest = {},
            title = { Text("Обновление PiMusic") },
            text = {
                Column {
                    Text(if (v.percent == null) "Загрузка ${v.info.versionName}…" else "Загрузка ${v.info.versionName} — ${v.percent} %")
                    Spacer(Modifier.height(16.dp))
                    if (v.percent == null) LinearWavyProgressIndicator(Modifier.fillMaxWidth())
                    else LinearWavyProgressIndicator(progress = { v.percent / 100f }, modifier = Modifier.fillMaxWidth())
                }
            },
            confirmButton = {},
            dismissButton = { TextButton(onClick = { upd.dismiss() }) { Text("Отмена") } },
        )
        Updater.State.Installing -> AlertDialog(onDismissRequest = {}, title = { Text("Установка…") }, text = { Text("PiMusic перезапустится после установки.") }, confirmButton = {})
        is Updater.State.Error -> AlertDialog(
            onDismissRequest = { upd.dismiss() }, title = { Text("Обновление не установлено") }, text = { Text(v.text) },
            confirmButton = { TextButton(onClick = { upd.dismiss() }) { Text("Понятно") } },
        )
        is Updater.State.UpToDate -> LaunchedEffect(v) { st.msg("Установлена последняя версия ${v.version}"); upd.dismiss() }
        Updater.State.Idle -> {}
    }
}
