package io.github.ikriu.pimusic.player

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat
import io.github.ikriu.pimusic.R

/**
 * Уведомления с прогрессом в стиле Live Updates (Android 16): ProgressStyle + «повышенное» ongoing
 * (строка состояния, экран блокировки, OneUI Now Bar). Ограничения Live Updates соблюдены: ongoing, есть заголовок,
 * без colorized и custom view, канал не IMPORTANCE_MIN. Если система не разрешает повышенные уведомления
 * (canPostPromotedNotifications) или Android < 16 — обычное уведомление с прогрессом (тихий откат).
 * Для плеера Live Update не нужен (MediaStyle исключены правилами) — его показывает MediaSession.
 */
object LiveProgress {
    const val CH_DOWNLOADS = "downloads"
    const val CH_UPDATES = "updates"

    fun ensureChannel(ctx: Context, id: String, name: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(id) == null) {
            nm.createNotificationChannel(NotificationChannel(id, name, NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) })
        }
    }

    /** Можно ли просить повышение (Android 16+, пользователь не запретил). */
    fun canPromote(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT < 36) return false
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return false
        return runCatching { nm.canPostPromotedNotifications() }.getOrDefault(false)
    }

    /**
     * @param progress 0…[max] или null — неопределённый; [short] — короткий текст для чипа строки состояния («45%», «3/12»).
     */
    fun build(
        ctx: Context, channel: String, title: String, text: String?, progress: Int?, max: Int, short: String?,
        intent: PendingIntent? = null, promote: Boolean = canPromote(ctx),
    ): Notification {
        val b = NotificationCompat.Builder(ctx, channel)
            .setSmallIcon(R.drawable.ic_stat_pimusic)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setContentIntent(intent)
        if (promote) {
            b.setRequestPromotedOngoing(true)
            if (short != null) b.setShortCriticalText(short)
            val style = NotificationCompat.ProgressStyle()
            if (progress == null) style.setProgressIndeterminate(true)
            else style.setProgressSegments(listOf(NotificationCompat.ProgressStyle.Segment(max.coerceAtLeast(1)))).setProgress(progress.coerceIn(0, max))
            b.setStyle(style)
        } else {
            b.setProgress(max.coerceAtLeast(1), (progress ?: 0).coerceIn(0, max), progress == null)
            if (short != null) b.setSubText(short)
        }
        return b.build()
    }
}
