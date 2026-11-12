package id.serverpenyimpanan.android.data.upload

import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.data.api.ServerApi
import id.serverpenyimpanan.android.data.api.UploadInitRequest
import id.serverpenyimpanan.android.data.api.UploadOut
import id.serverpenyimpanan.android.data.api.apiCall
import kotlinx.coroutines.delay
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.MessageDigest

/** Sumber data file yang bisa dibaca per bagian (Uri Android, File, atau ByteArray di tes). */
interface ChunkSource {
    val size: Long
    /** Baca tepat [length] byte mulai [offset]. */
    fun read(offset: Long, length: Int): ByteArray
    /** SHA-256 seluruh isi (hex huruf kecil). */
    fun sha256(): String
}

class ByteArrayChunkSource(private val data: ByteArray) : ChunkSource {
    override val size: Long get() = data.size.toLong()
    override fun read(offset: Long, length: Int): ByteArray = data.copyOfRange(offset.toInt(), offset.toInt() + length)
    override fun sha256(): String = sha256Hex(data)
}

/** Menyimpan upload_id per file agar upload bisa dilanjutkan setelah aplikasi/HP restart. */
interface UploadStateStore {
    fun get(key: String): String?
    fun put(key: String, uploadId: String)
    fun remove(key: String)
}

class InMemoryUploadStateStore : UploadStateStore {
    private val map = java.util.concurrent.ConcurrentHashMap<String, String>()
    override fun get(key: String) = map[key]
    override fun put(key: String, uploadId: String) { map[key] = uploadId }
    override fun remove(key: String) { map.remove(key) }
}

sealed interface UploadResult {
    data class Uploaded(val file: FileOut) : UploadResult
    /** File dengan isi identik sudah ada di server (hanya bila skipIfDuplicate). */
    data class Duplicate(val existingFileId: String) : UploadResult
}

fun sha256Hex(bytes: ByteArray): String =
    MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

private val OCTET = "application/octet-stream".toMediaType()

/**
 * Upload file ke Server_penyimpanan.
 *
 * - File kecil (≤ [simpleThreshold]) dikirim sekali lewat POST /files.
 * - File besar lewat /uploads (chunked): bisa dilanjutkan, tiap chunk diverifikasi SHA-256,
 *   chunk dikirim BERURUTAN agar server bisa menghitung checksum total sambil menerima
 *   (complete jadi instan; lihat docs/API.md bagian 4).
 */
class ChunkedUploader(
    private val api: ServerApi,
    private val state: UploadStateStore,
    private val simpleThreshold: Long = 4L * 1024 * 1024,
    private val maxChunkAttempts: Int = 5,
    private val retryDelayMs: (attempt: Int) -> Long = { attempt -> (1000L shl attempt.coerceAtMost(5)) },
) {
    suspend fun upload(
        source: ChunkSource,
        filename: String,
        folderId: String?,
        key: String,
        skipIfDuplicate: Boolean = false,
        onProgress: (sent: Long, total: Long) -> Unit = { _, _ -> },
    ): UploadResult {
        val sha = source.sha256()
        if (source.size <= simpleThreshold && !skipIfDuplicate) {
            val bytes = source.read(0, source.size.toInt())
            val file = withRetry {
                apiCall { api.uploadSimple(filename, folderId, sha, bytes.toRequestBody(OCTET)) }
            }
            onProgress(source.size, source.size)
            return UploadResult.Uploaded(file)
        }
        repeat(3) { restart ->
            val upload = when (val start = resumeOrInit(source, sha, filename, folderId, key)) {
                is Start.AlreadyDone -> {
                    state.remove(key)
                    return UploadResult.Uploaded(start.file)
                }
                is Start.Active -> start.upload
            }
            if (skipIfDuplicate && upload.duplicateFileId != null) {
                runCatching { apiCall { api.cancelUpload(upload.id) } }
                state.remove(key)
                return UploadResult.Duplicate(upload.duplicateFileId)
            }
            try {
                return UploadResult.Uploaded(sendAndComplete(source, upload, onProgress))
            } catch (e: RestartUpload) {
                state.remove(key)
                if (restart == 2) throw e.cause ?: e
            }
        }
        error("tidak tercapai")
    }

    private sealed interface Start {
        data class Active(val upload: UploadOut) : Start
        /** Sesi lama ternyata sudah selesai (mis. aplikasi mati tepat setelah complete). */
        data class AlreadyDone(val file: FileOut) : Start
    }

    /** Lanjutkan sesi upload lama yang cocok, atau mulai sesi baru. */
    private suspend fun resumeOrInit(
        source: ChunkSource, sha: String, filename: String, folderId: String?, key: String,
    ): Start {
        state.get(key)?.let { uploadId ->
            val existing = runCatching { apiCall { api.uploadStatus(uploadId) } }.getOrNull()
            if (existing != null && existing.totalSize == source.size && existing.expectedSha256 == sha) {
                when (existing.status) {
                    "pending", "uploading" -> return Start.Active(existing)
                    "completed" -> existing.fileId?.let { fileId ->
                        return Start.AlreadyDone(apiCall { api.getFile(fileId) })
                    }
                }
            }
            state.remove(key)
        }
        val created = withRetry {
            apiCall {
                api.initUpload(UploadInitRequest(filename = filename, totalSize = source.size, folderId = folderId, sha256 = sha))
            }
        }
        state.put(key, created.id)
        return Start.Active(created)
    }

    private suspend fun sendAndComplete(
        source: ChunkSource, initial: UploadOut, onProgress: (Long, Long) -> Unit,
    ): FileOut {
        var upload = initial
        repeat(3) {
            val received = upload.receivedChunks.toHashSet()
            var sent = upload.receivedBytes
            onProgress(sent, source.size)
            for (index in 0 until upload.totalChunks) {
                if (index in received) continue
                val offset = index.toLong() * upload.chunkSize
                val length = minOf(upload.chunkSize.toLong(), source.size - offset).toInt()
                val bytes = source.read(offset, length)
                val chunkSha = sha256Hex(bytes)
                sendChunk(upload.id, index, bytes, chunkSha)
                sent += length
                onProgress(sent, source.size)
            }
            try {
                val done = withRetry { apiCall { api.completeUpload(upload.id) } }
                return done.file
            } catch (e: ApiException) {
                when (e.code) {
                    "upload_incomplete" -> upload = apiCall { api.uploadStatus(upload.id) } // kirim yang hilang
                    "upload_busy" -> return waitForCompletion(upload.id)
                    "upload_already_completed" -> return waitForCompletion(upload.id)
                    "checksum_mismatch", "upload_corrupted", "upload_not_active", "upload_expired", "upload_not_found" ->
                        throw RestartUpload(e)
                    else -> throw e
                }
            }
        }
        throw ApiException(409, "upload_incomplete", "Upload tidak bisa diselesaikan")
    }

    private suspend fun sendChunk(uploadId: String, index: Int, bytes: ByteArray, sha: String) {
        var attempt = 0
        while (true) {
            try {
                apiCall { api.uploadChunk(uploadId, index, sha, bytes.toRequestBody(OCTET)) }
                return
            } catch (e: ApiException) {
                when (e.code) {
                    // Sesi upload tidak bisa dilanjutkan: mulai upload baru.
                    "upload_not_active", "upload_expired", "upload_not_found" -> throw RestartUpload(e)
                    // Gangguan sementara: kirim ulang chunk ini.
                    ApiException.NETWORK, "chunk_checksum_mismatch", "chunk_incomplete", "client_disconnected",
                    "database_error", "primary_storage_unavailable", "database_unavailable" -> Unit
                    else -> if (e.status !in 500..599) throw e
                }
                attempt++
                if (attempt >= maxChunkAttempts) throw e
                delay(e.retryAfterSeconds?.times(1000L) ?: retryDelayMs(attempt))
            }
        }
    }

    private suspend fun waitForCompletion(uploadId: String): FileOut {
        repeat(120) {
            val status = apiCall { api.uploadStatus(uploadId) }
            if (status.status == "completed" && status.fileId != null) return apiCall { api.getFile(status.fileId) }
            if (status.status == "failed") throw RestartUpload(ApiException(422, status.errorMessage ?: "upload_failed", "Upload gagal di server"))
            delay(2000)
        }
        throw ApiException(0, ApiException.NETWORK, "Server terlalu lama memproses upload")
    }

    /** Ulangi panggilan untuk gangguan jaringan / 5xx sementara. */
    private suspend fun <T> withRetry(block: suspend () -> T): T {
        var attempt = 0
        while (true) {
            try {
                return block()
            } catch (e: ApiException) {
                val transient = e.isNetwork || e.status in 500..599
                attempt++
                if (!transient || attempt >= maxChunkAttempts) throw e
                delay(e.retryAfterSeconds?.times(1000L) ?: retryDelayMs(attempt))
            }
        }
    }

    private class RestartUpload(cause: ApiException) : Exception(cause.message, cause) {
        override val cause: ApiException get() = super.cause as ApiException
    }
}
