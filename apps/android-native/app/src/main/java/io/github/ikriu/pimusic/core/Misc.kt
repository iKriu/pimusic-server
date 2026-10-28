package io.github.ikriu.pimusic.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.net.URI
import java.util.Locale

/** Манифест обновления android.json (docs/design/APPS_API.md, «Обновления»). */
data class UpdateInfo(val versionCode: Int, val versionName: String, val url: String, val sha256: String, val notes: String, val size: Long?)

object UpdateManifest {
    /**
     * Разбор и проверка: APK только из выпусков репозитория обновлений
     * (https://github.com/<UPDATE_REPO>/releases/download/…), sha256 — 64 hex. Ошибка — IllegalArgumentException.
     */
    fun parse(body: String): UpdateInfo {
        val j: JsonObject = Json.parseToJsonElement(body).jsonObject
        val code = j["versionCode"]?.jsonPrimitive?.intOrNull ?: throw IllegalArgumentException("нет versionCode")
        val url = j["url"]?.jsonPrimitive?.contentOrNull ?: throw IllegalArgumentException("нет url")
        val sha = j["sha256"]?.jsonPrimitive?.contentOrNull?.lowercase(Locale.ROOT) ?: throw IllegalArgumentException("нет sha256")
        val u = URI(url)
        val prefix = "/${ServerUrl.UPDATE_REPO}/releases/download/"
        if (u.scheme != "https" || !"github.com".equals(u.host, ignoreCase = true) || u.port != -1 ||
            !(u.rawPath ?: "").startsWith(prefix) || u.rawPath.contains("..")) {
            throw IllegalArgumentException("url не из выпусков https://github.com/${ServerUrl.UPDATE_REPO}")
        }
        if (!Regex("[0-9a-f]{64}").matches(sha)) throw IllegalArgumentException("неверный sha256")
        return UpdateInfo(
            versionCode = code,
            versionName = j["versionName"]?.jsonPrimitive?.contentOrNull ?: code.toString(),
            url = url,
            sha256 = sha,
            notes = j["notes"]?.jsonPrimitive?.contentOrNull?.trim().orEmpty(),
            size = j["size"]?.jsonPrimitive?.contentOrNull?.toLongOrNull(),
        )
    }
}

/** Синхронный текст: индекс текущей строки по времени (последняя строка с t ≤ позиции). */
fun lyricIndex(times: List<Double>, positionSec: Double): Int {
    var lo = 0
    var hi = times.size - 1
    var ans = -1
    while (lo <= hi) {
        val mid = (lo + hi) / 2
        if (times[mid] <= positionSec + 0.15) { ans = mid; lo = mid + 1 } else hi = mid - 1
    }
    return ans
}

/** «3:42», «1:02:03». */
fun fmtTime(sec: Long): String {
    val s = if (sec < 0) 0 else sec
    val h = s / 3600
    val m = (s % 3600) / 60
    val ss = s % 60
    return if (h > 0) String.format(Locale.ROOT, "%d:%02d:%02d", h, m, ss) else String.format(Locale.ROOT, "%d:%02d", m, ss)
}

/** Склонение: plural(5, "трек", "трека", "треков"). */
fun plural(n: Long, one: String, few: String, many: String): String {
    val a = n % 100
    val b = n % 10
    return when {
        a in 11..14 -> many
        b == 1L -> one
        b in 2..4 -> few
        else -> many
    }
}

fun nTracks(n: Long) = "$n ${plural(n, "трек", "трека", "треков")}"

/** «1,2 ГБ», «350 МБ». */
fun fmtBytes(b: Long): String {
    val units = listOf("Б", "КБ", "МБ", "ГБ", "ТБ")
    var v = b.toDouble()
    var i = 0
    while (v >= 1024 && i < units.size - 1) { v /= 1024; i++ }
    val s = if (i == 0 || v >= 100) String.format(Locale.ROOT, "%.0f", v) else String.format(Locale.ROOT, "%.1f", v)
    return s.replace('.', ',') + " " + units[i]
}

/** «32 мин», «1 ч 05 мин». */
fun fmtDuration(sec: Long): String {
    val m = (sec + 30) / 60
    return if (m >= 60) "${m / 60} ч ${String.format(Locale.ROOT, "%02d", m % 60)} мин" else "$m мин"
}
