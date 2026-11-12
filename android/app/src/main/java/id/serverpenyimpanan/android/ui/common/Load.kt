package id.serverpenyimpanan.android.ui.common

import android.content.Context
import android.widget.Toast
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.CreationExtras
import androidx.lifecycle.viewmodel.compose.viewModel
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.container
import id.serverpenyimpanan.android.data.api.ApiException

/** Status pemuatan data untuk layar. */
sealed interface Load<out T> {
    data object Loading : Load<Nothing>
    data class Ready<T>(val data: T) : Load<T>
    data class Failed(val error: ApiException) : Load<Nothing>
}

val <T> Load<T>.dataOrNull: T? get() = (this as? Load.Ready<T>)?.data

/** viewModel() dengan akses ke AppContainer. */
@Composable
inline fun <reified VM : ViewModel> appViewModel(
    key: String? = null,
    crossinline create: (AppContainer) -> VM,
): VM {
    val container = LocalContext.current.container
    return viewModel(
        key = key,
        factory = object : ViewModelProvider.Factory {
            override fun <T : ViewModel> create(modelClass: Class<T>, extras: CreationExtras): T {
                @Suppress("UNCHECKED_CAST")
                return create(container) as T
            }
        },
    )
}

fun Context.toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
