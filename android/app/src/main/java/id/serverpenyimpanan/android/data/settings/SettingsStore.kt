package id.serverpenyimpanan.android.data.settings

import android.content.Context
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

data class BackupSettings(
    val enabled: Boolean = false,
    val includeVideos: Boolean = true,
    val wifiOnly: Boolean = true,
    val chargingOnly: Boolean = false,
    val folderId: String? = null,
    val folderName: String = "Backup HP",
)

/** Setelan non-rahasia (alamat server, backup otomatis). Token ada di TokenStore terenkripsi. */
class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    private val serverState = MutableStateFlow(prefs.getString(K_SERVER, null))
    val serverUrl: StateFlow<String?> = serverState.asStateFlow()

    private val backupState = MutableStateFlow(loadBackup())
    val backup: StateFlow<BackupSettings> = backupState.asStateFlow()

    fun setServerUrl(url: String) {
        prefs.edit().putString(K_SERVER, url).apply()
        serverState.value = url
    }

    fun updateBackup(transform: (BackupSettings) -> BackupSettings) {
        val next = transform(backupState.value)
        prefs.edit()
            .putBoolean(K_B_ENABLED, next.enabled)
            .putBoolean(K_B_VIDEOS, next.includeVideos)
            .putBoolean(K_B_WIFI, next.wifiOnly)
            .putBoolean(K_B_CHARGING, next.chargingOnly)
            .putString(K_B_FOLDER_ID, next.folderId)
            .putString(K_B_FOLDER_NAME, next.folderName)
            .apply()
        backupState.value = next
    }

    /** Batas waktu (detik, MediaStore DATE_ADDED) media terakhir yang sudah diantrekan backup. */
    var backupWatermark: Long
        get() = prefs.getLong(K_B_WATERMARK, 0L)
        set(value) = prefs.edit().putLong(K_B_WATERMARK, value).apply()

    var deviceName: String
        get() = prefs.getString(K_DEVICE, null) ?: (android.os.Build.MODEL ?: "Android")
        set(value) = prefs.edit().putString(K_DEVICE, value).apply()

    private fun loadBackup() = BackupSettings(
        enabled = prefs.getBoolean(K_B_ENABLED, false),
        includeVideos = prefs.getBoolean(K_B_VIDEOS, true),
        wifiOnly = prefs.getBoolean(K_B_WIFI, true),
        chargingOnly = prefs.getBoolean(K_B_CHARGING, false),
        folderId = prefs.getString(K_B_FOLDER_ID, null),
        folderName = prefs.getString(K_B_FOLDER_NAME, "Backup HP") ?: "Backup HP",
    )

    companion object {
        private const val K_SERVER = "server_url"
        private const val K_DEVICE = "device_name"
        private const val K_B_ENABLED = "backup_enabled"
        private const val K_B_VIDEOS = "backup_videos"
        private const val K_B_WIFI = "backup_wifi_only"
        private const val K_B_CHARGING = "backup_charging_only"
        private const val K_B_FOLDER_ID = "backup_folder_id"
        private const val K_B_FOLDER_NAME = "backup_folder_name"
        private const val K_B_WATERMARK = "backup_watermark"

        /** "192.168.1.10:8000" -> "http://192.168.1.10:8000"; buang "/" dan "/api/v1" di akhir. */
        fun normalizeServerUrl(input: String): String? {
            var url = input.trim()
            if (url.isEmpty()) return null
            if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
            url = url.trimEnd('/')
            if (url.endsWith("/api/v1")) url = url.removeSuffix("/api/v1")
            val parsed = url.toHttpUrlOrNull() ?: return null
            if (parsed.host.isBlank()) return null
            val port = if (parsed.port != HttpUrl.defaultPort(parsed.scheme)) ":${parsed.port}" else ""
            return "${parsed.scheme}://${parsed.host}$port${parsed.encodedPath.trimEnd('/')}"
        }
    }
}
