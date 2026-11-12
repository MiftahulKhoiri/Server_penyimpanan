package id.serverpenyimpanan.android.ui.transfers

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Cancel
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.ClearAll
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.work.WorkInfo
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.work.TransferItem
import id.serverpenyimpanan.android.work.Transfers

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TransfersScreen() {
    val transfers = LocalContext.current.container.transfers
    val items by transfers.items.collectAsState(initial = emptyList())
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Transfer") },
                actions = {
                    IconButton(onClick = { transfers.clearFinished() }) { Icon(Icons.Filled.ClearAll, "Bersihkan yang selesai") }
                },
            )
        },
    ) { padding ->
        if (items.isEmpty()) {
            EmptyBox(Icons.Filled.CloudUpload, "Tidak ada upload atau unduhan", Modifier.padding(padding).fillMaxSize())
        } else {
            LazyColumn(Modifier.padding(padding).fillMaxSize()) {
                items(items, key = { it.id.toString() }) { item ->
                    TransferRow(item, onCancel = { transfers.cancel(item.id) })
                }
            }
        }
    }
}

@Composable
private fun TransferRow(item: TransferItem, onCancel: () -> Unit) {
    val running = item.state == WorkInfo.State.RUNNING
    val waiting = item.state == WorkInfo.State.ENQUEUED || item.state == WorkInfo.State.BLOCKED
    val statusText = when (item.state) {
        WorkInfo.State.RUNNING -> if (item.total > 0) "${formatBytes(item.sent)} / ${formatBytes(item.total)}" else "Berjalan…"
        WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED -> "Menunggu (jaringan/antrean)"
        WorkInfo.State.SUCCEEDED -> if (item.total > 0) "Selesai · ${formatBytes(item.total)}" else "Selesai"
        WorkInfo.State.FAILED -> "Gagal: ${item.error ?: "tidak diketahui"}"
        WorkInfo.State.CANCELLED -> "Dibatalkan"
    }
    ListItem(
        headlineContent = { Text(item.name.ifBlank { "(tanpa nama)" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
        supportingContent = {
            Column {
                Text(
                    statusText,
                    color = if (item.state == WorkInfo.State.FAILED) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (running) {
                    if (item.total > 0) {
                        LinearProgressIndicator(progress = { item.progress }, modifier = Modifier.fillMaxWidth().padding(top = 4.dp))
                    } else {
                        LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 4.dp))
                    }
                }
            }
        },
        leadingContent = {
            when {
                item.state == WorkInfo.State.SUCCEEDED -> Icon(Icons.Filled.CheckCircle, null, tint = MaterialTheme.colorScheme.primary)
                item.state == WorkInfo.State.FAILED -> Icon(Icons.Filled.Error, null, tint = MaterialTheme.colorScheme.error)
                item.kind == Transfers.KIND_DOWNLOAD -> Icon(Icons.Filled.CloudDownload, null)
                item.kind == Transfers.KIND_BACKUP -> Icon(Icons.Filled.PhotoLibrary, null)
                else -> Icon(Icons.Filled.CloudUpload, null)
            }
        },
        trailingContent = if (running || waiting) {
            { IconButton(onClick = onCancel) { Icon(Icons.Filled.Cancel, "Batalkan") } }
        } else {
            null
        },
    )
}
