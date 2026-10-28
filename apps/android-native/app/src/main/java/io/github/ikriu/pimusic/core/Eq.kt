package io.github.ikriu.pimusic.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/** Полоса эквалайзера — формат eq.js: {type, f, g, q}. */
data class Band(val type: String = "peaking", val f: Double, val g: Double = 0.0, val q: Double = 1.0)

/** Профиль: полосы + преамп + пресет; enabled/limiter — у устройства. */
data class EqState(
    val enabled: Boolean = true,
    val preamp: Double = 0.0,
    val bands: List<Band> = Eq.flatBands(),
    val limiter: Boolean = true,
)

data class Preset(val id: String, val name: String, val preamp: Double, val bands: List<Band>, val builtin: Boolean = false)

object Eq {
    val FREQS = listOf(31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
    val F_RANGE = 20.0..20000.0
    val G_RANGE = -12.0..12.0
    val Q_RANGE = 0.3..8.0
    val PREAMP_RANGE = -12.0..6.0
    val TYPES = listOf("peaking", "lowshelf", "highshelf")

    fun flatBands() = FREQS.map { Band(f = it) }

    private fun preset(id: String, name: String, gains: List<Double>): Preset {
        val max = max(0.0, gains.max())
        return Preset(id, name, -(max * 0.5 * 2).roundToInt() / 2.0, FREQS.mapIndexed { i, f -> Band(f = f, g = gains[i]) }, true)
    }

    /** Встроенные пресеты — те же, что BUILTIN в eq.js. */
    val BUILTIN = listOf(
        preset("flat", "Ровно", listOf(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
        preset("bass", "Бас+", listOf(6.0, 5.0, 4.0, 2.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0)),
        preset("treble", "Высокие+", listOf(0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 2.0, 4.0, 5.0, 6.0)),
        preset("vocal", "Вокал", listOf(-2.0, -2.0, -1.0, 0.0, 2.0, 3.0, 3.0, 2.0, 0.0, -1.0)),
        preset("rock", "Рок", listOf(4.0, 3.0, 2.0, 0.0, -1.0, -1.0, 1.0, 2.5, 3.5, 4.0)),
        preset("electro", "Электроника", listOf(5.0, 4.0, 2.0, 0.0, -2.0, 0.0, 1.0, 2.0, 4.0, 5.0)),
        preset("classic", "Классика", listOf(3.0, 2.0, 1.0, 0.0, 0.0, 0.0, -1.0, -1.0, 1.0, 2.0)),
        preset("podcast", "Подкаст", listOf(-6.0, -4.0, -2.0, 0.0, 2.0, 3.0, 3.0, 2.0, 0.0, -2.0)),
        preset("warm", "Наушники · тёплые", listOf(3.0, 3.0, 2.0, 1.0, 0.0, 0.0, -1.0, -2.0, -2.0, -3.0)),
        preset("night", "Громкость ночью", listOf(-4.0, -3.0, -1.0, 0.0, 1.0, 2.0, 2.0, 1.0, 0.0, -2.0)),
    )

    /** Как sanitizeBands в eq.js: ровно 10 полос, полки только у крайних, значения в границах, усиление шаг 0,1. */
    fun sanitize(bands: List<Band>?): List<Band> {
        if (bands == null || bands.size != FREQS.size) return flatBands()
        return bands.mapIndexed { i, b ->
            val edge = i == 0 || i == FREQS.size - 1
            Band(
                type = if (b.type in TYPES && (edge || b.type == "peaking")) b.type else "peaking",
                f = b.f.takeIf { it.isFinite() && it > 0 }?.coerceIn(F_RANGE) ?: FREQS[i],
                g = ((b.g.takeIf { it.isFinite() } ?: 0.0) * 10).roundToInt() / 10.0,
                q = b.q.takeIf { it.isFinite() && it > 0 }?.coerceIn(Q_RANGE) ?: 1.0,
            ).let { it.copy(g = it.g.coerceIn(G_RANGE)) }
        }
    }

    fun dbToGain(db: Double) = 10.0.pow(db / 20)

    /** Нормализация (как player.js): подъём ≤ +6 дБ и не выше пика (0,99 / peak). */
    fun normalizationGain(gainDb: Double?, peak: Double?, enabled: Boolean): Double {
        if (!enabled || gainDb == null || !gainDb.isFinite()) return 1.0
        var g = 10.0.pow(min(6.0, gainDb) / 20)
        if (peak != null && peak > 0) g = min(g, max(1.0, 0.99 / peak))
        return g
    }
}

/** Коэффициенты биквада (RBJ Audio EQ Cookbook — как BiquadFilterNode Web Audio), нормированы на a0. */
class BiquadCoefs(val b0: Double, val b1: Double, val b2: Double, val a1: Double, val a2: Double) {
    companion object {
        fun of(band: Band, sampleRate: Double): BiquadCoefs {
            val nyq = sampleRate / 2
            val f = min(band.f, nyq * 0.999)
            val w0 = 2 * PI * f / sampleRate
            val cw = cos(w0)
            val sw = sin(w0)
            val a = 10.0.pow(band.g / 40)
            return when (band.type) {
                "lowshelf" -> {
                    // Web Audio: S = 1 (Q у полок не используется)
                    val alpha = sw / 2 * sqrt((a + 1 / a) * (1 / 1.0 - 1) + 2)
                    val k = 2 * sqrt(a) * alpha
                    val b0 = a * ((a + 1) - (a - 1) * cw + k)
                    val b1 = 2 * a * ((a - 1) - (a + 1) * cw)
                    val b2 = a * ((a + 1) - (a - 1) * cw - k)
                    val a0 = (a + 1) + (a - 1) * cw + k
                    val a1 = -2 * ((a - 1) + (a + 1) * cw)
                    val a2 = (a + 1) + (a - 1) * cw - k
                    BiquadCoefs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
                }
                "highshelf" -> {
                    val alpha = sw / 2 * sqrt((a + 1 / a) * (1 / 1.0 - 1) + 2)
                    val k = 2 * sqrt(a) * alpha
                    val b0 = a * ((a + 1) + (a - 1) * cw + k)
                    val b1 = -2 * a * ((a - 1) + (a + 1) * cw)
                    val b2 = a * ((a + 1) + (a - 1) * cw - k)
                    val a0 = (a + 1) - (a - 1) * cw + k
                    val a1 = 2 * ((a - 1) - (a + 1) * cw)
                    val a2 = (a + 1) - (a - 1) * cw - k
                    BiquadCoefs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
                }
                else -> {
                    val alpha = sw / (2 * band.q)
                    val a0 = 1 + alpha / a
                    BiquadCoefs((1 + alpha * a) / a0, -2 * cw / a0, (1 - alpha * a) / a0, -2 * cw / a0, (1 - alpha / a) / a0)
                }
            }
        }
    }

    /** |H(f)| в дБ. */
    fun magnitudeDb(freq: Double, sampleRate: Double): Double {
        val w = 2 * PI * freq / sampleRate
        val c1 = cos(w); val s1 = sin(w); val c2 = cos(2 * w); val s2 = sin(2 * w)
        val nr = b0 + b1 * c1 + b2 * c2
        val ni = -(b1 * s1 + b2 * s2)
        val dr = 1 + a1 * c1 + a2 * c2
        val di = -(a1 * s1 + a2 * s2)
        val mag = sqrt((nr * nr + ni * ni) / (dr * dr + di * di))
        return 20 * log10(max(mag, 1e-9))
    }
}

/** Суммарная АЧХ эквалайзера (кривая на экране). */
fun eqResponseDb(state: EqState, freqs: DoubleArray, sampleRate: Double = 48000.0): DoubleArray {
    val coefs = state.bands.map { BiquadCoefs.of(it, sampleRate) }
    return DoubleArray(freqs.size) { i -> state.preamp + coefs.sumOf { it.magnitudeDb(freqs[i], sampleRate) } }
}

/** Ручной ввод и подписи значений EQ (DESIGN.md §9.4): запятая/точка, «−», пробелы, «1.5k», «1,5 кГц». */
object EqFormat {
    private fun clean(s: String) = s.trim().lowercase()
        .replace('−', '-').replace('–', '-').replace(',', '.')
        .replace(" ", "").replace(" ", "").replace(" ", "")

    fun parseFreq(input: String): Double? {
        var s = clean(input).removeSuffix("гц").removeSuffix("hz")
        var mul = 1.0
        if (s.endsWith("кгц") || s.endsWith("khz")) { s = s.removeSuffix("кгц").removeSuffix("khz"); mul = 1000.0 }
        if (s.endsWith("k") || s.endsWith("к")) { s = s.dropLast(1); mul = 1000.0 }
        val v = s.toDoubleOrNull() ?: return null
        return (v * mul).takeIf { it.isFinite() }
    }

    fun parseNumber(input: String, vararg units: String): Double? {
        var s = clean(input)
        for (u in units) s = s.removeSuffix(u.lowercase())
        if (s.startsWith("+")) s = s.substring(1)
        return s.toDoubleOrNull()?.takeIf { it.isFinite() }
    }

    /** Частота с точностью: 0,1 Гц до 100 Гц, 1 Гц выше. */
    fun roundFreq(f: Double): Double = if (f < 100) (f * 10).roundToInt() / 10.0 else f.roundToInt().toDouble()

    private fun groupThousands(n: Long): String {
        val s = abs(n).toString()
        val sb = StringBuilder()
        for ((i, ch) in s.withIndex()) {
            if (i > 0 && (s.length - i) % 3 == 0) sb.append(' ')
            sb.append(ch)
        }
        return (if (n < 0) "−" else "") + sb
    }

    fun num(v: Double, decimals: Int): String {
        val p = 10.0.pow(decimals)
        val r = (v * p).roundToInt() / p
        val whole = r.toLong()
        val frac = abs(r - whole)
        val base = if (r < 0 && whole == 0L) "−0" else groupThousands(whole)
        if (decimals == 0) return base
        val digits = (frac * p).roundToInt().toString().padStart(decimals, '0')
        return "$base,$digits"
    }

    fun freq(f: Double) = if (f < 100 && f != f.roundToInt().toDouble()) "${num(f, 1)} Гц" else "${num(f, 0)} Гц"
    fun freqShort(f: Double) = if (f >= 1000) "${num(f / 1000, if (f % 1000 == 0.0) 0 else 1)}k" else num(f, 0)
    fun gain(g: Double) = (if (g > 0.04) "+" else "") + num(g, 1) + " дБ"
    fun q(q: Double) = "Q " + num(q, 2)

    /** Лог-шкала для слайдеров частоты/Q: 0…1 ↔ значение. */
    fun toLog(v: Double, r: ClosedFloatingPointRange<Double>) = (ln(v / r.start) / ln(r.endInclusive / r.start)).coerceIn(0.0, 1.0)
    fun fromLog(x: Double, r: ClosedFloatingPointRange<Double>) = r.start * (r.endInclusive / r.start).pow(x.coerceIn(0.0, 1.0))
}
