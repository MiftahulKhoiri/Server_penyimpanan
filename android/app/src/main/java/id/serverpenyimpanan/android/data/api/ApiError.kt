package id.serverpenyimpanan.android.data.api

import kotlinx.coroutines.CancellationException
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import kotlinx.serialization.json.JsonPrimitive
import retrofit2.HttpException
import java.io.IOException

@OptIn(ExperimentalSerializationApi::class)
val ApiJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
    namingStrategy = JsonNamingStrategy.SnakeCase
}

/**
 * Error dari server ({"detail","code"}) atau dari jaringan.
 * [code] dipakai untuk logika aplikasi; [message] untuk ditampilkan.
 */
class ApiException(
    val status: Int,
    val code: String,
    message: String,
    val retryAfterSeconds: Int? = null,
    cause: Throwable? = null,
) : Exception(message, cause) {
    val isNetwork: Boolean get() = status == 0
    val isUnauthorized: Boolean get() = status == 401
    val isNotFound: Boolean get() = status == 404

    companion object {
        const val NETWORK = "network_error"
    }
}

fun parseErrorBody(status: Int, body: String?, retryAfter: String? = null): ApiException {
    val parsed = runCatching { body?.let { ApiJson.decodeFromString(ErrorBody.serializer(), it) } }.getOrNull()
    val detail = when (val d = parsed?.detail) {
        is JsonPrimitive -> d.content
        null -> null
        else -> d.toString()
    }
    val code = parsed?.code ?: "http_$status"
    val message = detail ?: when (status) {
        401 -> "Sesi berakhir, silakan login lagi"
        403 -> "Akses ditolak"
        404 -> "Data tidak ditemukan"
        413 -> "Ukuran terlalu besar"
        503 -> "Server sedang tidak tersedia"
        else -> "Terjadi kesalahan pada server ($status)"
    }
    return ApiException(status, code, message, retryAfter?.toIntOrNull())
}

fun Throwable.toApiException(): ApiException = when (this) {
    is ApiException -> this
    is HttpException -> {
        val response = response()
        parseErrorBody(code(), response?.errorBody()?.string(), response?.headers()?.get("Retry-After"))
    }
    is IOException -> ApiException(0, ApiException.NETWORK, "Tidak bisa terhubung ke server. Periksa jaringan & alamat server.", cause = this)
    is kotlinx.serialization.SerializationException ->
        ApiException(-1, "bad_response", "Respons server tidak dikenali (versi server berbeda?)", cause = this)
    else -> ApiException(-1, "unknown", message ?: "Terjadi kesalahan", cause = this)
}

/** Jalankan panggilan API dan ubah semua error menjadi [ApiException] (pembatalan tetap diteruskan). */
suspend fun <T> apiCall(block: suspend () -> T): T = try {
    block()
} catch (e: CancellationException) {
    throw e
} catch (e: Throwable) {
    throw e.toApiException()
}
