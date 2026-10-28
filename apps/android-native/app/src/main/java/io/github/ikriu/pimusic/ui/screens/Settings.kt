package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.Logout
import androidx.compose.material.icons.rounded.BrightnessAuto
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.DarkMode
import androidx.compose.material.icons.rounded.DeleteSweep
import androidx.compose.material.icons.rounded.Devices
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.DownloadForOffline
import androidx.compose.material.icons.rounded.Edit
import androidx.compose.material.icons.rounded.Equalizer
import androidx.compose.material.icons.rounded.Info
import androidx.compose.material.icons.rounded.LightMode
import androidx.compose.material.icons.rounded.Speed
import androidx.compose.material.icons.rounded.SwapHoriz
import io.github.ikriu.pimusic.ui.fmtXf
import androidx.compose.material.icons.rounded.SystemUpdate
import androidx.compose.material.icons.rounded.Timer
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.BuildConfig
import io.github.ikriu.pimusic.core.M3Color
import io.github.ikriu.pimusic.core.fmtBytes
import io.github.ikriu.pimusic.data.Devices
import io.github.ikriu.pimusic.ui.ConfirmDialog
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.MenuRow
import io.github.ikriu.pimusic.ui.NewPlaylistDialog
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.SectionTitle
import io.github.ikriu.pimusic.ui.SleepSheet
import io.github.ikriu.pimusic.ui.SpeedSheet
import io.github.ikriu.pimusic.ui.fmtSpeed
import io.github.ikriu.pimusic.ui.rememberLoader
import io.github.ikriu.pimusic.ui.theme.ACCENTS
import io.github.ikriu.pimusic.ui.theme.LocalDark
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.math.roundToInt

/** Лимит кэша потока: ступени (МБ). */
private val CACHE_STEPS = listOf(256, 512, 1024, 2048, 4096, 8192, 16384)

@Composable
private fun SwitchRow(text: String, supporting: String?, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable { onChange(!checked) }.padding(horizontal = 24.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(text, style = MaterialTheme.typography.bodyLarge)
            if (supporting != null) Text(supporting, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Switch(checked, onChange)
    }
}

/**
 * Настройки: оформление (тема, акцент — синхронизируются ключом ui.theme), воспроизведение (нормализация, скорость,
 * таймер сна), звук (эквалайзер, на других устройствах), кэш и скачанное, это устройство, входы, аккаунт, сервер, обновления.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen() {
    val st = LocalAppState.current
    val app = st.app
    val opts by app.eqc.opts.collectAsState()
    val offlineOn by app.prefs.offline.collectAsState()
    var sheet by remember { mutableStateOf<String?>(null) }
    var dialog by remember { mutableStateOf<String?>(null) }
    var cacheIdx by remember { mutableFloatStateOf(CACHE_STEPS.indexOf(app.prefs.cacheLimitMb).coerceAtLeast(0).toFloat()) }
    var streamBytes by remember { mutableLongStateOf(0L) }
    var offBytes by remember { mutableLongStateOf(0L) }
    var wifiOnly by remember { mutableStateOf(app.prefs.wifiOnly) }
    var cacheTick by remember { mutableStateOf(0) }
    LaunchedEffect(cacheTick) {
        withContext(Dispatchers.IO) { streamBytes = app.caches.streamBytes(); offBytes = app.caches.offlineBytes() }
    }
    val devices = rememberLoader("/devices", Devices.serializer(), cache = false)
    val dark = LocalDark.current
    Column(Modifier.fillMaxSize()) {
        PageBar("Настройки")
        LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
            item { SectionTitle("Оформление") }
            item {
                ConnectedGroup(
                    listOf("system" to "Как в системе", "light" to "Светлая", "dark" to "Тёмная"), st.theme.mode, { st.changeTheme(st.theme.copy(mode = it)) },
                    Modifier.padding(horizontal = 24.dp),
                    icons = mapOf("system" to Icons.Rounded.BrightnessAuto, "light" to Icons.Rounded.LightMode, "dark" to Icons.Rounded.DarkMode),
                )
            }
            item {
                FlowRow(Modifier.padding(horizontal = 24.dp, vertical = 12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    ACCENTS.forEach { a ->
                        val c = remember(a.id, dark) { Color(M3Color.scheme(M3Color.rgbToOklch(a.seed), dark).getValue(M3Color.Role.Primary)) }
                        val on = st.theme.accent == a.id
                        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.width(80.dp).clickable { st.changeTheme(st.theme.copy(accent = a.id)) }.semantics { contentDescription = a.name + if (on) ", выбрано" else "" }) {
                            Box(Modifier.size(48.dp).clip(CircleShape).background(c).border(if (on) 3.dp else 0.dp, MaterialTheme.colorScheme.onSurface, CircleShape), contentAlignment = Alignment.Center) {
                                if (on) Icon(Icons.Rounded.Check, null, tint = MaterialTheme.colorScheme.surface)
                            }
                            Text(a.name, style = MaterialTheme.typography.labelSmall, maxLines = 2, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
                        }
                    }
                }
            }
            item { SectionTitle("Воспроизведение") }
            item { SwitchRow("Нормализация громкости", "Выравнивает громкость треков (до +6 дБ, без клиппинга)", opts.normalize) { app.eqc.setOpts(opts.copy(normalize = it)) } }
            item { MenuRow(Icons.Rounded.Speed, "Скорость", fmtSpeed(opts.speed.toFloat())) { sheet = "speed" } }
            item { MenuRow(Icons.Rounded.SwapHoriz, "Плавный переход", fmtXf(opts.crossfade)) { sheet = "xf" } }
            item { MenuRow(Icons.Rounded.Timer, "Таймер сна") { sheet = "sleep" } }
            item { SectionTitle("Звук") }
            item { MenuRow(Icons.Rounded.Equalizer, "Эквалайзер", "10 полос, пресеты, синхронизация с другими устройствами") { st.push(Screen.Equalizer) } }
            val others = app.eqc.devices().filter { it.first != app.session.deviceId }
            if (others.isNotEmpty()) {
                item { SectionTitle("Эквалайзер на других устройствах") }
                others.forEach { (id, d) ->
                    item(key = "eq$id") { MenuRow(Icons.Rounded.Devices, d.name, if (d.mode == "sync") "Синхронизирован" else "Свой профиль") { st.push(Screen.RemoteEq(id, d.name)) } }
                }
            }
            item { SectionTitle("Кэш и скачанное") }
            item {
                Column(Modifier.padding(horizontal = 24.dp)) {
                    Text("Кэш потока: ${fmtBytes(streamBytes)} из ${fmtBytes(CACHE_STEPS[cacheIdx.roundToInt()] * 1024L * 1024L)}", style = MaterialTheme.typography.bodyLarge)
                    Text("Треки целиком сохраняются при прослушивании (текущий и 2 следующих) — играют без задержек; старые вытесняются.",
                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Slider(cacheIdx, { cacheIdx = it }, valueRange = 0f..(CACHE_STEPS.size - 1).toFloat(), steps = CACHE_STEPS.size - 2, onValueChangeFinished = {
                        val mb = CACHE_STEPS[cacheIdx.roundToInt()]
                        app.prefs.cacheLimitMb = mb
                        st.scope.launchIo { app.caches.evictor.applyLimit(mb * 1024L * 1024L); cacheTick++ }
                    })
                    TextButton(onClick = { dialog = "clear" }) { Icon(Icons.Rounded.DeleteSweep, null); Spacer(Modifier.width(6.dp)); Text("Очистить кэш потока") }
                }
            }
            item { MenuRow(Icons.Rounded.DownloadForOffline, "Скачанное", "${fmtBytes(offBytes)} — хранится до удаления") { st.push(Screen.Offline) } }
            item { SwitchRow("Скачивать только по Wi-Fi", null, wifiOnly) { wifiOnly = it; app.prefs.wifiOnly = it; app.offline.setWifiOnly(it) } }
            item { SwitchRow("Офлайн-режим", "Не ходить в сеть: медиатека из кэша, играет скачанное и закэшированное", offlineOn) { app.prefs.setOffline(it) } }
            item { SectionTitle("Это устройство") }
            item { MenuRow(Icons.Rounded.Edit, app.eqc.device()?.name ?: app.session.deviceName, "Имя видно в эквалайзере на других устройствах") { dialog = "rename" } }
            item { SectionTitle("Входы вне Telegram") }
            devices.data?.items?.forEach { d ->
                item(key = "d${d.id}") {
                    Row(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Rounded.Devices, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.width(20.dp))
                        Column(Modifier.weight(1f)) {
                            Text(d.name + if (d.current) " · это устройство" else "", style = MaterialTheme.typography.bodyLarge)
                            Text(d.platform + " · был " + java.text.SimpleDateFormat("d MMM, HH:mm", java.util.Locale("ru")).format(java.util.Date(d.lastSeen * 1000)),
                                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (!d.current) TextButton(onClick = {
                            st.run { app.api.post("/devices/${d.id}/revoke", null, kotlinx.serialization.json.JsonObject.serializer()); devices.reload() }
                        }) { Text("Отозвать") }
                    }
                }
            }
            item { SectionTitle("Аккаунт") }
            item { MenuRow(Icons.AutoMirrored.Rounded.Logout, "Выйти", st.me?.login?.let { "Вы вошли как $it" }) { dialog = "logout" } }
            item { MenuRow(Icons.AutoMirrored.Rounded.Logout, "Выйти на всех устройствах", color = MaterialTheme.colorScheme.error) { dialog = "logout_all" } }
            item { SectionTitle("О приложении") }
            item { MenuRow(Icons.Rounded.Dns, "Сервер", app.session.server) { dialog = "server" } }
            item { MenuRow(Icons.Rounded.SystemUpdate, "Проверить обновления", "Установлена ${BuildConfig.VERSION_NAME}") { app.updater.check(manual = true) } }
            item { MenuRow(Icons.Rounded.Info, "PiMusic ${BuildConfig.VERSION_NAME}", "Нативный клиент · Material 3 Expressive") {} }
        }
    }
    when (sheet) {
        "speed" -> SpeedSheet(st) { sheet = null }
        "sleep" -> SleepSheet(st) { sheet = null }
        "xf" -> io.github.ikriu.pimusic.ui.CrossfadeSheet(st) { sheet = null }
    }
    when (dialog) {
        "clear" -> ConfirmDialog("Очистить кэш потока?", "Скачанные треки останутся.", "Очистить", { dialog = null }) {
            dialog = null
            st.scope.launchIo { app.caches.clearStream(); cacheTick++ }
        }
        "logout" -> ConfirmDialog("Выйти?", "Скачанные треки останутся на устройстве.", "Выйти", { dialog = null }) { dialog = null; st.logout() }
        "logout_all" -> ConfirmDialog("Выйти на всех устройствах?", "Вход отменится везде, включая это устройство.", "Выйти везде", { dialog = null }) { dialog = null; st.logout(all = true) }
        "rename" -> NewPlaylistDialog(st, { dialog = null }, initial = app.eqc.device()?.name ?: "", title = "Имя устройства", action = "Сохранить", showShared = false) { n, _ -> dialog = null; app.eqc.rename(n) }
        "server" -> ServerDialog { dialog = null }
    }
}

fun kotlinx.coroutines.CoroutineScope.launchIo(block: suspend () -> Unit) = launch(Dispatchers.IO) { block() }
