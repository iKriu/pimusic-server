package io.github.ikriu.pimusic.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CheckCircle
import androidx.compose.material.icons.rounded.CloudDone
import androidx.compose.material.icons.rounded.ErrorOutline
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material.icons.rounded.MusicNote
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.Constraints
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.TextAutoSize
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.ToggleButton
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.sp
import coil3.SingletonImageLoader
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.SuccessResult
import coil3.request.allowHardware
import coil3.toBitmap
import io.github.ikriu.pimusic.core.M3Color
import io.github.ikriu.pimusic.core.ServerUrl
import io.github.ikriu.pimusic.data.Track
import io.github.ikriu.pimusic.ui.theme.LocalExtra
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

fun coverKey(url: String) = ServerUrl.coverKey(url)

@Composable
fun coverRequest(url: String?, sizePx: Int? = null): ImageRequest? {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    val abs = ServerUrl.absolute(st.app.session.server, url) ?: return null
    return remember(abs, sizePx) {
        ImageRequest.Builder(ctx).data(abs).memoryCacheKey(coverKey(abs) + (sizePx?.let { "@$it" } ?: "")).diskCacheKey(coverKey(abs))
            .apply { if (sizePx != null) size(sizePx) }
            .build()
    }
}

/** Обложка с плейсхолдером (нота на тональной поверхности). */
@Composable
fun Cover(url: String?, size: Dp, shape: Shape = MaterialTheme.shapes.medium, modifier: Modifier = Modifier, icon: ImageVector = Icons.Rounded.MusicNote) {
    val req = coverRequest(url)
    Box(modifier.size(size).clip(shape).background(MaterialTheme.colorScheme.surfaceContainerHighest), contentAlignment = Alignment.Center) {
        Icon(icon, null, tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(size * 0.4f))
        if (req != null) AsyncImage(model = req, contentDescription = null, contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
    }
}

/** Цвет из обложки (порт sourceFromImage): обложка 32×32 → M3Color.sourceFromPixels. */
@Composable
fun rememberCoverSource(url: String?): M3Color.Source? {
    val st = LocalAppState.current
    val ctx = LocalContext.current
    val abs = ServerUrl.absolute(st.app.session.server, url)
    val v by produceState<M3Color.Source?>(null, abs) {
        if (abs == null) { value = null; return@produceState }
        value = withContext(Dispatchers.IO) {
            runCatching {
                val req = ImageRequest.Builder(ctx).data(abs).diskCacheKey(coverKey(abs)).memoryCacheKey(coverKey(abs) + "@32")
                    .size(32).allowHardware(false).build()
                val r = SingletonImageLoader.get(ctx).execute(req) as? SuccessResult ?: return@runCatching null
                val bmp = r.image.toBitmap(32, 32)
                val px = IntArray(bmp.width * bmp.height)
                bmp.getPixels(px, 0, bmp.width, 0, 0, bmp.width, bmp.height)
                M3Color.sourceFromPixels(px)
            }.getOrNull()
        }
    }
    return v
}

/** Эквалайзер-столбики на играющем ряду (на паузе — стоят). */
@Composable
fun EqBars(animate: Boolean, color: Color = Color.White, modifier: Modifier = Modifier) {
    val tr = rememberInfiniteTransition(label = "eq")
    val hs = listOf(0, 180, 360).map { d ->
        tr.animateFloat(0.25f, 1f, infiniteRepeatable(tween(420, delayMillis = d), RepeatMode.Reverse), label = "b$d")
    }
    Row(modifier.size(18.dp), horizontalArrangement = Arrangement.spacedBy(2.dp), verticalAlignment = Alignment.Bottom) {
        hs.forEachIndexed { i, h ->
            val f = if (animate) h.value else listOf(0.5f, 0.8f, 0.35f)[i]
            Box(Modifier.width(4.dp).fillMaxHeight(f).clip(RoundedCornerShape(2.dp)).background(color))
        }
    }
}

/**
 * Ряд трека: обложка 56dp (shape medium) или номер, заголовок, «исполнитель · альбом · 3:42», ✓ скачан, ⋮.
 * Играющий — primary-заголовок и столбики поверх обложки (scrim 50 %).
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun TrackRow(
    t: Track,
    playing: Boolean,
    animating: Boolean,
    onClick: () -> Unit,
    onMore: (() -> Unit)?,
    number: Int? = null,
    showAlbum: Boolean = true,
    offline: Boolean = false,
    leading: (@Composable () -> Unit)? = null,
    trailing: (@Composable () -> Unit)? = null,
    selected: Boolean? = null,
) {
    val cs = MaterialTheme.colorScheme
    Row(
        Modifier.fillMaxWidth().heightIn(min = 64.dp)
            .background(if (selected == true) cs.secondaryContainer else Color.Transparent)
            .combinedClickable(onClick = onClick, onLongClick = if (selected != null) null else onMore)
            .semantics { if (selected != null) stateDescription = if (selected) "выбран" else "не выбран" }
            .padding(start = 16.dp, end = 4.dp, top = 6.dp, bottom = 6.dp)
            .semantics(mergeDescendants = true) { if (playing) contentDescription = "Играет: ${t.title}, ${t.artist}" },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (selected != null) {
            androidx.compose.material3.Checkbox(selected, null, Modifier.padding(end = 8.dp))
        }
        leading?.invoke()
        if (number != null) {
            Box(Modifier.width(32.dp), contentAlignment = Alignment.Center) {
                if (playing) EqBars(animating, cs.primary)
                else Text(number.toString(), style = MaterialTheme.typography.bodyMedium, color = cs.onSurfaceVariant)
            }
        } else {
            Box {
                Cover(t.cover, 56.dp)
                if (playing) Box(Modifier.size(56.dp).clip(MaterialTheme.shapes.medium).background(Color.Black.copy(alpha = 0.5f)), contentAlignment = Alignment.Center) {
                    EqBars(animating)
                }
            }
        }
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(
                t.title, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = if (playing) FontWeight.SemiBold else FontWeight.Normal),
                color = if (playing) cs.primary else cs.onSurface,
            )
            val sub = buildList {
                if (t.artist.isNotEmpty()) add(t.artist)
                if (showAlbum && t.album.isNotEmpty()) add(t.album)
                if (t.duration.isNotEmpty()) add(t.duration)
            }.joinToString(" · ")
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (offline) Icon(Icons.Rounded.CloudDone, "Скачан", tint = LocalExtra.current.success, modifier = Modifier.size(14.dp).padding(end = 2.dp))
                Text(
                    if (!t.owned) "Не в медиатеке · $sub" else sub, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodyMedium.copy(fontStyle = if (!t.owned) FontStyle.Italic else FontStyle.Normal),
                    color = cs.onSurfaceVariant,
                )
            }
        }
        if (selected == null) {
            trailing?.invoke()
            if (onMore != null) IconButton(onClick = onMore) { Icon(Icons.Rounded.MoreVert, "Действия") }
        }
    }
}

/** M3E loading indicator по центру. */
@Composable
fun LoadingBox(modifier: Modifier = Modifier.fillMaxSize()) {
    Box(modifier.padding(32.dp), contentAlignment = Alignment.Center) { LoadingIndicator() }
}

/** Пустое состояние: выразительная форма M3E + текст + tonal-кнопка. */
@Composable
fun EmptyState(icon: ImageVector, title: String, text: String? = null, action: String? = null, onAction: (() -> Unit)? = null, cookie: Boolean = true) {
    val cs = MaterialTheme.colorScheme
    Column(Modifier.fillMaxWidth().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Box(
            Modifier.size(112.dp).clip(PolygonShape(if (cookie) MaterialShapes.Cookie4Sided else MaterialShapes.Clover4Leaf)).background(cs.primaryContainer),
            contentAlignment = Alignment.Center,
        ) { Icon(icon, null, tint = cs.onPrimaryContainer, modifier = Modifier.size(48.dp)) }
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center)
        if (text != null) {
            Spacer(Modifier.height(8.dp))
            Text(text, style = MaterialTheme.typography.bodyMedium, color = cs.onSurfaceVariant, textAlign = TextAlign.Center)
        }
        if (action != null && onAction != null) {
            Spacer(Modifier.height(16.dp))
            FilledTonalButton(onClick = onAction) { Text(action) }
        }
    }
}

@Composable
fun ErrorState(text: String, onRetry: (() -> Unit)?) {
    val cs = MaterialTheme.colorScheme
    Column(Modifier.fillMaxWidth().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(96.dp).clip(PolygonShape(MaterialShapes.SoftBurst)).background(cs.errorContainer), contentAlignment = Alignment.Center) {
            Icon(Icons.Rounded.ErrorOutline, null, tint = cs.onErrorContainer, modifier = Modifier.size(40.dp))
        }
        Spacer(Modifier.height(16.dp))
        Text(text, style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center)
        if (onRetry != null) {
            Spacer(Modifier.height(16.dp))
            Button(onClick = onRetry) { Text("Повторить") }
        }
    }
}

/** Экран данных: загрузка/ошибка/контент. */
@Composable
fun <T> Loaded(l: Loader<T>, empty: (T) -> Boolean = { false }, emptyContent: @Composable () -> Unit = {}, content: @Composable (T) -> Unit) {
    val d = l.data
    when {
        d != null && !l.notFound -> if (empty(d)) emptyContent() else content(d)
        l.loading -> LoadingBox()
        else -> ErrorState(l.error ?: "Не удалось загрузить", if (l.notFound) null else ({ l.reload() }))
    }
}

/** Заголовок раздела (title-small, primary). */
@Composable
fun SectionTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary, modifier = modifier.padding(start = 16.dp, end = 16.dp, top = 20.dp, bottom = 8.dp))
}

/**
 * Connected button group M3E (DESIGN.md §6a.2 — вместо segmented buttons): ToggleButton из material3 с формами
 * ButtonGroupDefaults (внешние углы скруглены, внутренние — малый радиус, выбранный морфит в полную пилюлю), на всю
 * ширину с равными весами. Подписи не исчезают: не помещаются в строку с иконкой — иконка над подписью; подпись
 * ужимается (autoSize до 10sp), без иконки — до двух строк.
 */
@Composable
fun ConnectedGroup(
    options: List<Pair<String, String>>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
    icons: Map<String, ImageVector> = emptyMap(),
    @Suppress("UNUSED_PARAMETER") compact: Boolean = false,
    short: Map<String, String> = emptyMap(),
) {
    val gap = ButtonGroupDefaults.ConnectedSpaceBetween
    val base = MaterialTheme.typography.labelLarge
    val measurer = rememberTextMeasurer()
    val density = LocalDensity.current
    BoxWithConstraints(modifier.fillMaxWidth()) {
        val n = options.size.coerceAtLeast(1)
        val per = (maxWidth - gap * (n - 1)) / n
        fun widest(labels: List<String>, st: androidx.compose.ui.text.TextStyle) =
            labels.maxOf { with(density) { measurer.measure(it, st, maxLines = 1, softWrap = false).size.width.toDp() } }
        // помещается ли «иконка + подпись» в одну строку у самого длинного варианта
        val iconPart = if (icons.isEmpty()) 0.dp else 18.dp + 8.dp
        val inline = widest(options.map { it.second }, base) + iconPart + 24.dp <= per
        // иначе — иконка над подписью; не влезает и так — короткие подписи; кегль общий для всех кнопок (до 70 %)
        val full = options.map { it.second }
        val useShort = !inline && short.isNotEmpty() && widest(full, base) + 16.dp > per
        val labels = options.map { (k, l) -> if (useShort) short[k] ?: l else l }
        var k = 1f
        while (!inline && k > 0.65f && widest(labels, base.copy(fontSize = base.fontSize * k)) + 8.dp > per) k -= 0.05f
        val style = if (inline) base else base.copy(fontSize = base.fontSize * k, letterSpacing = base.letterSpacing * k)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(gap)) {
            options.forEachIndexed { i, (key, _) ->
                val label = labels[i]
                val on = key == selected
                ToggleButton(
                    checked = on,
                    onCheckedChange = { onSelect(key) },
                    shapes = when (i) {
                        0 -> ButtonGroupDefaults.connectedLeadingButtonShapes()
                        options.lastIndex -> ButtonGroupDefaults.connectedTrailingButtonShapes()
                        else -> ButtonGroupDefaults.connectedMiddleButtonShapes()
                    },
                    contentPadding = PaddingValues(horizontal = if (inline) 8.dp else 4.dp, vertical = if (inline) 8.dp else 6.dp),
                    // TalkBack читает полную подпись, даже если на кнопке короткая
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).semantics { contentDescription = options[i].second },
                ) {
                    val icon = icons[key]
                    val color = LocalContentColor.current
                    if (inline) {
                        if (icon != null) { Icon(icon, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)) }
                        Text(label, style = style, maxLines = 1)
                    } else {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            if (icon != null) Icon(icon, null, Modifier.size(20.dp))
                            Text(label, style = style, color = color, maxLines = 1, softWrap = false, overflow = TextOverflow.Ellipsis, textAlign = TextAlign.Center)
                        }
                    }
                }
            }
        }
    }
}

/**
 * Подпись, которая не обрезается при крупном системном шрифте: если не помещается в ширину — кегль уменьшается
 * (до [minScale] от исходного), затем — многоточие. Для кнопок, вкладок, плиток.
 */
@Composable
fun FitText(text: String, style: TextStyle, modifier: Modifier = Modifier, color: Color = Color.Unspecified, minScale: Float = 0.7f, textAlign: TextAlign? = null) {
    val measurer = rememberTextMeasurer()
    BoxWithConstraints(modifier) {
        val maxPx = constraints.maxWidth
        val fitted = remember(text, style, maxPx) {
            if (maxPx == Constraints.Infinity) style else {
                var s = style
                var k = 1f
                while (k > minScale && measurer.measure(text, s, maxLines = 1, softWrap = false).size.width > maxPx) {
                    k -= 0.05f
                    s = style.copy(fontSize = style.fontSize * k, letterSpacing = style.letterSpacing * k)
                }
                s
            }
        }
        Text(text, style = fitted, color = color, maxLines = 1, softWrap = false, overflow = TextOverflow.Ellipsis, textAlign = textAlign)
    }
}

/** ✓ «в медиатеке» (success-роль). */
@Composable
fun InLibraryIcon() = Icon(Icons.Rounded.CheckCircle, "В медиатеке", tint = LocalExtra.current.success)

/** Кликабельная строка списка настроек/меню. */
@Composable
fun MenuRow(icon: ImageVector, text: String, supporting: String? = null, color: Color = Color.Unspecified, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().heightIn(min = 56.dp).clickable(onClick = onClick).padding(horizontal = 24.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, tint = if (color != Color.Unspecified) color else MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(20.dp))
        Column(Modifier.weight(1f)) {
            Text(text, style = MaterialTheme.typography.bodyLarge, color = if (color != Color.Unspecified) color else MaterialTheme.colorScheme.onSurface)
            if (supporting != null) Text(supporting, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

/** Максимальная ширина контента на широких экранах. */
fun Modifier.contentWidth() = this.widthIn(max = 840.dp)

/** Мелкий помощник: однократно выполнить при первом показе. */
@Composable
fun OnFirst(block: () -> Unit) {
    var done by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { if (!done) { done = true; block() } }
}

@Composable
fun CircleCover(url: String?, size: Dp) = Cover(url, size, CircleShape)
