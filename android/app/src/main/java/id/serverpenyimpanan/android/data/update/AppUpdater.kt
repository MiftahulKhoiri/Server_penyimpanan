package id.serverpenyimpanan.android.data.update

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.coroutines.coroutineContext

class UpdateException(message: String) : Exception(message)

/** Rilis APK terbaru di GitHub Releases. [build] = GITHUB_RUN_NUMBER = versionCode APK. */
data class AppRelease(
    val build: Int,
    val title: String,
    val apkUrl: String,
    val sizeBytes: Long,
    val sha256: String?,
    val commit: String?,
)

/**
 * Pembaruan aplikasi dari dalam aplikasi:
 *  1. baca rilis `android-latest` dari GitHub Releases (diterbitkan CI di setiap push ke main)
 *  2. unduh APK, periksa ukuran & SHA-256 (kolom `digest` GitHub)
 *  3. pasang lewat [ApkInstaller]; Android menampilkan konfirmasi "Perbarui".
 *
 * Keaslian dijamin Android sendiri: pembaruan hanya diterima bila APK ditandatangani
 * kunci rilis yang sama dengan aplikasi terpasang.
 */
class AppUpdater(
    private val http: OkHttpClient = defaultHttp,
    private val apiBase: String = "https://api.github.com",
    private val userAgent: String = "Penyimpanan-Android",
) {
    private val json = Json { ignoreUnknownKeys = true }

    @Serializable
    private data class GhAsset(
        val name: String,
        val size: Long,
        val digest: String? = null,
        @SerialName("browser_download_url") val url: String,
    )

    @Serializable
    private data class GhRelease(
        val name: String? = null,
        @SerialName("tag_name") val tag: String,
        @SerialName("target_commitish") val commit: String? = null,
        val assets: List<GhAsset> = emptyList(),
    )

    suspend fun latest(): AppRelease = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("${apiBase.trimEnd('/')}/repos/$REPO/releases/tags/$TAG")
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", userAgent)
            .build()
        val body = try {
            http.newCall(req).execute().use { r ->
                when {
                    r.code == 404 -> throw UpdateException("Belum ada rilis APK di GitHub")
                    r.code == 403 || r.code == 429 -> throw UpdateException("Batas akses GitHub tercapai, coba lagi nanti")
                    !r.isSuccessful -> throw UpdateException("GitHub menjawab kode ${r.code}")
                }
                r.body?.string().orEmpty()
            }
        } catch (e: java.io.IOException) {
            throw UpdateException("Tidak bisa menghubungi GitHub: ${e.message ?: "jaringan bermasalah"}")
        }
        val rel = runCatching { json.decodeFromString<GhRelease>(body) }
            .getOrElse { throw UpdateException("Data rilis GitHub tidak dikenali") }
        val asset = rel.assets.firstOrNull { it.name == ASSET }
            ?: rel.assets.firstOrNull { it.name.endsWith(".apk") }
            ?: throw UpdateException("Rilis belum memuat APK (mungkin sedang dibuat CI)")
        val build = parseBuildNumber(rel.name) ?: throw UpdateException("Nomor build rilis tidak dikenali")
        AppRelease(
            build = build,
            title = rel.name ?: rel.tag,
            apkUrl = asset.url,
            sizeBytes = asset.size,
            sha256 = asset.digest?.takeIf { it.startsWith("sha256:") }?.removePrefix("sha256:")?.lowercase(),
            commit = rel.commit,
        )
    }

    /**
     * Unduh APK ke [dest] sambil melapor kemajuan. Bila ukuran/checksum tidak cocok
     * atau dibatalkan, berkas dihapus dan exception dilempar.
     */
    suspend fun download(release: AppRelease, dest: File, onProgress: (done: Long, total: Long) -> Unit) =
        withContext(Dispatchers.IO) {
            dest.parentFile?.mkdirs()
            dest.delete()
            var ok = false
            try {
                val req = Request.Builder().url(release.apkUrl).header("User-Agent", userAgent).build()
                val sha = MessageDigest.getInstance("SHA-256")
                var done = 0L
                try {
                    http.newCall(req).execute().use { r ->
                        if (!r.isSuccessful) throw UpdateException("Unduhan gagal, kode ${r.code}")
                        val body = r.body ?: throw UpdateException("Unduhan kosong")
                        val total = release.sizeBytes.takeIf { it > 0 } ?: body.contentLength()
                        body.byteStream().use { input ->
                            dest.outputStream().use { out ->
                                val buf = ByteArray(64 * 1024)
                                var lastReport = 0L
                                while (true) {
                                    coroutineContext.ensureActive()
                                    val n = input.read(buf)
                                    if (n < 0) break
                                    out.write(buf, 0, n)
                                    sha.update(buf, 0, n)
                                    done += n
                                    if (done - lastReport >= 128 * 1024) {
                                        lastReport = done
                                        onProgress(done, total)
                                    }
                                }
                                onProgress(done, total)
                            }
                        }
                    }
                } catch (e: java.io.IOException) {
                    throw UpdateException("Unduhan terputus: ${e.message ?: "jaringan bermasalah"}")
                }
                if (release.sizeBytes > 0 && done != release.sizeBytes) {
                    throw UpdateException("Ukuran unduhan tidak cocok ($done dari ${release.sizeBytes} byte)")
                }
                if (done < 1024) throw UpdateException("Berkas unduhan terlalu kecil")
                val hex = sha.digest().joinToString("") { "%02x".format(it) }
                if (release.sha256 != null && hex != release.sha256) {
                    throw UpdateException("Checksum SHA-256 tidak cocok, unduhan rusak")
                }
                ok = true
            } finally {
                if (!ok) dest.delete()
            }
        }

    companion object {
        const val REPO = "MiftahulKhoiri/Server_penyimpanan"
        const val TAG = "android-latest"
        const val ASSET = "Penyimpanan.apk"

        private val defaultHttp: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(60, TimeUnit.SECONDS)
                .build()
        }

        /** "Aplikasi Android (build #7)" → 7. */
        fun parseBuildNumber(title: String?): Int? =
            title?.let { Regex("""build\s*#(\d+)""", RegexOption.IGNORE_CASE).find(it)?.groupValues?.get(1)?.toIntOrNull() }
    }
}
