package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.Login
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.GraphicEq
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.AuthPoll
import io.github.ikriu.pimusic.data.AuthStart
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.PolygonShape
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Вход вне Telegram (APPS_API.md): POST /api/auth/start → код подтверждения крупно + «Войти через Telegram»
 * (t.me/<бот>?start=login_… — приложение Telegram), опрос /api/auth/poll каждые 2 с. Здесь же — смена сервера.
 */
@Composable
fun LoginScreen() {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val session by st.app.session.state.collectAsState()
    var start by remember { mutableStateOf<AuthStart?>(null) }
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var job by remember { mutableStateOf<Job?>(null) }
    // адреса по умолчанию нет — при первом запуске сразу спрашиваем адрес своего сервера
    var serverDialog by remember { mutableStateOf(session.server.isEmpty()) }

    fun begin() {
        job?.cancel()
        busy = true
        status = null
        job = scope.launch {
            try {
                val s = st.app.api.post("/auth/start", buildJsonObject { put("device", st.app.session.deviceName); put("platform", "android") }, AuthStart.serializer(), auth = false)
                start = s
                if (s.link == null) { status = "Бот ещё не готов — попробуйте через минуту"; busy = false; return@launch }
                st.openTelegram(ctx, s.link)
                status = "Ждём подтверждения в Telegram…"
                val until = System.currentTimeMillis() + s.expiresIn * 1000L
                while (System.currentTimeMillis() < until) {
                    delay(2000)
                    val p = try {
                        st.app.api.post("/auth/poll", buildJsonObject { put("nonce", s.nonce) }, AuthPoll.serializer(), auth = false)
                    } catch (e: ApiException) {
                        if (e.status == 0 || e.status == 429) continue else throw e
                    }
                    when (p.status) {
                        "ok" -> {
                            st.app.session.signIn(p.token ?: return@launch, p.login ?: "")
                            st.afterLogin()
                            return@launch
                        }
                        "denied" -> { status = "Вход отклонён. Попробуйте снова."; start = null; busy = false; return@launch }
                        "expired" -> { status = "Ссылка устарела. Попробуйте снова."; start = null; busy = false; return@launch }
                        "too_many_devices" -> status = p.message ?: "Слишком много устройств — выйдите на одном из старых."
                    }
                }
                status = "Время ожидания истекло. Попробуйте снова."
                start = null
            } catch (e: ApiException) {
                status = e.message
                start = null
            } finally {
                busy = false
            }
        }
    }

    val cs = MaterialTheme.colorScheme
    Box(Modifier.fillMaxSize().safeDrawingPadding(), contentAlignment = Alignment.Center) {
        Column(
            Modifier.widthIn(max = 480.dp).fillMaxWidth().verticalScroll(rememberScrollState()).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Box(Modifier.size(128.dp).clip(PolygonShape(MaterialShapes.Cookie9Sided)).background(cs.primary), contentAlignment = Alignment.Center) {
                Icon(Icons.Rounded.GraphicEq, null, tint = cs.onPrimary, modifier = Modifier.size(64.dp))
            }
            Text("Вход в PiMusic", style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.SemiBold))
            Text(
                "Подтвердите вход в Telegram: бот спросит, разрешить ли вход на этом устройстве, и предложит выбрать код.",
                style = MaterialTheme.typography.bodyLarge, color = cs.onSurfaceVariant, textAlign = TextAlign.Center,
            )
            start?.code?.let { code ->
                Text("Код подтверждения", style = MaterialTheme.typography.labelLarge, color = cs.onSurfaceVariant)
                Text(code, style = MaterialTheme.typography.displayLarge.copy(fontWeight = FontWeight.Bold, letterSpacing = 8.sp), color = cs.primary)
            }
            Button(onClick = { if (session.server.isEmpty()) serverDialog = true else begin() }, enabled = !busy, modifier = Modifier.fillMaxWidth().height(56.dp)) {
                Icon(Icons.AutoMirrored.Rounded.Login, null)
                Spacer(Modifier.size(8.dp))
                Text(if (start != null) "Открыть Telegram ещё раз" else "Войти через Telegram")
            }
            if (start != null && !busy) TextButton(onClick = { start?.link?.let { st.openTelegram(ctx, it) } }) { Text("Открыть ссылку снова") }
            if (busy) LoadingIndicator()
            status?.let { Text(it, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center, modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
            Spacer(Modifier.height(8.dp))
            TextButton(onClick = { serverDialog = true }) {
                Icon(Icons.Rounded.Dns, null)
                Spacer(Modifier.size(8.dp))
                Text("Сервер: " + session.server.removePrefix("https://").ifEmpty { "не указан" })
            }
        }
    }
    if (serverDialog) ServerDialog(onDismiss = { serverDialog = false })
}

/** Адрес сервера: https://…; http:// — только для локальной сети. */
@Composable
fun ServerDialog(onDismiss: () -> Unit) {
    val st = LocalAppState.current
    var text by remember { mutableStateOf(st.app.session.server) }
    var err by remember { mutableStateOf<String?>(null) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Сервер PiMusic") },
        text = {
            OutlinedTextField(
                text, { text = it; err = null }, label = { Text("Адрес") }, singleLine = true, isError = err != null,
                supportingText = { Text(err ?: "Например, https://music.example.com; http:// — только в локальной сети") },
            )
        },
        confirmButton = {
            TextButton(onClick = {
                try {
                    st.app.session.setServer(text)
                    onDismiss()
                } catch (e: IllegalArgumentException) { err = e.message }
            }) { Text("Сохранить") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Отмена") } },
    )
}
