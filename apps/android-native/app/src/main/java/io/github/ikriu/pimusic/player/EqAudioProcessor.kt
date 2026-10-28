package io.github.ikriu.pimusic.player

import androidx.media3.common.C
import androidx.media3.common.audio.AudioProcessor
import androidx.media3.common.audio.BaseAudioProcessor
import io.github.ikriu.pimusic.core.BiquadCoefs
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqState
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.min

/**
 * Эквалайзер в цепочке ExoPlayer (PCM 16 бит или float): преамп × нормализация → 10 биквадов (RBJ, как Web Audio) →
 * лимитер (потолок −1,5 дБFS, атака мгновенная, отпускание 120 мс). Параметры меняются на лету из главного потока;
 * усиление (нормализация) — плавно, без щелчков. Всегда активен: выключенный EQ — только усиление и лимитер.
 */
class EqAudioProcessor : BaseAudioProcessor() {
    @Volatile private var state = EqState(enabled = false)
    @Volatile private var bypass = false
    @Volatile private var trackGain = 1.0
    @Volatile private var version = 0

    private var appliedVersion = -1
    private var sampleRate = 48000
    private var channels = 2
    private var encoding = C.ENCODING_PCM_16BIT
    private var coefs: Array<BiquadCoefs> = emptyArray()
    // состояние фильтров: [полоса][канал][x1, x2, y1, y2]
    private var z: Array<Array<DoubleArray>> = emptyArray()
    private var eqOn = false
    private var limiterOn = true
    private var pre = 1.0
    private var curGain = 1.0
    private var env = 1.0
    private val thr = Eq.dbToGain(-1.5)
    private var releaseCoef = 0.0

    // анализатор спектра: кольцевой буфер моно-отсчётов на выходе (пишется, только пока открыт экран EQ)
    @Volatile var analyze = false
    private val ring = FloatArray(4096)
    @Volatile private var ringPos = 0 // гонки с snapshot безвредны: это только картинка
    val outputSampleRate get() = sampleRate

    /** Последние out.size отсчётов выхода (моно). */
    fun snapshot(out: FloatArray) = synchronized(ring) {
        val n = out.size.coerceAtMost(ring.size)
        var p = (ringPos - n + ring.size) % ring.size
        for (i in 0 until n) { out[i] = ring[p]; p = (p + 1) % ring.size }
    }

    fun setEq(s: EqState) { state = s; version++ }
    fun setBypass(on: Boolean) { bypass = on; version++ }
    /** Нормализация текущего трека (множитель, может быть > 1 — тогда работает лимитер). */
    fun setTrackGain(g: Double) { trackGain = g; version++ }

    override fun onConfigure(inputAudioFormat: AudioProcessor.AudioFormat): AudioProcessor.AudioFormat {
        if (inputAudioFormat.encoding != C.ENCODING_PCM_16BIT && inputAudioFormat.encoding != C.ENCODING_PCM_FLOAT) {
            return AudioProcessor.AudioFormat.NOT_SET // иной формат — пропускаем без обработки
        }
        return inputAudioFormat
    }

    override fun onFlush() {
        sampleRate = inputAudioFormat.sampleRate.takeIf { it > 0 } ?: 48000
        channels = inputAudioFormat.channelCount.coerceAtLeast(1)
        encoding = inputAudioFormat.encoding
        appliedVersion = -1
        env = 1.0
        z = emptyArray()
    }

    override fun onReset() {
        z = emptyArray()
        appliedVersion = -1
    }

    private fun refresh() {
        val v = version
        if (v == appliedVersion && z.isNotEmpty()) return
        appliedVersion = v
        val s = state
        eqOn = s.enabled && !bypass
        limiterOn = s.limiter
        val bands = Eq.sanitize(s.bands)
        coefs = bands.map { BiquadCoefs.of(it, sampleRate.toDouble()) }.toTypedArray()
        if (z.size != coefs.size || z.firstOrNull()?.size != channels) {
            z = Array(coefs.size) { Array(channels) { DoubleArray(4) } }
        }
        pre = (if (eqOn) Eq.dbToGain(s.preamp) else 1.0) * trackGain
        releaseCoef = 1 - exp(-1.0 / (0.12 * sampleRate))
    }

    override fun queueInput(inputBuffer: ByteBuffer) {
        val remaining = inputBuffer.remaining()
        if (remaining == 0) return
        refresh()
        val out = replaceOutputBuffer(remaining)
        val float = encoding == C.ENCODING_PCM_FLOAT
        val bytesPerSample = if (float) 4 else 2
        val frameBytes = bytesPerSample * channels
        val frames = remaining / frameBytes
        val inb = inputBuffer.order(ByteOrder.nativeOrder())
        val frame = DoubleArray(channels)
        val target = pre
        // плавный переход усиления за буфер (смена трека/преампа без щелчка)
        val step = if (frames > 0) (target - curGain) / frames else 0.0
        val nb = coefs.size
        val tap = analyze
        for (f in 0 until frames) {
            curGain += step
            var peak = 0.0
            for (c in 0 until channels) {
                var x = if (float) inb.getFloat().toDouble() else inb.getShort() / 32768.0
                x *= curGain
                if (eqOn) {
                    for (b in 0 until nb) {
                        val k = coefs[b]
                        val s = z[b][c]
                        val y = k.b0 * x + k.b1 * s[0] + k.b2 * s[1] - k.a1 * s[2] - k.a2 * s[3]
                        s[1] = s[0]; s[0] = x; s[3] = s[2]; s[2] = y
                        x = y
                    }
                }
                frame[c] = x
                peak = max(peak, abs(x))
            }
            if (limiterOn) {
                val want = if (peak > thr) thr / peak else 1.0
                env = if (want < env) want else env + (want - env) * releaseCoef
            } else env = 1.0
            var mono = 0.0
            for (c in 0 until channels) {
                val y = (frame[c] * env).coerceIn(-1.0, 1.0)
                mono += y
                if (float) out.putFloat(y.toFloat()) else out.putShort((y * 32767.0).toInt().toShort())
            }
            if (tap) { ring[ringPos] = (mono / channels).toFloat(); ringPos = (ringPos + 1) % ring.size }
        }
        // хвост, не кратный кадру (не бывает при корректном PCM) — как есть
        while (inb.hasRemaining()) out.put(inb.get())
        curGain = target
        out.flip()
    }
}
