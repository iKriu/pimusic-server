package io.github.ikriu.pimusic.data

import android.content.Context
import android.os.Build
import io.github.ikriu.pimusic.core.ServerUrl
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

/** Сервер, токен устройства, логин, id устройства (для ключей eq.device.<id>). Только в памяти приложения. */
class Session(ctx: Context) {
    private val p = ctx.getSharedPreferences("pimusic", Context.MODE_PRIVATE)

    data class State(val server: String, val token: String?, val login: String?)

    private val _state = MutableStateFlow(
        State(
            server = runCatching { ServerUrl.normalize(p.getString("server", null) ?: ServerUrl.DEFAULT) }.getOrDefault(ServerUrl.DEFAULT),
            token = p.getString("token", null),
            login = p.getString("login", null),
        )
    )
    val state: StateFlow<State> = _state.asStateFlow()

    val server get() = _state.value.server
    val token get() = _state.value.token
    val login get() = _state.value.login
    val loggedIn get() = !token.isNullOrEmpty() && !login.isNullOrEmpty()

    /** Стабильный id устройства (uuid) — как deviceId() в device.js. */
    val deviceId: String = p.getString("device_id", null) ?: UUID.randomUUID().toString().also { p.edit().putString("device_id", it).apply() }

    val deviceName: String get() = "Android · " + (Build.MODEL ?: "телефон").take(40)

    fun setServer(url: String) {
        val n = ServerUrl.normalize(url)
        p.edit().putString("server", n).apply()
        _state.value = _state.value.copy(server = n)
    }

    fun signIn(token: String, login: String) {
        p.edit().putString("token", token).putString("login", login).apply()
        _state.value = _state.value.copy(token = token, login = login)
    }

    fun signOut() {
        p.edit().remove("token").apply()
        _state.value = _state.value.copy(token = null)
    }
}
