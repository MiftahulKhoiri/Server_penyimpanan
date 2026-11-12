package id.serverpenyimpanan.android.data.api

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

// DTO sesuai skema server (docs/openapi.json). Nama field Kotlin camelCase dipetakan ke
// snake_case JSON oleh JsonNamingStrategy.SnakeCase (lihat ApiJson).

// ---------------- Auth ----------------
@Serializable
data class LoginRequest(val username: String, val password: String, val deviceName: String? = null)

@Serializable
data class RefreshRequest(val refreshToken: String)

@Serializable
data class ChangePasswordRequest(val currentPassword: String, val newPassword: String)

@Serializable
data class TokenResponse(
    val accessToken: String,
    val refreshToken: String,
    val tokenType: String = "bearer",
    val expiresIn: Int,
    val refreshExpiresIn: Int,
    val sessionId: String,
)

@Serializable
data class UserOut(
    val id: Int,
    val username: String,
    val email: String? = null,
    val role: String,
    val isActive: Boolean,
    val storageQuotaBytes: Long? = null,
    val createdAt: String,
    val lastLoginAt: String? = null,
) {
    val isAdmin: Boolean get() = role == "admin"
}

@Serializable
data class SessionOut(
    val id: String,
    val deviceName: String? = null,
    val ipAddress: String? = null,
    val userAgent: String? = null,
    val createdAt: String,
    val lastUsedAt: String? = null,
    val expiresAt: String,
    val current: Boolean = false,
)

@Serializable
data class MessageResponse(val detail: String)

// ---------------- File & folder ----------------
@Serializable
data class FileOut(
    val id: String,
    val name: String,
    val folderId: String? = null,
    val extension: String? = null,
    val mimeType: String,
    val mediaType: String,
    val sizeBytes: Long,
    val sha256: String,
    val width: Int? = null,
    val height: Int? = null,
    val durationSeconds: Double? = null,
    val hasThumbnail: Boolean = false,
    val createdAt: String,
    val updatedAt: String,
) {
    val isImage: Boolean get() = mediaType == "image"
    val isVideo: Boolean get() = mediaType == "video"
    val isAudio: Boolean get() = mediaType == "audio"
    val canThumbnail: Boolean get() = (isImage && mimeType != "image/svg+xml") || isVideo
}

@Serializable
data class FileList(val items: List<FileOut>, val total: Int, val page: Int, val pageSize: Int)

@Serializable
data class FolderOut(
    val id: String,
    val name: String,
    val parentId: String? = null,
    val createdAt: String,
    val updatedAt: String,
)

@Serializable
data class BreadcrumbItem(val id: String, val name: String)

@Serializable
data class FolderDetail(
    val id: String,
    val name: String,
    val parentId: String? = null,
    val createdAt: String,
    val updatedAt: String,
    val path: List<BreadcrumbItem>,
    val folders: List<FolderOut>,
    val fileCount: Int,
    val totalSizeBytes: Long,
)

@Serializable
data class FolderCreate(val name: String, val parentId: String? = null)

@Serializable
data class RenameRequest(val name: String)

@Serializable
data class MoveFileRequest(val folderId: String?)

@Serializable
data class CopyFileRequest(val folderId: String? = null, val name: String? = null)

@Serializable
data class FolderMoveRequest(val parentId: String?)

// ---------------- Upload chunked ----------------
@Serializable
data class UploadInitRequest(
    val filename: String,
    val totalSize: Long,
    val chunkSize: Int? = null,
    val folderId: String? = null,
    val sha256: String? = null,
    val mimeType: String? = null,
)

@Serializable
data class UploadOut(
    val id: String,
    val filename: String,
    val folderId: String? = null,
    val totalSize: Long,
    val chunkSize: Int,
    val totalChunks: Int,
    val status: String,
    val receivedBytes: Long,
    val receivedChunks: List<Int>,
    val missingChunks: Int,
    val expectedSha256: String? = null,
    val fileId: String? = null,
    val errorMessage: String? = null,
    val createdAt: String,
    val expiresAt: String,
    val duplicateFileId: String? = null,
)

@Serializable
data class ChunkResult(
    val uploadId: String,
    val chunkIndex: Int,
    val sizeBytes: Long,
    val sha256: String,
    val receivedBytes: Long,
    val receivedChunksCount: Int,
    val totalChunks: Int,
    val allReceived: Boolean,
)

@Serializable
data class UploadCompleteOut(val upload: UploadOut, val file: FileOut)

// ---------------- Trash ----------------
@Serializable
data class TrashItemOut(
    val id: String,
    val type: String,
    val name: String,
    val originalFolderId: String? = null,
    val sizeBytes: Long,
    val deletedAt: String,
    val purgeAfter: String,
)

@Serializable
data class TrashList(
    val items: List<TrashItemOut>,
    val total: Int,
    val page: Int,
    val pageSize: Int,
    val retentionDays: Int,
)

@Serializable
data class RestoreResult(val type: String, val id: String, val name: String, val folderId: String? = null)

@Serializable
data class PurgeResult(val type: String, val id: String, val files: Int, val bytes: Long)

@Serializable
data class EmptyTrashResult(val purgedItems: Int)

// ---------------- Storage & sistem ----------------
@Serializable
data class DeviceSummary(
    val available: Boolean,
    val totalBytes: Long? = null,
    val freeBytes: Long? = null,
    val usedPercent: Double? = null,
)

@Serializable
data class UserUsage(
    val fileCount: Int,
    val usedBytes: Long,
    val trashBytes: Long,
    val quotaBytes: Long? = null,
    val quotaFreeBytes: Long? = null,
)

@Serializable
data class StorageStatusOut(val primary: DeviceSummary, val backup: DeviceSummary, val usage: UserUsage)

@Serializable
data class DeviceOut(
    val name: String,
    val role: String,
    val mountPoint: String,
    val storagePath: String,
    val mounted: Boolean,
    val available: Boolean,
    val reason: String? = null,
    val filesystem: String? = null,
    val device: String? = null,
    val totalBytes: Long? = null,
    val usedBytes: Long? = null,
    val freeBytes: Long? = null,
    val usedPercent: Double? = null,
)

@Serializable
data class CpuInfo(
    val percent: Double,
    val count: Int,
    val loadAverage: List<Double>? = null,
    val temperatureC: Double? = null,
)

@Serializable
data class MemoryInfo(
    val totalBytes: Long,
    val availableBytes: Long,
    val usedPercent: Double,
    val swapTotalBytes: Long,
    val swapUsedBytes: Long,
    val processRssBytes: Long,
)

@Serializable
data class SystemStatusOut(
    val status: String,
    val version: String,
    val python: String,
    val platform: String,
    val serverUptimeSeconds: Long,
    val systemUptimeSeconds: Long,
    val cpu: CpuInfo,
    val memory: MemoryInfo,
    val database: String,
    val storage: Map<String, DeviceOut>,
    val mediaTools: Map<String, Boolean>,
)

@Serializable
data class CountSize(val count: Int, val totalBytes: Long)

@Serializable
data class FileStats(val count: Int, val totalBytes: Long, val byMediaType: Map<String, CountSize>)

@Serializable
data class StatisticsOut(
    val users: Map<String, Int>,
    val files: FileStats,
    val folders: Int,
    val trash: Map<String, Long>,
    val activeUploads: Int,
    val last24h: Map<String, Int>,
)

@Serializable
data class HealthOut(
    val status: String,
    val database: String,
    val primaryStorage: String,
    val backupStorage: String,
)

// ---------------- Backup ----------------
@Serializable
data class BackupStartRequest(val verifyExisting: Boolean = false)

@Serializable
data class BackupJobOut(
    val id: Int,
    val status: String,
    val triggeredById: Int? = null,
    val createdAt: String,
    val startedAt: String? = null,
    val finishedAt: String? = null,
    val filesTotal: Int,
    val filesCopied: Int,
    val filesSkipped: Int,
    val filesFailed: Int,
    val bytesCopied: Long,
    val errorMessage: String? = null,
    val report: JsonObject? = null,
) {
    val isRunning: Boolean get() = status == "pending" || status == "running"
}

@Serializable
data class BackupJobList(val items: List<BackupJobOut>, val total: Int, val page: Int, val pageSize: Int)

// ---------------- Activity & admin ----------------
@Serializable
data class ActivityOut(
    val id: Int,
    val userId: Int? = null,
    val action: String,
    val targetType: String? = null,
    val targetId: String? = null,
    val ipAddress: String? = null,
    val userAgent: String? = null,
    val details: JsonObject? = null,
    val createdAt: String,
)

@Serializable
data class ActivityList(val items: List<ActivityOut>, val total: Int, val page: Int, val pageSize: Int)

@Serializable
data class UserList(val items: List<UserOut>, val total: Int, val page: Int, val pageSize: Int)

@Serializable
data class AdminUserCreate(
    val username: String,
    val password: String,
    val email: String? = null,
    val role: String = "user",
    val storageQuotaBytes: Long? = null,
)

/** Hanya field yang tidak null yang dikirim (explicitNulls = false). */
@Serializable
data class AdminUserUpdate(
    val role: String? = null,
    val isActive: Boolean? = null,
    val password: String? = null,
    val storageQuotaBytes: Long? = null,
)

/** Body error standar server: {"detail": ..., "code": ...}. */
@Serializable
data class ErrorBody(val detail: kotlinx.serialization.json.JsonElement? = null, val code: String? = null)
