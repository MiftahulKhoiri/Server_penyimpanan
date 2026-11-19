package id.serverpenyimpanan.android.data.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import java.io.File

/** Hasil pemasangan dari PackageInstaller (dikirim lewat [InstallResultReceiver]). */
sealed interface InstallEvent {
    data object None : InstallEvent
    data object WaitingUser : InstallEvent
    data object Success : InstallEvent
    data class Failed(val message: String) : InstallEvent
}

object ApkInstaller {
    const val ACTION_RESULT = "id.serverpenyimpanan.android.action.INSTALL_RESULT"

    private val _events = MutableStateFlow<InstallEvent>(InstallEvent.None)
    val events: StateFlow<InstallEvent> = _events.asStateFlow()

    internal fun publish(e: InstallEvent) { _events.value = e }

    fun reset() { _events.value = InstallEvent.None }

    fun canInstall(context: Context): Boolean = context.packageManager.canRequestPackageInstalls()

    /** Serahkan APK ke PackageInstaller; konfirmasi & hasil datang ke [InstallResultReceiver]. */
    suspend fun install(context: Context, apk: File) = withContext(Dispatchers.IO) {
        val app = context.applicationContext
        val installer = app.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(app.packageName)
            setSize(apk.length())
        }
        val id = installer.createSession(params)
        try {
            installer.openSession(id).use { session ->
                session.openWrite("base.apk", 0, apk.length()).use { out ->
                    apk.inputStream().use { it.copyTo(out, 64 * 1024) }
                    session.fsync(out)
                }
                val intent = Intent(app, InstallResultReceiver::class.java).setAction(ACTION_RESULT)
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                    (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                val pending = PendingIntent.getBroadcast(app, id, intent, flags)
                publish(InstallEvent.WaitingUser)
                session.commit(pending.intentSender)
            }
        } catch (e: Exception) {
            runCatching { installer.abandonSession(id) }
            throw UpdateException("Gagal menyiapkan pemasangan: ${e.message ?: e.javaClass.simpleName}")
        }
    }
}

class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ApkInstaller.ACTION_RESULT) return
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(Intent.EXTRA_INTENT)
                }
                if (confirm == null) {
                    ApkInstaller.publish(InstallEvent.Failed("Android tidak menampilkan konfirmasi pemasangan"))
                    return
                }
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                runCatching { context.startActivity(confirm) }
                    .onSuccess { ApkInstaller.publish(InstallEvent.WaitingUser) }
                    .onFailure { ApkInstaller.publish(InstallEvent.Failed("Tidak bisa membuka konfirmasi pemasangan")) }
            }
            PackageInstaller.STATUS_SUCCESS -> ApkInstaller.publish(InstallEvent.Success)
            PackageInstaller.STATUS_FAILURE_ABORTED -> ApkInstaller.publish(InstallEvent.Failed("Pemasangan dibatalkan"))
            PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                ApkInstaller.publish(
                    InstallEvent.Failed(
                        "APK tidak cocok dengan aplikasi terpasang (tanda tangan beda). " +
                            "Hapus aplikasi lama, lalu pasang APK dari GitHub Releases sekali secara manual.",
                    ),
                )
            PackageInstaller.STATUS_FAILURE_STORAGE -> ApkInstaller.publish(InstallEvent.Failed("Ruang penyimpanan HP tidak cukup"))
            else -> {
                val msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
                ApkInstaller.publish(InstallEvent.Failed("Pemasangan gagal" + (msg?.let { ": $it" } ?: "")))
            }
        }
    }
}
