package io.github.ikriu.pimusic.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cbrt
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sign
import kotlin.math.sin

/**
 * M3-цвет без зависимостей — порт `static/js/color.js` (те же цвета, что у Mini App):
 * тон = CIE L* (контраст ролей как в Material Color Utilities), оттенок и хрома — OKLCH,
 * выход за sRGB — бинарный поиск максимальной хромы.
 */
object M3Color {
    data class Source(val h: Double, val c: Double)

    const val SEED = 0xFFF5A524.toInt()

    private fun lin(c: Double) = if (c <= 0.04045) c / 12.92 else ((c + 0.055) / 1.055).pow(2.4)
    private fun gam(c: Double) = if (c <= 0.0031308) 12.92 * c else 1.055 * c.pow(1 / 2.4) - 0.055

    fun rgbToOklch(argb: Int): Source {
        val r = lin(((argb shr 16) and 255) / 255.0)
        val g = lin(((argb shr 8) and 255) / 255.0)
        val b = lin((argb and 255) / 255.0)
        val l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
        val m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
        val s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
        val a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
        val bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
        var h = atan2(bb, a) * 180 / PI
        if (h < 0) h += 360
        return Source(h, hypot(a, bb))
    }

    /** L (OKLab) для отбора пикселей обложки. */
    fun oklabL(argb: Int): Double {
        val r = lin(((argb shr 16) and 255) / 255.0)
        val g = lin(((argb shr 8) and 255) / 255.0)
        val b = lin((argb and 255) / 255.0)
        val l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
        val m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
        val s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
        return 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    }

    private fun oklabToLinear(L: Double, a: Double, b: Double): DoubleArray {
        val l = (L + 0.3963377774 * a + 0.2158037573 * b).pow(3)
        val m = (L - 0.1055613458 * a - 0.0638541728 * b).pow(3)
        val s = (L - 0.0894841775 * a - 1.2914855480 * b).pow(3)
        return doubleArrayOf(
            4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
        )
    }

    private fun yFromTone(t: Double) = if (t > 8) ((t + 16) / 116).pow(3) else t / 903.2962962963
    private fun luminance(rgb: DoubleArray) = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    private class Attempt(val ok: Boolean, val rgb: DoubleArray)

    /** Цвет тона [tone] (L*) с оттенком [h] и хромой [c] (OKLCH) → ARGB. */
    fun solve(h: Double, c: Double, tone: Double): Int {
        if (tone <= 0) return argb(0.0, 0.0, 0.0)
        if (tone >= 100) return argb(255.0, 255.0, 255.0)
        val yt = yFromTone(tone)
        val rad = h * PI / 180
        fun attempt(cc: Double): Attempt {
            val a = cc * cos(rad)
            val b = cc * sin(rad)
            var lo = 0.0
            var hi = 1.0
            var rgb = doubleArrayOf(0.0, 0.0, 0.0)
            repeat(22) {
                val l = (lo + hi) / 2
                rgb = oklabToLinear(l, a, b)
                if (luminance(rgb) < yt) lo = l else hi = l
            }
            return Attempt(rgb.all { it >= -1e-4 && it <= 1 + 1e-4 }, rgb)
        }
        var r = attempt(c)
        if (!r.ok) {
            var lo = 0.0
            var hi = c
            var best = attempt(0.0)
            repeat(14) {
                val mid = (lo + hi) / 2
                val t = attempt(mid)
                if (t.ok) {
                    best = t; lo = mid
                } else hi = mid
            }
            r = best
        }
        val out = r.rgb.map { gam(min(1.0, max(0.0, it))) * 255 }
        return argb(out[0], out[1], out[2])
    }

    private fun argb(r: Double, g: Double, b: Double): Int {
        fun ch(v: Double) = v.roundToInt().coerceIn(0, 255)
        return (0xFF shl 24) or (ch(r) shl 16) or (ch(g) shl 8) or ch(b)
    }

    /** Роли M3: палитра и тоны (тёмная, светлая) — как ROLES в color.js. */
    enum class Role(val pal: String, val dark: Int, val light: Int) {
        Primary("p", 80, 40), OnPrimary("p", 20, 100), PrimaryContainer("p", 30, 90), OnPrimaryContainer("p", 90, 10),
        InversePrimary("p", 40, 80), SurfaceTint("p", 80, 40),
        Secondary("s", 80, 40), OnSecondary("s", 20, 100), SecondaryContainer("s", 30, 90), OnSecondaryContainer("s", 90, 10),
        Tertiary("t", 80, 40), OnTertiary("t", 20, 100), TertiaryContainer("t", 30, 90), OnTertiaryContainer("t", 90, 10),
        Error("e", 80, 40), OnError("e", 20, 100), ErrorContainer("e", 30, 90), OnErrorContainer("e", 90, 10),
        Background("n", 6, 98), OnBackground("n", 90, 10),
        Surface("n", 6, 98), SurfaceDim("n", 6, 87), SurfaceBright("n", 24, 98),
        SurfaceContainerLowest("n", 4, 100), SurfaceContainerLow("n", 10, 96), SurfaceContainer("n", 12, 94),
        SurfaceContainerHigh("n", 17, 92), SurfaceContainerHighest("n", 22, 90),
        OnSurface("n", 90, 10), SurfaceVariant("nv", 30, 90), OnSurfaceVariant("nv", 80, 30),
        Outline("nv", 60, 50), OutlineVariant("nv", 30, 80),
        InverseSurface("n", 90, 20), InverseOnSurface("n", 20, 95), Scrim("n", 0, 0),
        Success("ok", 80, 40), OnSuccess("ok", 20, 100), SuccessContainer("ok", 30, 90), OnSuccessContainer("ok", 90, 10),
    }

    /** «в медиатеке» — зелёно-бирюзовый hue 165, притянутый к источнику не более чем на 15° (harmonize). */
    fun harmonize(target: Double, src: Double): Double {
        val d = ((src - target + 540) % 360) - 180
        return (target + sign(d) * min(abs(d) * 0.5, 15.0) + 360) % 360
    }

    /** Схема ролей: tonalSpot — из seed; content — из обложки (хрома следует источнику). */
    fun scheme(src: Source, dark: Boolean, content: Boolean = false): Map<Role, Int> {
        val h = src.h
        val spec = HashMap<String, Pair<Double, Double>>()
        if (content) {
            val pc = min(max(src.c, 0.075), 0.17)
            spec["p"] = h to pc
            spec["s"] = h to min(max(pc - 0.09, pc * 0.42), 0.07)
            spec["t"] = harmonize(165.0, h) to max(min(pc * 0.62, 0.11), 0.06)
            spec["n"] = h to min(pc / 9, 0.018)
            spec["nv"] = h to min(pc / 9 + 0.012, 0.03)
        } else {
            spec["p"] = h to 0.105
            spec["s"] = h to 0.042
            spec["t"] = (h + 60) % 360 to 0.068
            spec["n"] = h to 0.012
            spec["nv"] = h to 0.022
        }
        spec["e"] = 29.0 to 0.21
        spec["ok"] = harmonize(165.0, h) to 0.1
        val cache = HashMap<String, Int>()
        val out = LinkedHashMap<Role, Int>()
        for (role in Role.entries) {
            val tone = if (dark) role.dark else role.light
            val key = role.pal + tone
            out[role] = cache.getOrPut(key) {
                val (hh, cc) = spec.getValue(role.pal)
                solve(hh, cc, tone.toDouble())
            }
        }
        return out
    }

    fun contrast(a: Int, b: Int): Double {
        fun y(c: Int) = luminance(doubleArrayOf(lin(((c shr 16) and 255) / 255.0), lin(((c shr 8) and 255) / 255.0), lin((c and 255) / 255.0)))
        val ya = y(a) + 0.05
        val yb = y(b) + 0.05
        return if (ya > yb) ya / yb else yb / ya
    }

    /**
     * Цвет из обложки (как sourceFromImage): пиксели (уже уменьшенные до ~32×32) → гистограмма оттенков (36 корзин),
     * вес — хрома; почти чёрное/белое/серое отбрасывается. null — обложка серая (берём seed).
     */
    fun sourceFromPixels(pixels: IntArray): Source? {
        val w = DoubleArray(36)
        val hs = DoubleArray(36)
        val cnt = IntArray(36)
        for (p in pixels) {
            if (((p ushr 24) and 255) < 128) continue
            val l = oklabL(p)
            val s = rgbToOklch(p)
            if (l < 0.2 || l > 0.96 || s.c < 0.03) continue
            val i = (s.h / 10).toInt() % 36
            w[i] += s.c; hs[i] += s.h; cnt[i]++
        }
        var best = -1
        var bestScore = 0.0
        for (i in 0 until 36) {
            if (cnt[i] == 0) continue
            val score = w[i] + 0.5 * (w[(i + 35) % 36] + w[(i + 1) % 36])
            if (score > bestScore) { bestScore = score; best = i }
        }
        if (best < 0 || cnt[best] < 12) return null
        return Source(hs[best] / cnt[best], w[best] / cnt[best])
    }
}
