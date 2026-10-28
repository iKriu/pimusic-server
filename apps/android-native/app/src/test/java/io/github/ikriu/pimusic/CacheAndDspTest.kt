package io.github.ikriu.pimusic

import androidx.media3.common.C
import androidx.media3.common.audio.AudioProcessor
import androidx.media3.datasource.cache.Cache
import androidx.media3.datasource.cache.CacheSpan
import io.github.ikriu.pimusic.core.Band
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqState
import io.github.ikriu.pimusic.player.EqAudioProcessor
import io.github.ikriu.pimusic.player.MutableLruEvictor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.lang.reflect.Proxy
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sin

/** LRU-вытеснение кэша потока (изменяемый лимит) и обработка звука эквалайзером. */
class CacheAndDspTest {
    /** Поддельный Cache: removeSpan сообщает вытеснителю об удалении (как SimpleCache). */
    private fun fakeCache(ev: MutableLruEvictor, removed: MutableList<String>): Cache =
        Proxy.newProxyInstance(Cache::class.java.classLoader, arrayOf(Cache::class.java)) { proxy, m, args ->
            when (m.name) {
                "removeSpan" -> { val s = args[0] as CacheSpan; removed += s.key; ev.onSpanRemoved(proxy as Cache, s); null }
                else -> null
            }
        } as Cache

    private fun span(key: String, len: Long, touched: Long) = CacheSpan(key, 0, len, touched, File("/tmp/$key"))

    @Test fun evictsLeastRecentlyUsedOverLimit() {
        val ev = MutableLruEvictor(300)
        val removed = mutableListOf<String>()
        val cache = fakeCache(ev, removed)
        ev.onSpanAdded(cache, span("track:1", 100, 1))
        ev.onSpanAdded(cache, span("track:2", 100, 2))
        ev.onSpanAdded(cache, span("track:3", 100, 3))
        assertTrue(removed.isEmpty())
        // трек 1 прослушан снова — теперь самый свежий
        ev.onSpanTouched(cache, span("track:1", 100, 1), span("track:1", 100, 4))
        ev.onSpanAdded(cache, span("track:4", 100, 5))
        assertEquals(listOf("track:2"), removed)
        // лимит уменьшили в настройках — лишнее вытесняется сразу, старые первыми
        ev.onStartFile(cache, "track:5", 0, C.LENGTH_UNSET.toLong())
        ev.applyLimit(150)
        assertEquals(listOf("track:2", "track:3", "track:1"), removed)
    }

    @Test fun startFileMakesRoomForKnownLength() {
        val ev = MutableLruEvictor(200)
        val removed = mutableListOf<String>()
        val cache = fakeCache(ev, removed)
        ev.onSpanAdded(cache, span("a", 150, 1))
        ev.onStartFile(cache, "b", 0, 100)
        assertEquals(listOf("a"), removed)
    }

    private fun sine(freq: Double, frames: Int, amp: Double, sr: Int = 48000): ByteBuffer {
        val b = ByteBuffer.allocateDirect(frames * 4).order(ByteOrder.nativeOrder())
        for (i in 0 until frames) {
            val v = (amp * sin(2 * PI * freq * i / sr) * 32767).toInt().toShort()
            b.putShort(v); b.putShort(v)
        }
        b.flip()
        return b
    }

    private fun peak(out: ByteBuffer, skip: Int): Double {
        var p = 0.0
        var i = 0
        while (out.remaining() >= 2) {
            val s = out.getShort() / 32768.0
            if (i++ >= skip * 2) p = max(p, abs(s))
        }
        return p
    }

    private fun run(proc: EqAudioProcessor, input: ByteBuffer): ByteBuffer {
        proc.configure(AudioProcessor.AudioFormat(48000, 2, C.ENCODING_PCM_16BIT))
        proc.flush()
        proc.queueInput(input)
        return proc.output
    }

    @Test fun disabledEqPassesThrough() {
        val p = EqAudioProcessor()
        val out = run(p, sine(1000.0, 4800, 0.5))
        assertEquals(0.5, peak(out, 100), 0.01)
    }

    @Test fun peakingBandBoostsItsFrequency() {
        val p = EqAudioProcessor()
        p.setEq(EqState(enabled = true, preamp = 0.0, bands = Eq.flatBands().mapIndexed { i, b -> if (i == 5) b.copy(g = 6.0) else b }, limiter = false))
        val out = run(p, sine(1000.0, 9600, 0.25))
        assertEquals(0.25 * Eq.dbToGain(6.0), peak(out, 4800), 0.02)
    }

    @Test fun limiterKeepsBelowCeiling() {
        val p = EqAudioProcessor()
        p.setEq(EqState(enabled = true, preamp = 6.0, bands = Eq.flatBands().map { it.copy(g = 12.0) } + emptyList<Band>(), limiter = true))
        p.setTrackGain(2.0)
        val out = run(p, sine(200.0, 9600, 0.9))
        assertTrue(peak(out, 4800) <= Eq.dbToGain(-1.5) + 0.01)
    }
}
