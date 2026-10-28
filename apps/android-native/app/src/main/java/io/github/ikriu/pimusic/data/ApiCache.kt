package io.github.ikriu.pimusic.data

import java.io.File
import java.security.MessageDigest

/**
 * Ответы GET API на диске (cacheDir/api): экран открывается мгновенно с прошлым ответом, затем обновляется.
 * Ключ — логин + путь (у разных пользователей на одном устройстве — свои ответы). Нужен и для офлайн-режима.
 */
class ApiCache(private val dir: File, private val session: Session) {
    private fun file(path: String): File {
        val md = MessageDigest.getInstance("SHA-1").digest(("${session.login}|${session.server}|$path").toByteArray())
        return File(dir, md.joinToString("") { "%02x".format(it) } + ".json")
    }

    fun read(path: String): String? = runCatching { file(path).takeIf { it.isFile }?.readText() }.getOrNull()

    fun write(path: String, body: String) {
        runCatching {
            dir.mkdirs()
            val f = file(path)
            val tmp = File(dir, f.name + ".tmp")
            tmp.writeText(body)
            tmp.renameTo(f)
        }
    }

    fun clear() { dir.listFiles()?.forEach { it.delete() } }
    fun size(): Long = dir.listFiles()?.sumOf { it.length() } ?: 0
}
