package id.serverpenyimpanan.android.ui.media

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material.icons.filled.PlayCircle
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.ui.common.EmptyBox
import id.serverpenyimpanan.android.ui.common.ErrorBox
import id.serverpenyimpanan.android.ui.common.FileThumbnail
import id.serverpenyimpanan.android.ui.common.LoadingBox
import id.serverpenyimpanan.android.ui.common.appViewModel
import id.serverpenyimpanan.android.ui.common.formatDate
import kotlinx.coroutines.launch

/**
 * Daftar media (foto/video) dengan paginasi. Juga dipakai penampil foto sebagai sumber
 * daftar geser (pager), sehingga satu instance dibagi per jenis media.
 */
class MediaListViewModel(private val app: AppContainer, val video: Boolean) : ViewModel() {
    var items by mutableStateOf<List<FileOut>>(emptyList())
        private set
    var total by mutableIntStateOf(0)
        private set
    var loading by mutableStateOf(true)
        private set
    var error by mutableStateOf<ApiException?>(null)
        private set
    private var page = 0
    private var loadingMore = false

    init { refresh() }

    private suspend fun fetch(p: Int) = app.client().api.let { api ->
        apiCall { if (video) api.videos(page = p, pageSize = PAGE) else api.photos(page = p, pageSize = PAGE) }
    }

    fun refresh() {
        loading = true
        error = null
        viewModelScope.launch {
            try {
                val list = fetch(1)
                items = list.items
                total = list.total
                page = 1
            } catch (e: ApiException) {
                error = e
            }
            loading = false
        }
    }

    fun loadMore() {
        if (loading || loadingMore || items.size >= total) return
        loadingMore = true
        viewModelScope.launch {
            try {
                val list = fetch(page + 1)
                items = items + list.items
                total = list.total
                page += 1
            } catch (_: ApiException) {
            }
            loadingMore = false
        }
    }

    companion object { const val PAGE = 120 }
}

@Composable
fun mediaListViewModel(video: Boolean): MediaListViewModel =
    appViewModel(key = if (video) "media-video" else "media-photo") { MediaListViewModel(it, video) }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GalleryScreen(onOpenPhoto: (index: Int) -> Unit, onOpenVideo: (FileOut) -> Unit) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    Scaffold(topBar = { TopAppBar(title = { Text("Galeri") }) }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            PrimaryTabRow(selectedTabIndex = tab) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Foto") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Video") })
            }
            val vm = mediaListViewModel(video = tab == 1)
            MediaGrid(vm, onClick = { index, file -> if (tab == 0) onOpenPhoto(index) else onOpenVideo(file) })
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MediaGrid(vm: MediaListViewModel, onClick: (Int, FileOut) -> Unit) {
    val client = LocalContext.current.container.client()
    val error = vm.error
    when {
        vm.loading && vm.items.isEmpty() -> LoadingBox()
        error != null && vm.items.isEmpty() -> ErrorBox(error.message ?: "Gagal memuat", onRetry = vm::refresh)
        else -> PullToRefreshBox(isRefreshing = vm.loading, onRefresh = vm::refresh, modifier = Modifier.fillMaxSize()) {
            if (vm.items.isEmpty()) {
                EmptyBox(Icons.Filled.PhotoLibrary, if (vm.video) "Belum ada video" else "Belum ada foto")
            } else {
                val grid = rememberLazyGridState()
                val nearEnd by remember {
                    derivedStateOf {
                        (grid.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0) >= grid.layoutInfo.totalItemsCount - 30
                    }
                }
                LaunchedEffect(nearEnd, vm.items.size) { if (nearEnd) vm.loadMore() }
                // Kelompokkan per tanggal unggah (server mengurutkan terbaru dulu).
                val groups = remember(vm.items) { vm.items.withIndex().groupBy { formatDate(it.value.createdAt) } }
                LazyVerticalGrid(
                    columns = GridCells.Adaptive(110.dp),
                    state = grid,
                    contentPadding = PaddingValues(2.dp),
                    modifier = Modifier.fillMaxSize(),
                ) {
                    groups.forEach { (date, entries) ->
                        item(span = { GridItemSpan(maxLineSpan) }, key = "h-$date") {
                            Text(date, style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(8.dp, 12.dp, 8.dp, 4.dp))
                        }
                        items(entries, key = { it.value.id }) { (index, file) ->
                            Box(
                                Modifier.padding(2.dp).aspectRatio(1f).clip(RoundedCornerShape(4.dp))
                                    .clickable { onClick(index, file) },
                            ) {
                                FileThumbnail(file, client.thumbnailUrl(file.id), Modifier.fillMaxSize(), iconSize = 36)
                                if (vm.video) {
                                    Icon(
                                        Icons.Filled.PlayCircle, null, tint = Color.White,
                                        modifier = Modifier.align(Alignment.Center).fillMaxWidth(0.35f).aspectRatio(1f),
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
