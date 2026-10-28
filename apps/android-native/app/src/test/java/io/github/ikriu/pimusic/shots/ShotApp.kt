package io.github.ikriu.pimusic.shots

import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.data.Api

/** Приложение для скриншот-тестов: вошедший пользователь bob, API — поддельный сервер (FakeServer), без сети. */
class ShotApp : PiApp() {
    override fun onCreate() {
        Api.testInterceptor = FakeServer
        // нативная графика Robolectric — сначала в главном потоке (иначе JNI в чужом потоке: «Class not found: java/nio/…»)
        // JniConstants кэширует java.nio.*Buffer при первом обращении; из фонового потока (Coil, OkHttp) — падает
        android.graphics.Bitmap.createBitmap(4, 4, android.graphics.Bitmap.Config.ARGB_8888).also { b ->
            b.copyPixelsToBuffer(java.nio.ByteBuffer.allocate(b.byteCount))
            b.copyPixelsFromBuffer(java.nio.IntBuffer.allocate(16))
            android.graphics.Canvas(b).drawPath(android.graphics.Path().apply { addCircle(2f, 2f, 1f, android.graphics.Path.Direction.CW) }, android.graphics.Paint())
            android.graphics.Matrix().apply { setScale(2f, 2f); mapPoints(FloatArray(4)) }
            b.recycle()
        }
        FakeServer.warmUp()
        getSharedPreferences("pimusic", MODE_PRIVATE).edit()
            .putString("server", "https://music.example")
            .putString("token", "t".repeat(43))
            .putString("login", "bob")
            .putString("device_id", "test-device")
            .commit()
        super.onCreate()
    }

    /** Освободить плеер и кэши (иначе потоки ExoPlayer копятся между тестами). */
    fun shutdown() {
        runCatching { player.player.release() }
        runCatching { offline.manager.release() }
        runCatching { caches.stream.release() }
        runCatching { caches.offline.release() }
    }
}
