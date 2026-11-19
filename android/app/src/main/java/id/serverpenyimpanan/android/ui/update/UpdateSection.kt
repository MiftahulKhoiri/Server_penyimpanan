package id.serverpenyimpanan.android.ui.update

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.SystemUpdate
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import id.serverpenyimpanan.android.BuildConfig
import id.serverpenyimpanan.android.data.update.ApkInstaller
import id.serverpenyimpanan.android.data.update.AppRelease
import id.serverpenyimpanan.android.data.update.AppUpdater
import id.serverpenyimpanan.android.data.update.InstallEvent
import id.serverpenyimpanan.android.data.update.UpdateException
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.toast
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.File

sealed interface UpdateUi {
    data object Idle : UpdateUi
    data object Checking : UpdateUi
    data class UpToDate(val release: AppRelease) : UpdateUi
    data class Available(val release: AppRelease) : UpdateUi
    data class Downloading(val release: AppRelease, val done: Long, val total: Long) : UpdateUi
    data class Installing(val release: AppRelease) : UpdateUi
    data class Error(val message: String, val release: AppRelease? = null) : UpdateUi
}

class UpdateViewModel(app: Application) : AndroidViewModel(app) {
    private val updater = AppUpdater(userAgent = "Penyimpanan-Android/${BuildConfig.VERSION_NAME}")
    private val apkFile = File(app.cacheDir, "update/${AppUpdater.ASSET}")
    private var job: Job? = null

    var state by mutableStateOf<UpdateUi>(UpdateUi.Idle)
        private set

    /** Pembaruan dalam aplikasi hanya untuk APK rilis (APK debug punya paket & kunci berbeda). */
    val supported: Boolean = !BuildConfig.DEBUG

    init {
        apkFile.delete() // sisa unduhan sebelumnya
        if (supported) check()
    }

    fun check() {
        if (job?.isActive == true) return
        job = viewModelScope.launch {
            state = UpdateUi.Checking
            state = try {
                val r = updater.latest()
                if (r.build > BuildConfig.VERSION_CODE) UpdateUi.Available(r) else UpdateUi.UpToDate(r)
            } catch (e: UpdateException) {
                UpdateUi.Error(e.message ?: "Gagal memeriksa pembaruan")
            }
        }
    }

    fun downloadAndInstall(release: AppRelease) {
        if (job?.isActive == true) return
        ApkInstaller.reset()
        job = viewModelScope.launch {
            state = UpdateUi.Downloading(release, 0, release.sizeBytes)
            try {
                updater.download(release, apkFile) { done, total -> state = UpdateUi.Downloading(release, done, total) }
                state = UpdateUi.Installing(release)
                ApkInstaller.install(getApplication(), apkFile)
            } catch (e: CancellationException) {
                state = UpdateUi.Available(release)
                throw e
            } catch (e: UpdateException) {
                state = UpdateUi.Error(e.message ?: "Pembaruan gagal", release)
            }
        }
    }

    fun cancel() {
        job?.cancel()
    }

    fun onInstallEvent(e: InstallEvent) {
        val current = state
        val release = (current as? UpdateUi.Installing)?.release ?: return
        when (e) {
            is InstallEvent.Failed -> {
                apkFile.delete()
                state = UpdateUi.Error(e.message, release)
            }
            InstallEvent.Success -> apkFile.delete()
            else -> Unit
        }
    }
}

/** Baris "Pembaruan aplikasi" di menu Pengaturan. */
@Composable
fun UpdateSection() {
    val vm: UpdateViewModel = viewModel()
    val context = LocalContext.current
    val installEvent by ApkInstaller.events.collectAsState()
    LaunchedEffect(installEvent) { vm.onInstallEvent(installEvent) }

    val current = "Versi ${BuildConfig.VERSION_NAME}"

    fun startUpdate(release: AppRelease) {
        if (!ApkInstaller.canInstall(context)) {
            context.toast("Izinkan \"Instal aplikasi tak dikenal\" untuk Penyimpanan, lalu ketuk lagi")
            runCatching {
                context.startActivity(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}")),
                )
            }
            return
        }
        vm.downloadAndInstall(release)
    }

    if (!vm.supported) {
        ListItem(
            headlineContent = { Text("Pembaruan aplikasi") },
            supportingContent = { Text("$current · build debug, pembaruan otomatis hanya untuk APK rilis") },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null) },
        )
        return
    }

    when (val s = vm.state) {
        UpdateUi.Idle, UpdateUi.Checking -> ListItem(
            headlineContent = { Text("Pembaruan aplikasi") },
            supportingContent = { Text("$current · memeriksa…") },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null) },
        )
        is UpdateUi.UpToDate -> ListItem(
            headlineContent = { Text("Pembaruan aplikasi") },
            supportingContent = { Text("$current · sudah versi terbaru") },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null) },
            modifier = Modifier.clickable { vm.check() },
        )
        is UpdateUi.Available -> ListItem(
            headlineContent = { Text("Versi baru tersedia") },
            supportingContent = {
                Text("Build #${s.release.build} (${formatBytes(s.release.sizeBytes)}) · terpasang build #${BuildConfig.VERSION_CODE}")
            },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null, tint = MaterialTheme.colorScheme.primary) },
            trailingContent = { Button(onClick = { startUpdate(s.release) }) { Text("Unduh & pasang") } },
        )
        is UpdateUi.Downloading -> ListItem(
            headlineContent = { Text("Mengunduh build #${s.release.build}") },
            supportingContent = {
                Column(Modifier.fillMaxWidth()) {
                    if (s.total > 0) {
                        LinearProgressIndicator(
                            progress = { (s.done.toFloat() / s.total).coerceIn(0f, 1f) },
                            modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
                        )
                    } else {
                        LinearProgressIndicator(Modifier.fillMaxWidth().padding(vertical = 6.dp))
                    }
                    Text("${formatBytes(s.done)} / ${formatBytes(s.total.takeIf { it > 0 })}")
                }
            },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null, tint = MaterialTheme.colorScheme.primary) },
            trailingContent = { TextButton(onClick = vm::cancel) { Text("Batal") } },
        )
        is UpdateUi.Installing -> ListItem(
            headlineContent = { Text("Memasang build #${s.release.build}") },
            supportingContent = { Text("Ketuk \"Perbarui\" pada konfirmasi Android") },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null, tint = MaterialTheme.colorScheme.primary) },
        )
        is UpdateUi.Error -> ListItem(
            headlineContent = { Text("Pembaruan aplikasi") },
            supportingContent = { Text(s.message, color = MaterialTheme.colorScheme.error) },
            leadingContent = { Icon(Icons.Filled.SystemUpdate, null) },
            trailingContent = {
                TextButton(onClick = { s.release?.let { startUpdate(it) } ?: vm.check() }) { Text("Coba lagi") }
            },
        )
    }
}
