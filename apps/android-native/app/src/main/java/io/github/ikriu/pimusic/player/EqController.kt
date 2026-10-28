package io.github.ikriu.pimusic.player

import io.github.ikriu.pimusic.core.Band
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqState
import io.github.ikriu.pimusic.core.Preset
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Session
import io.github.ikriu.pimusic.data.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlin.math.roundToInt

/**
 * Эквалайзер и опции воспроизведения из настроек (тот же формат ключей, что eq.js / playerctl.js):
 *   eq.presets — общая библиотека своих пресетов; eq.shared — общий активный профиль;
 *   eq.device.<id> — устройство {name, platform, mode: sync|local, active, preamp, bands, enabled, limiter, spectrum, output};
 *   player — {normalize, crossfade, speed}.
 */
class EqController(private val settings: SettingsStore, private val session: Session, private val hub: PlayerHub, scope: CoroutineScope) {
    @Serializable data class JBand(val type: String = "peaking", val f: Double = 1000.0, val g: Double = 0.0, val q: Double = 1.0)
    @Serializable
    data class JDevice(
        val name: String = "", val platform: String = "android", val mode: String = "sync", val active: String = "flat",
        val preamp: Double = 0.0, val bands: List<JBand>? = null, val enabled: Boolean = true, val limiter: Boolean = true,
        val spectrum: Boolean = true, val output: String = "default",
    )
    @Serializable data class JShared(val active: String = "flat", val preamp: Double = 0.0, val bands: List<JBand>? = null)
    @Serializable data class JPreset(val id: String, val name: String, val preamp: Double = 0.0, val bands: List<JBand>? = null)
    @Serializable data class PlayerOpts(val normalize: Boolean = true, val crossfade: Double = 0.0, val speed: Double = 1.0)

    data class View(
        val device: JDevice,
        val effective: EqState,
        val active: String,
        val presets: List<Preset>,
        val custom: List<Preset>,
        val bypass: Boolean,
    )

    private val _view = MutableStateFlow(read())
    val view: StateFlow<View> = _view.asStateFlow()
    private val _opts = MutableStateFlow(readOpts())
    val opts: StateFlow<PlayerOpts> = _opts.asStateFlow()
    private var bypass = false

    init {
        scope.launch {
            settings.changes.collect { (key, _) ->
                if (key.startsWith("eq.")) apply()
                if (key == "player") applyOpts()
            }
        }
        apply()
        applyOpts()
    }

    private fun devKey(id: String = session.deviceId) = "eq.device.$id"
    private fun toBands(b: List<JBand>?) = Eq.sanitize(b?.map { Band(it.type, it.f, it.g, it.q) })
    private fun fromBands(b: List<Band>) = b.map { JBand(it.type, it.f, it.g, it.q) }

    private fun defaults() = JDevice(name = session.deviceName, platform = "android", bands = fromBands(Eq.flatBands()))

    fun device(id: String = session.deviceId): JDevice? =
        settings.getAs<JDevice>(devKey(id)) ?: if (id == session.deviceId) defaults() else null

    private fun shared(): JShared = settings.getAs<JShared>("eq.shared") ?: JShared(bands = fromBands(Eq.flatBands()))

    fun customPresets(): List<Preset> = (settings.get("eq.presets")?.let {
        runCatching { PiJson.decodeFromJsonElement(ListSerializer(JPreset.serializer()), it) }.getOrNull()
    } ?: emptyList()).map { Preset(it.id, it.name, it.preamp.coerceIn(Eq.PREAMP_RANGE), toBands(it.bands)) }

    fun allPresets() = Eq.BUILTIN + customPresets()

    fun effective(id: String = session.deviceId): Pair<JDevice, EqState> {
        val d = device(id) ?: defaults()
        val (active, preamp, bands) = if (d.mode == "sync") shared().let { Triple(it.active, it.preamp, it.bands) } else Triple(d.active, d.preamp, d.bands)
        return d.copy(active = active) to EqState(d.enabled, preamp.coerceIn(Eq.PREAMP_RANGE), toBands(bands), d.limiter)
    }

    private fun read(id: String = session.deviceId): View {
        val (d, st) = effective(id)
        return View(d, st, d.active, allPresets(), customPresets(), bypass)
    }

    fun viewFor(id: String) = read(id)

    private fun apply() {
        val v = read()
        _view.value = v
        hub.applyEq(v.effective, bypass)
    }

    /** Первая запись своего устройства: имя и платформа видны на других устройствах. */
    fun ensureDevice() {
        if (settings.get(devKey()) == null) writeDevice(session.deviceId, defaults())
    }

    private fun writeDevice(id: String, d: JDevice) = settings.set(devKey(id), PiJson.encodeToJsonElement(JDevice.serializer(), d))

    private fun editProfile(id: String, active: String? = null, preamp: Double? = null, bands: List<Band>? = null) {
        val d = device(id) ?: defaults()
        if (d.mode == "sync") {
            val s = shared()
            settings.set("eq.shared", PiJson.encodeToJsonElement(JShared.serializer(),
                s.copy(active = active ?: s.active, preamp = preamp ?: s.preamp, bands = bands?.let(::fromBands) ?: s.bands)))
        } else {
            writeDevice(id, d.copy(active = active ?: d.active, preamp = preamp ?: d.preamp, bands = bands?.let(::fromBands) ?: d.bands))
        }
        if (id == session.deviceId) apply()
    }

    fun setBand(i: Int, band: Band, id: String = session.deviceId) {
        val bands = effective(id).second.bands.toMutableList()
        if (i !in bands.indices) return
        bands[i] = band
        editProfile(id, active = "custom", bands = Eq.sanitize(bands))
    }

    fun setPreamp(v: Double, id: String = session.deviceId) =
        editProfile(id, active = "custom", preamp = ((v * 10).roundToInt() / 10.0).coerceIn(Eq.PREAMP_RANGE))

    fun applyPreset(pid: String, id: String = session.deviceId) {
        val p = allPresets().find { it.id == pid } ?: return
        editProfile(id, active = p.id, preamp = p.preamp, bands = p.bands)
    }

    fun setEnabled(on: Boolean, id: String = session.deviceId) { writeDevice(id, (device(id) ?: defaults()).copy(enabled = on)); if (id == session.deviceId) apply() }
    fun setSpectrum(on: Boolean, id: String = session.deviceId) { writeDevice(id, (device(id) ?: defaults()).copy(spectrum = on)) }
    fun setLimiter(on: Boolean, id: String = session.deviceId) { writeDevice(id, (device(id) ?: defaults()).copy(limiter = on)); if (id == session.deviceId) apply() }
    fun rename(name: String, id: String = session.deviceId) { writeDevice(id, (device(id) ?: defaults()).copy(name = name.trim().take(60).ifEmpty { session.deviceName })) }

    /** Смена режима: в «local» переносим текущий общий профиль, чтобы звук не прыгнул. */
    fun setMode(mode: String, id: String = session.deviceId) {
        val d = device(id) ?: defaults()
        if (d.mode == mode) return
        if (mode == "local") { val s = shared(); writeDevice(id, d.copy(mode = mode, active = s.active, preamp = s.preamp, bands = s.bands)) }
        else writeDevice(id, d.copy(mode = mode))
        if (id == session.deviceId) apply()
    }

    fun setBypass(on: Boolean) { bypass = on; apply() }

    private fun pid() = "u" + System.currentTimeMillis().toString(36) + (0..9999).random().toString(36)

    fun savePreset(name: String, id: String = session.deviceId) {
        val st = effective(id).second
        val list = settings.get("eq.presets")?.let { runCatching { PiJson.decodeFromJsonElement(ListSerializer(JPreset.serializer()), it) }.getOrNull() } ?: emptyList()
        val item = JPreset(pid(), name.trim().take(40).ifEmpty { "Мой пресет" }, st.preamp, fromBands(st.bands))
        settings.set("eq.presets", PiJson.encodeToJsonElement(ListSerializer(JPreset.serializer()), list + item))
        editProfile(id, active = item.id)
    }

    private fun editPresets(f: (List<JPreset>) -> List<JPreset>) {
        val list = settings.get("eq.presets")?.let { runCatching { PiJson.decodeFromJsonElement(ListSerializer(JPreset.serializer()), it) }.getOrNull() } ?: emptyList()
        settings.set("eq.presets", PiJson.encodeToJsonElement(ListSerializer(JPreset.serializer()), f(list)))
        apply()
    }

    fun renamePreset(pid: String, name: String) = editPresets { l -> l.map { if (it.id == pid) it.copy(name = name.trim().take(40).ifEmpty { it.name }) else it } }
    fun deletePreset(pid: String) = editPresets { l -> l.filter { it.id != pid } }

    /** Все устройства из настроек (Настройки → «Эквалайзер на других устройствах»). */
    fun devices(): List<Pair<String, JDevice>> = settings.keys("eq.device.").mapNotNull { k ->
        val id = k.removePrefix("eq.device.")
        device(id)?.takeIf { it.name.isNotEmpty() }?.let { id to it }
    }

    // ---------------------------------------------------------------- опции плеера (ключ «player»)

    private fun readOpts() = settings.getAs<PlayerOpts>("player") ?: PlayerOpts()

    private fun applyOpts() {
        val o = readOpts()
        _opts.value = o
        hub.setNormalize(o.normalize)
        hub.setSpeed(o.speed.toFloat())
        hub.setCrossfade(o.crossfade)
    }

    fun setOpts(o: PlayerOpts) {
        settings.set("player", PiJson.encodeToJsonElement(PlayerOpts.serializer(), o))
        applyOpts()
    }

    fun refreshAll() { apply(); applyOpts() }
}
