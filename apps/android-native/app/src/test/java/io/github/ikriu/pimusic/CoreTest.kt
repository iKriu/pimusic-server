package io.github.ikriu.pimusic

import io.github.ikriu.pimusic.core.Band
import io.github.ikriu.pimusic.core.BiquadCoefs
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqFormat
import io.github.ikriu.pimusic.core.EqState
import io.github.ikriu.pimusic.core.M3Color
import io.github.ikriu.pimusic.core.QueueModel
import io.github.ikriu.pimusic.core.RepeatMode
import io.github.ikriu.pimusic.core.ServerUrl
import io.github.ikriu.pimusic.core.UpdateManifest
import io.github.ikriu.pimusic.core.eqResponseDb
import io.github.ikriu.pimusic.core.fmtTime
import io.github.ikriu.pimusic.core.lyricIndex
import io.github.ikriu.pimusic.core.nTracks
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import kotlin.math.abs
import kotlin.random.Random

class QueueModelTest {
    @Test fun shuffleKeepsCurrentFirstAndAllItems() {
        val items = (1..50).toList()
        val s = QueueModel.shuffled(items, 10, Random(1))
        assertEquals(11, s.first())
        assertEquals(items.toSet(), s.toSet())
        assertEquals(items.size, s.size)
    }

    @Test fun unshuffleRestoresOrderAndAppendsNew() {
        val original = listOf("a", "b", "c", "d")
        val now = listOf("c", "a", "x", "d") // b удалён, x добавлен во время перемешивания
        assertEquals(listOf("a", "c", "d", "x"), QueueModel.unshuffled(original, now))
    }

    @Test fun moveClamps() {
        assertEquals(listOf(2, 3, 1), QueueModel.move(listOf(1, 2, 3), 0, 10))
        assertEquals(listOf(1, 2, 3), QueueModel.move(listOf(1, 2, 3), 5, 0))
    }

    @Test fun nextAndRepeat() {
        assertEquals(1, QueueModel.nextIndex(0, 3, RepeatMode.OFF))
        assertNull(QueueModel.nextIndex(2, 3, RepeatMode.OFF))
        assertEquals(0, QueueModel.nextIndex(2, 3, RepeatMode.ALL))
        assertEquals(2, QueueModel.nextIndex(2, 3, RepeatMode.ONE))
        assertEquals(RepeatMode.ALL, RepeatMode.OFF.next())
        assertEquals(RepeatMode.OFF, RepeatMode.ONE.next())
    }

    @Test fun prefetchCurrentAndTwoNext() {
        assertEquals(listOf(3, 4, 5), QueueModel.prefetchIndices(3, 10, RepeatMode.OFF))
        assertEquals(listOf(9), QueueModel.prefetchIndices(9, 10, RepeatMode.OFF))
        assertEquals(listOf(9, 0, 1), QueueModel.prefetchIndices(9, 10, RepeatMode.ALL))
        assertEquals(listOf(0, 1), QueueModel.prefetchIndices(0, 2, RepeatMode.ALL))
        assertTrue(QueueModel.prefetchIndices(-1, 0, RepeatMode.OFF).isEmpty())
    }

    @Test fun prevGoesToStartAfterThreeSeconds() {
        assertTrue(QueueModel.prevGoesToStart(3500))
        assertFalse(QueueModel.prevGoesToStart(2000))
    }
}

class EqTest {
    private val sr = 48000.0

    @Test fun flatIsZero() {
        eqResponseDb(EqState(), doubleArrayOf(31.0, 1000.0, 16000.0), sr).forEach { assertEquals(0.0, it, 1e-6) }
    }

    @Test fun peakingGainAtCenter() {
        val c = BiquadCoefs.of(Band("peaking", 1000.0, 6.0, 1.0), sr)
        assertEquals(6.0, c.magnitudeDb(1000.0, sr), 0.05)
        assertEquals(0.0, c.magnitudeDb(20.0, sr), 0.2)
    }

    @Test fun shelvesReachGainAtExtremes() {
        val lo = BiquadCoefs.of(Band("lowshelf", 100.0, -8.0), sr)
        assertEquals(-8.0, lo.magnitudeDb(20.0, sr), 0.6)
        assertEquals(0.0, lo.magnitudeDb(10000.0, sr), 0.2)
        val hi = BiquadCoefs.of(Band("highshelf", 4000.0, 5.0), sr)
        assertEquals(5.0, hi.magnitudeDb(20000.0, sr), 0.6)
    }

    @Test fun preampAddsEverywhere() {
        eqResponseDb(EqState(preamp = -3.0), doubleArrayOf(100.0, 5000.0), sr).forEach { assertEquals(-3.0, it, 1e-6) }
    }

    @Test fun sanitizeLikeWeb() {
        val bad = Eq.flatBands().toMutableList()
        bad[3] = Band("lowshelf", 5.0, 30.0, 99.0) // полка не у крайней, частота/усиление/Q вне границ
        val s = Eq.sanitize(bad)
        assertEquals("peaking", s[3].type)
        assertEquals(20.0, s[3].f, 0.0)
        assertEquals(12.0, s[3].g, 0.0)
        assertEquals(8.0, s[3].q, 0.0)
        assertEquals(10, Eq.sanitize(listOf(Band(f = 1.0))).size)
        assertEquals(1.2, Eq.sanitize(Eq.flatBands().map { it.copy(g = 1.234) })[0].g, 1e-9)
    }

    @Test fun builtinPresetsMatchWeb() {
        assertEquals(10, Eq.BUILTIN.size)
        val bass = Eq.BUILTIN.first { it.id == "bass" }
        assertEquals(-3.0, bass.preamp, 0.0) // -round(6 * 0.5 * 2) / 2
        assertEquals(6.0, bass.bands[0].g, 0.0)
    }

    @Test fun normalization() {
        assertEquals(1.0, Eq.normalizationGain(-3.0, 0.9, false), 0.0)
        assertEquals(Eq.dbToGain(-3.0), Eq.normalizationGain(-3.0, 0.9, true), 1e-9)
        assertEquals(Eq.dbToGain(6.0), Eq.normalizationGain(10.0, 0.1, true), 1e-9) // не выше +6 дБ
        assertEquals(0.99 / 0.8, Eq.normalizationGain(6.0, 0.8, true), 1e-9) // не выше пика
        assertEquals(1.0, Eq.normalizationGain(null, null, true), 0.0)
    }

    @Test fun parseInput() {
        assertEquals(1500.0, EqFormat.parseFreq("1500")!!, 0.0)
        assertEquals(1500.0, EqFormat.parseFreq("1.5k")!!, 0.0)
        assertEquals(1500.0, EqFormat.parseFreq("1,5 кГц")!!, 0.0)
        assertEquals(1500.0, EqFormat.parseFreq("1 500 Гц")!!, 0.0)
        assertNull(EqFormat.parseFreq("abc"))
        assertEquals(-3.4, EqFormat.parseNumber("−3,4 дБ", "дб")!!, 1e-9)
        assertEquals(2.0, EqFormat.parseNumber("+2", "дб")!!, 0.0)
    }

    @Test fun formatValues() {
        assertEquals("1 500 Гц", EqFormat.freq(1500.0))
        assertEquals("31,5 Гц", EqFormat.freq(31.5))
        assertEquals("+3,4 дБ", EqFormat.gain(3.4))
        assertEquals("−1,0 дБ", EqFormat.gain(-1.0))
        assertEquals("Q 1,40", EqFormat.q(1.4))
        assertEquals("1,5k", EqFormat.freqShort(1500.0))
        assertEquals(1000.0, EqFormat.fromLog(EqFormat.toLog(1000.0, Eq.F_RANGE), Eq.F_RANGE), 1e-6)
    }
}

class ColorTest {
    @Test fun seedSchemeHasAaContrast() {
        for (dark in listOf(true, false)) {
            val s = M3Color.scheme(M3Color.rgbToOklch(M3Color.SEED), dark)
            fun c(a: M3Color.Role, b: M3Color.Role) = M3Color.contrast(s.getValue(a), s.getValue(b))
            assertTrue(c(M3Color.Role.Primary, M3Color.Role.OnPrimary) >= 4.5)
            assertTrue(c(M3Color.Role.Surface, M3Color.Role.OnSurface) >= 4.5)
            assertTrue(c(M3Color.Role.Surface, M3Color.Role.OnSurfaceVariant) >= 4.5)
            assertTrue(c(M3Color.Role.PrimaryContainer, M3Color.Role.OnPrimaryContainer) >= 4.5)
        }
    }

    @Test fun primaryKeepsSeedHue() {
        val src = M3Color.rgbToOklch(M3Color.SEED)
        assertTrue(src.h in 50.0..90.0)
        val h = M3Color.rgbToOklch(M3Color.scheme(src, dark = true).getValue(M3Color.Role.Primary)).h
        assertTrue("оттенок primary $h", abs(h - src.h) < 15)
    }

    @Test fun harmonizePullsAtMost15() {
        assertEquals(150.0, M3Color.harmonize(165.0, 80.0), 1e-9)
        assertEquals(170.0, M3Color.harmonize(165.0, 175.0), 1e-9)
    }

    @Test fun grayCoverGivesNull() {
        assertNull(M3Color.sourceFromPixels(IntArray(1024) { 0xFF808080.toInt() }))
        val red = M3Color.sourceFromPixels(IntArray(1024) { 0xFFD03030.toInt() })!!
        assertTrue(red.h < 45 || red.h > 340)
    }
}

class MiscTest {
    @Test fun serverUrl() {
        assertEquals("https://music.example.com", ServerUrl.normalize("music.example.com"))
        assertEquals("https://music.example.com", ServerUrl.normalize("https://music.example.com/app/"))
        assertEquals("http://192.168.1.10:8080", ServerUrl.normalize("http://192.168.1.10:8080/app"))
        try { ServerUrl.normalize("http://example.com"); fail() } catch (e: IllegalArgumentException) { assertTrue(e.message!!.contains("локальной")) }
        assertTrue(ServerUrl.isLanHost("10.1.2.3"))
        assertTrue(ServerUrl.isLanHost("nas.local"))
        assertFalse(ServerUrl.isLanHost("8.8.8.8"))
        assertEquals("https://h/api/cover/1?u=a", ServerUrl.absolute("https://h", "/api/cover/1?u=a"))
        assertEquals("https://x/y.jpg", ServerUrl.absolute("https://h", "https://x/y.jpg"))
        assertNull(ServerUrl.absolute("https://h", null))
    }

    @Test fun coverKeyDropsOnlySignature() {
        // Обложка трека: подпись и срок меняются — ключ нет
        assertEquals("https://h/api/cover/7?u=max", ServerUrl.coverKey("https://h/api/cover/7?u=max&s=abc&e=100"))
        assertEquals(ServerUrl.coverKey("https://h/api/cover/7?u=max&s=abc&e=100"),
            ServerUrl.coverKey("https://h/api/cover/7?u=max&s=zzz&e=200"))
        // Картинки исполнителей: разные ?a= → разные ключи (раньше совпадали)
        val a = ServerUrl.coverKey("https://h/api/artist-image?u=max&a=Queen&s=1")
        val b = ServerUrl.coverKey("https://h/api/artist-image?u=max&a=ABBA&s=2")
        assertEquals("https://h/api/artist-image?u=max&a=Queen", a)
        assertFalse(a == b)
        assertEquals(a, ServerUrl.coverKey("https://h/api/artist-image?u=max&a=Queen&s=other"))
        // Внешние адреса и прочие варианты
        assertEquals("https://x/y.jpg", ServerUrl.coverKey("https://x/y.jpg"))
        assertEquals("https://x/y.jpg?size=500", ServerUrl.coverKey("https://x/y.jpg?size=500&sig=q&Expires=9#f"))
        assertEquals("https://x/y.jpg", ServerUrl.coverKey("https://x/y.jpg?s=1&e=2"))
    }

    @Test fun updateManifest() {
        val sha = "a".repeat(64)
        val rel = "https://github.com/${ServerUrl.UPDATE_REPO}/releases/download"
        val u = UpdateManifest.parse("""{"versionCode":10001,"versionName":"1.0.1","url":"$rel/v1.0.1/PiMusic-1.0.1.apk","sha256":"${sha.uppercase()}","notes":" x ","size":5}""")
        assertEquals(10001, u.versionCode)
        assertEquals(sha, u.sha256)
        assertEquals("x", u.notes)
        for (bad in listOf(
            """{"versionCode":1,"url":"http://github.com/${ServerUrl.UPDATE_REPO}/releases/download/v1/a.apk","sha256":"$sha"}""",
            """{"versionCode":1,"url":"https://evil.example/a.apk","sha256":"$sha"}""",
            """{"versionCode":1,"url":"https://github.com/someone/else/releases/download/v1/a.apk","sha256":"$sha"}""",
            """{"versionCode":1,"url":"$rel/../../x/a.apk","sha256":"$sha"}""",
            """{"versionCode":1,"url":"$rel/v1/a.apk","sha256":"zz"}""",
            """{"url":"$rel/v1/a.apk","sha256":"$sha"}""",
        )) {
            try { UpdateManifest.parse(bad); fail(bad) } catch (e: IllegalArgumentException) { /* ожидаемо */ }
        }
    }

    @Test fun lyrics() {
        val t = listOf(0.0, 5.0, 10.0, 15.0)
        assertEquals(-1, lyricIndex(listOf(2.0), 0.5))
        assertEquals(0, lyricIndex(t, 1.0))
        assertEquals(1, lyricIndex(t, 9.7))
        assertEquals(2, lyricIndex(t, 10.0))
        assertEquals(3, lyricIndex(t, 100.0))
    }

    @Test fun formatting() {
        assertEquals("3:42", fmtTime(222))
        assertEquals("1:02:03", fmtTime(3723))
        assertEquals("1 трек", nTracks(1))
        assertEquals("3 трека", nTracks(3))
        assertEquals("11 треков", nTracks(11))
        assertEquals("21 трек", nTracks(21))
    }
}

class SpectrumAndCrossfadeTest {
    @Test fun fftFindsSine() {
        val sr = 48000.0
        val sp = io.github.ikriu.pimusic.core.Spectrum(2048, smoothing = 0.0)
        val buf = FloatArray(2048) { (0.5 * kotlin.math.sin(2 * Math.PI * 1000.0 * it / sr)).toFloat() }
        sp.update(buf)
        val at1k = sp.dbAt(1000.0, sr)
        val at8k = sp.dbAt(8000.0, sr)
        assertTrue("1 кГц $at1k > 8 кГц $at8k", at1k > at8k + 30)
    }

    @Test fun crossfadeEqualPower() {
        val c = io.github.ikriu.pimusic.core.Crossfade
        assertEquals(1.0, c.outGain(0.0), 1e-9)
        assertEquals(0.0, c.inGain(0.0), 1e-9)
        assertEquals(1.0, c.inGain(1.0), 1e-9)
        val mid = c.outGain(0.5) * c.outGain(0.5) + c.inGain(0.5) * c.inGain(0.5)
        assertEquals(1.0, mid, 1e-9) // равная мощность
    }

    @Test fun crossfadeStartRules() {
        val c = io.github.ikriu.pimusic.core.Crossfade
        assertTrue(c.shouldStart(6.0, 5000, 200_000, 1f, false, true, false))
        assertFalse(c.shouldStart(6.0, 7000, 200_000, 1f, false, true, false)) // ещё рано
        assertTrue(c.shouldStart(6.0, 7000, 200_000, 1.5f, false, true, false)) // на 1,5× осталось меньше 6 с
        assertFalse(c.shouldStart(0.0, 1000, 200_000, 1f, false, true, false)) // выключен
        assertFalse(c.shouldStart(6.0, 1000, 200_000, 1f, true, true, false)) // повтор трека
        assertFalse(c.shouldStart(6.0, 1000, 200_000, 1f, false, false, false)) // нет следующего
        assertFalse(c.shouldStart(6.0, 1000, 200_000, 1f, false, true, true)) // таймер «до конца трека»
        assertFalse(c.shouldStart(6.0, 1000, 12_000, 1f, false, true, false)) // трек короче 2,5× перехода
    }
}
