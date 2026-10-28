package io.github.ikriu.pimusic.data

import io.github.ikriu.pimusic.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.BufferedSink
import okio.source
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resumeWithException

val PiJson = Json {
    ignoreUnknownKeys = true
    coerceInputValues = true
    explicitNulls = false
    isLenient = true
    encodeDefaults = true // настройки (eq.device.* и т. п.) пишутся целиком, как в eq.js
}

/** Ошибка API: status 0 — нет связи; code — поле error ответа; message — текст для пользователя. */
class ApiException(val status: Int, val code: String, override val message: String, val data: JsonObject? = null) : IOException(message)

private val MESSAGES = mapOf(
    0 to "Нет связи с сервером",
    401 to "Вход больше не действует — войдите снова",
    403 to "Нет доступа",
    404 to "Не найдено",
    409 to "Уже выполняется",
    500 to "Ошибка сервера",
    502 to "Внешний сервис недоступен",
)

/**
 * JSON API PiMusic (`Authorization: Bearer <токен устройства>`). 401 по токену — токен стёрт, событие [authLost]
 * (экран входа). Тела — kotlinx.serialization.
 */
class Api(private val session: Session, val http: OkHttpClient) {
    private val json = "application/json; charset=utf-8".toMediaType()
    private val _authLost = MutableSharedFlow<String>(extraBufferCapacity = 1)
    val authLost: SharedFlow<String> = _authLost

    companion object {
        /** Только для тестов (скриншоты Roborazzi): поддельный сервер вместо сети. */
        @Volatile var testInterceptor: okhttp3.Interceptor? = null

        fun client(): OkHttpClient = OkHttpClient.Builder()
            .apply { testInterceptor?.let { addInterceptor(it) } }
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .followRedirects(false)
            .addInterceptor { chain ->
                chain.proceed(chain.request().newBuilder().header("User-Agent", "PiMusic-Android/${BuildConfig.VERSION_NAME}").build())
            }
            .build()
    }

    fun url(path: String) = session.server.trimEnd('/') + "/api" + path

    private fun request(path: String, auth: Boolean = true): Request.Builder {
        val b = Request.Builder().url(url(path))
        val t = session.token
        if (auth && !t.isNullOrEmpty()) b.header("Authorization", "Bearer $t")
        return b
    }

    suspend fun raw(req: Request, auth: Boolean = true): String = withContext(Dispatchers.IO) {
        val resp = try { http.newCall(req).await() } catch (e: IOException) {
            throw ApiException(0, "network", MESSAGES.getValue(0))
        }
        resp.use { r ->
            val body = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw error(r.code, body, auth)
            body
        }
    }

    private fun error(status: Int, body: String, auth: Boolean): ApiException {
        val obj = runCatching { PiJson.parseToJsonElement(body).jsonObject }.getOrNull()
        val code = obj?.get("error")?.jsonPrimitive?.contentOrNull ?: status.toString()
        val msg = obj?.get("message")?.jsonPrimitive?.contentOrNull ?: obj?.get("text")?.jsonPrimitive?.contentOrNull
            ?: MESSAGES[status] ?: "Ошибка $status"
        if (status == 401 && auth && !session.token.isNullOrEmpty()) {
            session.signOut()
            val reason = when (code) {
                "revoked" -> "Вход на этом устройстве отозван (на другом устройстве или в боте). Войдите снова."
                "token_expired" -> "Срок входа на этом устройстве истёк. Войдите снова."
                else -> "Вход на этом устройстве больше не действует. Войдите снова."
            }
            _authLost.tryEmit(reason)
        }
        return ApiException(status, code, msg, obj)
    }

    suspend fun getText(path: String, auth: Boolean = true) = raw(request(path, auth).get().build(), auth)

    suspend fun <T> get(path: String, de: DeserializationStrategy<T>): T = PiJson.decodeFromString(de, getText(path))

    suspend fun <T> post(path: String, body: JsonElement?, de: DeserializationStrategy<T>, auth: Boolean = true): T {
        val rb = (body ?: buildJsonObject { }).toString().toRequestBody(json)
        return PiJson.decodeFromString(de, raw(request(path, auth).post(rb).build(), auth))
    }

    suspend fun <T> put(path: String, body: JsonElement, de: DeserializationStrategy<T>): T =
        PiJson.decodeFromString(de, raw(request(path).put(body.toString().toRequestBody(json)).build()))

    suspend fun delete(path: String): String = raw(request(path).delete().build())

    /** Файл для multipart: имя, тип, длина и поток. */
    class Part(val field: String, val name: String, val type: String, val length: Long, val open: () -> InputStream)

    /** multipart/form-data с прогрессом отправки (байты, всего). */
    suspend fun <T> multipart(path: String, parts: List<Part>, de: DeserializationStrategy<T>, progress: (Long, Long) -> Unit): T {
        val total = parts.sumOf { maxOf(0L, it.length) }
        var sent = 0L
        val mb = MultipartBody.Builder().setType(MultipartBody.FORM)
        for (p in parts) {
            val body = object : RequestBody() {
                override fun contentType(): MediaType = p.type.toMediaType()
                override fun contentLength(): Long = if (p.length >= 0) p.length else -1
                override fun writeTo(sink: BufferedSink) {
                    p.open().source().use { src ->
                        val buf = okio.Buffer()
                        while (true) {
                            val n = src.read(buf, 64 * 1024)
                            if (n <= 0) break
                            sink.write(buf, n)
                            sent += n
                            progress(sent, total)
                        }
                    }
                }
            }
            mb.addFormDataPart(p.field, p.name, body)
        }
        val req = request(path).post(mb.build()).build()
        val client = http.newBuilder().writeTimeout(10, TimeUnit.MINUTES).readTimeout(5, TimeUnit.MINUTES).build()
        return withContext(Dispatchers.IO) {
            val resp = try { client.newCall(req).await() } catch (e: IOException) { throw ApiException(0, "network", MESSAGES.getValue(0)) }
            resp.use { r ->
                val text = r.body?.string().orEmpty()
                if (!r.isSuccessful) {
                    // 413/415/429/507 с частично принятыми файлами: тело — тот же формат
                    if (r.code in listOf(413, 415, 429, 507)) {
                        runCatching { return@withContext PiJson.decodeFromString(de, text) }
                    }
                    throw error(r.code, text, true)
                }
                PiJson.decodeFromString(de, text)
            }
        }
    }
}

suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
    enqueue(object : Callback {
        override fun onFailure(call: Call, e: IOException) { if (!cont.isCancelled) cont.resumeWithException(e) }
        override fun onResponse(call: Call, response: Response) { cont.resume(response) { _, _, _ -> response.close() } }
    })
    cont.invokeOnCancellation { runCatching { cancel() } }
}
