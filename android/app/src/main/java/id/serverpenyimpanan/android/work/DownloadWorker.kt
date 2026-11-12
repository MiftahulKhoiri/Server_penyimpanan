package id.serverpenyimpanan.android.work

import android.content.ContentValues
import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import id.serverpenyimpanan.android.PenyimpananApp
import id.serverpenyimpanan.android.R
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.parseErrorBody
import id.serverpenyimpanan.android.data.api.toApiException
import java.io.IOException
import java.security.MessageDigest

/** Mengunduh file ke folder Download/Penyimpanan di HP, lalu memverifikasi SHA-256-nya. */
class DownloadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private val notificationId = id.hashCode()
    private val name = inputData.getString(IN_NAME) ?: "file"

    override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo(0, inputData.getLong(IN_SIZE, 0))

    override suspend fun doWork(): Result {
        val app = applicationContext.container
        val fileId = inputData.getString(IN_FILE_ID) ?: return fail("File tidak ada")
        val expectedSha = inputData.getString(IN_SHA256)
        val mime = inputData.getString(IN_MIME) ?: "application/octet-stream"
        runCatching { setForeground(foregroundInfo(0, inputData.getLong(IN_SIZE, 0))) }

        val resolver = applicationContext.contentResolver
        var target: Uri? = null
        return try {
            val response = app.client().transferApi.download(fileId)
            if (!response.isSuccessful) {
                throw parseErrorBody(response.code(), response.errorBody()?.string(), response.headers()["Retry-After"])
            }
            val body = response.body() ?: throw IOException("Respons kosong")
            val total = body.contentLength()
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime)
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Penyimpanan")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            target = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IOException("Tidak bisa membuat file di folder Download")
            val digest = MessageDigest.getInstance("SHA-256")
            var received = 0L
            var lastUpdate = 0L
            body.byteStream().use { input ->
                (resolver.openOutputStream(target) ?: throw IOException("Tidak bisa menulis file")).use { output ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        digest.update(buf, 0, n)
                        received += n
                        val now = System.currentTimeMillis()
                        if (now - lastUpdate > 700) {
                            lastUpdate = now
                            setProgress(workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_SENT to received, Transfers.KEY_TOTAL to total))
                            runCatching { setForeground(foregroundInfo(received, total)) }
                        }
                    }
                }
            }
            val actual = digest.digest().joinToString("") { "%02x".format(it) }
            val serverSha = response.headers()["X-Content-SHA256"] ?: expectedSha
            if (serverSha != null && !serverSha.equals(actual, ignoreCase = true)) {
                throw IOException("Checksum tidak cocok, file rusak saat diunduh")
            }
            resolver.update(target, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            Result.success(
                workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_TOTAL to received, OUT_URI to target.toString()),
            )
        } catch (e: Exception) {
            target?.let { runCatching { resolver.delete(it, null, null) } }
            val error = e.toApiException()
            if ((error.isNetwork || error.status in 500..599 || e is IOException && e !is ApiException) && runAttemptCount < 5 &&
                e.message?.contains("Checksum") != true
            ) {
                Result.retry()
            } else {
                fail(error.message ?: "Unduhan gagal")
            }
        }
    }

    private fun fail(message: String) = Result.failure(workDataOf(Transfers.KEY_NAME to name, Transfers.KEY_ERROR to message))

    private fun foregroundInfo(sent: Long, total: Long): ForegroundInfo {
        val percent = if (total > 0) ((sent * 100) / total).toInt() else 0
        val notification = NotificationCompat.Builder(applicationContext, PenyimpananApp.CHANNEL_TRANSFERS)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(applicationContext.getString(R.string.app_name) + " · Unduh")
            .setContentText(name)
            .setProgress(100, percent, total <= 0)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .build()
        return ForegroundInfo(notificationId, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    }

    companion object {
        const val IN_FILE_ID = "file_id"
        const val IN_NAME = "name"
        const val IN_MIME = "mime"
        const val IN_SIZE = "size"
        const val IN_SHA256 = "sha256"
        const val OUT_URI = "uri"
    }
}
