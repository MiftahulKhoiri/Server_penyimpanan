package id.serverpenyimpanan.android.ui.files

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.CopyFileRequest
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.data.api.FolderCreate
import id.serverpenyimpanan.android.data.api.FolderDetail
import id.serverpenyimpanan.android.data.api.FolderMoveRequest
import id.serverpenyimpanan.android.data.api.FolderOut
import id.serverpenyimpanan.android.data.api.MoveFileRequest
import id.serverpenyimpanan.android.data.api.RenameRequest
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.data.upload.UriChunkSource
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch

enum class SortOption(val label: String, val field: String, val order: String) {
    NAME("Nama (A–Z)", "name", "asc"),
    NEWEST("Terbaru", "created_at", "desc"),
    OLDEST("Terlama", "created_at", "asc"),
    LARGEST("Terbesar", "size", "desc"),
    SMALLEST("Terkecil", "size", "asc"),
}

data class FilesState(
    val loading: Boolean = true,
    val error: ApiException? = null,
    val detail: FolderDetail? = null,
    val folders: List<FolderOut> = emptyList(),
    val files: List<FileOut> = emptyList(),
    val total: Int = 0,
    val page: Int = 1,
    val loadingMore: Boolean = false,
)

class FilesViewModel(private val app: AppContainer, val folderId: String?) : ViewModel() {
    var state by mutableStateOf(FilesState())
        private set
    var query by mutableStateOf("")
        private set
    var sort by mutableStateOf(SortOption.NAME)
        private set
    /** Pesan singkat untuk Snackbar (null setelah ditampilkan). */
    var message by mutableStateOf<String?>(null)

    private val api get() = app.client().api

    init {
        refresh()
    }

    fun refresh() {
        state = state.copy(loading = true, error = null)
        viewModelScope.launch {
            try {
                coroutineScope {
                    val searching = query.isNotBlank()
                    val detail = folderId?.let { async { apiCall { api.getFolder(it) } } }
                    val folders = async {
                        if (searching) emptyList() else apiCall { api.listFolders(folderId) }
                    }
                    val files = async {
                        apiCall {
                            api.listFiles(
                                folderId = if (searching) null else folderId,
                                query = query.ifBlank { null },
                                sort = sort.field, order = sort.order, page = 1, pageSize = PAGE,
                            )
                        }
                    }
                    val list = files.await()
                    state = FilesState(
                        loading = false,
                        detail = detail?.await(),
                        folders = sortFolders(folders.await()),
                        files = list.items,
                        total = list.total,
                        page = 1,
                    )
                }
            } catch (e: ApiException) {
                state = state.copy(loading = false, error = e)
            }
        }
    }

    fun loadMore() {
        val s = state
        if (s.loading || s.loadingMore || s.files.size >= s.total) return
        state = s.copy(loadingMore = true)
        viewModelScope.launch {
            try {
                val next = apiCall {
                    api.listFiles(
                        folderId = if (query.isNotBlank()) null else folderId,
                        query = query.ifBlank { null }, sort = sort.field, order = sort.order,
                        page = s.page + 1, pageSize = PAGE,
                    )
                }
                state = state.copy(files = state.files + next.items, page = s.page + 1, total = next.total, loadingMore = false)
            } catch (e: ApiException) {
                state = state.copy(loadingMore = false)
                message = e.message
            }
        }
    }

    fun search(text: String) {
        query = text
        refresh()
    }

    fun changeSort(option: SortOption) {
        sort = option
        refresh()
    }

    private fun sortFolders(list: List<FolderOut>) = when (sort) {
        SortOption.NEWEST -> list.sortedByDescending { it.createdAt }
        SortOption.OLDEST -> list.sortedBy { it.createdAt }
        else -> list.sortedBy { it.name.lowercase() }
    }

    // ---------------- Aksi ----------------
    private fun act(success: String, block: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                block()
                message = success
                refresh()
            } catch (e: ApiException) {
                message = e.message
            }
        }
    }

    fun createFolder(name: String) = act("Folder \"$name\" dibuat") { apiCall { api.createFolder(FolderCreate(name, folderId)) } }

    fun renameFile(file: FileOut, name: String) = act("Nama diganti") { apiCall { api.renameFile(file.id, RenameRequest(name)) } }

    fun renameFolder(folder: FolderOut, name: String) = act("Nama diganti") { apiCall { api.renameFolder(folder.id, RenameRequest(name)) } }

    fun moveFile(file: FileOut, target: String?) = act("Dipindahkan") { apiCall { api.moveFile(file.id, MoveFileRequest(target)) } }

    fun moveFolder(folder: FolderOut, target: String?) = act("Dipindahkan") { apiCall { api.moveFolder(folder.id, FolderMoveRequest(target)) } }

    fun copyFile(file: FileOut, target: String?) = act("Disalin") { apiCall { api.copyFile(file.id, CopyFileRequest(folderId = target)) } }

    fun deleteFile(file: FileOut) = act("\"${file.name}\" dipindahkan ke trash") { apiCall { api.deleteFile(file.id) } }

    fun deleteFolder(folder: FolderOut) = act("Folder \"${folder.name}\" dipindahkan ke trash") { apiCall { api.deleteFolder(folder.id) } }

    fun download(file: FileOut) {
        app.transfers.enqueueDownload(file)
        message = "Mengunduh \"${file.name}\" ke folder Download/Penyimpanan"
    }

    /** Upload file pilihan pengguna ke folder ini (berjalan di latar belakang). */
    fun upload(uris: List<Uri>, context: Context) {
        for (uri in uris) {
            // Izin baca permanen agar upload tetap bisa berjalan/dilanjutkan setelah aplikasi ditutup.
            runCatching { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            val name = UriChunkSource.queryName(context, uri)
            val size = UriChunkSource.querySize(context.contentResolver, uri)
            app.transfers.enqueueUpload(uri, name, folderId, size)
        }
        message = if (uris.size == 1) "Upload dimulai" else "${uris.size} file masuk antrean upload"
    }

    companion object {
        private const val PAGE = 100
    }
}
