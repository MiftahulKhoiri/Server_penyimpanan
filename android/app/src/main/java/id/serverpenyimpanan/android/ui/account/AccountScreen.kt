package id.serverpenyimpanan.android.ui.account

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.AdminPanelSettings
import androidx.compose.material.icons.filled.Backup
import androidx.compose.material.icons.filled.BatteryChargingFull
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Devices
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PhonelinkErase
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.SdStorage
import androidx.compose.material.icons.filled.Storage
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material.icons.filled.Wifi
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ActivityOut
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.ChangePasswordRequest
import id.serverpenyimpanan.android.data.api.MemoryInfo
import id.serverpenyimpanan.android.data.api.SessionOut
import id.serverpenyimpanan.android.data.api.StorageStatusOut
import id.serverpenyimpanan.android.data.api.UserOut
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.data.settings.BackupSettings
import id.serverpenyimpanan.android.ui.common.ConfirmDialog
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.Load
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.appViewModel
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.formatDateTime
import id.serverpenyimpanan.android.ui.common.toast
import id.serverpenyimpanan.android.work.AutoBackupWorker
import kotlinx.coroutines.launch

class AccountViewModel(private val app: AppContainer) : ViewModel() {
    var me by mutableStateOf<Load<UserOut>>(Load.Loading)
        private set
    var storage by mutableStateOf<StorageStatusOut?>(null)
        private set

    /** RAM server; hanya tersedia untuk admin (endpoint /system/status khusus admin). */
    var memory by mutableStateOf<MemoryInfo?>(null)
        private set

    init { refresh() }

    fun refresh() {
        viewModelScope.launch {
            me = try { Load.Ready(apiCall { app.client().api.me() }) } catch (e: ApiException) { Load.Failed(e) }
            storage = runCatching { apiCall { app.client().api.storageStatus() } }.getOrNull()
            val isAdmin = (me as? Load.Ready)?.data?.isAdmin == true
            memory = if (isAdmin) runCatching { apiCall { app.client().api.systemStatus() }.memory }.getOrNull() else null
        }
    }

    /** Logout di server (error jaringan diabaikan) lalu hapus token lokal. */
    fun logout(all: Boolean) {
        viewModelScope.launch {
            runCatching { apiCall { if (all) app.client().api.logoutAll() else app.client().api.logout() } }
            // Backup otomatis dihentikan agar worker tidak berjalan tanpa sesi.
            updateBackup(app.settings.backup.value.copy(enabled = false))
            app.tokens.clear(null)
        }
    }

    fun updateBackup(settings: BackupSettings) {
        app.settings.updateBackup { settings }
        app.transfers.scheduleAutoBackup(settings)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AccountScreen(
    onOpenSessions: () -> Unit,
    onOpenActivity: () -> Unit,
    onOpenPassword: () -> Unit,
    onOpenTrash: () -> Unit,
    onOpenAdmin: () -> Unit,
    onChangeServer: () -> Unit,
) {
    val vm = appViewModel { AccountViewModel(it) }
    val context = LocalContext.current
    val container = context.container
    val serverUrl by container.settings.serverUrl.collectAsState()
    val backup by container.settings.backup.collectAsState()
    var confirmLogoutAll by remember { mutableStateOf(false) }

    val permLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { _ ->
        if (AutoBackupWorker.hasMediaPermission(context, false)) {
            val next = backup.copy(enabled = true, includeVideos = backup.includeVideos && AutoBackupWorker.hasMediaPermission(context, true))
            vm.updateBackup(next)
            container.transfers.runBackupNow()
            context.toast("Backup otomatis aktif")
        } else {
            context.toast("Izin akses foto/video diperlukan untuk backup otomatis")
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Pengaturan") },
                actions = { IconButton(onClick = vm::refresh) { Icon(Icons.Filled.Refresh, "Muat ulang") } },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize().verticalScroll(rememberScrollState())) {
            when (val me = vm.me) {
                Load.Loading -> LoadingBox(Modifier.fillMaxWidth().padding(24.dp))
                is Load.Failed -> ErrorBox(me.error.message ?: "Gagal memuat profil", onRetry = vm::refresh, modifier = Modifier.fillMaxWidth())
                is Load.Ready -> ListItem(
                    headlineContent = { Text(me.data.username, style = MaterialTheme.typography.titleLarge) },
                    supportingContent = { Text(if (me.data.isAdmin) "Administrator" else "Pengguna") },
                    leadingContent = { Icon(Icons.Filled.Person, null) },
                )
            }

            SectionTitle("Penyimpanan server")
            ResourceCard(vm.storage, vm.memory, showRam = (vm.me as? Load.Ready)?.data?.isAdmin == true)

            SectionTitle("Backup otomatis galeri")
            SwitchRow(Icons.Filled.Backup, "Backup foto ke server", "Foto baru diunggah otomatis ke folder \"${backup.folderName}\"", backup.enabled) { on ->
                if (on && !AutoBackupWorker.hasMediaPermission(context, backup.includeVideos)) {
                    permLauncher.launch(buildPerms(backup.includeVideos))
                } else {
                    vm.updateBackup(backup.copy(enabled = on))
                    if (on) container.transfers.runBackupNow()
                }
            }
            if (backup.enabled) {
                SwitchRow(Icons.Filled.Videocam, "Sertakan video", null, backup.includeVideos) { on ->
                    if (on && !AutoBackupWorker.hasMediaPermission(context, true)) {
                        permLauncher.launch(buildPerms(true))
                    } else {
                        vm.updateBackup(backup.copy(includeVideos = on))
                    }
                }
                SwitchRow(Icons.Filled.Wifi, "Hanya lewat Wi-Fi", null, backup.wifiOnly) { vm.updateBackup(backup.copy(wifiOnly = it)) }
                SwitchRow(Icons.Filled.BatteryChargingFull, "Hanya saat mengisi daya", null, backup.chargingOnly) {
                    vm.updateBackup(backup.copy(chargingOnly = it))
                }
                NavRow("Jalankan backup sekarang", Icons.Filled.CloudUpload) {
                    container.transfers.runBackupNow()
                    context.toast("Backup dijalankan")
                }
            }

            SectionTitle("Keamanan")
            NavRow("Sesi login", Icons.Filled.Devices, onOpenSessions)
            NavRow("Riwayat aktivitas", Icons.AutoMirrored.Filled.List, onOpenActivity)
            NavRow("Ganti password", Icons.Filled.Lock, onOpenPassword)

            SectionTitle("Lainnya")
            NavRow("Trash", Icons.Filled.Delete, onOpenTrash)
            if ((vm.me as? Load.Ready)?.data?.isAdmin == true) NavRow("Admin server", Icons.Filled.AdminPanelSettings, onOpenAdmin)
            ListItem(
                headlineContent = { Text("Server") },
                supportingContent = { Text(serverUrl ?: "-") },
                leadingContent = { Icon(Icons.Filled.Dns, null) },
                modifier = Modifier.clickable(onClick = onChangeServer),
            )
            HorizontalDivider()
            ListItem(
                headlineContent = { Text("Keluar", color = MaterialTheme.colorScheme.error) },
                leadingContent = { Icon(Icons.AutoMirrored.Filled.ExitToApp, null, tint = MaterialTheme.colorScheme.error) },
                modifier = Modifier.clickable { vm.logout(all = false) },
            )
            ListItem(
                headlineContent = { Text("Keluar dari semua perangkat", color = MaterialTheme.colorScheme.error) },
                leadingContent = { Icon(Icons.Filled.PhonelinkErase, null, tint = MaterialTheme.colorScheme.error) },
                modifier = Modifier.clickable { confirmLogoutAll = true },
            )
        }
    }
    if (confirmLogoutAll) {
        ConfirmDialog(
            title = "Keluar dari semua perangkat?",
            message = "Semua sesi login akun ini, termasuk di HP ini, akan diakhiri.",
            confirmText = "Keluar semua",
            onConfirm = { vm.logout(all = true) },
            onDismiss = { confirmLogoutAll = false },
            destructive = true,
        )
    }
}

private fun buildPerms(includeVideos: Boolean): Array<String> =
    AutoBackupWorker.requiredPermissions(includeVideos) +
        if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.POST_NOTIFICATIONS) else emptyArray()

// ------------------------------------------------------------------
// Grafik sisa SSD / HDD / RAM
// ------------------------------------------------------------------

@Composable
private fun ResourceCard(s: StorageStatusOut?, memory: MemoryInfo?, showRam: Boolean) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
    ) {
        if (s == null) {
            Text(
                "Info penyimpanan tidak tersedia",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(16.dp),
            )
            return@Card
        }
        Row(
            Modifier.fillMaxWidth().padding(top = 16.dp, start = 8.dp, end = 8.dp),
            horizontalArrangement = Arrangement.SpaceEvenly,
        ) {
            UsageRing(
                label = "SSD", icon = Icons.Filled.SdStorage, color = MaterialTheme.colorScheme.primary,
                available = s.primary.available, total = s.primary.totalBytes, free = s.primary.freeBytes,
                modifier = Modifier.weight(1f),
            )
            UsageRing(
                label = "HDD", icon = Icons.Filled.Storage, color = MaterialTheme.colorScheme.tertiary,
                available = s.backup.available, total = s.backup.totalBytes, free = s.backup.freeBytes,
                modifier = Modifier.weight(1f),
            )
            if (showRam) {
                UsageRing(
                    label = "RAM", icon = Icons.Filled.Memory, color = MaterialTheme.colorScheme.secondary,
                    available = memory != null, total = memory?.totalBytes, free = memory?.availableBytes,
                    modifier = Modifier.weight(1f),
                )
            }
        }

        val usage = s.usage
        Column(Modifier.padding(16.dp)) {
            Text(
                "Data kamu: ${formatBytes(usage.usedBytes)} · ${usage.fileCount} file · trash ${formatBytes(usage.trashBytes)}",
                style = MaterialTheme.typography.bodyMedium,
            )
            val quota = usage.quotaBytes
            if (quota != null && quota > 0) {
                LinearProgressIndicator(
                    progress = { (usage.usedBytes.toFloat() / quota).coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
                )
                Text(
                    "Sisa kuota ${formatBytes(usage.quotaFreeBytes ?: (quota - usage.usedBytes).coerceAtLeast(0))} dari ${formatBytes(quota)}",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

/** Grafik donat: busur berwarna = terpakai, angka tengah = persen sisa. */
@Composable
private fun UsageRing(
    label: String,
    icon: ImageVector,
    color: Color,
    available: Boolean,
    total: Long?,
    free: Long?,
    modifier: Modifier = Modifier,
) {
    val ok = available && total != null && total > 0 && free != null
    val usedFraction = if (ok) (1f - free!!.toFloat() / total!!.toFloat()).coerceIn(0f, 1f) else 0f
    val freePercent = if (ok) ((1f - usedFraction) * 100).toInt() else 0
    // Sisa < 10% → warna peringatan.
    val arcColor = if (ok && freePercent < 10) MaterialTheme.colorScheme.error else color
    val track = MaterialTheme.colorScheme.surfaceVariant
    val animated by animateFloatAsState(usedFraction, animationSpec = tween(700), label = "ring-$label")

    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(88.dp), contentAlignment = Alignment.Center) {
            Canvas(Modifier.fillMaxSize()) {
                val stroke = 9.dp.toPx()
                val inset = stroke / 2
                val arcSize = Size(size.width - stroke, size.height - stroke)
                drawArc(track, -90f, 360f, false, Offset(inset, inset), arcSize, style = Stroke(stroke))
                if (ok && animated > 0f) {
                    drawArc(arcColor, -90f, 360f * animated, false, Offset(inset, inset), arcSize, style = Stroke(stroke, cap = StrokeCap.Round))
                }
            }
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(icon, null, tint = if (ok) arcColor else MaterialTheme.colorScheme.outline, modifier = Modifier.size(18.dp))
                Text(
                    if (ok) "$freePercent%" else "—",
                    style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold,
                )
                Text("sisa", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
            }
        }
        Text(label, style = MaterialTheme.typography.labelLarge, modifier = Modifier.padding(top = 6.dp))
        Text(
            if (ok) "${formatBytes(free)} kosong\ndari ${formatBytes(total)}" else "Tidak tersedia",
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline,
            textAlign = TextAlign.Center,
        )
    }
}

@Composable
fun SectionTitle(text: String) {
    Text(
        text, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(start = 16.dp, top = 20.dp, bottom = 4.dp),
    )
}

@Composable
private fun SwitchRow(icon: ImageVector, title: String, subtitle: String?, checked: Boolean, onChange: (Boolean) -> Unit) {
    ListItem(
        headlineContent = { Text(title) },
        supportingContent = subtitle?.let { { Text(it) } },
        leadingContent = { Icon(icon, null) },
        trailingContent = { Switch(checked = checked, onCheckedChange = onChange) },
        modifier = Modifier.clickable { onChange(!checked) },
    )
}

@Composable
private fun NavRow(title: String, icon: ImageVector, onClick: () -> Unit) {
    ListItem(headlineContent = { Text(title) }, leadingContent = { Icon(icon, null) }, modifier = Modifier.clickable(onClick = onClick))
}

// ------------------------------------------------------------------
// Sub-layar: sesi, aktivitas, ganti password
// ------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SubScaffold(title: String, onBack: () -> Unit, content: @Composable (Modifier) -> Unit) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(title) },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Kembali") } },
            )
        },
    ) { padding -> content(Modifier.padding(padding).fillMaxSize()) }
}

class SessionsViewModel(private val app: AppContainer) : ViewModel() {
    var sessions by mutableStateOf<Load<List<SessionOut>>>(Load.Loading)
        private set

    init { refresh() }

    fun refresh() {
        viewModelScope.launch {
            sessions = try { Load.Ready(apiCall { app.client().api.sessions() }) } catch (e: ApiException) { Load.Failed(e) }
        }
    }

    fun revoke(s: SessionOut, onError: (String) -> Unit) {
        viewModelScope.launch {
            try {
                apiCall { app.client().api.revokeSession(s.id) }
                refresh()
            } catch (e: ApiException) {
                onError(e.message ?: "Gagal")
            }
        }
    }
}

@Composable
fun SessionsScreen(onBack: () -> Unit) {
    val vm = appViewModel { SessionsViewModel(it) }
    val context = LocalContext.current
    SubScaffold("Sesi login", onBack) { mod ->
        when (val s = vm.sessions) {
            Load.Loading -> LoadingBox(mod)
            is Load.Failed -> ErrorBox(s.error.message ?: "Gagal", onRetry = vm::refresh, modifier = mod)
            is Load.Ready -> LazyColumn(mod) {
                items(s.data, key = { it.id }) { session ->
                    ListItem(
                        headlineContent = { Text((session.deviceName ?: session.userAgent ?: "Perangkat") + if (session.current) " (HP ini)" else "") },
                        supportingContent = { Text("${session.ipAddress ?: "-"} · aktif ${formatDateTime(session.lastUsedAt ?: session.createdAt)}") },
                        trailingContent = if (!session.current) {
                            { TextButton(onClick = { vm.revoke(session) { context.toast(it) } }) { Text("Akhiri") } }
                        } else {
                            null
                        },
                    )
                }
            }
        }
    }
}

class ActivityViewModel(private val app: AppContainer) : ViewModel() {
    var items by mutableStateOf<Load<List<ActivityOut>>>(Load.Loading)
        private set

    init { refresh() }

    fun refresh() {
        viewModelScope.launch {
            items = try { Load.Ready(apiCall { app.client().api.activity() }.items) } catch (e: ApiException) { Load.Failed(e) }
        }
    }
}

@Composable
fun ActivityScreen(onBack: () -> Unit) {
    val vm = appViewModel { ActivityViewModel(it) }
    SubScaffold("Riwayat aktivitas", onBack) { mod ->
        when (val s = vm.items) {
            Load.Loading -> LoadingBox(mod)
            is Load.Failed -> ErrorBox(s.error.message ?: "Gagal", onRetry = vm::refresh, modifier = mod)
            is Load.Ready ->
                if (s.data.isEmpty()) EmptyBox(Icons.AutoMirrored.Filled.List, "Belum ada aktivitas", mod) else ActivityList(s.data, mod)
        }
    }
}

@Composable
fun ActivityList(items: List<ActivityOut>, modifier: Modifier = Modifier) {
    LazyColumn(modifier) {
        items(items, key = { it.id }) { a ->
            ListItem(
                headlineContent = { Text(a.action) },
                supportingContent = {
                    Text(listOfNotNull(a.userId?.let { "user #$it" }, a.targetType, a.ipAddress, formatDateTime(a.createdAt)).joinToString(" · "))
                },
            )
        }
    }
}

@Composable
fun ChangePasswordScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val container = context.container
    val scope = rememberCoroutineScope()
    var old by remember { mutableStateOf("") }
    var new1 by remember { mutableStateOf("") }
    var new2 by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    SubScaffold("Ganti password", onBack) { mod ->
        Column(mod.padding(16.dp)) {
            OutlinedTextField(
                old, { old = it }, label = { Text("Password lama") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                new1, { new1 = it }, label = { Text("Password baru (min. 8 karakter)") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            OutlinedTextField(
                new2, { new2 = it }, label = { Text("Ulangi password baru") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            error?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(top = 8.dp)) }
            Text(
                "Setelah password diganti, sesi di perangkat lain akan diakhiri.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(top = 8.dp),
            )
            Button(
                enabled = !busy && old.isNotEmpty() && new1.length >= 8,
                onClick = {
                    if (new1 != new2) {
                        error = "Password baru tidak sama"
                        return@Button
                    }
                    busy = true
                    error = null
                    scope.launch {
                        try {
                            val t = apiCall { container.client().api.changePassword(ChangePasswordRequest(old, new1)) }
                            // Server merotasi token & mencabut sesi lain; simpan token baru.
                            container.tokens.updateTokens(t.accessToken, t.refreshToken, t.sessionId)
                            context.toast("Password diganti")
                            onBack()
                        } catch (e: ApiException) {
                            error = e.message
                        }
                        busy = false
                    }
                },
                modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
            ) { Text(if (busy) "Menyimpan…" else "Simpan") }
        }
    }
}
