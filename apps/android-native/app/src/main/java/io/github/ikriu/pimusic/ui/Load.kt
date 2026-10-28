package io.github.ikriu.pimusic.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.PiJson
import kotlinx.serialization.DeserializationStrategy

val LocalAppState = staticCompositionLocalOf<AppState> { error("AppState не задан") }

/**
 * Данные экрана: сначала прошлый ответ из ApiCache (мгновенно, и офлайн), затем свежий с сервера.
 * error — только если показать нечего; notFound — 404 (плейлист недоступен и т. п.).
 */
class Loader<T>(private val st: AppState, val path: String?, private val de: DeserializationStrategy<T>, private val cache: Boolean) {
    var data by mutableStateOf<T?>(null)
    var loading by mutableStateOf(true)
    var error by mutableStateOf<String?>(null)
    var notFound by mutableStateOf(false)
    var stale by mutableStateOf(false)
    var tick by mutableIntStateOf(0)

    fun reload() { tick++ }

    suspend fun load() {
        val p = path ?: run { loading = false; return }
        loading = true
        if (cache && data == null) {
            st.app.apiCache.read(p)?.let { txt -> runCatching { PiJson.decodeFromString(de, txt) }.getOrNull()?.let { data = it; stale = true } }
        }
        if (st.offlineMode) {
            loading = false
            if (data == null) error = "Нет сети, а этот экран ещё не открывался"
            return
        }
        try {
            val txt = st.app.api.getText(p)
            data = PiJson.decodeFromString(de, txt)
            stale = false
            error = null
            notFound = false
            if (cache) st.app.apiCache.write(p, txt)
        } catch (e: ApiException) {
            if (e.status == 404) notFound = true
            if (data == null || e.status == 404) error = e.message
        } catch (e: Exception) {
            if (data == null) error = e.message ?: "Ошибка"
        } finally {
            loading = false
        }
    }
}

@Composable
fun <T> rememberLoader(path: String?, de: DeserializationStrategy<T>, cache: Boolean = true, vararg keys: Any?): Loader<T> {
    val st = LocalAppState.current
    val l = remember(path) { Loader(st, path, de, cache) }
    LaunchedEffect(path, l.tick, st.libraryVersion, *keys) { l.load() }
    return l
}
