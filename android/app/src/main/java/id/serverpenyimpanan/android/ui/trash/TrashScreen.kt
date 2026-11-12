package id.serverpenyimpanan.android.ui.trash

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.InsertDriveFile
import androidx.compose.material.icons.filled.DeleteForever
import androidx.compose.material.icons.filled.RestoreFromTrash
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.TrashItemOut
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.ui.common.ConfirmDialog
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.FolderIcon
import id.serverpenyimpanan.android.ui.common.Load
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.appViewModel
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.formatDate
import id.serverpenyimpanan.android.ui.common.formatDateTime
import kotlinx.coroutines.launch

class TrashViewModel(private val app: AppContainer) : ViewModel() {
    var items by mutableStateOf<Load<List<TrashItemOut>>>(Load.Loading)
        private set
    var message by mutableStateOf<String?>(null)
    private val api get() = app.client().api

    init { refresh() }

    fun refresh() {
        viewModelScope.launch {
            items = try { Load.Ready(apiCall { api.trash() }.items) } catch (e: ApiException) { Load.Failed(e) }
        }
    }

    private fun act(ok: String, block: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                block()
                message = ok
                refresh()
            } catch (e: ApiException) {
                message = e.message
            }
        }
    }

    fun restore(item: TrashItemOut) = act("\"${item.name}\" dipulihkan") { apiCall { api.restore(item.id) } }
    fun purge(item: TrashItemOut) = act("\"${item.name}\" dihapus permanen") { apiCall { api.purge(item.id) } }
    fun empty() = act("Trash dikosongkan") { apiCall { api.emptyTrash() } }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TrashScreen(onBack: () -> Unit) {
    val vm = appViewModel { TrashViewModel(it) }
    val snackbar = remember { SnackbarHostState() }
    var confirmEmpty by remember { mutableStateOf(false) }
    var confirmPurge by remember { mutableStateOf<TrashItemOut?>(null) }
    LaunchedEffect(vm.message) {
        vm.message?.let {
            snackbar.showSnackbar(it)
            vm.message = null
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Trash") },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Kembali") } },
                actions = { TextButton(onClick = { confirmEmpty = true }) { Text("Kosongkan") } },
            )
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (val s = vm.items) {
                Load.Loading -> LoadingBox()
                is Load.Failed -> ErrorBox(s.error.message ?: "Gagal", onRetry = vm::refresh)
                is Load.Ready -> if (s.data.isEmpty()) {
                    EmptyBox(Icons.Filled.DeleteForever, "Trash kosong")
                } else {
                    LazyColumn {
                        items(s.data, key = { it.id }) { item ->
                            val isFolder = item.type == "folder"
                            ListItem(
                                headlineContent = { Text(item.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                supportingContent = {
                                    Text(
                                        (if (isFolder) "Folder · " else "") + formatBytes(item.sizeBytes) +
                                            " · dihapus ${formatDateTime(item.deletedAt)} · terhapus permanen ${formatDate(item.purgeAfter)}",
                                    )
                                },
                                leadingContent = { Icon(if (isFolder) FolderIcon else Icons.AutoMirrored.Filled.InsertDriveFile, null) },
                                trailingContent = {
                                    Row {
                                        IconButton(onClick = { vm.restore(item) }) { Icon(Icons.Filled.RestoreFromTrash, "Pulihkan") }
                                        IconButton(onClick = { confirmPurge = item }) { Icon(Icons.Filled.DeleteForever, "Hapus permanen") }
                                    }
                                },
                            )
                        }
                    }
                }
            }
        }
    }
    if (confirmEmpty) {
        ConfirmDialog(
            title = "Kosongkan trash?", message = "Semua item di trash akan dihapus permanen dan tidak bisa dipulihkan.",
            confirmText = "Kosongkan", onConfirm = { vm.empty() }, onDismiss = { confirmEmpty = false }, destructive = true,
        )
    }
    confirmPurge?.let { item ->
        ConfirmDialog(
            title = "Hapus permanen?", message = "\"${item.name}\" akan dihapus selamanya.",
            confirmText = "Hapus", onConfirm = { vm.purge(item) }, onDismiss = { confirmPurge = null }, destructive = true,
        )
    }
}
