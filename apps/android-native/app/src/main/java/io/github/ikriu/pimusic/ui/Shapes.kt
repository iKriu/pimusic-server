package io.github.ikriu.pimusic.ui

import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Outline
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import androidx.graphics.shapes.Cubic
import androidx.graphics.shapes.Morph
import androidx.graphics.shapes.RoundedPolygon

/** Путь из кубик (RoundedPolygon/Morph), вписанный в size (сохраняя пропорции, по центру). */
private fun pathOf(cubics: List<Cubic>, size: Size): Path {
    val p = Path()
    if (cubics.isEmpty()) return p
    var minX = Float.MAX_VALUE; var minY = Float.MAX_VALUE; var maxX = -Float.MAX_VALUE; var maxY = -Float.MAX_VALUE
    for (c in cubics) {
        for ((x, y) in listOf(c.anchor0X to c.anchor0Y, c.control0X to c.control0Y, c.control1X to c.control1Y, c.anchor1X to c.anchor1Y)) {
            if (x < minX) minX = x; if (x > maxX) maxX = x
            if (y < minY) minY = y; if (y > maxY) maxY = y
        }
    }
    val w = (maxX - minX).coerceAtLeast(1e-6f)
    val h = (maxY - minY).coerceAtLeast(1e-6f)
    val s = minOf(size.width / w, size.height / h)
    val ox = (size.width - w * s) / 2 - minX * s
    val oy = (size.height - h * s) / 2 - minY * s
    fun X(v: Float) = v * s + ox
    fun Y(v: Float) = v * s + oy
    p.moveTo(X(cubics[0].anchor0X), Y(cubics[0].anchor0Y))
    for (c in cubics) p.cubicTo(X(c.control0X), Y(c.control0Y), X(c.control1X), Y(c.control1Y), X(c.anchor1X), Y(c.anchor1Y))
    p.close()
    return p
}

/** Выразительная форма M3E (MaterialShapes.*) как Shape для clip/background. */
class PolygonShape(private val polygon: RoundedPolygon) : Shape {
    override fun createOutline(size: Size, layoutDirection: LayoutDirection, density: Density) = Outline.Generic(pathOf(polygon.cubics, size))
}

/** Морфинг двух форм (кнопка ▶/❚❚: пауза — cookie9, играет — круг). */
class MorphShape(private val morph: Morph, private val progress: Float) : Shape {
    override fun createOutline(size: Size, layoutDirection: LayoutDirection, density: Density) = Outline.Generic(pathOf(morph.asCubics(progress), size))
}
