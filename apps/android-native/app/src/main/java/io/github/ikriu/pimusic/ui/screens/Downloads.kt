package io.github.ikriu.pimusic.ui.screens

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
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
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CheckCircle
import androidx.compose.material.icons.rounded.Download
import androidx.compose.material.icons.rounded.ErrorOutline
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Stop
import androidx.compose.material.icons.rounded.Upload
import androidx.compose.material3.Button
import androidx.compose.material3.CircularWavyProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.fmtBytes
import io.github.ikriu.pimusic.core.fmtTime
import io.github.ikriu.pimusic.data.Activity
import io.github.ikriu.pimusic.data.Api
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.Import
import io.github.ikriu.pimusic.data.ImportFileResp
import io.github.ikriu.pimusic.data.Imports
import io.github.ikriu.pimusic.data.Upload
import io.github.ikriu.pimusic.data.UploadResp
import io.github.ikriu.pimusic.data.Uploads
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.Cover
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.SectionTitle
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private data class Picked(val uri: Uri, val name: String, val size: Long, val type: String)

private fun pick(ctx: Context, uri: Uri): Picked {
    var name = uri.lastPathSegment ?: "file"
    var size = -1L
    ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
        if (c.moveToFirst()) {
            c.getString(0)?.let { name = it }
            if (!c.isNull(1)) size = c.getLong(1)
        }
    }
    return Picked(uri, name, size, ctx.contentResolver.getType(uri) ?: "application/octet-stream")
}

private val LIST_EXT = setOf("txt", "csv", "tsv")

/** Отправка своих файлов: аудио → /api/upload (пачками по 20, прогресс), списки TXT/CSV → /api/import/file. */
private suspend fun sendFiles(st: AppState, ctx: Context, files: List<Picked>, progress: (String?) -> Unit) {
    val (lists, audio) = files.partition { it.name.substringAfterLast('.', "").lowercase() in LIST_EXT }
    for (f in lists) {
        progress("Импорт «${f.name}»…")
        try {
            val r = st.app.api.multipart("/import/file", listOf(Api.Part("file", f.name, "text/plain", f.size) { ctx.contentResolver.openInputStream(f.uri)!! }), ImportFileResp.serializer()) { _, _ -> }
            st.msg("Импорт «${r.name}»: ${r.total} тр. (${r.format})" + (r.playlist?.let { " → плейлист «${it.name}»" } ?: ""))
        } catch (e: ApiException) { st.msg(e.message) }
    }
    for (chunk in audio.chunked(20)) {
        try {
            val r = st.app.api.multipart("/upload", chunk.map { f -> Api.Part("file", f.name, f.type, f.size) { ctx.contentResolver.openInputStream(f.uri)!! } }, UploadResp.serializer()) { sent, total ->
                progress(if (total > 0) "Отправка: ${sent * 100 / total} % (${fmtBytes(sent)} из ${fmtBytes(total)})" else "Отправка: ${fmtBytes(sent)}")
            }
            val rej = r.rejected.joinToString("; ") { "${it.name}: ${it.message.ifEmpty { it.error }}" }
            st.msg("Принято файлов: ${r.uploads.size}" + if (rej.isNotEmpty()) ". Не приняты: $rej" else "")
        } catch (e: ApiException) { st.msg(e.message) }
    }
    progress(null)
}

/**
 * Загрузки (на сервер): «Свои файлы» (аудио и списки TXT/CSV), импорты (пауза/стоп, прогресс), «Сейчас качается»,
 * лента событий. Опрос /api/activity, /api/import, /api/uploads каждые 3 с, пока экран открыт.
 */
@Composable
fun DownloadsScreen() {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    var act by remember { mutableStateOf<Activity?>(null) }
    var imports by remember { mutableStateOf<List<Import>>(emptyList()) }
    val uploads = remember { mutableStateListOf<Upload>() }
    var sending by remember { mutableStateOf<String?>(null) }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(tick) {
        while (true) {
            if (!st.offlineMode) {
                runCatching { st.app.api.get("/activity", Activity.serializer()) }.onSuccess { act = it }
                runCatching { st.app.api.get("/import", Imports.serializer()) }.onSuccess { imports = it.imports }
                runCatching { st.app.api.get("/uploads", Uploads.serializer()) }.onSuccess { r -> uploads.clear(); uploads.addAll(r.items) }
            }
            delay(3000)
        }
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isEmpty()) return@rememberLauncherForActivityResult
        val files = uris.map { pick(ctx, it) }
        st.scope.launch { sendFiles(st, ctx, files) { sending = it }; tick++ }
    }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
        item { TabHeader("Загрузки") }
        item {
            Surface(color = MaterialTheme.colorScheme.surfaceContainerHigh, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().padding(16.dp)) {
                Column(Modifier.padding(20.dp)) {
                    Text("Свои файлы", style = MaterialTheme.typography.titleLarge)
                    Text("Аудио (mp3, flac, m4a, ogg, wav…) — сервер опознает и подпишет. Списки TXT/CSV (Tune My Music, Exportify, «Артист - Песня») — импорт в плейлист.",
                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.size(12.dp))
                    Button(onClick = { picker.launch(arrayOf("audio/*", "text/*", "application/octet-stream")) }, enabled = sending == null) {
                        Icon(Icons.Rounded.Upload, null); Spacer(Modifier.width(6.dp)); Text("Загрузить файлы")
                    }
                    sending?.let {
                        Spacer(Modifier.size(12.dp))
                        Text(it, style = MaterialTheme.typography.bodyMedium)
                        LinearWavyProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp))
                    }
                }
            }
        }
        if (imports.isNotEmpty()) {
            item { SectionTitle("Импорт списков") }
            items(imports, key = { "i" + it.id }) { ImportCard(st, it) { tick++ } }
        }
        val needInfo = uploads.filter { it.state == "need_info" }
        if (needInfo.isNotEmpty()) {
            item { SectionTitle("Ждут названия") }
            items(needInfo, key = { "n" + it.id }) { NeedInfoCard(st, it) { tick++ } }
        }
        val a = act
        if (a != null && (a.active.isNotEmpty() || a.jobs.isNotEmpty())) {
            item { SectionTitle("Сейчас качается" + if (a.queue > 0) " · в очереди ${a.queue}" else "") }
            items(a.active) { t ->
                Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    CircularWavyProgressIndicator(Modifier.size(32.dp))
                    Spacer(Modifier.width(16.dp))
                    Text(t, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
                }
            }
            items(a.jobs) { j ->
                Text("${j.title} — ${j.total} тр.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp))
            }
        }
        val other = uploads.filter { it.state != "need_info" }
        if (other.isNotEmpty()) {
            item { SectionTitle("Свои файлы") }
            items(other.take(50), key = { "u" + it.id }) { u -> UploadRow(st, u) }
        }
        if (a != null && a.log.isNotEmpty()) {
            item { SectionTitle("События") }
            val fmt = SimpleDateFormat("HH:mm", Locale.ROOT)
            items(a.log.take(60)) { line ->
                Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)) {
                    Text(fmt.format(Date(line.ts * 1000)), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.width(48.dp))
                    Text(line.text, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        if (a != null && a.active.isEmpty() && a.jobs.isEmpty() && a.log.isEmpty() && imports.isEmpty() && uploads.isEmpty()) item {
            io.github.ikriu.pimusic.ui.EmptyState(Icons.Rounded.Download, "Загрузок пока нет", "Найдите музыку в поиске или загрузите свои файлы.")
        }
        if (st.offlineMode) item { io.github.ikriu.pimusic.ui.EmptyState(Icons.Rounded.ErrorOutline, "Нет сети", "Загрузки на сервер появятся, когда будет связь.", cookie = false) }
    }
}

@Composable
private fun ImportCard(st: AppState, imp: Import, changed: () -> Unit) {
    val done = imp.counts?.let { c -> listOf("done", "cached", "failed").sumOf { k -> c[k]?.jsonPrimitive?.intOrNull ?: 0 } } ?: 0
    val frac = if (imp.total > 0) (done.toFloat() / imp.total).coerceIn(0f, 1f) else 0f
    Surface(color = MaterialTheme.colorScheme.surfaceContainerHigh, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp)) {
        Column(Modifier.padding(16.dp)) {
            Text(imp.filename, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(imp.text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.size(8.dp))
            LinearWavyProgressIndicator(progress = { frac }, modifier = Modifier.fillMaxWidth())
            Row(Modifier.padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (imp.state == "paused") FilledTonalButton(onClick = { st.run { st.app.api.post("/import/resume", buildJsonObject { put("id", imp.id) }, JsonObject.serializer()); changed() } }) {
                    Icon(Icons.Rounded.PlayArrow, null); Text("Продолжить")
                } else FilledTonalButton(onClick = { st.run { st.app.api.post("/import/pause", buildJsonObject { put("id", imp.id) }, JsonObject.serializer()); changed() } }) {
                    Icon(Icons.Rounded.Pause, null); Text("Пауза")
                }
                OutlinedButton(onClick = { st.run { st.app.api.post("/import/cancel", buildJsonObject { put("id", imp.id) }, JsonObject.serializer()); changed() } }) {
                    Icon(Icons.Rounded.Stop, null); Text("Стоп")
                }
            }
        }
    }
}

/** «Ждёт названия»: Исполнитель / Название / Альбом (необязательно); Отмена · Оставить как есть · Сохранить. */
@Composable
private fun NeedInfoCard(st: AppState, u: Upload, changed: () -> Unit) {
    val guess = u.name.substringBeforeLast('.').split(" - ", " — ", limit = 2)
    var artist by remember(u.id) { mutableStateOf(u.artist ?: guess.getOrNull(0)?.trim().orEmpty()) }
    var title by remember(u.id) { mutableStateOf(u.title ?: guess.getOrNull(1)?.trim().orEmpty()) }
    var album by remember(u.id) { mutableStateOf(u.album.orEmpty()) }
    Surface(color = MaterialTheme.colorScheme.surfaceContainerHigh, shape = MaterialTheme.shapes.extraLarge, modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(u.name + (u.duration?.let { " · " + fmtTime(it.toLong()) } ?: ""), style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Text("Не удалось опознать — подскажите, что это за трек.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            OutlinedTextField(artist, { artist = it }, label = { Text("Исполнитель") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(title, { title = it }, label = { Text("Название") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(album, { album = it }, label = { Text("Альбом (необязательно)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { st.run { st.app.api.post("/uploads/${u.id}/cancel", null, JsonObject.serializer()); changed() } }) { Text("Отмена") }
                TextButton(onClick = { st.run { st.app.api.post("/uploads/${u.id}/skip", null, JsonObject.serializer()); changed() } }) { Text("Оставить как есть") }
                Spacer(Modifier.weight(1f))
                Button(enabled = artist.isNotBlank() && title.isNotBlank(), onClick = {
                    st.run {
                        st.app.api.post("/uploads/${u.id}/info", buildJsonObject {
                            put("artist", artist.trim()); put("title", title.trim()); if (album.isNotBlank()) put("album", album.trim())
                        }, JsonObject.serializer())
                        st.msg("Сохраняю «${artist.trim()} — ${title.trim()}»")
                        changed()
                    }
                }) { Text("Сохранить") }
            }
        }
    }
}

@Composable
private fun UploadRow(st: AppState, u: Upload) {
    val cs = MaterialTheme.colorScheme
    val t = u.track
    Row(Modifier.fillMaxWidth().heightIn(min = 64.dp).padding(horizontal = 16.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
            when (u.state) {
                "done", "duplicate" -> if (t != null) Cover(t.cover, 48.dp) else Icon(Icons.Rounded.CheckCircle, null, tint = cs.primary)
                "failed", "cancelled" -> Icon(Icons.Rounded.ErrorOutline, null, tint = cs.error)
                else -> CircularWavyProgressIndicator(Modifier.size(32.dp))
            }
        }
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(t?.let { "${it.artist} — ${it.title}" } ?: u.name, style = MaterialTheme.typography.bodyLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(listOfNotNull(u.stateText.ifEmpty { u.state }, u.note).joinToString(" · "), style = MaterialTheme.typography.bodySmall,
                color = if (u.state == "failed") cs.error else cs.onSurfaceVariant, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        if (t != null && t.stream != null) TextButton(onClick = { st.app.player.playList(listOf(t), 0, false, io.github.ikriu.pimusic.player.PlaySource.UPLOADS) }) { Text("Слушать") }
    }
}
