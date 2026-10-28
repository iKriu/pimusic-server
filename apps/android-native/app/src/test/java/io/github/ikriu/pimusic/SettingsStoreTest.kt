package io.github.ikriu.pimusic

import io.github.ikriu.pimusic.data.ApiException
import io.github.ikriu.pimusic.data.SettingItem
import io.github.ikriu.pimusic.data.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Логика настроек по ключам (как settings.js): base, конфликты 409, «парковка», синхронизация. */
class SettingsStoreTest {
    private class MemStorage : SettingsStore.Storage {
        val data = HashMap<String, String>()
        override fun load(login: String) = data[login]
        override fun save(login: String, json: String) { data[login] = json }
    }

    private class FakeBackend : SettingsStore.Backend {
        val server = HashMap<String, SettingItem>()
        val puts = ArrayList<Triple<String, JsonElement, Long?>>()
        var fail: ((String, Long?) -> ApiException?)? = null
        var clock = 1000L
        override suspend fun put(key: String, value: JsonElement, base: Long?): Long {
            puts += Triple(key, value, base)
            fail?.invoke(key, base)?.let { throw it }
            clock += 10
            server[key] = SettingItem(value, clock)
            return clock
        }
        override suspend fun all() = HashMap(server)
    }

    // отдельный планировщик, который никто не продвигает: отложенная отправка (1,2 с) в тестах не срабатывает сама
    private val idle = CoroutineScope(StandardTestDispatcher(TestCoroutineScheduler()))
    private fun store(storage: MemStorage = MemStorage(), backend: FakeBackend = FakeBackend()) =
        SettingsStore(storage, backend, idle, { true }).also { it.setLogin("bob") }

    @Test fun setPushesAndClearsDirtyWithServerBase() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        s.set("player", JsonPrimitive(1), now = 5)
        assertTrue(s.item("player")!!.dirty)
        assertFalse(s.pushAll())
        val it = s.item("player")!!
        assertFalse(it.dirty)
        assertEquals(1010L, it.base)
        assertNull(b.puts.single().third) // первый раз — без base
        s.set("player", JsonPrimitive(2), now = 6)
        s.pushAll()
        assertEquals(1010L, b.puts.last().third) // дальше — base = updated_at сервера
    }

    @Test fun conflictAdoptsServerValue() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        val current = JsonObject(mapOf("value" to JsonPrimitive("server"), "updated_at" to JsonPrimitive(777)))
        b.fail = { _, _ -> ApiException(409, "conflict", "конфликт", JsonObject(mapOf("current" to current))) }
        s.set("ui.theme", JsonPrimitive("local"))
        s.pushAll()
        assertEquals(JsonPrimitive("server"), s.get("ui.theme"))
        assertEquals(777L, s.item("ui.theme")!!.base)
        assertFalse(s.item("ui.theme")!!.dirty)
    }

    @Test fun conflictWithoutCurrentRetriesWithoutBase() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        s.set("eq.shared", JsonPrimitive(1))
        s.pushAll() // base = 1010
        b.server.clear()
        b.fail = { _, base -> if (base != null) ApiException(409, "conflict", "нет ключа", JsonObject(mapOf("current" to JsonNull))) else null }
        s.set("eq.shared", JsonPrimitive(2))
        assertTrue(s.pushAll()) // повтор позже
        assertEquals(0L, s.item("eq.shared")!!.base)
        assertTrue(s.item("eq.shared")!!.dirty)
        assertFalse(s.pushAll()) // без base — прошло
        assertFalse(s.item("eq.shared")!!.dirty)
    }

    @Test fun badRequestParksKey() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        b.fail = { _, _ -> ApiException(413, "too_large", "больше 32 КБ") }
        s.set("eq.presets", JsonPrimitive("x"))
        assertFalse(s.pushAll())
        assertTrue(s.item("eq.presets")!!.parked)
        val n = b.puts.size
        s.pushAll()
        assertEquals(n, b.puts.size) // запаркованный ключ больше не отправляется
        s.set("eq.presets", JsonPrimitive("y")) // новое значение снимает «парковку»
        assertFalse(s.item("eq.presets")!!.parked)
    }

    @Test fun networkErrorRetriesLater() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        b.fail = { _, _ -> ApiException(0, "network", "нет связи") }
        s.set("player", JsonPrimitive(1))
        assertTrue(s.pushAll())
        assertTrue(s.item("player")!!.dirty)
    }

    @Test fun syncTakesNewerServerKeepsLocalEdits() = runTest {
        val b = FakeBackend()
        val s = store(backend = b)
        b.server["a"] = SettingItem(JsonPrimitive("server-a"), 50)
        b.server["b"] = SettingItem(JsonPrimitive("server-b"), 60)
        s.set("b", JsonPrimitive("local-b")) // несохранённая локальная правка выигрывает
        assertTrue(s.sync())
        assertEquals(JsonPrimitive("server-a"), s.get("a"))
        assertEquals(JsonPrimitive("local-b"), s.get("b"))
        assertEquals(listOf("a"), s.keys("a"))
    }

    @Test fun storageIsPerLogin() {
        val storage = MemStorage()
        val s = store(storage = storage)
        s.set("player", JsonPrimitive(1))
        s.setLogin("alice")
        assertNull(s.get("player"))
        s.setLogin("bob")
        assertEquals(JsonPrimitive(1), s.get("player"))
        assertTrue(storage.data.containsKey("bob"))
    }
}
