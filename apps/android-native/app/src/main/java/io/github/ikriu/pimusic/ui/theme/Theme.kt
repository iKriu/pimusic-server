package io.github.ikriu.pimusic.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.remember
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.M3Color
import io.github.ikriu.pimusic.core.M3Color.Role
import kotlinx.serialization.Serializable

/** Выбор пользователя (как theme.js): mode — system|light|dark, accent — dynamic|amber|coral|… Ключ настроек ui.theme. */
@Serializable
data class ThemePref(val mode: String = "system", val accent: String = "dynamic") {
    fun clean() = ThemePref(if (mode in MODES.map { it.first }) mode else "system", if (ACCENTS.any { it.id == accent }) accent else "dynamic")
}

data class Accent(val id: String, val name: String, val seed: Int)

val MODES = listOf("system" to "Как в системе", "light" to "Светлая", "dark" to "Тёмная")
val ACCENTS = listOf(
    Accent("dynamic", "Из обложки", M3Color.SEED),
    Accent("amber", "Янтарь", M3Color.SEED),
    Accent("coral", "Коралл", 0xFFE2654F.toInt()),
    Accent("rose", "Роза", 0xFFD24D7E.toInt()),
    Accent("violet", "Сирень", 0xFF8A6BD1.toInt()),
    Accent("ocean", "Океан", 0xFF2F7FD0.toInt()),
    Accent("teal", "Бирюза", 0xFF1E9A8F.toInt()),
    Accent("green", "Зелень", 0xFF4E9A3A.toInt()),
)

/** Роли вне ColorScheme Material: success («в медиатеке»). */
@Immutable
data class ExtraColors(val success: Color, val onSuccess: Color, val successContainer: Color, val onSuccessContainer: Color)

val LocalExtra = staticCompositionLocalOf { ExtraColors(Color(0xFF4CAF8E), Color.Black, Color(0xFF1F4D40), Color.White) }
val LocalDark = staticCompositionLocalOf { true }
val LocalPref = staticCompositionLocalOf { ThemePref() }

private fun Map<Role, Int>.c(r: Role) = Color(getValue(r))

fun schemeFrom(roles: Map<Role, Int>, dark: Boolean): ColorScheme {
    val r = roles
    return if (dark) darkColorScheme(
        primary = r.c(Role.Primary), onPrimary = r.c(Role.OnPrimary), primaryContainer = r.c(Role.PrimaryContainer),
        onPrimaryContainer = r.c(Role.OnPrimaryContainer), inversePrimary = r.c(Role.InversePrimary),
        secondary = r.c(Role.Secondary), onSecondary = r.c(Role.OnSecondary), secondaryContainer = r.c(Role.SecondaryContainer),
        onSecondaryContainer = r.c(Role.OnSecondaryContainer),
        tertiary = r.c(Role.Tertiary), onTertiary = r.c(Role.OnTertiary), tertiaryContainer = r.c(Role.TertiaryContainer),
        onTertiaryContainer = r.c(Role.OnTertiaryContainer),
        background = r.c(Role.Background), onBackground = r.c(Role.OnBackground),
        surface = r.c(Role.Surface), onSurface = r.c(Role.OnSurface), surfaceVariant = r.c(Role.SurfaceVariant),
        onSurfaceVariant = r.c(Role.OnSurfaceVariant), surfaceTint = r.c(Role.SurfaceTint),
        inverseSurface = r.c(Role.InverseSurface), inverseOnSurface = r.c(Role.InverseOnSurface),
        error = r.c(Role.Error), onError = r.c(Role.OnError), errorContainer = r.c(Role.ErrorContainer), onErrorContainer = r.c(Role.OnErrorContainer),
        outline = r.c(Role.Outline), outlineVariant = r.c(Role.OutlineVariant), scrim = r.c(Role.Scrim),
        surfaceBright = r.c(Role.SurfaceBright), surfaceDim = r.c(Role.SurfaceDim),
        surfaceContainer = r.c(Role.SurfaceContainer), surfaceContainerHigh = r.c(Role.SurfaceContainerHigh),
        surfaceContainerHighest = r.c(Role.SurfaceContainerHighest), surfaceContainerLow = r.c(Role.SurfaceContainerLow),
        surfaceContainerLowest = r.c(Role.SurfaceContainerLowest),
    ) else lightColorScheme(
        primary = r.c(Role.Primary), onPrimary = r.c(Role.OnPrimary), primaryContainer = r.c(Role.PrimaryContainer),
        onPrimaryContainer = r.c(Role.OnPrimaryContainer), inversePrimary = r.c(Role.InversePrimary),
        secondary = r.c(Role.Secondary), onSecondary = r.c(Role.OnSecondary), secondaryContainer = r.c(Role.SecondaryContainer),
        onSecondaryContainer = r.c(Role.OnSecondaryContainer),
        tertiary = r.c(Role.Tertiary), onTertiary = r.c(Role.OnTertiary), tertiaryContainer = r.c(Role.TertiaryContainer),
        onTertiaryContainer = r.c(Role.OnTertiaryContainer),
        background = r.c(Role.Background), onBackground = r.c(Role.OnBackground),
        surface = r.c(Role.Surface), onSurface = r.c(Role.OnSurface), surfaceVariant = r.c(Role.SurfaceVariant),
        onSurfaceVariant = r.c(Role.OnSurfaceVariant), surfaceTint = r.c(Role.SurfaceTint),
        inverseSurface = r.c(Role.InverseSurface), inverseOnSurface = r.c(Role.InverseOnSurface),
        error = r.c(Role.Error), onError = r.c(Role.OnError), errorContainer = r.c(Role.ErrorContainer), onErrorContainer = r.c(Role.OnErrorContainer),
        outline = r.c(Role.Outline), outlineVariant = r.c(Role.OutlineVariant), scrim = r.c(Role.Scrim),
        surfaceBright = r.c(Role.SurfaceBright), surfaceDim = r.c(Role.SurfaceDim),
        surfaceContainer = r.c(Role.SurfaceContainer), surfaceContainerHigh = r.c(Role.SurfaceContainerHigh),
        surfaceContainerHighest = r.c(Role.SurfaceContainerHighest), surfaceContainerLow = r.c(Role.SurfaceContainerLow),
        surfaceContainerLowest = r.c(Role.SurfaceContainerLowest),
    )
}

fun extraFrom(roles: Map<Role, Int>) = ExtraColors(roles.c(Role.Success), roles.c(Role.OnSuccess), roles.c(Role.SuccessContainer), roles.c(Role.OnSuccessContainer))

fun seedOf(accent: String): M3Color.Source = M3Color.rgbToOklch((ACCENTS.find { it.id == accent } ?: ACCENTS[0]).seed)

/** Формы M3E (shape scale): extra-small 4 … extra-large 28; 32/48 — вручную на крупных объектах. */
val PiShapes = Shapes(
    extraSmall = RoundedCornerShape(4.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(16.dp),
    extraLarge = RoundedCornerShape(28.dp),
)

@Composable
fun isDark(pref: ThemePref): Boolean = when (pref.mode) {
    "light" -> false
    "dark" -> true
    else -> isSystemInDarkTheme()
}

/** Базовая тема приложения: схема из seed/акцента, motion scheme expressive. */
@Composable
fun PiTheme(pref: ThemePref, content: @Composable () -> Unit) {
    val dark = isDark(pref)
    val roles = remember(pref.accent, dark) { M3Color.scheme(seedOf(pref.accent), dark) }
    CompositionLocalProvider(LocalExtra provides extraFrom(roles), LocalDark provides dark, LocalPref provides pref) {
        MaterialExpressiveTheme(
            colorScheme = schemeFrom(roles, dark),
            motionScheme = MotionScheme.expressive(),
            shapes = PiShapes,
            content = content,
        )
    }
}

/**
 * Контент-схема из обложки (альбом, исполнитель, «Сейчас играет», мини-плеер), только с акцентом «Из обложки».
 * src = null (обложка серая/нет) — остаётся базовая схема.
 */
@Composable
fun ContentTheme(src: M3Color.Source?, content: @Composable () -> Unit) {
    val pref = LocalPref.current
    val dark = LocalDark.current
    if (src == null || pref.accent != "dynamic") { content(); return }
    val roles = remember(src, dark) { M3Color.scheme(src, dark, content = true) }
    CompositionLocalProvider(LocalExtra provides extraFrom(roles)) {
        // вложенная expressive-тема наследует формы, типографику и motion scheme внешней
        MaterialExpressiveTheme(colorScheme = schemeFrom(roles, dark), content = content)
    }
}
