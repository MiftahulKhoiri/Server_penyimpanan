package id.serverpenyimpanan.android.ui.admin

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Backup
import androidx.compose.material.icons.filled.PersonAdd
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PrimaryScrollableTabRow
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.data.api.ActivityOut
import id.serverpenyimpanan.android.data.api.AdminUserCreate
import id.serverpenyimpanan.android.data.api.AdminUserUpdate
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.BackupJobOut
import id.serverpenyimpanan.android.data.api.BackupStartRequest
import id.serverpenyimpanan.android.data.api.DeviceOut
import id.serverpenyimpanan.android.data.api.HealthOut
import id.serverpenyimpanan.android.data.api.StatisticsOut
import id.serverpenyimpanan.android.data.api.SystemStatusOut
import id.serverpenyimpanan.android.data.api.UserOut
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.ui.account.ActivityList
import id.serverpenyimpanan.android.ui.account.SubScaffold
import id.serverpenyimpanan.android.ui.common.ConfirmDialog
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.appViewModel
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.formatDateTime
import id.serverpenyimpanan.android.ui.common.formatUptime
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

data class Dashboard(
    val system: SystemStatusOut,
    val stats: StatisticsOut?,
    val health: HealthOut?,
    val ssd: DeviceOut?,
    val hdd: DeviceOut?,
)

class AdminViewModel(private val app: AppContainer) : ViewModel() {
    private val api get() = app.client().api
    var dashboard by mutableStateOf<Dashboard?>(null)
        private set
    var dashboardError by mutableStateOf<ApiException?>(null)
        private set
    var users by mutableStateOf<List<UserOut>?>(null)
        private set
    var backups by mutableStateOf<List<BackupJobOut>?>(null)
        private set
    var activity by mutableStateOf<List<ActivityOut>?>(null)
        private set
    var message by mutableStateOf<String?>(null)
    private var backupWatcher: Job? = null

    fun loadDashboard() {
        viewModelScope.launch {
            try {
                dashboardError = null
                val system = apiCall { api.systemStatus() }
                // Endpoint lain boleh gagal (mis. HDD tidak terpasang) – tampilkan yang tersedia.
                dashboard = Dashboard(
                    system = system,
                    stats = runCatching { apiCall { api.statistics() } }.getOrNull(),
                    health = runCatching { apiCall { api.health() } }.getOrNull(),
                    ssd = runCatching { apiCall { api.ssd() } }.getOrNull(),
                    hdd = runCatching { apiCall { api.hdd() } }.getOrNull(),
                )
            } catch (e: ApiException) {
                dashboardError = e
            }
        }
    }

    fun loadUsers(): Job = viewModelScope.launch {
        try { users = apiCall { api.users() }.items } catch (e: ApiException) { message = e.message }
    }

    fun loadBackups(): Job = viewModelScope.launch {
        try { backups = apiCall { api.backups() }.items } catch (e: ApiException) { message = e.message }
    }

    fun loadActivity(): Job = viewModelScope.launch {
        try { activity = apiCall { api.allActivity() }.items } catch (e: ApiException) { message = e.message }
    }

    private fun act(ok: String, reload: () -> Unit, block: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                block()
                message = ok
                reload()
            } catch (e: ApiException) {
                message = e.message
            }
        }
    }

    fun createUser(username: String, password: String, admin: Boolean) =
        act("Pengguna $username dibuat", { loadUsers() }) {
            apiCall { api.createUser(AdminUserCreate(username = username, password = password, role = if (admin) "admin" else "user")) }
        }

    fun setActive(user: UserOut, active: Boolean) =
        act(if (active) "${user.username} diaktifkan" else "${user.username} dinonaktifkan", { loadUsers() }) {
            apiCall { api.updateUser(user.id, AdminUserUpdate(isActive = active)) }
        }

    fun setAdmin(user: UserOut, admin: Boolean) =
        act("Peran ${user.username} diubah", { loadUsers() }) {
            apiCall { api.updateUser(user.id, AdminUserUpdate(role = if (admin) "admin" else "user")) }
        }

    fun resetPassword(user: UserOut, password: String) =
        act("Password ${user.username} direset", { loadUsers() }) {
            apiCall { api.updateUser(user.id, AdminUserUpdate(password = password)) }
        }

    fun deleteUser(user: UserOut) = act("${user.username} dihapus", { loadUsers() }) { apiCall { api.deleteUser(user.id) } }

    fun startBackup() = act("Backup ke HDD dimulai", { watchBackups() }) {
        apiCall { api.startBackup(BackupStartRequest()) }
    }

    /** Muat ulang daftar backup tiap 5 detik sampai tidak ada yang berjalan (maks. 10 menit). */
    private fun watchBackups() {
        backupWatcher?.cancel()
        backupWatcher = viewModelScope.launch {
            repeat(120) {
                loadBackups().join()
                if (backups?.none { it.isRunning } == true) return@launch
                delay(5_000)
            }
        }
    }
}

@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
fun AdminScreen(onBack: () -> Unit) {
    val vm = appViewModel { AdminViewModel(it) }
    var tab by rememberSaveable { mutableIntStateOf(0) }
    val snackbar = remember { SnackbarHostState() }
    var showCreate by remember { mutableStateOf(false) }
    LaunchedEffect(vm.message) {
        vm.message?.let {
            snackbar.showSnackbar(it)
            vm.message = null
        }
    }
    LaunchedEffect(tab) {
        when (tab) {
            0 -> vm.loadDashboard()
            1 -> vm.loadUsers()
            2 -> vm.loadBackups()
            3 -> vm.loadActivity()
        }
    }

    SubScaffold("Admin server", onBack) { mod ->
        Scaffold(
            modifier = mod,
            snackbarHost = { SnackbarHost(snackbar) },
            floatingActionButton = {
                if (tab == 1) FloatingActionButton(onClick = { showCreate = true }) { Icon(Icons.Filled.PersonAdd, "Tambah pengguna") }
            },
        ) { inner ->
            Column(Modifier.padding(inner).fillMaxSize()) {
                PrimaryScrollableTabRow(selectedTabIndex = tab) {
                    listOf("Dashboard", "Pengguna", "Backup", "Aktivitas").forEachIndexed { i, t ->
                        Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t) })
                    }
                }
                when (tab) {
                    0 -> DashboardTab(vm)
                    1 -> UsersTab(vm)
                    2 -> BackupsTab(vm)
                    else -> {
                        val list = vm.activity
                        when {
                            list == null -> LoadingBox()
                            list.isEmpty() -> EmptyBox(Icons.AutoMirrored.Filled.List, "Belum ada aktivitas")
                            else -> ActivityList(list, Modifier.fillMaxSize())
                        }
                    }
                }
            }
        }
    }
    if (showCreate) {
        CreateUserDialog(
            onCreate = { u, p, a ->
                vm.createUser(u, p, a)
                showCreate = false
            },
            onDismiss = { showCreate = false },
        )
    }
}

@Composable
private fun DashboardTab(vm: AdminViewModel) {
    val d = vm.dashboard
    val error = vm.dashboardError
    when {
        error != null -> ErrorBox(error.message ?: "Gagal", onRetry = vm::loadDashboard)
        d == null -> LoadingBox()
        else -> Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
            val s = d.system
            StatCard("Sistem") {
                Line("Versi", "${s.version} · ${s.platform}")
                Line("Uptime server / sistem", "${formatUptime(s.serverUptimeSeconds)} / ${formatUptime(s.systemUptimeSeconds)}")
                Line("CPU", "${s.cpu.percent}% dari ${s.cpu.count} inti" + (s.cpu.temperatureC?.let { " · %.1f °C".format(it) } ?: ""))
                Meter("RAM", s.memory.totalBytes - s.memory.availableBytes, s.memory.totalBytes)
                Line("Database", s.database)
            }
            d.health?.let { h ->
                StatCard("Kesehatan") {
                    Line("Status", h.status)
                    Line("SSD / HDD", "${h.primaryStorage} / ${h.backupStorage}")
                }
            }
            d.ssd?.let { DeviceCard("SSD (penyimpanan utama)", it) }
            d.hdd?.let { DeviceCard("HDD (backup)", it) }
            d.stats?.let { st ->
                StatCard("Statistik") {
                    Line("Pengguna", st.users.entries.joinToString(" · ") { "${it.key}: ${it.value}" })
                    Line("File", "${st.files.count} file · ${formatBytes(st.files.totalBytes)} · ${st.folders} folder")
                    st.files.byMediaType.forEach { (type, cs) -> Line(type, "${cs.count} · ${formatBytes(cs.totalBytes)}") }
                    Line("Trash", st.trash.entries.joinToString(" · ") { "${it.key}: ${it.value}" })
                    Line("Upload aktif", st.activeUploads.toString())
                }
            }
            TextButton(onClick = vm::loadDashboard) { Text("Muat ulang") }
        }
    }
}

@Composable
private fun DeviceCard(title: String, dev: DeviceOut) = StatCard(title) {
    Line("Status", if (dev.mounted) "Terpasang" else "Tidak terpasang")
    dev.reason?.let { Line("Keterangan", it) }
    if (dev.mounted) Meter("Terpakai", dev.usedBytes ?: 0, dev.totalBytes ?: 0)
    Line("Lokasi", dev.storagePath)
}

@Composable
private fun StatCard(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth().padding(bottom = 12.dp)) {
        Column(Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
private fun Line(label: String, value: String) {
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.outline)
        Text(value, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun Meter(label: String, used: Long, total: Long) {
    Column(Modifier.padding(vertical = 6.dp)) {
        Text("$label: ${formatBytes(used)} / ${formatBytes(total)}", style = MaterialTheme.typography.bodyMedium)
        LinearProgressIndicator(
            progress = { if (total > 0) (used.toFloat() / total).coerceIn(0f, 1f) else 0f },
            modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
        )
    }
}

@Composable
private fun UsersTab(vm: AdminViewModel) {
    val users = vm.users
    if (users == null) {
        LoadingBox()
        return
    }
    var menuFor by remember { mutableStateOf<UserOut?>(null) }
    var resetFor by remember { mutableStateOf<UserOut?>(null) }
    var deleteFor by remember { mutableStateOf<UserOut?>(null) }
    LazyColumn(Modifier.fillMaxSize()) {
        items(users, key = { it.id }) { user ->
            ListItem(
                headlineContent = { Text(user.username) },
                supportingContent = {
                    Text(
                        buildString {
                            append(if (user.isAdmin) "Admin" else "Pengguna")
                            if (!user.isActive) append(" · nonaktif")
                            user.lastLoginAt?.let { append(" · login ${formatDateTime(it)}") }
                        },
                    )
                },
                trailingContent = {
                    Box {
                        TextButton(onClick = { menuFor = user }) { Text("Kelola") }
                        DropdownMenu(expanded = menuFor == user, onDismissRequest = { menuFor = null }) {
                            DropdownMenuItem(
                                text = { Text(if (user.isActive) "Nonaktifkan" else "Aktifkan") },
                                onClick = { menuFor = null; vm.setActive(user, !user.isActive) },
                            )
                            DropdownMenuItem(
                                text = { Text(if (user.isAdmin) "Jadikan pengguna biasa" else "Jadikan admin") },
                                onClick = { menuFor = null; vm.setAdmin(user, !user.isAdmin) },
                            )
                            DropdownMenuItem(text = { Text("Reset password") }, onClick = { menuFor = null; resetFor = user })
                            DropdownMenuItem(
                                text = { Text("Hapus", color = MaterialTheme.colorScheme.error) },
                                onClick = { menuFor = null; deleteFor = user },
                            )
                        }
                    }
                },
            )
        }
    }
    resetFor?.let { user ->
        PasswordDialog(
            "Reset password ${user.username}",
            onConfirm = {
                vm.resetPassword(user, it)
                resetFor = null
            },
            onDismiss = { resetFor = null },
        )
    }
    deleteFor?.let { user ->
        ConfirmDialog(
            title = "Hapus ${user.username}?",
            message = "Akun dan semua file miliknya akan dihapus dari server.",
            confirmText = "Hapus", onConfirm = { vm.deleteUser(user) }, onDismiss = { deleteFor = null }, destructive = true,
        )
    }
}

@Composable
private fun BackupsTab(vm: AdminViewModel) {
    val jobs = vm.backups
    Column(Modifier.fillMaxSize()) {
        Button(
            onClick = vm::startBackup,
            enabled = jobs?.none { it.isRunning } != false,
            modifier = Modifier.fillMaxWidth().padding(16.dp),
        ) { Text("Mulai backup SSD → HDD") }
        when {
            jobs == null -> LoadingBox()
            jobs.isEmpty() -> EmptyBox(Icons.Filled.Backup, "Belum ada riwayat backup")
            else -> LazyColumn {
                items(jobs, key = { it.id }) { job ->
                    ListItem(
                        headlineContent = { Text("#${job.id} ${job.status} · ${formatDateTime(job.startedAt ?: job.createdAt)}") },
                        supportingContent = {
                            Column {
                                Text(
                                    buildString {
                                        append("${job.filesCopied}/${job.filesTotal} disalin · ${job.filesSkipped} dilewati · ")
                                        append("${job.filesFailed} gagal · ${formatBytes(job.bytesCopied)}")
                                        job.errorMessage?.let { append(" · $it") }
                                    },
                                )
                                if (job.isRunning) LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 4.dp))
                            }
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun CreateUserDialog(onCreate: (String, String, Boolean) -> Unit, onDismiss: () -> Unit) {
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var admin by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Pengguna baru") },
        text = {
            Column {
                OutlinedTextField(username, { username = it.trim() }, label = { Text("Username") }, singleLine = true)
                OutlinedTextField(
                    password, { password = it }, label = { Text("Password (min. 8)") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(), modifier = Modifier.padding(top = 8.dp),
                )
                ListItem(headlineContent = { Text("Admin") }, trailingContent = { Switch(admin, { admin = it }) })
            }
        },
        confirmButton = {
            Button(enabled = username.isNotBlank() && password.length >= 8, onClick = { onCreate(username, password, admin) }) { Text("Buat") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Batal") } },
    )
}

@Composable
private fun PasswordDialog(title: String, onConfirm: (String) -> Unit, onDismiss: () -> Unit) {
    var password by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            OutlinedTextField(
                password, { password = it }, label = { Text("Password baru (min. 8)") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
            )
        },
        confirmButton = { Button(enabled = password.length >= 8, onClick = { onConfirm(password) }) { Text("Simpan") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Batal") } },
    )
}
