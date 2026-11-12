package id.serverpenyimpanan.android

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.disk.DiskCache
import coil3.disk.directory
import coil3.memory.MemoryCache
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import id.serverpenyimpanan.android.data.ApiClient
import id.serverpenyimpanan.android.data.auth.EncryptedTokenStore
import id.serverpenyimpanan.android.data.auth.TokenStorage
import id.serverpenyimpanan.android.data.settings.SettingsStore
import id.serverpenyimpanan.android.data.upload.PrefsUploadStateStore
import id.serverpenyimpanan.android.data.upload.UploadStateStore
import id.serverpenyimpanan.android.work.Transfers
import okhttp3.OkHttpClient

/** Dependensi aplikasi (DI manual, cukup untuk aplikasi sebesar ini). */
class AppContainer(context: Context) {
    val settings = SettingsStore(context)
    val tokens: TokenStorage = EncryptedTokenStore(context)
    val uploadState: UploadStateStore = PrefsUploadStateStore(context)
    val transfers = Transfers(context)

    @Volatile
    private var cached: ApiClient? = null

    /** Klien untuk alamat server saat ini (dibuat ulang bila alamat diganti). */
    fun client(): ApiClient {
        val url = settings.serverUrl.value ?: error("Alamat server belum diatur")
        cached?.let { if (it.serverUrl == url) return it }
        synchronized(this) {
            cached?.let { if (it.serverUrl == url) return it }
            return ApiClient(url, tokens).also { cached = it }
        }
    }

    fun clientOrNull(): ApiClient? = settings.serverUrl.value?.let { client() }

    /** Klien HTTP untuk Coil/ExoPlayer; bila server belum diatur, klien kosong tanpa auth. */
    fun okHttp(): OkHttpClient = clientOrNull()?.okHttp ?: emptyHttp

    private val emptyHttp by lazy { OkHttpClient() }
}

class PenyimpananApp : Application(), SingletonImageLoader.Factory {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        createNotificationChannels()
        container.transfers.scheduleAutoBackup(container.settings.backup.value)
    }

    /** Coil memuat thumbnail lewat OkHttp yang sama (token otomatis + cache HTTP/ETag server). */
    override fun newImageLoader(context: PlatformContext): ImageLoader = ImageLoader.Builder(context)
        .components { add(OkHttpNetworkFetcherFactory(callFactory = { container.okHttp() })) }
        .memoryCache { MemoryCache.Builder().maxSizePercent(context, 0.20).build() }
        .diskCache {
            DiskCache.Builder()
                .directory(cacheDir.resolve("image_cache"))
                .maxSizeBytes(256L * 1024 * 1024)
                .build()
        }
        .build()

    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_TRANSFERS, getString(R.string.channel_transfers), NotificationManager.IMPORTANCE_LOW)
                .apply { description = getString(R.string.channel_transfers_desc) },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_BACKUP, getString(R.string.channel_backup), NotificationManager.IMPORTANCE_MIN)
                .apply { description = getString(R.string.channel_backup_desc) },
        )
    }

    companion object {
        const val CHANNEL_TRANSFERS = "transfers"
        const val CHANNEL_BACKUP = "backup"
    }
}

val Context.container: AppContainer get() = (applicationContext as PenyimpananApp).container
