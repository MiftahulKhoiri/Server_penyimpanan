package id.serverpenyimpanan.android.ui.admin

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import id.serverpenyimpanan.android.data.api.DiskRoleOut
import id.serverpenyimpanan.android.data.api.PartitionOut
import id.serverpenyimpanan.android.data.api.SetupJobOut
import id.serverpenyimpanan.android.data.api.StorageSetupOut
import id.serverpenyimpanan.android.ui.common.ConfirmDialog
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.formatDateTime

/** Lokasi mount yang diterima server: /mnt/<nama>, /media/<nama>, /srv/<nama>. */
private val MOUNT_POINT_REGEX = Regex("^/(mnt|media|srv)/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

internal fun isValidMountPoint(value: String): Boolean = MOUNT_POINT_REGEX.matches(value)

internal fun roleTitle(role: String): String = if (role == "ssd") "SSD (penyimpanan utama)" else "HDD (backup)"

internal fun partitionTitle(p: PartitionOut): String = buildString {
    append(p.label ?: p.model ?: p.path)
    append(" · ")
    append(formatBytes(p.sizeBytes))
}

internal fun partitionSubtitle(p: PartitionOut): String = buildString {
    append(p.path)
    p.fstype?.let { append(" · $it") }
    p.model?.takeIf { p.label != null }?.let { append(" · $it") }
    p.transport?.let { append(" · ${it.uppercase()}") }
    when {
        p.role != null -> append(" · dipakai sebagai ${p.role.uppercase()}")
        p.mountpoint != null -> append(" · terpasang di ${p.mountpoint}")
    }
}

@Composable
internal fun DisksTab(vm: AdminViewModel) {
    val data = vm.disks
    val error = vm.disksError
    var picking by remember { mutableStateOf(false) }
    var confirmMount by remember { mutableStateOf<PartitionOut?>(null) }
    var confirmUnmount by remember { mutableStateOf(false) }
    var editRole by remember { mutableStateOf<DiskRoleOut?>(null) }

    when {
        error != null -> ErrorBox(error.message ?: "Gagal memuat disk", onRetry = { vm.loadDisks() })
        data == null -> LoadingBox()
        !data.helperAvailable -> ErrorBox(data.helperError ?: "Helper pemasangan disk belum terpasang", onRetry = { vm.loadDisks() })
        else -> DisksContent(
            data = data,
            busy = vm.diskBusy,
            onPick = { picking = true },
            onUnmount = { confirmUnmount = true },
            onEdit = { editRole = it },
            onReload = { vm.loadDisks() },
        )
    }

    val hdd = data?.roles?.firstOrNull { it.role == "hdd" }
    if (picking && data != null) {
        PickDiskDialog(
            partitions = data.partitions.filter { it.role == null },
            onPick = { picking = false; confirmMount = it },
            onDismiss = { picking = false },
        )
    }
    confirmMount?.let { p ->
        ConfirmDialog(
            title = "Pasang sebagai HDD backup?",
            message = "${partitionTitle(p)} (${p.fstype}) akan dipasang di ${hdd?.mountPoint ?: "lokasi HDD"} " +
                "dan otomatis terpasang setiap boot. Disk TIDAK diformat; isinya tetap utuh.",
            confirmText = "Pasang",
            onConfirm = { vm.mountHdd(p) },
            onDismiss = { confirmMount = null },
        )
    }
    if (confirmUnmount) {
        ConfirmDialog(
            title = "Lepas HDD?",
            message = "Backup tidak bisa berjalan sampai HDD dipasang lagi. Data di HDD tidak dihapus, " +
                "dan disk aman dicabut setelah dilepas.",
            confirmText = "Lepas",
            onConfirm = { vm.unmountHdd() },
            onDismiss = { confirmUnmount = false },
            destructive = true,
        )
    }
    editRole?.let { role ->
        MountPointDialog(
            role = role,
            onConfirm = { vm.setMountPoint(role.role, it); editRole = null },
            onDismiss = { editRole = null },
        )
    }
}

@Composable
private fun DisksContent(
    data: StorageSetupOut,
    busy: String?,
    onPick: () -> Unit,
    onUnmount: () -> Unit,
    onEdit: (DiskRoleOut) -> Unit,
    onReload: () -> Unit,
) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        if (busy != null) {
            NoticeCard(busy, progress = true)
        } else if (data.restartPending) {
            NoticeCard("Perubahan lokasi mount belum dipakai server (server sedang atau perlu dijalankan ulang).")
        }
        data.helperError?.let { NoticeCard(it, error = true) }
        data.roles.forEach { role ->
            RoleCard(role, enabled = busy == null, onPick = onPick, onUnmount = onUnmount, onEdit = { onEdit(role) })
        }
        data.job?.let { JobCard(it) }
        Text("Disk terdeteksi", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 8.dp))
        Text(
            "Hanya partisi ext4/xfs/btrfs/f2fs yang bisa dipakai. Disk tidak pernah diformat dari aplikasi.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
        data.partitions.forEach { p ->
            ListItem(
                headlineContent = { Text(partitionTitle(p)) },
                supportingContent = {
                    Column {
                        Text(partitionSubtitle(p))
                        p.reasonText?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                    }
                },
            )
            HorizontalDivider()
        }
        if (data.partitions.isEmpty()) Text("Tidak ada disk terdeteksi", modifier = Modifier.padding(vertical = 12.dp))
        TextButton(onClick = onReload, enabled = busy == null) { Text("Muat ulang") }
    }
}

@Composable
private fun NoticeCard(text: String, progress: Boolean = false, error: Boolean = false) {
    Card(
        Modifier.fillMaxWidth().padding(bottom = 12.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (error) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.secondaryContainer,
        ),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(text, style = MaterialTheme.typography.bodyMedium)
            if (progress) LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp))
        }
    }
}

@Composable
private fun RoleCard(role: DiskRoleOut, enabled: Boolean, onPick: () -> Unit, onUnmount: () -> Unit, onEdit: () -> Unit) =
    StatCard(roleTitle(role.role)) {
        Line(
            "Status",
            if (role.mounted) {
                listOfNotNull("Terpasang", role.device, role.fstype, role.label ?: role.model).joinToString(" · ")
            } else {
                "Tidak terpasang"
            },
        )
        Line("Lokasi mount", role.mountPoint)
        role.paths.forEach { (key, value) -> Line(key, value) }
        val total = role.totalBytes
        val free = role.freeBytes
        if (role.mounted && total != null && free != null) Meter("Terpakai", total - free, total)
        Line(
            "Otomatis saat boot",
            when {
                role.managed -> "Ya (dikelola aplikasi)"
                role.mounted -> "Diatur manual di /etc/fstab"
                else -> "Tidak"
            },
        )
        if (role.isSsd) {
            Text(
                "SSD menyimpan database server, jadi memasang/mengganti SSD hanya lewat SSH. " +
                    "Dari sini Anda bisa memindah lokasi mount-nya.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(vertical = 4.dp),
            )
        }
        Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (role.canMount && !role.mounted) Button(onClick = onPick, enabled = enabled) { Text("Pilih disk") }
            if (role.canUnmount) {
                OutlinedButton(
                    onClick = onUnmount,
                    enabled = enabled,
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error),
                ) { Text("Lepas") }
            }
            TextButton(onClick = onEdit, enabled = enabled) { Text("Ubah lokasi") }
        }
    }

@Composable
private fun JobCard(job: SetupJobOut) = StatCard("Operasi terakhir") {
    val what = when (job.op) {
        "mount" -> "Pasang"
        "unmount" -> "Lepas"
        else -> "Pindah lokasi"
    }
    val status = when (job.status) {
        "running" -> "berjalan"
        "succeeded" -> "berhasil"
        else -> "gagal"
    }
    Line("$what ${job.role.uppercase()} · $status", job.message ?: "-")
    Line("Waktu", formatDateTime(job.finishedAt ?: job.startedAt))
    if (job.status == "failed" && job.steps.isNotEmpty()) {
        Text(
            job.steps.takeLast(4).joinToString("\n"),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}

@Composable
private fun PickDiskDialog(partitions: List<PartitionOut>, onPick: (PartitionOut) -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Pilih disk untuk HDD") },
        text = {
            Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                if (partitions.none { it.usable }) {
                    Text(
                        "Tidak ada disk yang bisa dipakai. Colokkan HDD berformat ext4 " +
                            "(format lewat SSH bila perlu), lalu Muat ulang.",
                        modifier = Modifier.padding(bottom = 8.dp),
                    )
                }
                partitions.sortedByDescending { it.usable }.forEach { p ->
                    ListItem(
                        headlineContent = {
                            Text(
                                partitionTitle(p),
                                color = if (p.usable) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.outline,
                            )
                        },
                        supportingContent = { Text(p.reasonText ?: partitionSubtitle(p)) },
                        modifier = if (p.usable) Modifier.clickable { onPick(p) } else Modifier,
                    )
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Batal") } },
    )
}

@Composable
private fun MountPointDialog(role: DiskRoleOut, onConfirm: (String) -> Unit, onDismiss: () -> Unit) {
    var value by remember { mutableStateOf(role.mountPoint) }
    val trimmed = value.trim()
    val valid = isValidMountPoint(trimmed)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Lokasi mount ${role.role.uppercase()}") },
        text = {
            Column {
                OutlinedTextField(
                    value = value,
                    onValueChange = { value = it },
                    label = { Text("Lokasi baru") },
                    singleLine = true,
                    isError = trimmed.isNotEmpty() && !valid,
                    supportingText = { Text("Bentuk: /mnt/nama, /media/nama, atau /srv/nama (folder harus kosong)") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "Disk yang sama dipasang ulang di lokasi baru; data tidak berubah. Server akan berhenti " +
                        "±10-30 detik lalu berjalan lagi. Bila gagal, pengaturan lama dipulihkan otomatis.",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        },
        confirmButton = {
            Button(onClick = { onConfirm(trimmed) }, enabled = valid && trimmed != role.mountPoint) { Text("Pindahkan") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Batal") } },
    )
}
