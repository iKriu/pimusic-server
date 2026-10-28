package io.github.ikriu.pimusic.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import io.github.ikriu.pimusic.BuildConfig
import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.core.ServerUrl
import io.github.ikriu.pimusic.core.UpdateInfo
import io.github.ikriu.pimusic.core.UpdateManifest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileInputStream
import java.io.IOException
import java.net.URL
import java.security.MessageDigest
import javax.net.ssl.HttpsURLConnection

/**
 * Автообновление вне Google Play (docs/design/APPS_API.md, «Обновления»): android.json последнего выпуска GitHub
 * ([ServerUrl.UPDATE_MANIFEST]) при запуске не чаще раза в сутки и по кнопке в Настройках; versionCode больше —
 * диалог M3 (рисует Compose по [state]), загрузка APK в кэш с прогрессом, сверка sha256, установка сессией
 * PackageInstaller. Только https и только хосты [ServerUrl.UPDATE_HOSTS] — в том числе на каждом перенаправлении.
 */
class Updater(private val ctx: Context, private val scope: CoroutineScope) {
    sealed interface State {
        data object Idle : State
        data class Available(val info: UpdateInfo, val manual: Boolean) : State
        data class NeedPermission(val info: UpdateInfo) : State
        data class Downloading(val info: UpdateInfo, val percent: Int?) : State
        data object Installing : State
        data class Error(val text: String) : State
        data class UpToDate(val version: String) : State
    }

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state.asStateFlow()
    private val prefs = ctx.getSharedPreferences("pimusic", Context.MODE_PRIVATE)
    private var job: Job? = null
    @Volatile private var cancelled = false
    private var waitingPermission: UpdateInfo? = null

    companion object {
        private const val MANIFEST = ServerUrl.UPDATE_MANIFEST
        private const val MAX_REDIRECTS = 5
        private const val EVERY_MS = 24L * 3600 * 1000
        private const val MAX_APK = 200L * 1024 * 1024
        const val ACTION_STATUS = "io.github.ikriu.pimusic.UPDATE_STATUS"
    }

    private fun dir() = File(ctx.cacheDir, "updates")
    @Volatile private var lastNotify = 0L
    private val notifyId = 0x5055

    /** Ход загрузки APK в шторке/строке состояния (promoted ongoing + ProgressStyle, откат — обычный прогресс). */
    private fun notifyProgress(info: UpdateInfo, pct: Int?) {
        runCatching {
            io.github.ikriu.pimusic.player.LiveProgress.ensureChannel(ctx, io.github.ikriu.pimusic.player.LiveProgress.CH_UPDATES, "Обновления приложения")
            val n = io.github.ikriu.pimusic.player.LiveProgress.build(
                ctx, io.github.ikriu.pimusic.player.LiveProgress.CH_UPDATES, "Обновление PiMusic ${info.versionName}",
                if (pct == null) "Загрузка…" else "Загрузка — $pct %", pct, 100, pct?.let { "$it%" },
            )
            androidx.core.app.NotificationManagerCompat.from(ctx).notify(notifyId, n)
        } // нет разрешения на уведомления — без уведомления
    }

    private fun cancelNotify() { runCatching { androidx.core.app.NotificationManagerCompat.from(ctx).cancel(notifyId) } }

    /** При запуске: не чаще раза в сутки. */
    fun checkOnStart() {
        dir().listFiles()?.forEach { it.delete() }
        val now = System.currentTimeMillis()
        val last = prefs.getLong("update_last_check", 0)
        if (last in 1..now && now - last < EVERY_MS) return
        check(manual = false)
    }

    fun check(manual: Boolean) {
        if (job?.isActive == true) return
        job = scope.launch {
            try {
                val info = withContext(Dispatchers.IO) { UpdateManifest.parse(String(fetch(MANIFEST, 64 * 1024), Charsets.UTF_8)) }
                prefs.edit().putLong("update_last_check", System.currentTimeMillis()).apply()
                _state.value = if (info.versionCode > BuildConfig.VERSION_CODE) State.Available(info, manual)
                else if (manual) State.UpToDate(BuildConfig.VERSION_NAME) else State.Idle
            } catch (e: Exception) {
                Log.w("PiMusic", "update check: $e")
                if (manual) _state.value = State.Error("Не удалось проверить обновления: ${e.message}")
            }
        }
    }

    @androidx.annotation.VisibleForTesting
    fun setStateForTest(s: State) { _state.value = s }

    fun dismiss() { _state.value = State.Idle; cancelled = true }

    private fun canInstall() = Build.VERSION.SDK_INT < 26 || ctx.packageManager.canRequestPackageInstalls()

    /** «Обновить»: нет разрешения — экран настроек; иначе загрузка и установка. */
    fun start(info: UpdateInfo) {
        if (!canInstall()) { _state.value = State.NeedPermission(info); return }
        download(info)
    }

    fun openPermissionSettings(info: UpdateInfo): Intent {
        waitingPermission = info
        return Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${ctx.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }

    /** Вернулись из настроек: разрешили — продолжаем. */
    fun onResume() {
        val info = waitingPermission ?: return
        waitingPermission = null
        if (canInstall()) download(info) else _state.value = State.Idle
    }

    private fun download(info: UpdateInfo) {
        cancelled = false
        _state.value = State.Downloading(info, null)
        job = scope.launch {
            val f = File(dir(), "PiMusic-${info.versionCode}.apk")
            try {
                withContext(Dispatchers.IO) { fetchApk(info, f) }
                cancelNotify()
                _state.value = State.Installing
                withContext(Dispatchers.IO) { install(f) }
            } catch (e: Exception) {
                f.delete()
                cancelNotify()
                Log.w("PiMusic", "update download: $e")
                _state.value = if (cancelled) State.Idle else State.Error("Не удалось загрузить обновление: ${e.message}")
            }
        }
    }

    /** GitHub отдаёт файлы выпуска перенаправлением на своё хранилище: идём по нему сами, проверяя каждый адрес. */
    private fun open(url: String): HttpsURLConnection {
        var u = URL(url)
        repeat(MAX_REDIRECTS + 1) {
            if (u.protocol != "https" || ServerUrl.UPDATE_HOSTS.none { it.equals(u.host, ignoreCase = true) }) {
                throw IOException("адрес не на GitHub: ${u.host}")
            }
            val c = u.openConnection() as HttpsURLConnection
            c.instanceFollowRedirects = false
            c.connectTimeout = 10000
            c.readTimeout = 30000
            c.setRequestProperty("User-Agent", "PiMusic-Android/${BuildConfig.VERSION_NAME}")
            when (val code = c.responseCode) {
                200 -> return c
                301, 302, 303, 307, 308 -> {
                    val loc = c.getHeaderField("Location")
                    c.disconnect()
                    u = URL(u, loc ?: throw IOException("HTTP $code без Location"))
                }
                else -> { c.disconnect(); throw IOException("HTTP $code") }
            }
        }
        throw IOException("слишком много перенаправлений")
    }

    private fun fetch(url: String, max: Int): ByteArray {
        val c = open(url)
        try {
            c.inputStream.use { inp ->
                val out = java.io.ByteArrayOutputStream()
                val buf = ByteArray(8192)
                while (true) {
                    val n = inp.read(buf)
                    if (n <= 0) break
                    out.write(buf, 0, n)
                    if (out.size() > max) throw IOException("ответ слишком большой")
                }
                return out.toByteArray()
            }
        } finally { c.disconnect() }
    }

    private fun fetchApk(info: UpdateInfo, f: File) {
        f.parentFile?.mkdirs()
        val c = open(info.url)
        val md = MessageDigest.getInstance("SHA-256")
        try {
            val total = c.contentLengthLong
            if (total > MAX_APK) throw IOException("APK слишком большой")
            c.inputStream.use { inp ->
                f.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    var got = 0L
                    var last = -1
                    while (true) {
                        val n = inp.read(buf)
                        if (n <= 0) break
                        if (cancelled) throw IOException("отменено")
                        out.write(buf, 0, n); md.update(buf, 0, n); got += n
                        if (got > MAX_APK) throw IOException("APK слишком большой")
                        if (total > 0) {
                            val pct = (got * 100 / total).toInt()
                            if (pct != last) { last = pct; _state.value = State.Downloading(info, pct) }
                            // уведомление (Live Update на Android 16) — не чаще раза в секунду
                            val now = System.currentTimeMillis()
                            if (now - lastNotify >= 1000) { lastNotify = now; notifyProgress(info, pct) }
                        }
                    }
                }
            }
        } finally { c.disconnect() }
        val hex = md.digest().joinToString("") { "%02x".format(it) }
        if (hex != info.sha256) throw IOException("контрольная сумма не совпала")
    }

    /** Сессия PackageInstaller: APK из кэша → commit; итог — UpdateReceiver. */
    private fun install(f: File) {
        val pi = ctx.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(ctx.packageName)
        params.setSize(f.length())
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        val id = pi.createSession(params)
        try {
            pi.openSession(id).use { s ->
                FileInputStream(f).use { inp -> s.openWrite("base.apk", 0, f.length()).use { out -> inp.copyTo(out, 64 * 1024); s.fsync(out) } }
                val i = Intent(ctx, UpdateReceiver::class.java).setAction(ACTION_STATUS)
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                s.commit(PendingIntent.getBroadcast(ctx, id, i, flags).intentSender)
            }
        } catch (e: Exception) {
            runCatching { pi.abandonSession(id) }
            throw e
        }
    }

    fun onStatus(status: Int, message: String?) {
        Log.i("PiMusic", "update install status $status $message")
        if (status == PackageInstaller.STATUS_SUCCESS) { _state.value = State.Idle; return }
        _state.value = State.Error(
            when (status) {
                PackageInstaller.STATUS_FAILURE_ABORTED -> "Установка отменена."
                PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                    "Обновление подписано другим ключом или несовместимо. Удалите PiMusic и установите APK заново."
                PackageInstaller.STATUS_FAILURE_STORAGE -> "Не хватает места для установки."
                else -> "Ошибка установки" + (message?.let { ": $it" } ?: ".")
            }
        )
    }
}

/** Ответ сессии PackageInstaller: подтверждение пользователя или итог установки. */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Updater.ACTION_STATUS) return
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @Suppress("DEPRECATION")
            val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
            else intent.getParcelableExtra(Intent.EXTRA_INTENT)
            if (confirm != null) {
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (runCatching { ctx.startActivity(confirm) }.isSuccess) return
            }
        }
        PiApp.get().updater.onStatus(status, intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE))
    }
}
