package id.serverpenyimpanan.android.ui

import android.net.Uri
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material.icons.filled.SwapVert
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.viewmodel.compose.LocalViewModelStoreOwner
import androidx.navigation.NavBackStackEntry
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.FileOut
import id.serverpenyimpanan.android.ui.account.AccountScreen
import id.serverpenyimpanan.android.ui.account.ActivityScreen
import id.serverpenyimpanan.android.ui.account.ChangePasswordScreen
import id.serverpenyimpanan.android.ui.account.SessionsScreen
import id.serverpenyimpanan.android.ui.admin.AdminScreen
import id.serverpenyimpanan.android.ui.common.toast
import id.serverpenyimpanan.android.ui.files.FilesScreen
import id.serverpenyimpanan.android.ui.login.LoginScreen
import id.serverpenyimpanan.android.ui.login.ServerSetupScreen
import id.serverpenyimpanan.android.ui.media.GalleryScreen
import id.serverpenyimpanan.android.ui.media.PhotoViewerScreen
import id.serverpenyimpanan.android.ui.media.VideoPlayerScreen
import id.serverpenyimpanan.android.ui.media.mediaListViewModel
import id.serverpenyimpanan.android.ui.transfers.TransfersScreen
import id.serverpenyimpanan.android.ui.trash.TrashScreen

/** Gerbang utama: setelan server → login → aplikasi. */
@Composable
fun AppRoot() {
    val container = LocalContext.current.container
    val serverUrl by container.settings.serverUrl.collectAsState()
    val session by container.tokens.session.collectAsState()
    var editingServer by rememberSaveable { mutableStateOf(false) }

    when {
        serverUrl == null || editingServer -> ServerSetupScreen(onSaved = { editingServer = false })
        session == null -> LoginScreen(onLoggedIn = {}, onChangeServer = { editingServer = true })
        // key: navigasi & ViewModel di-reset bila pengguna/server berganti.
        else -> key(session?.userId, serverUrl) {
            MainNav(onChangeServer = { editingServer = true })
        }
    }
}

private enum class Tab(val route: String, val label: String, val icon: ImageVector) {
    FILES("files", "Berkas", Icons.Filled.Folder),
    GALLERY("gallery", "Galeri", Icons.Filled.PhotoLibrary),
    TRANSFERS("transfers", "Transfer", Icons.Filled.SwapVert),
    ACCOUNT("account", "Akun", Icons.Filled.Person),
}

@Composable
private fun MainNav(onChangeServer: () -> Unit) {
    val nav = rememberNavController()
    val context = LocalContext.current
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route
    val showBar = Tab.entries.any { it.route == route }
    // Daftar foto untuk penampil yang dibuka dari layar Berkas (bukan dari galeri).
    var looseList by remember { mutableStateOf<List<FileOut>>(emptyList()) }

    val openFile: (FileOut) -> Unit = { file ->
        when {
            file.isImage -> {
                looseList = listOf(file)
                nav.navigate("photo/loose/0")
            }
            file.isVideo || file.isAudio -> nav.navigate("video/${file.id}?title=${Uri.encode(file.name)}")
            else -> {
                context.container.transfers.enqueueDownload(file)
                context.toast("Mengunduh \"${file.name}\" ke Download/Penyimpanan")
            }
        }
    }

    Scaffold(
        bottomBar = {
            if (showBar) {
                NavigationBar {
                    Tab.entries.forEach { tab ->
                        NavigationBarItem(
                            selected = route == tab.route,
                            onClick = { nav.switchTab(tab.route) },
                            icon = { Icon(tab.icon, null) },
                            label = { Text(tab.label) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        // Layar anak punya Scaffold sendiri; hanya padding bawah (bottom bar) yang diteruskan.
        NavHost(nav, startDestination = Tab.FILES.route, modifier = Modifier.padding(bottom = padding.calculateBottomPadding())) {
            composable(Tab.FILES.route) {
                FilesScreen(null, onOpenFolder = { nav.navigate("folder/$it") }, onOpenFile = openFile, onBack = null)
            }
            composable("folder/{id}") { e ->
                FilesScreen(
                    e.arguments?.getString("id"),
                    onOpenFolder = { nav.navigate("folder/$it") },
                    onOpenFile = openFile,
                    onBack = { nav.popBackStack() },
                )
            }
            composable(Tab.GALLERY.route) {
                GalleryScreen(
                    onOpenPhoto = { index -> nav.navigate("photo/gallery/$index") },
                    onOpenVideo = { f -> nav.navigate("video/${f.id}?title=${Uri.encode(f.name)}") },
                )
            }
            composable("photo/{source}/{index}") { e ->
                val index = e.arguments?.getString("index")?.toIntOrNull() ?: 0
                val galleryEntry = remember(e) {
                    if (e.arguments?.getString("source") == "gallery") {
                        runCatching { nav.getBackStackEntry(Tab.GALLERY.route) }.getOrNull()
                    } else {
                        null
                    }
                }
                val photos = if (galleryEntry != null) galleryPhotos(galleryEntry) else looseList
                PhotoViewerScreen(photos, index, onBack = { nav.popBackStack() })
            }
            composable("video/{id}?title={title}") { e ->
                VideoPlayerScreen(
                    fileId = e.arguments?.getString("id").orEmpty(),
                    title = e.arguments?.getString("title").orEmpty(),
                    onBack = { nav.popBackStack() },
                )
            }
            composable(Tab.TRANSFERS.route) { TransfersScreen() }
            composable(Tab.ACCOUNT.route) {
                AccountScreen(
                    onOpenSessions = { nav.navigate("sessions") },
                    onOpenActivity = { nav.navigate("activity") },
                    onOpenPassword = { nav.navigate("password") },
                    onOpenTrash = { nav.navigate("trash") },
                    onOpenAdmin = { nav.navigate("admin") },
                    onChangeServer = onChangeServer,
                )
            }
            composable("sessions") { SessionsScreen(onBack = { nav.popBackStack() }) }
            composable("activity") { ActivityScreen(onBack = { nav.popBackStack() }) }
            composable("password") { ChangePasswordScreen(onBack = { nav.popBackStack() }) }
            composable("trash") { TrashScreen(onBack = { nav.popBackStack() }) }
            composable("admin") { AdminScreen(onBack = { nav.popBackStack() }) }
        }
    }
}

/** Daftar foto dari ViewModel galeri (tersimpan di back stack entry galeri) agar bisa digeser. */
@Composable
private fun galleryPhotos(owner: NavBackStackEntry): List<FileOut> {
    var list: List<FileOut> = emptyList()
    CompositionLocalProvider(LocalViewModelStoreOwner provides owner) {
        list = mediaListViewModel(video = false).items
    }
    return list
}

private fun NavHostController.switchTab(route: String) {
    navigate(route) {
        popUpTo(graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}
