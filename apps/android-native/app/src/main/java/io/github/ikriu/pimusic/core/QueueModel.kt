package io.github.ikriu.pimusic.core

import kotlin.random.Random

/** Повтор: выкл → весь список → один трек → выкл. */
enum class RepeatMode { OFF, ALL, ONE;
    fun next() = when (this) { OFF -> ALL; ALL -> ONE; ONE -> OFF }
}

/**
 * Чистая логика очереди (без плеера): элементы — уникальные uid (один трек может стоять в очереди дважды).
 * PlayerHub применяет результат к ExoPlayer минимальными операциями.
 */
object QueueModel {
    /** Перемешать: текущий — первым, остальные — случайно. */
    fun <T> shuffled(items: List<T>, current: Int, rnd: Random = Random.Default): List<T> {
        if (items.isEmpty()) return items
        val cur = items.getOrNull(current) ?: return items.shuffled(rnd)
        val rest = items.filterIndexed { i, _ -> i != current }.shuffled(rnd)
        return listOf(cur) + rest
    }

    /**
     * Вернуть исходный порядок: элементы [original], которые ещё в очереди, в их порядке; добавленные во время
     * перемешивания (их нет в original) — в конец, в текущем порядке.
     */
    fun <T> unshuffled(original: List<T>, now: List<T>): List<T> {
        val present = now.toHashSet()
        val kept = original.filter { it in present }
        val keptSet = kept.toHashSet()
        return kept + now.filter { it !in keptSet }
    }

    fun <T> move(items: List<T>, from: Int, to: Int): List<T> {
        if (from !in items.indices) return items
        val m = items.toMutableList()
        val x = m.removeAt(from)
        m.add(to.coerceIn(0, m.size), x)
        return m
    }

    /** «Предыдущий»: дальше 3 с от начала — в начало трека, иначе к предыдущему. */
    fun prevGoesToStart(positionMs: Long): Boolean = positionMs > 3000

    /** Индекс следующего трека с учётом повтора (null — конец очереди). */
    fun nextIndex(current: Int, size: Int, repeat: RepeatMode): Int? = when {
        size == 0 -> null
        repeat == RepeatMode.ONE -> current
        current + 1 < size -> current + 1
        repeat == RepeatMode.ALL -> 0
        else -> null
    }

    /** Индексы предзагрузки: текущий и [ahead] следующих (с учётом повтора всего списка), без повторов. */
    fun prefetchIndices(current: Int, size: Int, repeat: RepeatMode, ahead: Int = 2): List<Int> {
        if (current !in 0 until size) return emptyList()
        val out = linkedSetOf(current)
        var i = current
        repeat(ahead) {
            val n = when {
                i + 1 < size -> i + 1
                repeat != RepeatMode.OFF -> 0
                else -> return out.toList()
            }
            out += n
            i = n
        }
        return out.toList()
    }
}
