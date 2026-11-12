package id.serverpenyimpanan.android.data.upload

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.FileInputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.security.MessageDigest

/** Membaca file dari Uri (galeri, file manager, "Bagikan") per bagian tanpa memuat semuanya ke RAM. */
class UriChunkSource(private val resolver: ContentResolver, private val uri: Uri, knownSize: Long? = null) : ChunkSource {
    override val size: Long = knownSize?.takeIf { it >= 0 } ?: querySize(resolver, uri)
        ?: throw IOException("Ukuran file tidak diketahui")

    override fun read(offset: Long, length: Int): ByteArray {
        val pfd = resolver.openFileDescriptor(uri, "r") ?: throw IOException("File tidak bisa dibuka")
        pfd.use {
            FileInputStream(it.fileDescriptor).channel.use { channel ->
                val buffer = ByteBuffer.allocate(length)
                var position = offset
                while (buffer.hasRemaining()) {
                    val read = channel.read(buffer, position)
                    if (read < 0) throw IOException("File berubah/terpotong saat dibaca")
                    position += read
                }
                return buffer.array()
            }
        }
    }

    override fun sha256(): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val input = resolver.openInputStream(uri) ?: throw IOException("File tidak bisa dibuka")
        input.use { stream ->
            val buf = ByteArray(1 shl 20)
            var total = 0L
            while (true) {
                val n = stream.read(buf)
                if (n < 0) break
                digest.update(buf, 0, n)
                total += n
            }
            if (total != size) throw IOException("Ukuran file berubah saat dibaca ($total != $size)")
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    companion object {
        fun querySize(resolver: ContentResolver, uri: Uri): Long? =
            resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
            } ?: resolver.openFileDescriptor(uri, "r")?.use { it.statSize.takeIf { s -> s >= 0 } }

        fun queryName(context: Context, uri: Uri): String =
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) c.getString(0) else null
            } ?: uri.lastPathSegment?.substringAfterLast('/') ?: "file"
    }
}

/** UploadStateStore di SharedPreferences: upload_id bertahan walau aplikasi dimatikan. */
class PrefsUploadStateStore(context: Context) : UploadStateStore {
    private val prefs = context.getSharedPreferences("upload_state", Context.MODE_PRIVATE)
    override fun get(key: String): String? = prefs.getString(key, null)
    override fun put(key: String, uploadId: String) { prefs.edit().putString(key, uploadId).apply() }
    override fun remove(key: String) { prefs.edit().remove(key).apply() }
}
