package io.github.ikriu.pimusic

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.lifecycleScope
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import io.github.ikriu.pimusic.player.PlaybackService
import io.github.ikriu.pimusic.ui.App
import io.github.ikriu.pimusic.ui.AppState
import kotlinx.coroutines.launch

/**
 * Единственная Activity: Compose-интерфейс (ui/App.kt). Подключает MediaController к PlaybackService, чтобы служба
 * поднялась и держала воспроизведение в фоне (сам плеер — PiApp.player).
 */
class MainActivity : ComponentActivity() {
    private var controller: ListenableFuture<MediaController>? = null
    private lateinit var state: AppState

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        state = AppState(PiApp.get(), lifecycleScope)
        handleIntent(intent)
        setContent { App(state) }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            val p = getSharedPreferences("pimusic", MODE_PRIVATE)
            if (!p.getBoolean("asked_notifications", false)) {
                p.edit().putBoolean("asked_notifications", true).apply()
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
            }
        }
        PiApp.get().updater.checkOnStart()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(i: Intent?) {
        if (i?.getBooleanExtra("open_player", false) == true) state.openPlayer()
    }

    override fun onStart() {
        super.onStart()
        val token = SessionToken(this, ComponentName(this, PlaybackService::class.java))
        controller = MediaController.Builder(this, token).buildAsync()
        lifecycleScope.launch { PiApp.get().settings.sync() }
    }

    override fun onResume() {
        super.onResume()
        PiApp.get().updater.onResume()
    }

    override fun onStop() {
        controller?.let { MediaController.releaseFuture(it) }
        controller = null
        PiApp.get().player.savePosition()
        lifecycleScope.launch { PiApp.get().settings.pushAll() }
        super.onStop()
    }
}
