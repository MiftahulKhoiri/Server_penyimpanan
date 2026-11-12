package id.serverpenyimpanan.android.work

import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import id.serverpenyimpanan.android.PenyimpananApp
import id.serverpenyimpanan.android.R
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.upload.ChunkedUploader
import id.serverpenyimpanan.android.data.upload.UploadResult
import id.serverpenyimpanan.android.data.upload.UriChunkSource
import java.io.File
import java.io.IOException

/**
 * Upload satu file di latar belakang (foreground service, notifikasi progres).
 * Bila koneksi putus, WorkManager mengulang worker ini dan upload dilanjutkan dari chunk
 * terakhir yang diterima server (upload_id disimpan di UploadStateStore).
 */
class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private val notificationId = id.hashCode()
    private val name = inputData.getString(IN_NAME) ?: "file"

    override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo(0, 0)

    override suspend fun doWork(): Result {
        val app = applicationContext.container
        if (app.tokens.current() == null) return fail("Belum login")
        val uri = Uri.parse(inputData.getString(IN_URI) ?: return fail("File tidak ada"))
        val folderId = inputData.getString(IN_FOLDER)
        val knownSize = inputData.getLong(IN_SIZE, -1L).takeIf { it >= 0 }
        val skipDuplicate = inputData.getBoolean(IN_SKIP_DUPLICATE, false)

        runCatching { setForeground(foregroundInfo(0, knownSize ?: 0)) }
        return try {
            val source = UriChunkSource(applicationContext.contentResolver, uri, knownSize)
            val client = app.client()
            val uploader = ChunkedUploader(client.transferApi, app.uploadState)
            // Kunci resume: file + ukuran + folder tujuan (bertahan walau worker diulang).
            val key = "${uri}|${source.size}|${folderId.orEmpty()}"
            var lastUpdate = 0L
            val result = uploader.upload(source, name, folderId, key, skipDuplicate) { sent, total ->
                val now = System.currentTimeMillis()
                if (now - lastUpdate > 700 || sent == total) {
                    lastUpdate = now
                    setProgressAsync(progress(sent, total))
                    runCatching { setForegroundAsync(foregroundInfo(sent, total)) }
                }
            }
            cleanup(uri)
            when (result) {
                is UploadResult.Uploaded -> Result.success(
                    workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_TOTAL to source.size, Transfers.KEY_FILE_ID to result.file.id),
                )
                is UploadResult.Duplicate -> Result.success(
                    workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_TOTAL to source.size, Transfers.KEY_FILE_ID to result.existingFileId,
                        Transfers.KEY_ERROR to "Sudah ada di server (dilewati)"),
                )
            }
        } catch (e: ApiException) {
            when {
                // Gangguan sementara: coba lagi nanti, upload dilanjutkan dari chunk terakhir.
                (e.isNetwork || e.status in 500..599) && runAttemptCount < MAX_ATTEMPTS -> Result.retry()
                else -> { cleanup(uri); fail(e.message ?: e.code) }
            }
        } catch (e: SecurityException) {
            fail("Izin membaca file hilang; pilih ulang file")
        } catch (e: IOException) {
            if (runAttemptCount < MAX_ATTEMPTS) Result.retry() else fail(e.message ?: "Gagal membaca file")
        }
    }

    /** File salinan sementara (dari "Bagikan ke Penyimpanan") dihapus setelah selesai. */
    private fun cleanup(uri: Uri) {
        if (inputData.getBoolean(IN_DELETE_AFTER, false) && uri.scheme == "file") {
            uri.path?.let { File(it).delete() }
        }
    }

    private fun progress(sent: Long, total: Long) = workDataOf(
        Transfers.KEY_NAME to name, Transfers.KEY_SENT to sent, Transfers.KEY_TOTAL to total,
    )

    private fun fail(message: String) = Result.failure(
        workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_ERROR to message),
    )

    private fun foregroundInfo(sent: Long, total: Long): ForegroundInfo {
        val percent = if (total > 0) ((sent * 100) / total).toInt() else 0
        val notification = NotificationCompat.Builder(applicationContext, PenyimpananApp.CHANNEL_TRANSFERS)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle(applicationContext.getString(R.string.app_name) + " · Upload")
            .setContentText(name)
            .setProgress(100, percent, total <= 0)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .build()
        return ForegroundInfo(notificationId, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    }

    companion object {
        const val IN_URI = "uri"
        const val IN_NAME = "name"
        const val IN_FOLDER = "folder_id"
        const val IN_SIZE = "size"
        const val IN_DELETE_AFTER = "delete_after"
        const val IN_SKIP_DUPLICATE = "skip_duplicate"
        private const val MAX_ATTEMPTS = 20
    }
}
