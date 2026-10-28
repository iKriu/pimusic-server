package io.github.ikriu.pimusic.shots

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import java.io.ByteArrayOutputStream

/**
 * Поддельный сервер PiMusic для скриншот-тестов: ответы API по форматам PLAYER_API.md / APPS_API.md
 * и сгенерированные обложки (градиенты разных оттенков — проверка цвета из обложки).
 */
object FakeServer : Interceptor {
    private val json = "application/json; charset=utf-8".toMediaType()

    private val artists = listOf("Кино", "Земфира", "Сплин", "Radiohead", "Daft Punk", "Мумий Тролль", "Би-2", "Nirvana")
    private val titles = listOf(
        "Группа крови", "Хочешь?", "Выхода нет", "Karma Police", "One More Time", "Владивосток 2000", "Полковнику никто не пишет",
        "Smells Like Teen Spirit", "Звезда по имени Солнце", "Искала", "Орбит без сахара", "Paranoid Android",
        "Очень длинное название трека, которое не помещается в одну строку на телефоне", "Get Lucky", "Утекай", "Серебро",
    )
    private val genres = listOf("Rock", "Pop", "Русский рок", "Electronic", "Alternative", "Grunge")

    fun track(i: Int, itemId: Int? = null, owned: Boolean = true): String {
        val a = artists[i % artists.size]
        val t = titles[i % titles.size]
        val sec = 150 + (i * 37) % 200
        val item = if (itemId != null) ""","item_id":$itemId,"added_by":"bob"""" else ""
        return """{"id":${i + 1},"artist":"$a","title":"$t","album":"Альбом ${(i % 5) + 1}","duration":"${sec / 60}:${"%02d".format(sec % 60)}",
            "seconds":$sec,"cover":"/api/cover/${i + 1}?u=bob&s=x&e=9999999999","stream":"/api/stream/${i + 1}?u=bob&e=9999999999&s=x",
            "owned":$owned,"gain":-2.5,"peak":0.9,"genre":"${genres[i % genres.size]}","year":${1985 + i},"added_at":${1760000000 - i * 3600},
            "artist_key":"$a","album_key":"Альбом ${(i % 5) + 1}","size":${8_000_000 + i * 100_000}$item}"""
    }

    private fun tracks(n: Int, from: Int = 0) = (from until from + n).joinToString(",") { track(it) }

    private fun ctrack(i: Int) = """{"i":$i,"artist":"${artists[i % artists.size]}","title":"${titles[i % titles.size]}","album":"Альбом ${i % 3 + 1}",
        "duration":"3:${10 + i}","key":"${if (i % 3 == 0) "lib|$i" else "k|$i"}","isrc":null,"cover":"https://cdn.example/c$i.jpg","preview":"https://cdn.example/p$i.mp3","year":"${2000 + i}","no":${i + 1}}"""

    private fun playlist(id: Int, withTracks: Boolean): String {
        val tr = if (withTracks) ""","tracks":[${(0 until 12).joinToString(",") { track(it + id, itemId = 100 + it, owned = it != 3) }}]""" else ""
        return """{"id":$id,"name":"${listOf("Дорога", "Для бега", "Вечер", "Общий с друзьями")[(id - 1) % 4]}","description":"${if (id == 1) "Музыка в машину" else ""}",
            "count":${10 + id * 3},"seconds":${2400 + id * 300},"covers":[${(1..(if (id == 2) 1 else 4)).joinToString(",") { "\"/api/cover/${it + id}?u=bob\"" }}],
            "shared":${id == 4 || id == 1},"invite_enabled":${id == 1},"role":"${if (id == 4) "member" else "owner"}","owner":"${if (id == 4) "alice" else "bob"}",
            "members":${if (id == 1 || id == 4) """[{"login":"alice"},{"login":"pastur"}]""" else "[]"},"updated_at":${1760000000 - id}$tr}"""
    }

    fun route(path: String, q: Map<String, String>): String? = when {
        path == "/api/me" -> """{"login":"bob","tracks":1477,"stream_url":"https://music.example/stream/bob/x.mp3"}"""
        path == "/api/library" -> {
            val filtered = q["genre"]?.isNotEmpty() == true || q["q"]?.isNotEmpty() == true
            """{"total":${if (filtered) 12 else 1477},"library_total":1477,"offset":0,"limit":200,"sort":"added","items":[${tracks(if (filtered) 12 else 40)}]}"""
        }
        path == "/api/library/genres" -> """{"items":[${genres.mapIndexed { i, g -> """{"name":"$g","count":${300 - i * 40}}""" }.joinToString(",")},{"name":"Jazz","count":4},{"name":"Folk","count":3},{"name":"Metal","count":2}]}"""
        path == "/api/library/keys" -> """{"keys":["lib|0","lib|3","lib|6"],"isrc":[]}"""
        path == "/api/library/artists" -> """{"items":[${artists.mapIndexed { i, a -> """{"key":"$a","name":"$a","count":${60 - i * 5},"year":${2000 + i},"added_at":1760000000,"image":${if (i % 3 == 0) "null" else "\"/api/artist-image?u=bob&a=$i\""}}""" }.joinToString(",")}]}"""
        path == "/api/library/albums" -> """{"items":[${(0 until 10).joinToString(",") { i -> """{"artist_key":"${artists[i % artists.size]}","key":"Альбом $i","title":"${if (i == 2) "Очень длинное название альбома с подзаголовком (Deluxe Edition)" else "Альбом $i"}","artist":"${artists[i % artists.size]}","count":${8 + i},"year":${1990 + i},"added_at":1760000000,"cover":"/api/cover/${i + 1}?u=bob"}""" }}]}"""
        path == "/api/library/artist" -> """{"key":"Кино","name":"Кино","count":24,"image":"/api/artist-image?u=bob&a=1","albums":[
            {"key":"Группа крови","title":"Группа крови","cover":"/api/cover/1?u=bob","tracks":[${tracks(6)}]},
            {"key":"Звезда","title":"Звезда по имени Солнце","cover":"/api/cover/9?u=bob","tracks":[${tracks(5, 8)}]}]}"""
        path == "/api/library/album" -> """{"key":"Группа крови","title":"Группа крови","cover":"/api/cover/1?u=bob","artist_key":"Кино","artist":"Кино","tracks":[${tracks(11)}]}"""
        path == "/api/library/play" -> """{"total":40,"items":[${tracks(40)}]}"""
        // свежие ссылки: запрошенные id, кроме кратных 7 (недоступные — проверка удаления из очереди)
        path == "/api/tracks" -> """{"items":[${q["ids"].orEmpty().split(",").mapNotNull { it.toIntOrNull() }.filter { it % 7 != 0 }.joinToString(",") { track(it - 1) }}]}"""
        path == "/api/playlists" -> """{"items":[${(1..4).joinToString(",") { playlist(it, false) }}]}"""
        path.matches(Regex("/api/playlists/\\d+")) -> playlist(path.substringAfterLast('/').toInt(), true)
        path == "/api/playlists/for-track" -> """{"items":[{"id":1,"name":"Дорога","has":true},{"id":2,"name":"Для бега","has":false},{"id":3,"name":"Вечер","has":false}]}"""
        path == "/api/search" -> """{"token":"tok","artist":"Кино","artist_id":"123","picture":"https://cdn.example/a.jpg","fans":250000,
            "tracks":[${(0 until 10).joinToString(",") { ctrack(it) }}],
            "albums":[${(0 until 6).joinToString(",") { """{"i":$it,"id":"${400 + it}","title":"Альбом ${it + 1}","year":"${1984 + it}","kind":"${if (it == 4) "single" else "album"}","artist":"Кино","artist_id":"123","cover":"https://cdn.example/al$it.jpg"}""" }}],
            "artists":[{"id":"123","name":"Кино","picture":"https://cdn.example/a.jpg","fans":250000},{"id":"124","name":"Кино и Цой","picture":null,"fans":1200}]}"""
        path == "/api/catalog/album" -> """{"token":"tok2","id":"9","title":"Группа крови","artist":"Кино","artist_id":"123","year":"1988","kind":"album","cover":"https://cdn.example/al1.jpg",
            "tracks":[${(0 until 11).joinToString(",") { ctrack(it) }}]}"""
        path == "/api/catalog/artist" -> """{"token":"tok3","id":"123","name":"Кино","picture":"https://cdn.example/a.jpg","fans":250000,
            "tracks":[${(0 until 10).joinToString(",") { ctrack(it) }}],
            "albums":[${(0 until 8).joinToString(",") { """{"i":$it,"id":"${400 + it}","title":"Альбом ${it + 1}","year":"${1984 + it}","kind":"${if (it > 5) "single" else "album"}","artist":"Кино","cover":"https://cdn.example/al$it.jpg"}""" }}]}"""
        path == "/api/catalog/artist/tracks" -> """{"token":"tok4","id":"123","name":"Кино","albums":12,"capped":false,"tracks":[${(0 until 20).joinToString(",") { ctrack(it) }}]}"""
        path == "/api/activity" -> """{"active":["Кино — Группа крови","Сплин — Выхода нет"],"jobs":[{"title":"Кино — все треки","total":42,"started":1760000000}],
            "log":[{"ts":1760000000,"text":"✅ Кино — Кукушка: в медиатеке"},{"ts":1759999900,"text":"❌ Не нашлось: Неизвестный — Трек"},{"ts":1759999800,"text":"📥 В очереди: Кино — все треки — 42 треков"}],
            "queue":3,"active_items":[{"text":"Кино — Группа крови","artist":"Кино","title":"Группа крови","track_id":null,"progress":"Soulseek: 45 %"}]}"""
        path == "/api/import" -> """{"imports":[{"id":7,"filename":"Мой плейлист Spotify.csv","total":120,"state":"running","counts":{"pending":60,"active":2,"done":50,"cached":5,"failed":3},"text":"Импорт: 58 из 120 · осталось ≈ 20 мин"}]}"""
        path == "/api/uploads" -> """{"items":[
            {"id":1,"name":"Неизвестная запись 03.mp3","state":"need_info","state_text":"Ждёт названия","duration":222.0,"size":5000000,"created_at":1760000000},
            {"id":2,"name":"track01.flac","state":"processing","state_text":"Обрабатывается","created_at":1760000000},
            {"id":3,"name":"Земфира — Искала.mp3","state":"done","state_text":"Сохранён","created_at":1760000000,"track":${track(9)}},
            {"id":4,"name":"broken.wav","state":"failed","state_text":"Не удалось","note":"не удалось обработать файл","created_at":1760000000}]}"""
        path == "/api/wave/now" -> """{"live":true,"listeners":2,"artist":"Сплин","title":"Выхода нет","album":"Гранатовый альбом","duration":220,"started_at":1760000000,"cover":"/api/cover/3?u=bob","track_id":3}"""
        path.startsWith("/api/lyrics/") -> """{"synced":[${(0 until 24).joinToString(",") { """{"t":${it * 4.0},"text":"${listOf("Тёплое место, но улицы ждут", "Отпечатков наших ног", "Звёздная пыль на сапогах", "Мягкое кресло, клетчатый плед", "")[it % 5]}"}""" }}],"plain":null,"source":"lrc"}"""
        path == "/api/settings" -> """{"items":{}}"""
        path == "/api/devices" -> """{"items":[{"id":1,"name":"Android · Pixel 8","platform":"android","created_at":1760000000,"last_seen":1760000000,"current":true},
            {"id":2,"name":"Windows","platform":"windows","created_at":1750000000,"last_seen":1759000000,"current":false}]}"""
        else -> null
    }

    private val pngs = java.util.concurrent.ConcurrentHashMap<Int, ByteArray>()

    /** Обложки заранее, в главном потоке (нативная графика Robolectric). */
    fun warmUp() { for (i in 0..60) pngs.getOrPut(i) { png(i) } }

    private fun cover(seed: Int) = pngs.getOrPut(seed % 61) { png(seed % 61) }

    private fun png(seed: Int, size: Int = 256): ByteArray {
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val h1 = (seed * 47 % 360).toFloat()
        val p = Paint().apply {
            shader = LinearGradient(0f, 0f, size.toFloat(), size.toFloat(),
                Color.HSVToColor(floatArrayOf(h1, 0.65f, 0.85f)), Color.HSVToColor(floatArrayOf((h1 + 40) % 360, 0.8f, 0.35f)), Shader.TileMode.CLAMP)
        }
        c.drawRect(0f, 0f, size.toFloat(), size.toFloat(), p)
        c.drawCircle(size * 0.68f, size * 0.36f, size * 0.18f, Paint().apply { color = Color.argb(160, 255, 255, 255) })
        return ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }.toByteArray()
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val req = chain.request()
        val url = req.url
        val path = url.encodedPath.let { java.net.URLDecoder.decode(it, "UTF-8") }
        val q = url.queryParameterNames.associateWith { url.queryParameter(it).orEmpty() }
        val b = Response.Builder().request(req).protocol(Protocol.HTTP_1_1).message("OK")
        if (path.startsWith("/api/cover/") || path.startsWith("/api/artist-image") || url.host == "cdn.example") {
            val seed = (path.filter { it.isDigit() }.toIntOrNull() ?: 1) + (q["a"]?.toIntOrNull() ?: 0) * 13
            return b.code(200).body(cover(seed).toResponseBody("image/png".toMediaType())).build()
        }
        val body = if (req.method == "GET") route(path, q) else """{"ok":true}"""
        return if (body != null) b.code(200).body(body.toResponseBody(json)).build()
        else b.code(404).message("Not Found").body("""{"error":"not_found"}""".toResponseBody(json)).build()
    }
}
