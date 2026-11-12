package id.serverpenyimpanan.android.work

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.NetworkType
import androidx.work.WorkerParameters
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.FolderCreate
import id.serverpenyimpanan.android.data.api.apiCall

/**
 * Mencari foto/video baru di galeri HP lalu mengantrekan upload ke folder backup di server.
 * File yang isinya sudah ada di server dilewati (deteksi duplikat SHA-256 milik server).
 */
class AutoBackupWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext.container
        val settings = app.settings.backup.value
        if (!settings.enabled || app.tokens.current() == null) return Result.success()
        if (!hasMediaPermission(applicationContext, settings.includeVideos)) return Result.success()

        val folderId = try {
            ensureBackupFolder()
        } catch (e: Exception) {
            return Result.retry()
        }
        val watermark = app.settings.backupWatermark
        val media = buildList {
            addAll(query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, watermark))
            if (settings.includeVideos) addAll(query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, watermark))
        }.sortedBy { it.dateAdded }

        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (settings.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(settings.chargingOnly)
            .build()
        for (item in media) {
            app.transfers.enqueueUpload(
                uri = item.uri, name = item.name, folderId = folderId, size = item.size,
                constraints = constraints, uniqueName = "backup-${item.uri}", skipIfDuplicate = true,
                kind = Transfers.KIND_BACKUP,
            )
        }
        media.maxOfOrNull { it.dateAdded }?.let { app.settings.backupWatermark = it }
        return Result.success()
    }

    /** Pakai folder backup tersimpan; bila sudah dihapus di server, cari/buat folder baru di root. */
    private suspend fun ensureBackupFolder(): String {
        val app = applicationContext.container
        val api = app.client().api
        val settings = app.settings.backup.value
        settings.folderId?.let { id ->
            if (runCatching { apiCall { api.getFolder(id) } }.isSuccess) return id
        }
        val existing = apiCall { api.listFolders(null) }.firstOrNull { it.name.equals(settings.folderName, ignoreCase = true) }
        val folder = existing ?: apiCall { api.createFolder(FolderCreate(settings.folderName)) }
        app.settings.updateBackup { it.copy(folderId = folder.id) }
        return folder.id
    }

    private data class MediaItem(val uri: Uri, val name: String, val size: Long, val dateAdded: Long)

    private fun query(collection: Uri, after: Long): List<MediaItem> {
        val projection = arrayOf(
            MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.DATE_ADDED,
        )
        val selection = "${MediaStore.MediaColumns.DATE_ADDED} > ? AND ${MediaStore.MediaColumns.SIZE} > 0 AND " +
            "${MediaStore.MediaColumns.IS_PENDING} = 0"
        val result = mutableListOf<MediaItem>()
        applicationContext.contentResolver.query(
            collection, projection, selection, arrayOf(after.toString()), "${MediaStore.MediaColumns.DATE_ADDED} ASC",
        )?.use { c ->
            val idCol = c.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameCol = c.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val sizeCol = c.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
            val dateCol = c.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_ADDED)
            while (c.moveToNext()) {
                result += MediaItem(
                    uri = ContentUris.withAppendedId(collection, c.getLong(idCol)),
                    name = c.getString(nameCol) ?: "media_${c.getLong(idCol)}",
                    size = c.getLong(sizeCol),
                    dateAdded = c.getLong(dateCol),
                )
            }
        }
        return result
    }

    companion object {
        fun requiredPermissions(includeVideos: Boolean): Array<String> =
            if (android.os.Build.VERSION.SDK_INT >= 33) {
                if (includeVideos) arrayOf(Manifest.permission.READ_MEDIA_IMAGES, Manifest.permission.READ_MEDIA_VIDEO)
                else arrayOf(Manifest.permission.READ_MEDIA_IMAGES)
            } else {
                arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)
            }

        fun hasMediaPermission(context: Context, includeVideos: Boolean): Boolean =
            requiredPermissions(includeVideos).all {
                ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
            }
    }
}
