package id.serverpenyimpanan.android

import id.serverpenyimpanan.android.data.update.AppRelease
import id.serverpenyimpanan.android.data.update.AppUpdater
import id.serverpenyimpanan.android.data.update.UpdateException
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.security.MessageDigest

class AppUpdaterTest {
    private lateinit var server: MockWebServer
    private lateinit var updater: AppUpdater
    private val apk = ByteArray(200_000) { (it % 251).toByte() }
    private val apkSha = MessageDigest.getInstance("SHA-256").digest(apk).joinToString("") { "%02x".format(it) }

    @Before
    fun setUp() {
        server = MockWebServer().apply { start() }
        updater = AppUpdater(OkHttpClient(), server.url("/").toString())
    }

    @After
    fun tearDown() = server.shutdown()

    private fun release(sha: String? = apkSha, size: Long = apk.size.toLong()) = AppRelease(
        build = 9, title = "Aplikasi Android (build #9)", apkUrl = server.url("/dl/Penyimpanan.apk").toString(),
        sizeBytes = size, sha256 = sha, commit = null,
    )

    @Test
    fun parseBuildNumber_fromReleaseTitle() {
        assertEquals(7, AppUpdater.parseBuildNumber("Aplikasi Android (build #7)"))
        assertEquals(123, AppUpdater.parseBuildNumber("Build #123"))
        assertNull(AppUpdater.parseBuildNumber("Aplikasi Android"))
        assertNull(AppUpdater.parseBuildNumber(null))
    }

    @Test
    fun latest_readsReleaseAndDigest() = runTest {
        server.enqueue(
            MockResponse().setBody(
                """{"name":"Aplikasi Android (build #12)","tag_name":"android-latest","target_commitish":"abc",
                   "assets":[{"name":"Penyimpanan.apk","size":5407675,
                   "digest":"sha256:2E19573C1EBEA7510CB1A8925B7D85F0F1E3866435DED77EBACEB6432A67F378",
                   "browser_download_url":"https://example.com/Penyimpanan.apk","extra":1}]}""",
            ),
        )
        val r = updater.latest()
        assertEquals(12, r.build)
        assertEquals(5407675L, r.sizeBytes)
        assertEquals("2e19573c1ebea7510cb1a8925b7d85f0f1e3866435ded77ebaceb6432a67f378", r.sha256)
        assertEquals("https://example.com/Penyimpanan.apk", r.apkUrl)
        assertEquals("/repos/${AppUpdater.REPO}/releases/tags/${AppUpdater.TAG}", server.takeRequest().path)
    }

    @Test
    fun latest_withoutApk_or_404_fails() = runTest {
        server.enqueue(MockResponse().setBody("""{"name":"Aplikasi Android (build #3)","tag_name":"android-latest","assets":[]}"""))
        server.enqueue(MockResponse().setResponseCode(404))
        repeat(2) {
            try {
                updater.latest()
                fail("harus gagal")
            } catch (_: UpdateException) {
            }
        }
    }

    @Test
    fun download_verifiesChecksum() = runTest {
        server.enqueue(MockResponse().setBody(Buffer().write(apk)))
        val dest = File.createTempFile("upd", ".apk").apply { delete() }
        var last = 0L
        updater.download(release(), dest) { done, _ -> last = done }
        assertTrue(dest.exists())
        assertEquals(apk.size.toLong(), dest.length())
        assertEquals(apk.size.toLong(), last)
        dest.delete()
    }

    @Test
    fun download_badChecksum_deletesFile() = runTest {
        server.enqueue(MockResponse().setBody(Buffer().write(apk)))
        val dest = File.createTempFile("upd", ".apk").apply { delete() }
        try {
            updater.download(release(sha = "0".repeat(64)), dest) { _, _ -> }
            fail("harus gagal")
        } catch (e: UpdateException) {
            assertTrue(e.message!!.contains("SHA-256"))
        }
        assertFalse(dest.exists())
    }

    @Test
    fun download_wrongSize_deletesFile() = runTest {
        server.enqueue(MockResponse().setBody(Buffer().write(apk)))
        val dest = File.createTempFile("upd", ".apk").apply { delete() }
        try {
            updater.download(release(size = apk.size + 10L), dest) { _, _ -> }
            fail("harus gagal")
        } catch (e: UpdateException) {
            assertTrue(e.message!!.contains("Ukuran"))
        }
        assertFalse(dest.exists())
    }
}
