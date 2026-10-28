package io.github.ikriu.pimusic.data

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

/**
 * Настройки по ключам (порт static/js/settings.js): мгновенно — локально (своё хранилище у каждого логина), в фоне —
 * PUT /api/settings/<key> с base. 409 conflict → значение сервера выигрывает (current: null — base = 0 и повтор);
 * 409 limit / 400 / 413 → ключ «паркуется»; сеть/5xx/429 → повтор позже. Ключи и формат значений общие с Mini App.
 * Хранилище и сервер подставляются ([Storage], [Backend]) — логика проверяется JVM-тестами без Android.
 */
class SettingsStore(
    private val storage: Storage,
    private val backend: Backend,
    private val scope: CoroutineScope,
    private val loggedIn: () -> Boolean,
    private val log: (String) -> Unit = {},
) {
    /** Локальное хранилище JSON по логину. */
    interface Storage {
        fun load(login: String): String?
        fun save(login: String, json: String)
    }

    /** Сервер настроек: put → updated_at; ошибки — ApiException (status/code/data как у API). */
    interface Backend {
        suspend fun put(key: String, value: JsonElement, base: Long?): Long
        suspend fun all(): Map<String, SettingItem>
    }

    @Serializable
    data class Item(val value: JsonElement? = null, val updatedAt: Long = 0, val base: Long = 0, val dirty: Boolean = false, val parked: Boolean = false)

    companion object {
        /** Настройки приложения: SharedPreferences + /api/settings. */
        fun android(ctx: Context, api: Api, session: Session, scope: CoroutineScope): SettingsStore {
            val prefs = ctx.getSharedPreferences("settings", Context.MODE_PRIVATE)
            val storage = object : Storage {
                override fun load(login: String) = prefs.getString("v1:$login", null)
                override fun save(login: String, json: String) { prefs.edit().putString("v1:$login", json).apply() }
            }
            val backend = object : Backend {
                override suspend fun put(key: String, value: JsonElement, base: Long?): Long {
                    val body = buildJsonObject { put("value", value); if (base != null && base > 0) put("base", base) }
                    return api.put("/settings/$key", body, SettingPut.serializer()).updatedAt
                }
                override suspend fun all() = api.get("/settings", SettingsAll.serializer()).items
            }
            return SettingsStore(storage, backend, scope, { session.loggedIn }, { android.util.Log.w("PiMusic", it) })
        }
    }

    private val keyRe = Regex("^[a-z0-9._-]{1,80}$")
    private var login = ""
    private var items = HashMap<String, Item>()
    private val lock = Mutex()
    private val _changes = MutableSharedFlow<Pair<String, Boolean>>(extraBufferCapacity = 64)
    /** (ключ, пришло с сервера) */
    val changes: SharedFlow<Pair<String, Boolean>> = _changes
    private var pushJob: Job? = null
    private val ser = MapSerializer(String.serializer(), Item.serializer())

    @Synchronized
    fun setLogin(l: String?) {
        login = l.orEmpty()
        items = HashMap(runCatching { PiJson.decodeFromString(ser, storage.load(login) ?: "{}") }.getOrDefault(emptyMap()))
    }

    @Synchronized
    private fun persist() {
        if (login.isEmpty()) return
        storage.save(login, PiJson.encodeToString(ser, items))
    }

    @Synchronized
    fun get(key: String): JsonElement? = items[key]?.value?.takeIf { it !is JsonNull }

    /** Состояние ключа (для тестов и отладки). */
    @Synchronized
    fun item(key: String): Item? = items[key]

    inline fun <reified T> getAs(key: String): T? = get(key)?.let { runCatching { PiJson.decodeFromJsonElement<T>(it) }.getOrNull() }

    @Synchronized
    fun keys(prefix: String): List<String> = items.filter { it.key.startsWith(prefix) && it.value.value != null && it.value.value !is JsonNull }.keys.toList()

    fun set(key: String, value: JsonElement, now: Long = System.currentTimeMillis()) {
        require(keyRe.matches(key)) { "bad key $key" }
        synchronized(this) {
            val cur = items[key]
            items[key] = Item(value, now, cur?.base ?: 0, dirty = true, parked = false)
            persist()
        }
        _changes.tryEmit(key to false)
        schedule(1200)
    }

    private fun schedule(ms: Long) {
        pushJob?.cancel()
        pushJob = scope.launch { delay(ms); pushAll() }
    }

    /** Отправить несохранённое. Возвращает true, если что-то нужно повторить позже. */
    suspend fun pushAll(): Boolean {
        if (login.isEmpty() || !loggedIn()) return false
        var retryLater = false
        lock.withLock {
            val dirtyKeys = synchronized(this) { items.filter { it.value.dirty && !it.value.parked }.keys.toList() }
            for (key in dirtyKeys) {
                val it = synchronized(this) { items[key] } ?: continue
                val sent = it.updatedAt
                try {
                    val ts = backend.put(key, it.value ?: JsonNull, it.base.takeIf { b -> b > 0 })
                    synchronized(this) {
                        val cur = items[key] ?: return@synchronized
                        // за время отправки ключ могли изменить ещё раз — тогда он остаётся «грязным»
                        items[key] = cur.copy(base = if (ts > 0) ts else cur.base, dirty = cur.updatedAt != sent && cur.dirty)
                    }
                } catch (e: ApiException) {
                    when {
                        e.status == 409 && e.code == "conflict" -> {
                            val cur = e.data?.get("current")
                            if (cur == null || cur is JsonNull) {
                                synchronized(this) { items[key]?.let { c -> items[key] = c.copy(base = 0) } }
                                retryLater = true
                            } else {
                                val o = cur.jsonObject
                                val t = o["updated_at"]?.jsonPrimitive?.longOrNull ?: 0
                                synchronized(this) { items[key] = Item(o["value"], t, t) }
                                _changes.tryEmit(key to true)
                            }
                        }
                        e.status == 409 || e.status == 400 || e.status == 413 -> {
                            synchronized(this) { items[key]?.let { c -> items[key] = c.copy(parked = true) } }
                            log("setting parked $key ${e.code}")
                        }
                        else -> retryLater = true
                    }
                }
            }
            persist()
        }
        if (retryLater) schedule(15000)
        return retryLater
    }

    /** Забрать изменения с сервера: ключи, которые здесь не правились и на сервере новее. */
    suspend fun sync(): Boolean {
        if (login.isEmpty() || !loggedIn()) return false
        val remote = try { backend.all() } catch (e: Exception) { return false }
        val changed = ArrayList<String>()
        synchronized(this) {
            for ((key, s) in remote) {
                if (!keyRe.matches(key)) continue
                val cur = items[key]
                if (cur == null || (!cur.dirty && s.updatedAt > cur.base)) {
                    items[key] = Item(s.value, s.updatedAt, s.updatedAt)
                    changed += key
                }
            }
            persist()
        }
        changed.forEach { _changes.tryEmit(it to true) }
        if (synchronized(this) { items.values.any { it.dirty && !it.parked } }) scope.launch { pushAll() }
        return true
    }

    fun startPeriodic(isForeground: () -> Boolean) {
        scope.launch {
            while (true) {
                if (isForeground()) sync()
                delay(60_000)
            }
        }
    }
}
