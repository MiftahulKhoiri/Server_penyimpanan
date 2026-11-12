package id.serverpenyimpanan.android.ui.files

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.FolderOut
import id.serverpenyimpanan.android.data.api.apiCall

/**
 * Pilih folder tujuan (untuk pindah/salin). [excludeId] = folder yang sedang dipindah,
 * agar tidak bisa dipilih sebagai tujuan dirinya sendiri.
 */
@Composable
fun FolderPickerDialog(
    title: String,
    confirmText: String,
    excludeId: String? = null,
    onPick: (folderId: String?) -> Unit,
    onDismiss: () -> Unit,
) {
    val api = LocalContext.current.container.client().api
    val stack = remember { mutableStateListOf<FolderOut>() } // jalur dari root
    var folders by remember { mutableStateOf<List<FolderOut>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val current = stack.lastOrNull()

    LaunchedEffect(current?.id) {
        folders = null
        error = null
        try {
            folders = apiCall { api.listFolders(current?.id) }.filter { it.id != excludeId }.sortedBy { it.name.lowercase() }
        } catch (e: ApiException) {
            error = e.message
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            ListItem(
                headlineContent = { Text(title) },
                supportingContent = { Text("/" + stack.joinToString("/") { it.name }) },
                leadingContent = if (stack.isNotEmpty()) {
                    { IconButton(onClick = { stack.removeAt(stack.lastIndex) }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Naik") } }
                } else null,
            )
        },
        text = {
            Box(Modifier.fillMaxWidth().heightIn(min = 120.dp, max = 360.dp)) {
                val list = folders
                when {
                    error != null -> Text(error.orEmpty(), color = MaterialTheme.colorScheme.error)
                    list == null -> CircularProgressIndicator(Modifier.align(Alignment.Center))
                    list.isEmpty() -> Text("Tidak ada subfolder", color = MaterialTheme.colorScheme.outline, modifier = Modifier.padding(8.dp))
                    else -> LazyColumn {
                        items(list, key = { it.id }) { folder ->
                            ListItem(
                                headlineContent = { Text(folder.name) },
                                leadingContent = { Icon(Icons.Filled.Folder, null, tint = MaterialTheme.colorScheme.primary) },
                                modifier = Modifier.clickable { stack.add(folder) },
                            )
                        }
                    }
                }
            }
        },
        confirmButton = { Button(onClick = { onPick(current?.id); onDismiss() }) { Text(confirmText) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Batal") } },
    )
}
