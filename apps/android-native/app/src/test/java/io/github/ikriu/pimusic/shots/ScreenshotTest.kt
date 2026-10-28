package io.github.ikriu.pimusic.shots

import androidx.compose.runtime.remember
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performTextInput
import com.github.takahirom.roborazzi.ExperimentalRoborazziApi
import com.github.takahirom.roborazzi.captureScreenRoboImage
import io.github.ikriu.pimusic.PiApp
import io.github.ikriu.pimusic.core.UpdateInfo
import io.github.ikriu.pimusic.data.LibraryPage
import io.github.ikriu.pimusic.data.PiJson
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.App
import io.github.ikriu.pimusic.ui.AppState
import io.github.ikriu.pimusic.ui.BulkDelete
import io.github.ikriu.pimusic.ui.PlayerView
import io.github.ikriu.pimusic.ui.Screen
import io.github.ikriu.pimusic.ui.Tab
import io.github.ikriu.pimusic.ui.TrackMenu
import io.github.ikriu.pimusic.ui.screens.SortStore
import io.github.ikriu.pimusic.ui.theme.ThemePref
import io.github.ikriu.pimusic.update.Updater
import org.junit.After
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.Description
import org.junit.runner.RunWith
import org.junit.runners.model.Statement
import org.robolectric.ParameterizedRobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

/**
 * Скриншоты всех экранов и состояний (Roborazzi + Robolectric, без эмулятора) с поддельными данными (FakeServer).
 * Конфигурации: 360×800, 800×360, 1280×800 dp × светлая/тёмная × fontScale 1.0/1.3.
 * Запуск: ./gradlew testDebugUnitTest -Pscreenshots=true → app/build/shots/<конфигурация>/<экран>.png
 */
@OptIn(ExperimentalRoborazziApi::class)
@RunWith(ParameterizedRobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [34], application = ShotApp::class)
class ScreenshotTest(private val w: Int, private val h: Int, private val dark: Boolean, private val font: Float) {
    companion object {
        @JvmStatic
        @ParameterizedRobolectricTestRunner.Parameters(name = "{0}x{1}-dark{2}-font{3}")
        fun configs(): List<Array<Any>> = buildList {
            val only = System.getProperty("pimusic.shotcfg").orEmpty() // -Pshotcfg=360x800-light: только эти конфигурации
            for ((w, h) in listOf(360 to 800, 800 to 360, 1280 to 800)) for (dark in listOf(false, true)) for (f in listOf(1.0f, 1.3f)) {
                val name = "${w}x$h-${if (dark) "dark" else "light"}-font$f"
                if (only.isEmpty() || only.split(",").any { name.contains(it) }) add(arrayOf(w, h, dark, f))
            }
        }
    }

    private val cfg get() = "${w}x$h-${if (dark) "dark" else "light"}-font$font"

    /** Размер экрана и шрифт — до запуска Activity правила Compose. */
    @get:Rule(order = 0) val qualifiers = TestRule { base, _: Description ->
        object : Statement() {
            override fun evaluate() {
                assumeTrue("скриншоты: -Pscreenshots=true", System.getProperty("pimusic.screenshots") == "true")
                RuntimeEnvironment.setQualifiers("w${w}dp-h${h}dp-${if (w > h) "land" else "port"}-xhdpi")
                RuntimeEnvironment.setFontScale(font)
                base.evaluate()
            }
        }
    }
    @get:Rule(order = 1) val rule = createComposeRule()

    @After fun shutdown() { (PiApp.get() as? ShotApp)?.shutdown() }

    private val tracks: List<Track> by lazy { PiJson.decodeFromString(LibraryPage.serializer(), FakeServer.route("/api/library", emptyMap())!!).items }

    /** Дать отработать сети (FakeServer в потоках OkHttp), загрузке обложек и анимациям. */
    private fun settle(ms: Long = 2400) {
        var t = 0L
        while (t < ms) {
            Thread.sleep(25)
            rule.mainClock.advanceTimeBy(100)
            t += 100
        }
        rule.waitForIdle()
    }

    private fun shot(name: String, logout: Boolean = false, setup: AppState.() -> Unit = {}, act: () -> Unit = {}) {
        // нативная графика Robolectric должна загрузиться в главном потоке (иначе обложки FakeServer в потоке OkHttp
        // падают в JNI: «Class not found: java/nio/DoubleBuffer»)
        android.graphics.Bitmap.createBitmap(1, 1, android.graphics.Bitmap.Config.ARGB_8888).recycle()
        FakeServer.warmUp()
        rule.mainClock.autoAdvance = false // бесконечные анимации (LoadingIndicator, столбики) не дают «простоя»
        val app = PiApp.get()
        if (logout) app.session.signOut()
        // тема — как выбор пользователя (локальная копия ui.theme; afterLogin перечитывает её)
        app.prefs.themeLocal = PiJson.encodeToString(ThemePref.serializer(), ThemePref(mode = if (dark) "dark" else "light", accent = "dynamic"))
        // как lifecycleScope в MainActivity: главный поток (ExoPlayer требует обращений из него)
        val scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Main.immediate + kotlinx.coroutines.SupervisorJob())
        rule.setContent {
            val st = remember {
                AppState(app, scope).also {
                    it.online = true // в Robolectric нет активной сети — иначе экраны в офлайн-режиме
                    it.setup()
                }
            }
            App(st)
        }
        settle()
        // действие не удалось — снимок всё равно делаем (видно, что не так), тест помечается упавшим после снимка
        val err = runCatching { act() }.exceptionOrNull()
        settle()
        val f = File("build/shots/$cfg/$name.png")
        f.parentFile?.mkdirs()
        captureScreenRoboImage(f.path)
        if (err != null) throw err
    }

    private fun AppState.queue(i: Int = 2) = app.player.setQueuePaused(tracks, i)

    // ---------------------------------------------------------------- вход и медиатека
    @Test fun s01_login() = shot("01_login", logout = true)
    @Test fun s02_library_tracks() = shot("02_library_tracks")
    @Test fun s03_library_genre() = shot("03_library_genre", setup = { SortStore(app).setGenre("tracks", "Rock") })
    @Test fun s04_library_select() = shot("04_library_select", act = {
        rule.onNode(hasContentDescription("Выбрать треки")).performClick(); settle(600)
        rule.onAllNodes(hasText("Группа крови")).onFirst().performClick()
        rule.onAllNodes(hasText("Хочешь?")).onFirst().performClick()
    })
    @Test fun s05_library_sort_sheet() = shot("05_library_sort_sheet", act = { rule.onNode(hasContentDescription("Сортировка", substring = true)).performClick() })
    @Test fun s06_library_artists() = shot("06_library_artists", act = { rule.onNode(hasContentDescription("Исполнители")).performClick() })
    @Test fun s07_library_albums() = shot("07_library_albums", act = { rule.onNode(hasContentDescription("Альбомы")).performClick() })
    @Test fun s08_library_playlists() = shot("08_library_playlists", act = { rule.onNode(hasContentDescription("Плейлисты")).performClick() })
    @Test fun s09_artist() = shot("09_artist", setup = { push(Screen.LibArtist("Кино", "Кино")) })
    @Test fun s10_album() = shot("10_album", setup = { push(Screen.LibAlbum("Кино", "Группа крови", "Группа крови")) })
    @Test fun s11_playlist() = shot("11_playlist", setup = { push(Screen.PlaylistPage(1, "Дорога")) })
    @Test fun s12_playlist_select() = shot("12_playlist_select", setup = { push(Screen.PlaylistPage(1, "Дорога")) }, act = {
        rule.onNode(hasContentDescription("Выбрать треки")).performSemanticsAction(androidx.compose.ui.semantics.SemanticsActions.OnClick); settle(600)
    })
    @Test fun s13_playlist_member() = shot("13_playlist_member", setup = { push(Screen.PlaylistPage(4, "Общий с друзьями")) })

    // ---------------------------------------------------------------- поиск и каталог
    @Test fun s14_search_empty() = shot("14_search_empty", setup = { select(Tab.SEARCH) })
    @Test fun s15_search_results() = shot("15_search_results", setup = { select(Tab.SEARCH) }, act = {
        rule.onNode(hasSetTextAction()).performTextInput("Кино")
        rule.onNode(hasSetTextAction()).performImeAction()
    })
    @Test fun s16_search_link() = shot("16_search_link", setup = { select(Tab.SEARCH) }, act = {
        rule.onNode(hasSetTextAction()).performTextInput("https://open.spotify.com/playlist/abc")
    })
    @Test fun s17_catalog_album() = shot("17_catalog_album", setup = { select(Tab.SEARCH); push(Screen.CatalogAlbum("9", title = "Группа крови")) })
    @Test fun s18_catalog_artist() = shot("18_catalog_artist", setup = { select(Tab.SEARCH); push(Screen.CatalogArtist("123", "Кино")) })

    // ---------------------------------------------------------------- загрузки, волна
    @Test fun s19_downloads() = shot("19_downloads", setup = { select(Tab.DOWNLOADS) })
    @Test fun s20_wave() = shot("20_wave", setup = { select(Tab.WAVE) })

    // ---------------------------------------------------------------- плеер
    @Test fun s21_miniplayer() = shot("21_miniplayer", setup = { queue() })
    @Test fun s22_nowplaying() = shot("22_nowplaying", setup = { queue(); openPlayer() })
    @Test fun s23_nowplaying_lyrics() = shot("23_nowplaying_lyrics", setup = { queue(); openPlayer(PlayerView.LYRICS) })
    @Test fun s24_nowplaying_queue() = shot("24_nowplaying_queue", setup = { queue(); openPlayer(PlayerView.QUEUE) })
    @Test fun s25_nowplaying_eq() = shot("25_nowplaying_eq", setup = { queue(); openPlayer(PlayerView.EQ) })

    // ---------------------------------------------------------------- эквалайзер, настройки, скачанное
    @Test fun s26_eq() = shot("26_eq", setup = { push(Screen.Equalizer) })
    @Test fun s27_settings() = shot("27_settings", setup = { push(Screen.Settings) })
    @Test fun s28_offline() = shot("28_offline", setup = { push(Screen.Offline) })

    // ---------------------------------------------------------------- диалоги и листы
    @Test fun s29_dialog_update() = shot("29_dialog_update", setup = {
        app.updater.setStateForTest(Updater.State.Available(UpdateInfo(10001, "1.0.1", "https://github.com/iKriu/pimusic-server/releases/download/v1.0.1/PiMusic-1.0.1.apk", "a".repeat(64),
            "Жесты плеера, «Играет из…», добавление сразу в несколько плейлистов.", 3_100_000), manual = false))
    })
    @Test fun s30_sheet_track_menu() = shot("30_sheet_track_menu", setup = { menu = TrackMenu(tracks[0]) })
    @Test fun s31_sheet_playlists() = shot("31_sheet_playlists", setup = { addToPlaylist = listOf(tracks[0]) })
    @Test fun s32_dialog_bulk_delete() = shot("32_dialog_bulk_delete", setup = { bulkDelete = BulkDelete(tracks.take(3)) {} })
}
