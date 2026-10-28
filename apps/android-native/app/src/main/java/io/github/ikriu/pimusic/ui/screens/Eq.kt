package io.github.ikriu.pimusic.ui.screens

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Error
import androidx.compose.material.icons.rounded.Star
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.ikriu.pimusic.core.Band
import io.github.ikriu.pimusic.core.Eq
import io.github.ikriu.pimusic.core.EqFormat
import io.github.ikriu.pimusic.core.EqState
import io.github.ikriu.pimusic.core.eqResponseDb
import io.github.ikriu.pimusic.ui.ConnectedGroup
import io.github.ikriu.pimusic.ui.LocalAppState
import io.github.ikriu.pimusic.ui.NewPlaylistDialog
import kotlin.math.abs
import kotlin.math.ln
import kotlin.math.pow

/**
 * Поле ручного ввода (DESIGN.md §9.4): Enter/потеря фокуса — применить, вне границ — зажать и пояснить,
 * не число — ошибка. Принимает запятую/точку, «−», «1.5k», «1,5 кГц».
 */
@Composable
private fun ValueField(label: String, value: Double, unit: String, format: (Double) -> String, parse: (String) -> Double?, range: ClosedFloatingPointRange<Double>, onApply: (Double) -> Unit) {
    var text by remember(value) { mutableStateOf(format(value)) }
    var note by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf(false) }
    fun apply() {
        val v = parse(text)
        if (v == null) { error = true; note = "Введите число"; return }
        error = false
        val c = v.coerceIn(range)
        note = if (c != v) "Допустимо от ${format(range.start)} до ${format(range.endInclusive)} $unit — установлено ${format(c)}" else null
        text = format(c)
        onApply(c)
    }
    OutlinedTextField(
        text, { text = it; error = false }, singleLine = true, isError = error,
        suffix = { Text(unit, color = MaterialTheme.colorScheme.onSurfaceVariant) },
        textStyle = MaterialTheme.typography.bodyLarge.copy(textAlign = TextAlign.End),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = androidx.compose.ui.text.input.ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = { apply() }),
        trailingIcon = if (error) ({ Icon(Icons.Rounded.Error, null, tint = MaterialTheme.colorScheme.error) }) else null,
        supportingText = note?.let { { Text(it) } },
        modifier = Modifier.widthIn(min = 104.dp, max = 160.dp).onFocusChanged { if (!it.isFocused && text != format(value)) apply() }
            .semantics { contentDescription = label },
    )
}

/** Строка «подпись + поле» над слайдером. */
@Composable
private fun LabeledSlider(label: String, field: @Composable () -> Unit, slider: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(label, style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            field()
        }
        slider()
    }
}

/**
 * Эквалайзер: режим (Синхронизировать | Только это устройство) → вкл + A/B → кривая АЧХ (точки полос можно тянуть)
 * → чипы полос → частота/усиление/Q (слайдер + ручной ввод), «Пик | Полка» у крайних → преамп → пресеты → лимитер.
 * remote = (id, имя) — удалённая настройка другого устройства (без звука здесь).
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun EqScreen(remote: Pair<String, String>?, embedded: Boolean = false, onBack: () -> Unit) {
    val st = LocalAppState.current
    val eqc = st.app.eqc
    var tick by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) { st.app.settings.changes.collect { (k, _) -> if (k.startsWith("eq.")) tick++ } }
    val devId = remote?.first ?: st.app.session.deviceId
    val view = remember(tick, devId) { eqc.viewFor(devId) }
    var draft by remember { mutableStateOf<EqState?>(null) }
    val eq = draft ?: view.effective
    var sel by remember { mutableIntStateOf(5) }
    var bypass by remember { mutableStateOf(false) }
    var dialog by remember { mutableStateOf<String?>(null) }
    val local = remote == null
    fun preview(s: EqState) { draft = s; if (local) st.app.player.eq.setEq(s) }
    fun commitBand(i: Int, b: Band) { draft = null; eqc.setBand(i, b, devId); tick++ }

    Column(Modifier.fillMaxSize()) {
        if (!embedded) PageBar(if (remote != null) "Эквалайзер · ${remote.second}" else "Эквалайзер")
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            if (remote != null) Surface(color = MaterialTheme.colorScheme.tertiaryContainer, shape = MaterialTheme.shapes.large) {
                Text("Удалённая настройка: ${remote.second}. Звук меняется на том устройстве.", Modifier.padding(16.dp), style = MaterialTheme.typography.bodyMedium)
            }
            ConnectedGroup(listOf("sync" to "Синхронизировать", "local" to "Только это устройство"), view.device.mode, { eqc.setMode(it, devId); tick++ })
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Эквалайзер", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                if (local) FilterChip(bypass, { bypass = !bypass; eqc.setBypass(bypass) }, label = { Text(if (bypass) "B · оригинал" else "A · с EQ") })
                Spacer(Modifier.size(8.dp))
                Switch(view.device.enabled, { eqc.setEnabled(it, devId); tick++ })
            }
            // спектр — только на этом устройстве, пока экран открыт, и не при «уменьшении движения»
            val ctx = androidx.compose.ui.platform.LocalContext.current
            val reducedMotion = remember { android.provider.Settings.Global.getFloat(ctx.contentResolver, android.provider.Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f }
            val showSpectrum = local && view.device.spectrum && !reducedMotion
            var spectrum by remember { mutableStateOf<DoubleArray?>(null) }
            LaunchedEffect(showSpectrum) {
                if (!showSpectrum) { spectrum = null; return@LaunchedEffect }
                val proc = st.app.player.eq
                val sp = io.github.ikriu.pimusic.core.Spectrum(2048)
                val buf = FloatArray(2048)
                val n = 160
                val freqs = DoubleArray(n) { 20.0 * 1000.0.pow(it / (n - 1.0)) }
                proc.analyze = true
                try {
                    while (true) {
                        if (st.app.player.player.isPlaying) {
                            proc.snapshot(buf)
                            sp.update(buf)
                            val sr = proc.outputSampleRate.toDouble()
                            spectrum = DoubleArray(n) { sp.dbAt(freqs[it], sr) }
                        } else if (spectrum != null) spectrum = null
                        kotlinx.coroutines.delay(50)
                    }
                } finally { proc.analyze = false }
            }
            EqCurve(eq, sel, dim = bypass || !view.device.enabled, spectrum = spectrum, onSelect = { sel = it }, onDrag = { i, f, g ->
                preview(eq.copy(bands = eq.bands.mapIndexed { k, b -> if (k == i) b.copy(f = f, g = g) else b }))
            }, onDragEnd = { i -> draft?.let { commitBand(i, it.bands[i]) } })
            // чипы полос
            androidx.compose.foundation.lazy.LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
                items(eq.bands.size) { i ->
                    FilterChip(sel == i, { sel = i }, label = { Text(EqFormat.freqShort(eq.bands[i].f), maxLines = 1, softWrap = false) },
                        modifier = Modifier.semantics { contentDescription = "Полоса ${i + 1}: ${EqFormat.freq(eq.bands[i].f)}" })
                }
            }
            val b = eq.bands[sel]
            Surface(color = MaterialTheme.colorScheme.surfaceContainerHigh, shape = MaterialTheme.shapes.extraLarge) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Полоса ${sel + 1}", style = MaterialTheme.typography.titleMedium)
                    if (sel == 0 || sel == eq.bands.size - 1) {
                        val shelf = if (sel == 0) "lowshelf" else "highshelf"
                        ConnectedGroup(listOf("peaking" to "Пик", shelf to "Полка"), b.type, { commitBand(sel, b.copy(type = it)) })
                    }
                    LabeledSlider("Частота", { ValueField("Частота, Гц", b.f, "Гц", { EqFormat.num(EqFormat.roundFreq(it), if (it < 100) 1 else 0) }, EqFormat::parseFreq, Eq.F_RANGE) { commitBand(sel, b.copy(f = EqFormat.roundFreq(it))) } }) {
                        Slider(EqFormat.toLog(b.f, Eq.F_RANGE).toFloat(), { x -> preview(eq.withBand(sel, b.copy(f = EqFormat.roundFreq(EqFormat.fromLog(x.toDouble(), Eq.F_RANGE))))) },
                            onValueChangeFinished = { draft?.let { commitBand(sel, it.bands[sel]) } })
                    }
                    LabeledSlider("Усиление", { ValueField("Усиление, дБ", b.g, "дБ", { EqFormat.num(it, 1) }, { EqFormat.parseNumber(it, "дб", "db") }, Eq.G_RANGE) { commitBand(sel, b.copy(g = it)) } }) {
                        Slider(b.g.toFloat(), { x -> preview(eq.withBand(sel, b.copy(g = (x * 10).toInt() / 10.0))) }, valueRange = -12f..12f,
                            onValueChangeFinished = { draft?.let { commitBand(sel, it.bands[sel]) } })
                    }
                    if (b.type == "peaking") LabeledSlider("Добротность (Q)", { ValueField("Добротность", b.q, "", { EqFormat.num(it, 2) }, { EqFormat.parseNumber(it, "q") }, Eq.Q_RANGE) { commitBand(sel, b.copy(q = it)) } }) {
                        Slider(EqFormat.toLog(b.q, Eq.Q_RANGE).toFloat(), { x -> preview(eq.withBand(sel, b.copy(q = (EqFormat.fromLog(x.toDouble(), Eq.Q_RANGE) * 100).toInt() / 100.0))) },
                            onValueChangeFinished = { draft?.let { commitBand(sel, it.bands[sel]) } })
                    }
                }
            }
            LabeledSlider("Преамп", { ValueField("Преамп, дБ", eq.preamp, "дБ", { EqFormat.num(it, 1) }, { EqFormat.parseNumber(it, "дб", "db") }, Eq.PREAMP_RANGE) { draft = null; eqc.setPreamp(it, devId); tick++ } }) {
                Slider(eq.preamp.toFloat(), { x -> preview(eq.copy(preamp = (x * 10).toInt() / 10.0)) }, valueRange = -12f..6f,
                    onValueChangeFinished = { draft?.let { d -> draft = null; eqc.setPreamp(d.preamp, devId); tick++ } })
            }
            Text("Пресеты", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                view.presets.forEach { p ->
                    FilterChip(view.active == p.id, { eqc.applyPreset(p.id, devId); tick++ }, label = { Text(p.name) },
                        leadingIcon = if (!p.builtin) ({ Icon(Icons.Rounded.Star, null, Modifier.size(16.dp)) }) else null)
                }
            }
            val custom = view.custom.find { it.id == view.active }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = { dialog = "save" }) { Text("Сохранить как…") }
                if (custom != null) {
                    TextButton(onClick = { dialog = "rename" }) { Text("Переименовать") }
                    TextButton(onClick = { eqc.deletePreset(custom.id); tick++ }) { Text("Удалить") }
                }
                TextButton(onClick = { eqc.applyPreset("flat", devId); tick++ }) { Text("Сбросить") }
            }
            if (local) Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Спектр поверх кривой", style = MaterialTheme.typography.bodyLarge)
                    Text("То, что звучит сейчас; считается, только пока открыт этот экран", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Switch(view.device.spectrum, { eqc.setSpectrum(it, devId); tick++ })
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Лимитер", style = MaterialTheme.typography.bodyLarge)
                    Text("Не даёт звуку клиппировать при подъёме полос", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Switch(view.device.limiter, { eqc.setLimiter(it, devId); tick++ })
            }
            Spacer(Modifier.height(120.dp)) // под мини-плеер и навигацию
        }
    }
    when (dialog) {
        "save" -> NewPlaylistDialog(st, { dialog = null }, title = "Сохранить пресет", action = "Сохранить", showShared = false) { n, _ -> dialog = null; eqc.savePreset(n, devId); tick++ }
        "rename" -> view.custom.find { it.id == view.active }?.let { c ->
            NewPlaylistDialog(st, { dialog = null }, initial = c.name, title = "Переименовать пресет", action = "Сохранить", showShared = false) { n, _ -> dialog = null; eqc.renamePreset(c.id, n); tick++ }
        }
    }
}

private fun EqState.withBand(i: Int, b: Band) = copy(bands = bands.mapIndexed { k, x -> if (k == i) b else x })

/** Кривая АЧХ: сетка 0/±6/±12 дБ, заливка, точки полос (выбранная крупнее, с кольцом); точку можно тянуть. */
@Composable
private fun EqCurve(eq: EqState, sel: Int, dim: Boolean, spectrum: DoubleArray?, onSelect: (Int) -> Unit, onDrag: (Int, Double, Double) -> Unit, onDragEnd: (Int) -> Unit) {
    val cs = MaterialTheme.colorScheme
    val n = 160
    val freqs = remember { DoubleArray(n) { 20.0 * (1000.0).pow(it / (n - 1.0)) } }
    val resp = remember(eq) { eqResponseDb(eq, freqs) }
    val line = if (dim) cs.outline else cs.primary
    var dragging by remember { mutableStateOf<Int?>(null) }
    fun xOf(f: Double, w: Float) = (ln(f / 20.0) / ln(1000.0) * w).toFloat()
    fun fOf(x: Float, w: Float) = 20.0 * 1000.0.pow((x / w).toDouble().coerceIn(0.0, 1.0))
    fun yOf(db: Double, h: Float) = (h / 2 - db / 15.0 * (h / 2)).toFloat()
    fun dbOf(y: Float, h: Float) = ((h / 2 - y) / (h / 2) * 15.0).coerceIn(-12.0, 12.0)
    Canvas(
        Modifier.fillMaxWidth().height(220.dp).clip(RoundedCornerShape(28.dp))
            .semantics { contentDescription = "Кривая эквалайзера; выбранная полоса ${sel + 1}: ${EqFormat.freq(eq.bands[sel].f)}, ${EqFormat.gain(eq.bands[sel].g)}" }
            .pointerInput(eq.bands.size) {
                detectTapGestures { p ->
                    val w = size.width.toFloat()
                    val i = eq.bands.indices.minByOrNull { abs(xOf(eq.bands[it].f, w) - p.x) } ?: return@detectTapGestures
                    onSelect(i)
                }
            }
            .pointerInput(eq) {
                detectDragGestures(
                    onDragStart = { p ->
                        val w = size.width.toFloat(); val h = size.height.toFloat()
                        dragging = eq.bands.indices.minByOrNull { val b = eq.bands[it]; abs(xOf(b.f, w) - p.x) + abs(yOf(b.g, h) - p.y) }
                        dragging?.let(onSelect)
                    },
                    onDragEnd = { dragging?.let(onDragEnd); dragging = null },
                    onDragCancel = { dragging = null },
                ) { change, _ ->
                    val i = dragging ?: return@detectDragGestures
                    val w = size.width.toFloat(); val h = size.height.toFloat()
                    onDrag(i, EqFormat.roundFreq(fOf(change.position.x, w)), (dbOf(change.position.y, h) * 10).toInt() / 10.0)
                }
            },
    ) {
        val w = size.width; val h = size.height
        drawRect(cs.surfaceContainerHigh)
        // спектр (tertiary, полупрозрачный): −100…0 дБ на всю высоту
        spectrum?.let { sp ->
            val p = Path()
            p.moveTo(0f, h)
            sp.forEachIndexed { i, db -> p.lineTo(i / (sp.size - 1f) * w, (h * (1 - (db + 100) / 100)).toFloat()) }
            p.lineTo(w, h); p.close()
            drawPath(p, cs.tertiary.copy(alpha = 0.22f))
        }
        for (db in listOf(-12.0, -6.0, 0.0, 6.0, 12.0)) drawLine(cs.outlineVariant.copy(alpha = if (db == 0.0) 0.9f else 0.4f), Offset(0f, yOf(db, h)), Offset(w, yOf(db, h)), 1.dp.toPx())
        val path = Path()
        resp.forEachIndexed { i, db -> val x = i / (n - 1f) * w; val y = yOf(db.coerceIn(-15.0, 15.0), h); if (i == 0) path.moveTo(x, y) else path.lineTo(x, y) }
        val fill = Path().apply { addPath(path); lineTo(w, h / 2); lineTo(0f, h / 2); close() }
        drawPath(fill, line.copy(alpha = 0.18f))
        drawPath(path, line, style = Stroke(3.dp.toPx()))
        eq.bands.forEachIndexed { i, b ->
            val c = Offset(xOf(b.f, w), yOf(b.g, h))
            if (i == sel) drawCircle(line.copy(alpha = 0.3f), 14.dp.toPx(), c)
            drawCircle(if (i == sel) line else cs.secondary, (if (i == sel) 8 else 5).dp.toPx(), c)
        }
    }
}
