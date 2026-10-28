package io.github.ikriu.pimusic

import android.app.Application
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Search
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.EmptyState
import io.github.ikriu.pimusic.ui.Selection
import io.github.ikriu.pimusic.ui.SelectionHeader
import io.github.ikriu.pimusic.ui.theme.PiTheme
import io.github.ikriu.pimusic.ui.theme.ThemePref
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Базовые UI-тесты компонентов (Robolectric, без эмулятора). Приложение PiApp не поднимается. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = Application::class)
class ComposeUiTest {
    @get:Rule val rule = createComposeRule()

    @Test fun connectedGroupSelects() {
        var picked = ""
        rule.setContent {
            PiTheme(ThemePref(mode = "dark")) {
                var sel by remember { mutableStateOf("tracks") }
                ConnectedGroup(listOf("tracks" to "Треки", "albums" to "Альбомы"), sel, { sel = it; picked = it })
            }
        }
        rule.onNodeWithContentDescription("Треки").assertIsOn()
        rule.onNodeWithContentDescription("Альбомы").assertIsOff().performClick()
        rule.onNodeWithContentDescription("Альбомы").assertIsOn()
        rule.onNodeWithContentDescription("Треки").assertIsOff()
        assertEquals("albums", picked)
    }

    @Test fun emptyStateAction() {
        var clicked = false
        rule.setContent {
            PiTheme(ThemePref(mode = "light")) {
                EmptyState(Icons.Rounded.Search, "Ничего не нашлось", "Измените запрос.", "К поиску", onAction = { clicked = true })
            }
        }
        rule.onNodeWithText("Ничего не нашлось").assertIsDisplayed()
        rule.onNodeWithText("К поиску").performClick()
        assertTrue(clicked)
    }

    @Test fun selectionHeaderSelectAllAndClose() {
        val sel = Selection()
        val list = listOf(Track(1, title = "A"), Track(2, title = "B"), Track(3, title = "C"))
        sel.start(list[0])
        rule.setContent { PiTheme(ThemePref()) { SelectionHeader(sel, list) } }
        rule.onNodeWithText("Выбрано: 1").assertIsDisplayed()
        rule.onNodeWithContentDescription("Выбрать все").performClick()
        rule.onNodeWithText("Выбрано: 3").assertIsDisplayed()
        assertEquals(list, sel.ordered(list))
        rule.onNodeWithContentDescription("Закончить выбор").performClick()
        rule.waitForIdle()
        assertEquals(false, sel.active)
        assertEquals(0, sel.count)
    }
}
