package id.serverpenyimpanan.android

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.core.content.IntentCompat
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.lifecycleScope
import id.serverpenyimpanan.android.data.upload.UriChunkSource
import id.serverpenyimpanan.android.ui.AppRoot
import id.serverpenyimpanan.android.ui.common.toast
import id.serverpenyimpanan.android.ui.theme.PenyimpananTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        if (savedInstanceState == null) handleShare(intent)
        setContent { PenyimpananTheme { AppRoot() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    /**
     * File yang dibagikan dari aplikasi lain ("Bagikan → Penyimpanan").
     * Izin baca URI hanya berlaku selama activity hidup, jadi file disalin dulu ke cache
     * lalu diunggah di latar belakang ke folder utama; salinan dihapus setelah selesai.
     */
    private fun handleShare(intent: Intent?) {
        val uris: List<Uri> = when (intent?.action) {
            Intent.ACTION_SEND -> listOfNotNull(IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java))
            Intent.ACTION_SEND_MULTIPLE ->
                IntentCompat.getParcelableArrayListExtra(intent, Intent.EXTRA_STREAM, Uri::class.java).orEmpty()
            else -> return
        }
        if (uris.isEmpty()) return
        val container = this.container
        if (container.tokens.current() == null || container.clientOrNull() == null) {
            toast("Login dulu sebelum membagikan file ke Penyimpanan")
            return
        }
        toast(if (uris.size == 1) "Menyiapkan upload…" else "Menyiapkan ${uris.size} file…")
        lifecycleScope.launch {
            var queued = 0
            for (uri in uris) {
                val copy = withContext(Dispatchers.IO) { runCatching { copyToCache(uri) }.getOrNull() } ?: continue
                container.transfers.enqueueUpload(
                    uri = Uri.fromFile(copy.first),
                    name = copy.second,
                    folderId = null,
                    size = copy.first.length(),
                    deleteAfter = true,
                )
                queued++
            }
            toast(if (queued == uris.size) "$queued file masuk antrean upload" else "$queued dari ${uris.size} file bisa diunggah")
        }
    }

    private fun copyToCache(uri: Uri): Pair<File, String> {
        val name = UriChunkSource.queryName(this, uri)
        val dir = File(cacheDir, "share").apply { mkdirs() }
        val target = File.createTempFile("share-", ".bin", dir)
        try {
            contentResolver.openInputStream(uri)!!.use { input -> target.outputStream().use { input.copyTo(it) } }
        } catch (e: Exception) {
            target.delete()
            throw e
        }
        return target to name
    }
}
