package id.serverpenyimpanan.android.work

import android.content.Context
import android.net.Uri
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.workDataOf
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.data.settings.BackupSettings
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import java.util.UUID
import java.util.concurrent.TimeUnit

/** Satu baris di layar Transfer. */
data class TransferItem(
    val id: UUID,
    val kind: String,
    val name: String,
    val state: WorkInfo.State,
    val sent: Long,
    val total: Long,
    val error: String?,
) {
    val finished: Boolean get() = state.isFinished
    val progress: Float get() = if (total > 0) (sent.toFloat() / total).coerceIn(0f, 1f) else 0f
}

/** Antrean upload/unduh di WorkManager: tetap berjalan saat aplikasi ditutup & lanjut setelah reboot. */
class Transfers(context: Context) {
    private val work = WorkManager.getInstance(context)

    private val anyNetwork = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    fun enqueueUpload(
        uri: Uri,
        name: String,
        folderId: String?,
        size: Long? = null,
        deleteAfter: Boolean = false,
        constraints: Constraints = anyNetwork,
        uniqueName: String? = null,
        skipIfDuplicate: Boolean = false,
        kind: String = KIND_UPLOAD,
    ) {
        val request = OneTimeWorkRequestBuilder<UploadWorker>()
            .setInputData(
                workDataOf(
                    UploadWorker.IN_URI to uri.toString(),
                    UploadWorker.IN_NAME to name,
                    UploadWorker.IN_FOLDER to folderId,
                    UploadWorker.IN_SIZE to (size ?: -1L),
                    UploadWorker.IN_DELETE_AFTER to deleteAfter,
                    UploadWorker.IN_SKIP_DUPLICATE to skipIfDuplicate,
                    KEY_KIND to kind,
                    KEY_NAME to name,
                ),
            )
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag(TAG_TRANSFER)
            .addTag(kind)
            .build()
        if (uniqueName != null) {
            work.enqueueUniqueWork(uniqueName, ExistingWorkPolicy.KEEP, request)
        } else {
            work.enqueue(request)
        }
    }

    fun enqueueDownload(file: FileOut) {
        val request = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(
                workDataOf(
                    DownloadWorker.IN_FILE_ID to file.id,
                    DownloadWorker.IN_NAME to file.name,
                    DownloadWorker.IN_MIME to file.mimeType,
                    DownloadWorker.IN_SIZE to file.sizeBytes,
                    DownloadWorker.IN_SHA256 to file.sha256,
                    KEY_KIND to KIND_DOWNLOAD,
                    KEY_NAME to file.name,
                ),
            )
            .setConstraints(anyNetwork)
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag(TAG_TRANSFER)
            .addTag(KIND_DOWNLOAD)
            .build()
        work.enqueue(request)
    }

    fun scheduleAutoBackup(settings: BackupSettings) {
        if (!settings.enabled) {
            work.cancelUniqueWork(AUTO_BACKUP)
            return
        }
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (settings.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(settings.chargingOnly)
            .setRequiresBatteryNotLow(true)
            .build()
        val request = PeriodicWorkRequestBuilder<AutoBackupWorker>(1, TimeUnit.HOURS)
            .setConstraints(constraints)
            .build()
        work.enqueueUniquePeriodicWork(AUTO_BACKUP, ExistingPeriodicWorkPolicy.UPDATE, request)
    }

    fun runBackupNow() {
        val request = OneTimeWorkRequestBuilder<AutoBackupWorker>()
            .setConstraints(anyNetwork)
            .build()
        work.enqueueUniqueWork("$AUTO_BACKUP-now", ExistingWorkPolicy.REPLACE, request)
    }

    fun cancel(id: UUID) {
        work.cancelWorkById(id)
    }

    fun clearFinished() {
        work.pruneWork()
    }

    val items: Flow<List<TransferItem>> = work.getWorkInfosByTagFlow(TAG_TRANSFER).map { infos ->
        infos.sortedWith(compareBy<WorkInfo> { it.state.isFinished }.thenByDescending { it.id.toString() })
            .map { info ->
                val progress = info.progress
                val output = info.outputData
                TransferItem(
                    id = info.id,
                    kind = if (info.tags.contains(KIND_DOWNLOAD)) KIND_DOWNLOAD else if (info.tags.contains(KIND_BACKUP)) KIND_BACKUP else KIND_UPLOAD,
                    name = progress.getString(KEY_NAME) ?: output.getString(KEY_NAME) ?: "",
                    state = info.state,
                    sent = progress.getLong(KEY_SENT, if (info.state == WorkInfo.State.SUCCEEDED) output.getLong(KEY_TOTAL, 0) else 0),
                    total = progress.getLong(KEY_TOTAL, output.getLong(KEY_TOTAL, 0)),
                    error = output.getString(KEY_ERROR),
                )
            }
    }

    companion object {
        const val TAG_TRANSFER = "transfer"
        const val KIND_UPLOAD = "upload"
        const val KIND_DOWNLOAD = "download"
        const val KIND_BACKUP = "backup-upload"
        const val AUTO_BACKUP = "auto-backup"
        const val KEY_KIND = "kind"
        const val KEY_NAME = "name"
        const val KEY_SENT = "sent"
        const val KEY_TOTAL = "total"
        const val KEY_ERROR = "error"
        const val KEY_FILE_ID = "file_id"
    }
}
