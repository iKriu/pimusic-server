package io.github.ikriu.pimusic.core

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Анализатор спектра (как AnalyserNode Web Audio: fftSize 2048, окно Блэкмана, сглаживание 0,8, дБ).
 * Считается только пока открыт экран эквалайзера.
 */
class Spectrum(val size: Int = 2048, private val smoothing: Double = 0.8) {
    init { require(size > 0 && size and (size - 1) == 0) { "размер — степень двойки" } }

    private val window = DoubleArray(size) { i -> 0.42 - 0.5 * cos(2 * PI * i / size) + 0.08 * cos(4 * PI * i / size) }
    private val re = DoubleArray(size)
    private val im = DoubleArray(size)
    /** Сглаженные амплитуды бинов 0…size/2 (линейные). */
    val mag = DoubleArray(size / 2)

    /** Новый кадр (последние [size] отсчётов, моно −1…1). */
    fun update(samples: FloatArray) {
        for (i in 0 until size) { re[i] = (samples.getOrNull(i) ?: 0f) * window[i]; im[i] = 0.0 }
        fft(re, im)
        for (k in 0 until size / 2) {
            val m = sqrt(re[k] * re[k] + im[k] * im[k]) / size
            mag[k] = smoothing * mag[k] + (1 - smoothing) * m
        }
    }

    /** Уровень (дБ, −100…0) на частоте [f] при частоте дискретизации [sr]: максимум бинов вокруг. */
    fun dbAt(f: Double, sr: Double, width: Double = 1.06): Double {
        val bin = sr / size
        val lo = max(1, (f / width / bin).toInt())
        val hi = min(size / 2 - 1, max(lo, (f * width / bin).toInt()))
        var m = 0.0
        for (k in lo..hi) m = max(m, mag[k])
        return (20 * log10(max(m * 4, 1e-5))).coerceIn(-100.0, 0.0)
    }

    companion object {
        /** Итеративное БПФ radix-2 на месте. */
        fun fft(re: DoubleArray, im: DoubleArray) {
            val n = re.size
            var j = 0
            for (i in 1 until n) {
                var bit = n shr 1
                while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
                j = j xor bit
                if (i < j) { re[i] = re[j].also { re[j] = re[i] }; im[i] = im[j].also { im[j] = im[i] } }
            }
            var len = 2
            while (len <= n) {
                val ang = -2 * PI / len
                val wr = cos(ang); val wi = sin(ang)
                var i = 0
                while (i < n) {
                    var cr = 1.0; var ci = 0.0
                    for (k in 0 until len / 2) {
                        val ar = re[i + k + len / 2] * cr - im[i + k + len / 2] * ci
                        val ai = re[i + k + len / 2] * ci + im[i + k + len / 2] * cr
                        re[i + k + len / 2] = re[i + k] - ar; im[i + k + len / 2] = im[i + k] - ai
                        re[i + k] += ar; im[i + k] += ai
                        val t = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = t
                    }
                    i += len
                }
                len = len shl 1
            }
        }
    }
}

/** Плавный переход: громкость уходящего/входящего трека (равная мощность), t — 0…1. */
object Crossfade {
    fun outGain(t: Double) = cos(t.coerceIn(0.0, 1.0) * PI / 2)
    fun inGain(t: Double) = sin(t.coerceIn(0.0, 1.0) * PI / 2)

    /** Когда начинать переход (как player.js): включён, не волна, не «повтор трека», трек длиннее 2,5× перехода. */
    fun shouldStart(xfSec: Double, remainingMs: Long, durationMs: Long, speed: Float, repeatOne: Boolean, hasNext: Boolean, sleepEndOfTrack: Boolean): Boolean {
        if (xfSec <= 0 || repeatOne || !hasNext || sleepEndOfTrack || durationMs <= 0) return false
        if (durationMs < xfSec * 2500) return false
        val left = (remainingMs / max(speed, 0.1f)).toLong()
        return left in 1..(xfSec * 1000).toLong()
    }
}
