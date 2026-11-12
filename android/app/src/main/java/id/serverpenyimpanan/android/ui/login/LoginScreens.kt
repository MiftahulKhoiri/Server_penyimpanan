package id.serverpenyimpanan.android.ui.login

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import id.serverpenyimpanan.android.AppContainer
import id.serverpenyimpanan.android.data.api.ApiException
import id.serverpenyimpanan.android.data.api.LoginRequest
import id.serverpenyimpanan.android.data.api.apiCall
import id.serverpenyimpanan.android.data.auth.Session
import id.serverpenyimpanan.android.data.settings.SettingsStore
import id.serverpenyimpanan.android.ui.common.appViewModel
import kotlinx.coroutines.launch

class LoginViewModel(private val app: AppContainer) : ViewModel() {
    var busy by mutableStateOf(false)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    val serverUrl: String? get() = app.settings.serverUrl.value

    /** Pesan bila sebelumnya dikeluarkan paksa (sesi dicabut, akun dinonaktifkan, dsb.). */
    val logoutMessage: String? = when (app.tokens.lastLogoutReason) {
        "session_revoked" -> "Sesi Anda dicabut (logout dari perangkat lain atau password diganti). Silakan login lagi."
        "account_inactive" -> "Akun Anda dinonaktifkan oleh administrator."
        "session_expired" -> "Sesi berakhir. Silakan login lagi."
        else -> null
    }

    fun saveServer(input: String, onDone: () -> Unit) {
        val url = SettingsStore.normalizeServerUrl(input)
        if (url == null) {
            error = "Alamat server tidak valid. Contoh: 192.168.1.10:8000"
            return
        }
        busy = true
        error = null
        viewModelScope.launch {
            try {
                app.settings.setServerUrl(url)
                apiCall { app.client().api.health() }
                onDone()
            } catch (e: ApiException) {
                // 503 berarti server hidup tetapi SSD/DB bermasalah: tetap lanjut ke login.
                if (e.status == 503) onDone() else error = e.message
            } finally {
                busy = false
            }
        }
    }

    fun login(username: String, password: String, onDone: () -> Unit) {
        busy = true
        error = null
        viewModelScope.launch {
            try {
                val api = app.client().api
                val name = username.trim()
                val tokens = apiCall { api.login(LoginRequest(name, password, app.settings.deviceName)) }
                // Simpan token dulu agar /auth/me bisa dipanggil, lalu lengkapi data pengguna.
                app.tokens.save(Session(tokens.accessToken, tokens.refreshToken, tokens.sessionId, name, 0, "user"))
                val me = apiCall { api.me() }
                val latest = app.tokens.current() ?: error("sesi hilang")
                app.tokens.save(latest.copy(username = me.username, userId = me.id, role = me.role))
                onDone()
            } catch (e: ApiException) {
                app.tokens.clear()
                error = when (e.code) {
                    "too_many_attempts" -> "Terlalu banyak percobaan gagal. Coba lagi dalam ${(e.retryAfterSeconds ?: 60) / 60 + 1} menit."
                    else -> e.message
                }
            } finally {
                busy = false
            }
        }
    }
}

@Composable
fun ServerSetupScreen(onSaved: () -> Unit) {
    val vm = appViewModel { LoginViewModel(it) }
    var url by rememberSaveable { mutableStateOf(vm.serverUrl ?: "") }
    AuthLayout(subtitle = "Masukkan alamat server Raspberry Pi Anda") {
        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("Alamat server") },
            placeholder = { Text("192.168.1.10:8000") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Go),
            keyboardActions = KeyboardActions(onGo = { vm.saveServer(url, onSaved) }),
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "Gunakan IP lokal di jaringan rumah, atau IP Tailscale bila dari luar rumah.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
        ErrorText(vm.error)
        Button(onClick = { vm.saveServer(url, onSaved) }, enabled = !vm.busy && url.isNotBlank(), modifier = Modifier.fillMaxWidth()) {
            if (vm.busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp) else Text("Hubungkan")
        }
    }
}

@Composable
fun LoginScreen(onLoggedIn: () -> Unit, onChangeServer: () -> Unit) {
    val vm = appViewModel { LoginViewModel(it) }
    var username by rememberSaveable { mutableStateOf("") }
    var password by rememberSaveable { mutableStateOf("") }
    var showPassword by rememberSaveable { mutableStateOf(false) }
    AuthLayout(subtitle = vm.serverUrl ?: "") {
        vm.logoutMessage?.let {
            Text(it, color = MaterialTheme.colorScheme.tertiary, style = MaterialTheme.typography.bodyMedium)
        }
        OutlinedTextField(
            value = username,
            onValueChange = { username = it },
            label = { Text("Username") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            singleLine = true,
            visualTransformation = if (showPassword) VisualTransformation.None else PasswordVisualTransformation(),
            trailingIcon = {
                IconButton(onClick = { showPassword = !showPassword }) {
                    Icon(if (showPassword) Icons.Filled.VisibilityOff else Icons.Filled.Visibility, "Tampilkan password")
                }
            },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { vm.login(username, password, onLoggedIn) }),
            modifier = Modifier.fillMaxWidth(),
        )
        ErrorText(vm.error)
        Button(
            onClick = { vm.login(username, password, onLoggedIn) },
            enabled = !vm.busy && username.isNotBlank() && password.isNotEmpty(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            if (vm.busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp) else Text("Masuk")
        }
        TextButton(onClick = onChangeServer) { Text("Ganti alamat server") }
    }
}

@Composable
private fun ErrorText(message: String?) {
    if (message != null) Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
}

@Composable
private fun AuthLayout(subtitle: String, content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxSize().imePadding().verticalScroll(rememberScrollState()).padding(28.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(40.dp))
        Icon(Icons.Filled.Cloud, null, Modifier.size(72.dp), tint = MaterialTheme.colorScheme.primary)
        Text("Penyimpanan", style = MaterialTheme.typography.headlineMedium)
        Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.outline, textAlign = TextAlign.Center)
        Spacer(Modifier.height(8.dp))
        content()
    }
}
