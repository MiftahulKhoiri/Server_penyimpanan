package id.serverpenyimpanan.android.ui.common

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.abs

private val ID = Locale("id", "ID")
private val DATE_TIME = DateTimeFormatter.ofPattern("d MMM yyyy, HH:mm", ID).withZone(ZoneId.systemDefault())
private val DATE = DateTimeFormatter.ofPattern("d MMMM yyyy", ID).withZone(ZoneId.systemDefault())

fun formatBytes(bytes: Long?): String {
    if (bytes == null) return "-"
    val units = listOf("B", "KB", "MB", "GB", "TB")
    var value = bytes.toDouble()
    var unit = 0
    while (abs(value) >= 1024 && unit < units.lastIndex) {
        value /= 1024
        unit++
    }
    return if (unit == 0) "$bytes B" else String.format(ID, if (value >= 100) "%.0f %s" else "%.1f %s", value, units[unit])
}

fun parseInstant(iso: String?): Instant? = iso?.let { runCatching { Instant.parse(it) }.getOrNull() }

fun formatDateTime(iso: String?): String = parseInstant(iso)?.let { DATE_TIME.format(it) } ?: "-"

fun formatDate(iso: String?): String = parseInstant(iso)?.let { DATE.format(it) } ?: "-"

fun formatDuration(seconds: Double?): String {
    if (seconds == null) return ""
    val total = seconds.toLong()
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

fun formatUptime(seconds: Long): String {
    val d = seconds / 86400
    val h = (seconds % 86400) / 3600
    val m = (seconds % 3600) / 60
    return buildString {
        if (d > 0) append("$d hari ")
        if (h > 0 || d > 0) append("$h jam ")
        append("$m menit")
    }
}
