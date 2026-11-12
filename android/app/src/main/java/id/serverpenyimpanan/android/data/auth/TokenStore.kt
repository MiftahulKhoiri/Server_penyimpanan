package id.serverpenyimpanan.android.data.auth

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Data sesi login yang tersimpan di perangkat. */
data class Session(
    val accessToken: String,
    val refreshToken: String,
    val sessionId: String,
    val username: String,
    val userId: Int,
    val role: String,
) {
    val isAdmin: Boolean get() = role == "admin"
}

/** Kontrak penyimpanan token, agar logika refresh bisa diuji tanpa Android. */
interface TokenStorage {
    val session: StateFlow<Session?>
    fun current(): Session? = session.value
    fun save(session: Session)
    fun updateTokens(accessToken: String, refreshToken: String, sessionId: String)
    fun clear(reason: String? = null)
    /** Alasan logout terakhir (mis. "session_revoked") untuk ditampilkan di layar login. */
    val lastLogoutReason: String?
}

/** Token disimpan terenkripsi (Android Keystore, AES-256-GCM) lewat EncryptedSharedPreferences. */
class EncryptedTokenStore(context: Context) : TokenStorage {
    private val prefs: SharedPreferences = createPrefs(context)
    private val state = MutableStateFlow(load())
    override val session: StateFlow<Session?> = state.asStateFlow()

    @Volatile
    override var lastLogoutReason: String? = null
        private set

    @Synchronized
    override fun save(session: Session) {
        prefs.edit()
            .putString(K_ACCESS, session.accessToken)
            .putString(K_REFRESH, session.refreshToken)
            .putString(K_SESSION, session.sessionId)
            .putString(K_USERNAME, session.username)
            .putInt(K_USER_ID, session.userId)
            .putString(K_ROLE, session.role)
            .commit()
        lastLogoutReason = null
        state.value = session
    }

    @Synchronized
    override fun updateTokens(accessToken: String, refreshToken: String, sessionId: String) {
        val current = state.value ?: return
        save(current.copy(accessToken = accessToken, refreshToken = refreshToken, sessionId = sessionId))
    }

    @Synchronized
    override fun clear(reason: String?) {
        prefs.edit().clear().commit()
        lastLogoutReason = reason
        state.value = null
    }

    private fun load(): Session? {
        val access = prefs.getString(K_ACCESS, null) ?: return null
        val refresh = prefs.getString(K_REFRESH, null) ?: return null
        return Session(
            accessToken = access,
            refreshToken = refresh,
            sessionId = prefs.getString(K_SESSION, "") ?: "",
            username = prefs.getString(K_USERNAME, "") ?: "",
            userId = prefs.getInt(K_USER_ID, 0),
            role = prefs.getString(K_ROLE, "user") ?: "user",
        )
    }

    private companion object {
        const val FILE = "auth_tokens"
        const val K_ACCESS = "access"
        const val K_REFRESH = "refresh"
        const val K_SESSION = "session_id"
        const val K_USERNAME = "username"
        const val K_USER_ID = "user_id"
        const val K_ROLE = "role"

        fun createPrefs(context: Context): SharedPreferences {
            val key = MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
            return try {
                EncryptedSharedPreferences.create(
                    context, FILE, key,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
                )
            } catch (e: Exception) {
                // Kunci Keystore hilang (mis. setelah restore HP): hapus data lama, minta login ulang.
                context.deleteSharedPreferences(FILE)
                EncryptedSharedPreferences.create(
                    context, FILE, key,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
                )
            }
        }
    }
}

/** Implementasi di memori untuk unit test. */
class InMemoryTokenStore(initial: Session? = null) : TokenStorage {
    private val state = MutableStateFlow(initial)
    override val session: StateFlow<Session?> = state.asStateFlow()
    override var lastLogoutReason: String? = null
        private set

    @Synchronized
    override fun save(session: Session) {
        state.value = session
        lastLogoutReason = null
    }

    @Synchronized
    override fun updateTokens(accessToken: String, refreshToken: String, sessionId: String) {
        state.value = state.value?.copy(accessToken = accessToken, refreshToken = refreshToken, sessionId = sessionId)
    }

    @Synchronized
    override fun clear(reason: String?) {
        state.value = null
        lastLogoutReason = reason
    }
}
