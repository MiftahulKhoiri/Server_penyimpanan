package id.serverpenyimpanan.android

import id.serverpenyimpanan.android.data.ApiClient
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.FolderCreate
import id.serverpenyimpanan.android.data.api.LoginRequest
import id.serverpenyimpanan.android.data.api.UploadInitRequest
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.data.auth.InMemoryTokenStore
import id.serverpenyimpanan.android.data.auth.Session
import id.serverpenyimpanan.android.data.upload.ByteArrayChunkSource
import id.serverpenyimpanan.android.data.upload.ChunkedUploader
import id.serverpenyimpanan.android.data.upload.InMemoryUploadStateStore
import id.serverpenyimpanan.android.data.upload.UploadResult
import id.serverpenyimpanan.android.data.upload.sha256Hex
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import kotlin.random.Random

/**
 * Tes ke backend Python asli. Dilewati bila SP_SERVER_URL kosong.
 * Jalankan server dengan android/scripts/test-server.sh lalu:
 *   SP_SERVER_URL=http://127.0.0.1:8099 SP_ADMIN_PASSWORD=... ./gradlew testDebugUnitTest
 */
class ServerIntegrationTest {
    private val serverUrl = System.getenv("SP_SERVER_URL").orEmpty()
    private val password = System.getenv("SP_ADMIN_PASSWORD").orEmpty()
    private lateinit var tokens: InMemoryTokenStore
    private lateinit var client: ApiClient

    @Before
    fun setUp() {
        assumeTrue("SP_SERVER_URL tidak diisi; tes integrasi dilewati", serverUrl.isNotBlank())
        tokens = InMemoryTokenStore()
        client = ApiClient(serverUrl, tokens)
        runBlocking {
            val t = io { client.api.login(LoginRequest("admin", password, "android-test")) }
            tokens.save(Session(t.accessToken, t.refreshToken, t.sessionId, "admin", 0, "admin"))
        }
    }

    private suspend fun <T> io(block: suspend () -> T): T = withContext(Dispatchers.IO) { apiCall { block() } }

    @Test
    fun chunkedUpload_resume_duplicate_andRangeDownload() = runBlocking {
        val api = client.api
        val folder = io { api.createFolder(FolderCreate("tes-android-${System.nanoTime()}")) }
        // 2,5 MiB + sisa => beberapa chunk 256 KiB (ukuran chunk minimum server).
        val data = Random(42).nextBytes(2 * 1024 * 1024 + 512 * 1024 + 123)
        val sha = sha256Hex(data)
        val state = InMemoryUploadStateStore()
        val key = "tes-resume"

        // Simulasikan upload yang terputus: init + 2 chunk pertama, lalu "aplikasi ditutup".
        val chunk = 256 * 1024
        val init = io {
            api.initUpload(UploadInitRequest("acak.bin", data.size.toLong(), chunk, folder.id, sha, "application/octet-stream"))
        }
        for (i in 0 until 2) {
            val part = data.copyOfRange(i * chunk, (i + 1) * chunk)
            io { api.uploadChunk(init.id, i, sha256Hex(part), part.toRequestBody("application/octet-stream".toMediaType())) }
        }
        state.put(key, init.id)

        val uploader = ChunkedUploader(client.transferApi, state, simpleThreshold = 64 * 1024, retryDelayMs = { 10 })
        val progress = mutableListOf<Long>()
        val result = withContext(Dispatchers.IO) {
            uploader.upload(ByteArrayChunkSource(data), "acak.bin", folder.id, key) { sent, _ -> progress += sent }
        }
        assertTrue(result is UploadResult.Uploaded)
        val file = (result as UploadResult.Uploaded).file
        assertEquals(sha, file.sha256)
        assertEquals(data.size.toLong(), file.sizeBytes)
        assertEquals(init.id, io { api.uploadStatus(init.id) }.id) // sesi lama dilanjutkan, bukan dibuat ulang
        assertTrue("progres harus mulai dari chunk yang sudah ada", progress.first() >= 2L * chunk)

        // File yang sama lagi dengan skipIfDuplicate => tidak diunggah ulang.
        val dup = withContext(Dispatchers.IO) {
            uploader.upload(ByteArrayChunkSource(data), "acak-lagi.bin", folder.id, "tes-dup", skipIfDuplicate = true)
        }
        assertTrue("harus terdeteksi duplikat, dapat $dup", dup is UploadResult.Duplicate)

        // Download sebagian dengan Range.
        val resp = withContext(Dispatchers.IO) { api.download(file.id, "bytes=100-199") }
        assertEquals(206, resp.code())
        val bytes = resp.body()!!.bytes()
        assertEquals(data.copyOfRange(100, 200).toList(), bytes.toList())

        // File kecil lewat upload sederhana.
        val small = "halo dari android".toByteArray()
        val r2 = withContext(Dispatchers.IO) { uploader.upload(ByteArrayChunkSource(small), "halo.txt", folder.id, "tes-kecil") }
        assertEquals(sha256Hex(small), (r2 as UploadResult.Uploaded).file.sha256)

        io { api.deleteFolder(folder.id) }
        Unit
    }

    @Test
    fun invalidAccessToken_refreshesOnce_forParallelRequests() = runBlocking {
        val before = tokens.current()!!
        // Rusak access token => server menjawab token_invalid => Authenticator me-refresh.
        tokens.updateTokens("rusak.${before.accessToken}", before.refreshToken, before.sessionId)
        val results = (1..6).map { async(Dispatchers.IO) { apiCall { client.api.me() } } }.awaitAll()
        assertTrue(results.all { it.username == "admin" })
        val after = tokens.current()!!
        assertNotEquals(before.refreshToken, after.refreshToken)
        // Refresh token lama sudah dirotasi: memakainya lagi harus ditolak.
        try {
            io { client.api.refresh(id.serverpenyimpanan.android.data.api.RefreshRequest(before.refreshToken)) }
            throw AssertionError("refresh token lama seharusnya ditolak")
        } catch (e: ApiException) {
            assertTrue(e.status == 401)
        }
        Unit
    }
}
