package id.serverpenyimpanan.android

import id.serverpenyimpanan.android.data.api.parseErrorBody
import id.serverpenyimpanan.android.data.settings.SettingsStore
import id.serverpenyimpanan.android.data.upload.ByteArrayChunkSource
import id.serverpenyimpanan.android.data.upload.sha256Hex
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class UtilsTest {
    @Test
    fun normalizeServerUrl_variants() {
        val n = SettingsStore::normalizeServerUrl
        assertEquals("http://192.168.1.10:8000", n("192.168.1.10:8000"))
        assertEquals("http://192.168.1.10:8000", n("  http://192.168.1.10:8000/ "))
        assertEquals("http://192.168.1.10:8000", n("192.168.1.10:8000/api/v1"))
        assertEquals("http://192.168.1.10:8000", n("http://192.168.1.10:8000/api/v1/"))
        assertEquals("https://cloud.contoh.id", n("https://cloud.contoh.id:443"))
        assertEquals("http://pi.local", n("pi.local"))
        assertEquals("http://100.64.0.2:8000/sub", n("100.64.0.2:8000/sub/"))
        assertNull(n(""))
        assertNull(n("   "))
        assertNull(n("http://"))
    }

    @Test
    fun parseErrorBody_standardAndFallback() {
        val e = parseErrorBody(409, """{"detail":"Nama sudah dipakai","code":"name_conflict"}""")
        assertEquals(409, e.status)
        assertEquals("name_conflict", e.code)
        assertEquals("Nama sudah dipakai", e.message)

        val validation = parseErrorBody(422, """{"detail":[{"loc":["body","name"],"msg":"x"}],"code":"validation_error"}""")
        assertEquals("validation_error", validation.code)

        val fallback = parseErrorBody(503, "<html>bad gateway</html>")
        assertEquals("http_503", fallback.code)
        assertEquals("Server sedang tidak tersedia", fallback.message)

        val limited = parseErrorBody(429, """{"detail":"Terlalu banyak","code":"too_many_attempts"}""", retryAfter = "120")
        assertEquals(120, limited.retryAfterSeconds)
    }

    @Test
    fun sha256_knownVector() {
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            sha256Hex("abc".toByteArray()),
        )
        val src = ByteArrayChunkSource(ByteArray(10) { it.toByte() })
        assertEquals(10L, src.size)
        assertEquals(listOf<Byte>(3, 4, 5), src.read(3, 3).toList())
    }
}
