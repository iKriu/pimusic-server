package io.github.ikriu.pimusic.core

import java.net.URI

/** Адрес сервера PiMusic: только origin (scheme://host[:port]); http — только для локальной сети. */
object ServerUrl {
    /** Адреса по умолчанию нет: при первом запуске пользователь вводит адрес своего сервера. */
    const val DEFAULT = ""

    /** Репозиторий GitHub, из выпусков которого приходят обновления (в своём форке — поменять). */
    const val UPDATE_REPO = "iKriu/pimusic-server"
    const val UPDATE_MANIFEST = "https://github.com/$UPDATE_REPO/releases/latest/download/android.json"
    /** Куда можно ходить за обновлением: github.com и его хранилища файлов выпусков (перенаправления). */
    val UPDATE_HOSTS = setOf("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")

    /** Частные IPv4/IPv6, localhost, *.local, *.lan, *.home.arpa, имя без точки. */
    fun isLanHost(raw: String?): Boolean {
        var host = raw?.lowercase() ?: return false
        if (host.startsWith("[") && host.endsWith("]")) host = host.substring(1, host.length - 1)
        if (host == "localhost" || host.endsWith(".local") || host.endsWith(".lan") || host.endsWith(".home.arpa")) return true
        if (Regex("\\d{1,3}(\\.\\d{1,3}){3}").matches(host)) {
            val p = host.split(".").map { it.toInt() }
            val (a, b) = p[0] to p[1]
            return a == 10 || a == 127 || (a == 172 && b in 16..31) || (a == 192 && b == 168) || (a == 169 && b == 254) ||
                (a == 100 && b in 64..127)
        }
        if (host.contains(":")) return host == "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80")
        return !host.contains(".")
    }

    /** Нормализовать ввод пользователя → origin. Ошибка — IllegalArgumentException с текстом для пользователя. */
    fun normalize(input: String): String {
        var s = input.trim()
        if (s.isEmpty()) throw IllegalArgumentException("Пустой адрес")
        if (!s.contains("://")) s = "https://$s"
        val u = try { URI(s) } catch (e: Exception) { throw IllegalArgumentException("Неверный адрес") }
        val scheme = u.scheme?.lowercase() ?: throw IllegalArgumentException("Неверный адрес")
        val host = u.host?.lowercase()
        if (host.isNullOrEmpty()) throw IllegalArgumentException("Неверный адрес")
        if (scheme != "http" && scheme != "https") throw IllegalArgumentException("Нужен адрес https://")
        if (scheme == "http" && !isLanHost(host)) {
            throw IllegalArgumentException("http:// разрешён только для адресов локальной сети — используйте https://")
        }
        val h = if (host.contains(":") && !host.startsWith("[")) "[$host]" else host
        return if (u.port == -1) "$scheme://$h" else "$scheme://$h:${u.port}"
    }

    /** Абсолютный адрес для пути сервера («/api/cover/…») или как есть (https://…). */
    fun absolute(base: String, path: String?): String? {
        if (path.isNullOrEmpty()) return null
        if (path.startsWith("http://") || path.startsWith("https://")) return path
        return base.trimEnd('/') + (if (path.startsWith("/")) path else "/$path")
    }

    /** Параметры подписи и срока: меняются при каждой выдаче адреса, картинку не определяют. */
    private val SIGNATURE_PARAMS = setOf("s", "e", "exp", "expires", "sig", "signature")

    /**
     * Ключ кэша обложки: адрес без подписи и срока (?s=&e= меняется каждый час, картинка — нет),
     * но со смысловыми параметрами — у /api/artist-image исполнитель задаётся в ?a=.
     */
    fun coverKey(url: String): String {
        val noFrag = url.substringBefore('#')
        val q = noFrag.indexOf('?')
        if (q < 0) return noFrag
        val kept = noFrag.substring(q + 1).split('&')
            .filter { it.isNotEmpty() && it.substringBefore('=').lowercase() !in SIGNATURE_PARAMS }
        return noFrag.substring(0, q) + if (kept.isEmpty()) "" else "?" + kept.joinToString("&")
    }
}
