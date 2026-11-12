package id.serverpenyimpanan.android.data

import id.serverpenyimpanan.android.BuildConfig
import id.serverpenyimpanan.android.data.api.ApiJson
import id.serverpenyimpanan.android.data.api.ServerApi
import id.serverpenyimpanan.android.data.auth.AuthInterceptor
import id.serverpenyimpanan.android.data.auth.TokenAuthenticator
import id.serverpenyimpanan.android.data.auth.TokenStorage
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Semua klien HTTP untuk satu alamat server.
 *
 * - [okHttp]: request biasa + gambar (Coil) + streaming video (ExoPlayer). Token ditambahkan
 *   otomatis dan diperbarui otomatis saat kedaluwarsa.
 * - [transferApi]: timeout panjang untuk upload/unduh file besar.
 */
class ApiClient(serverUrl: String, private val tokens: TokenStorage) {
    val serverUrl: String = serverUrl.trimEnd('/')
    val baseUrl: String = "${this.serverUrl}/api/v1/"

    private val userAgent = "Penyimpanan-Android/${BuildConfig.VERSION_NAME}"

    private val baseHttp: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .addInterceptor { chain ->
            chain.proceed(chain.request().newBuilder().header("User-Agent", userAgent).build())
        }
        .build()

    val okHttp: OkHttpClient = baseHttp.newBuilder()
        .addInterceptor(AuthInterceptor(tokens))
        .authenticator(TokenAuthenticator(tokens, baseUrl, baseHttp))
        .build()

    private val transferHttp: OkHttpClient = okHttp.newBuilder()
        .readTimeout(10, TimeUnit.MINUTES) // complete file besar di Pi 3 bisa lama (hash ulang dari disk)
        .writeTimeout(5, TimeUnit.MINUTES)
        .build()

    val api: ServerApi = retrofit(okHttp).create(ServerApi::class.java)
    val transferApi: ServerApi = retrofit(transferHttp).create(ServerApi::class.java)

    fun thumbnailUrl(fileId: String) = "${baseUrl}media/$fileId/thumbnail"
    fun streamUrl(fileId: String) = "${baseUrl}media/$fileId/stream"

    private fun retrofit(client: OkHttpClient): Retrofit = Retrofit.Builder()
        .baseUrl(baseUrl)
        .client(client)
        .addConverterFactory(ApiJson.asConverterFactory("application/json".toMediaType()))
        .build()
}
