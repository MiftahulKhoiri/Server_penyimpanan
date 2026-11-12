package id.serverpenyimpanan.android.ui.files

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Sort
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.CreateNewFolder
import androidx.compose.material.icons.filled.FolderOpen
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.UploadFile
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.data.api.FolderOut
import id.serverpenyimpanan.android.ui.common.ConfirmDialog
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.FileThumbnail
import id.serverpenyimpanan.android.ui.common.FolderIcon
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.TextInputDialog
import id.serverpenyimpanan.android.ui.common.appViewModel
import id.serverpenyimpanan.android.ui.common.formatBytes
import id.serverpenyimpanan.android.ui.common.formatDateTime

/** Aksi/menu yang sedang terbuka. */
private sealed interface Pending {
    data class FileMenu(val file: FileOut) : Pending
    data class FolderMenu(val folder: FolderOut) : Pending
    data class RenameFile(val file: FileOut) : Pending
    data class RenameFolder(val folder: FolderOut) : Pending
    data class MoveFile(val file: FileOut) : Pending
    data class CopyFile(val file: FileOut) : Pending
    data class MoveFolder(val folder: FolderOut) : Pending
    data class DeleteFile(val file: FileOut) : Pending
    data class DeleteFolder(val folder: FolderOut) : Pending
    data class Info(val file: FileOut) : Pending
    data object NewFolder : Pending
}

@OptIn(ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun FilesScreen(
    folderId: String?,
    onOpenFolder: (String) -> Unit,
    onOpenFile: (FileOut) -> Unit,
    onBack: (() -> Unit)?,
) {
    val vm = appViewModel(key = "files-${folderId ?: "root"}") { FilesViewModel(it, folderId) }
    val state = vm.state
    val context = LocalContext.current
    val client = context.container.client()
    val snackbar = remember { SnackbarHostState() }
    var pending by remember { mutableStateOf<Pending?>(null) }
    var searching by remember { mutableStateOf(false) }
    var searchText by remember { mutableStateOf("") }
    var sortMenu by remember { mutableStateOf(false) }
    var addMenu by remember { mutableStateOf(false) }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) vm.upload(uris, context)
    }

    LaunchedEffect(vm.message) {
        vm.message?.let {
            snackbar.showSnackbar(it)
            vm.message = null
        }
    }

    val title = state.detail?.name ?: "Berkas"
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    if (searching) {
                        TextField(
                            value = searchText,
                            onValueChange = { searchText = it },
                            placeholder = { Text("Cari nama file") },
                            singleLine = true,
                            colors = TextFieldDefaults.colors(
                                focusedContainerColor = Color.Transparent,
                                unfocusedContainerColor = Color.Transparent,
                            ),
                            keyboardActions = KeyboardActions(onSearch = { vm.search(searchText) }),
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    } else {
                        Column {
                            Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            val crumbs = state.detail?.path.orEmpty()
                            if (crumbs.isNotEmpty()) {
                                Text(
                                    "/" + crumbs.joinToString("/") { it.name },
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                    }
                },
                navigationIcon = {
                    if (onBack != null) IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Kembali") }
                },
                actions = {
                    if (searching) {
                        IconButton(onClick = { searching = false; searchText = ""; vm.search("") }) { Icon(Icons.Filled.Close, "Tutup pencarian") }
                    } else {
                        IconButton(onClick = { searching = true }) { Icon(Icons.Filled.Search, "Cari") }
                    }
                    Box {
                        IconButton(onClick = { sortMenu = true }) { Icon(Icons.AutoMirrored.Filled.Sort, "Urutkan") }
                        DropdownMenu(expanded = sortMenu, onDismissRequest = { sortMenu = false }) {
                            SortOption.entries.forEach { opt ->
                                DropdownMenuItem(
                                    text = { Text(if (opt == vm.sort) "✓ ${opt.label}" else opt.label) },
                                    onClick = { sortMenu = false; vm.changeSort(opt) },
                                )
                            }
                        }
                    }
                },
            )
        },
        floatingActionButton = {
            Box {
                FloatingActionButton(onClick = { addMenu = true }) { Icon(Icons.Filled.Add, "Tambah") }
                DropdownMenu(expanded = addMenu, onDismissRequest = { addMenu = false }) {
                    DropdownMenuItem(
                        text = { Text("Upload file") },
                        leadingIcon = { Icon(Icons.Filled.UploadFile, null) },
                        onClick = { addMenu = false; picker.launch(arrayOf("*/*")) },
                    )
                    DropdownMenuItem(
                        text = { Text("Folder baru") },
                        leadingIcon = { Icon(Icons.Filled.CreateNewFolder, null) },
                        onClick = { addMenu = false; pending = Pending.NewFolder },
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            val error = state.error
            when {
                state.loading && state.files.isEmpty() && state.folders.isEmpty() -> LoadingBox()
                error != null && state.files.isEmpty() && state.folders.isEmpty() ->
                    ErrorBox(error.message ?: "Gagal memuat", onRetry = vm::refresh)
                else -> PullToRefreshBox(isRefreshing = state.loading, onRefresh = vm::refresh) {
                    if (state.folders.isEmpty() && state.files.isEmpty()) {
                        EmptyBox(Icons.Filled.FolderOpen, if (vm.query.isNotBlank()) "Tidak ada hasil untuk \"${vm.query}\"" else "Folder ini kosong")
                    } else {
                        val listState = rememberLazyListState()
                        val nearEnd by remember {
                            derivedStateOf {
                                val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
                                last >= listState.layoutInfo.totalItemsCount - 10
                            }
                        }
                        LaunchedEffect(nearEnd, state.files.size) { if (nearEnd) vm.loadMore() }
                        LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
                            items(state.folders, key = { "d-" + it.id }) { folder ->
                                ListItem(
                                    headlineContent = { Text(folder.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                    supportingContent = { Text(formatDateTime(folder.createdAt)) },
                                    leadingContent = { Icon(FolderIcon, null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(40.dp)) },
                                    trailingContent = {
                                        IconButton(onClick = { pending = Pending.FolderMenu(folder) }) { Icon(Icons.Filled.MoreVert, "Menu") }
                                    },
                                    modifier = Modifier.combinedClickable(
                                        onClick = { onOpenFolder(folder.id) },
                                        onLongClick = { pending = Pending.FolderMenu(folder) },
                                    ),
                                )
                            }
                            items(state.files, key = { "f-" + it.id }) { file ->
                                ListItem(
                                    headlineContent = { Text(file.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                    supportingContent = { Text("${formatBytes(file.sizeBytes)} · ${formatDateTime(file.createdAt)}") },
                                    leadingContent = { FileThumbnail(file, client.thumbnailUrl(file.id), Modifier.size(44.dp)) },
                                    trailingContent = {
                                        IconButton(onClick = { pending = Pending.FileMenu(file) }) { Icon(Icons.Filled.MoreVert, "Menu") }
                                    },
                                    modifier = Modifier.combinedClickable(
                                        onClick = { onOpenFile(file) },
                                        onLongClick = { pending = Pending.FileMenu(file) },
                                    ),
                                )
                            }
                            if (state.loadingMore) item { LoadingBox(Modifier.fillMaxWidth().padding(16.dp)) }
                        }
                    }
                }
            }
        }
    }

    // ------------- Dialog & menu aksi -------------
    when (val p = pending) {
        null -> Unit
        is Pending.FileMenu -> ActionSheet(
            title = p.file.name,
            actions = listOf(
                "Buka" to { onOpenFile(p.file) },
                "Unduh ke perangkat" to { vm.download(p.file) },
                "Ganti nama" to { pending = Pending.RenameFile(p.file) },
                "Pindahkan" to { pending = Pending.MoveFile(p.file) },
                "Salin" to { pending = Pending.CopyFile(p.file) },
                "Info" to { pending = Pending.Info(p.file) },
                "Hapus" to { pending = Pending.DeleteFile(p.file) },
            ),
            onDismiss = { if (pending == p) pending = null },
        )
        is Pending.FolderMenu -> ActionSheet(
            title = p.folder.name,
            actions = listOf(
                "Buka" to { onOpenFolder(p.folder.id) },
                "Ganti nama" to { pending = Pending.RenameFolder(p.folder) },
                "Pindahkan" to { pending = Pending.MoveFolder(p.folder) },
                "Hapus" to { pending = Pending.DeleteFolder(p.folder) },
            ),
            onDismiss = { if (pending == p) pending = null },
        )
        is Pending.RenameFile -> TextInputDialog(
            title = "Ganti nama", initial = p.file.name, label = "Nama file", confirmText = "Simpan",
            onConfirm = { vm.renameFile(p.file, it); pending = null }, onDismiss = { pending = null },
        )
        is Pending.RenameFolder -> TextInputDialog(
            title = "Ganti nama folder", initial = p.folder.name, label = "Nama folder", confirmText = "Simpan",
            onConfirm = { vm.renameFolder(p.folder, it); pending = null }, onDismiss = { pending = null },
        )
        Pending.NewFolder -> TextInputDialog(
            title = "Folder baru", initial = "", label = "Nama folder", confirmText = "Buat",
            onConfirm = { vm.createFolder(it); pending = null }, onDismiss = { pending = null },
        )
        is Pending.MoveFile -> FolderPickerDialog("Pindahkan ke", "Pindahkan",
            onPick = { vm.moveFile(p.file, it) }, onDismiss = { pending = null })
        is Pending.CopyFile -> FolderPickerDialog("Salin ke", "Salin",
            onPick = { vm.copyFile(p.file, it) }, onDismiss = { pending = null })
        is Pending.MoveFolder -> FolderPickerDialog("Pindahkan folder ke", "Pindahkan", excludeId = p.folder.id,
            onPick = { vm.moveFolder(p.folder, it) }, onDismiss = { pending = null })
        is Pending.DeleteFile -> ConfirmDialog(
            title = "Hapus file?", message = "\"${p.file.name}\" akan dipindahkan ke trash dan masih bisa dipulihkan.",
            confirmText = "Hapus", onConfirm = { vm.deleteFile(p.file) }, onDismiss = { pending = null }, destructive = true,
        )
        is Pending.DeleteFolder -> ConfirmDialog(
            title = "Hapus folder?", message = "Folder \"${p.folder.name}\" beserta isinya akan dipindahkan ke trash.",
            confirmText = "Hapus", onConfirm = { vm.deleteFolder(p.folder) }, onDismiss = { pending = null }, destructive = true,
        )
        is Pending.Info -> FileInfoDialog(p.file, onDismiss = { pending = null })
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ActionSheet(title: String, actions: List<Pair<String, () -> Unit>>, onDismiss: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Text(
            title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp),
        )
        actions.forEach { (label, action) ->
            ListItem(
                headlineContent = {
                    Text(label, color = if (label == "Hapus") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
                },
                // Tutup menu dulu, lalu jalankan aksi (aksi boleh membuka dialog lain).
                modifier = Modifier.clickable { onDismiss(); action() },
            )
        }
        Box(Modifier.padding(bottom = 24.dp))
    }
}

@Composable
private fun FileInfoDialog(file: FileOut, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Info file") },
        text = {
            Column {
                InfoRow("Nama", file.name)
                InfoRow("Ukuran", formatBytes(file.sizeBytes))
                InfoRow("Tipe", file.mimeType)
                if (file.width != null && file.height != null) InfoRow("Resolusi", "${file.width} × ${file.height}")
                InfoRow("Diunggah", formatDateTime(file.createdAt))
                InfoRow("SHA-256", file.sha256)
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("Tutup") } },
    )
}

@Composable
private fun InfoRow(label: String, value: String) {
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.outline)
        Text(value, style = MaterialTheme.typography.bodyMedium)
    }
}
