package id.serverpenyimpanan.android.data.auth

import id.serverpenyimpanan.android.data.api.ApiJson
import id.serverpenyimpanan.android.data.api.ErrorBody
import id.serverpenyimpanan.android.data.api.RefreshRequest
import id.serverpenyimpanan.android.data.api.TokenResponse
import okhttp3.Authenticator
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.Route
import java.io.IOException

private const val AUTH_HEADER = "Authorization"
private val JSON = "application/json".toMediaType()

/** Menambahkan "Authorization: Bearer <token>" ke setiap request (kecuali login/refresh). */
class AuthInterceptor(private val tokens: TokenStorage) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val path = request.url.encodedPath
        val token = tokens.current()?.accessToken
        if (token == null || request.header(AUTH_HEADER) != null ||
            path.endsWith("/auth/login") || path.endsWith("/auth/refresh")
        ) {
            return chain.proceed(request)
        }
        return chain.proceed(request.newBuilder().header(AUTH_HEADER, "Bearer $token").build())
    }
}

/**
 * Memperbarui access token otomatis saat server menjawab 401 token_expired/token_invalid.
 *
 * Refresh token server DIROTASI dan pemakaian ulang token lama membuat seluruh sesi dicabut,
 * jadi refresh WAJIB serial: bila beberapa request menerima 401 bersamaan, hanya satu yang
 * melakukan refresh; yang lain memakai token baru hasil refresh itu (lihat docs/API.md bagian 2).
 */
class TokenAuthenticator(
    private val tokens: TokenStorage,
    private val baseUrl: String,
    private val refreshClient: OkHttpClient,
) : Authenticator {
    private val lock = Any()

    override fun authenticate(route: Route?, response: Response): Request? {
        if (responseCount(response) >= 3) return null
        val code = errorCode(response)
        when (code) {
            "token_expired", "token_invalid", "not_authenticated" -> Unit
            "session_revoked", "account_inactive" -> {
                tokens.clear(code)
                return null
            }
            else -> return null
        }
        val sentToken = response.request.header(AUTH_HEADER)?.removePrefix("Bearer ")
        synchronized(lock) {
            val current = tokens.current() ?: return null
            if (sentToken != current.accessToken) {
                // Thread lain sudah me-refresh selama kita menunggu: pakai token barunya.
                return response.request.withToken(current.accessToken)
            }
            val fresh = try {
                refresh(current.refreshToken)
            } catch (e: IOException) {
                return null // jaringan putus: jangan logout, biarkan request gagal & dicoba lagi
            }
            if (fresh == null) {
                tokens.clear("session_expired")
                return null
            }
            tokens.updateTokens(fresh.accessToken, fresh.refreshToken, fresh.sessionId)
            return response.request.withToken(fresh.accessToken)
        }
    }

    /** Return null bila server menolak refresh token (sesi berakhir). */
    private fun refresh(refreshToken: String): TokenResponse? {
        val body = ApiJson.encodeToString(RefreshRequest.serializer(), RefreshRequest(refreshToken)).toRequestBody(JSON)
        val request = Request.Builder().url(baseUrl + "auth/refresh").post(body).build()
        refreshClient.newCall(request).execute().use { resp ->
            if (resp.isSuccessful) {
                val text = resp.body?.string() ?: return null
                return ApiJson.decodeFromString(TokenResponse.serializer(), text)
            }
            if (resp.code in 500..599) throw IOException("Server error ${resp.code} saat refresh")
            return null
        }
    }

    private fun Request.withToken(token: String): Request =
        newBuilder().header(AUTH_HEADER, "Bearer $token").build()

    private fun responseCount(response: Response): Int {
        var count = 1
        var prior = response.priorResponse
        while (prior != null) {
            count++
            prior = prior.priorResponse
        }
        return count
    }

    private fun errorCode(response: Response): String? = runCatching {
        ApiJson.decodeFromString(ErrorBody.serializer(), response.peekBody(4096).string()).code
    }.getOrNull()
}
