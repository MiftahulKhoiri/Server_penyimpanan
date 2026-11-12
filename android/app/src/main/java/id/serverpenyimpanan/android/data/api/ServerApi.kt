package id.serverpenyimpanan.android.data.api

import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.HTTP
import retrofit2.http.Header
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming

/** Endpoint Server_penyimpanan (lihat docs/API.md). Base URL = "<server>/api/v1/". */
interface ServerApi {
    // ---------------- Auth ----------------
    @POST("auth/login")
    suspend fun login(@Body body: LoginRequest): TokenResponse

    @POST("auth/refresh")
    suspend fun refresh(@Body body: RefreshRequest): TokenResponse

    @POST("auth/logout")
    suspend fun logout(): MessageResponse

    @POST("auth/logout-all")
    suspend fun logoutAll(): MessageResponse

    @GET("auth/me")
    suspend fun me(): UserOut

    @POST("auth/change-password")
    suspend fun changePassword(@Body body: ChangePasswordRequest): TokenResponse

    @GET("auth/sessions")
    suspend fun sessions(): List<SessionOut>

    @DELETE("auth/sessions/{id}")
    suspend fun revokeSession(@Path("id") id: String): MessageResponse

    // ---------------- File ----------------
    @GET("files")
    suspend fun listFiles(
        @Query("folder_id") folderId: String? = null,
        @Query("q") query: String? = null,
        @Query("media_type") mediaType: String? = null,
        @Query("sort") sort: String = "name",
        @Query("order") order: String = "asc",
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 100,
    ): FileList

    @POST("files")
    suspend fun uploadSimple(
        @Query("filename") filename: String,
        @Query("folder_id") folderId: String?,
        @Header("X-Content-SHA256") sha256: String?,
        @Body body: RequestBody,
    ): FileOut

    @GET("files/{id}")
    suspend fun getFile(@Path("id") id: String): FileOut

    @Streaming
    @GET("files/{id}/download")
    suspend fun download(@Path("id") id: String, @Header("Range") range: String? = null): Response<ResponseBody>

    @PATCH("files/{id}/rename")
    suspend fun renameFile(@Path("id") id: String, @Body body: RenameRequest): FileOut

    @POST("files/{id}/move")
    suspend fun moveFile(@Path("id") id: String, @Body body: MoveFileRequest): FileOut

    @POST("files/{id}/copy")
    suspend fun copyFile(@Path("id") id: String, @Body body: CopyFileRequest): FileOut

    @DELETE("files/{id}")
    suspend fun deleteFile(@Path("id") id: String): MessageResponse

    // ---------------- Folder ----------------
    @GET("folders")
    suspend fun listFolders(@Query("parent_id") parentId: String? = null): List<FolderOut>

    @POST("folders")
    suspend fun createFolder(@Body body: FolderCreate): FolderOut

    @GET("folders/{id}")
    suspend fun getFolder(@Path("id") id: String): FolderDetail

    @PATCH("folders/{id}/rename")
    suspend fun renameFolder(@Path("id") id: String, @Body body: RenameRequest): FolderOut

    @POST("folders/{id}/move")
    suspend fun moveFolder(@Path("id") id: String, @Body body: FolderMoveRequest): FolderOut

    @DELETE("folders/{id}")
    suspend fun deleteFolder(@Path("id") id: String): MessageResponse

    // ---------------- Upload chunked ----------------
    @POST("uploads/init")
    suspend fun initUpload(@Body body: UploadInitRequest): UploadOut

    @GET("uploads")
    suspend fun activeUploads(): List<UploadOut>

    @PUT("uploads/{id}/chunks/{index}")
    suspend fun uploadChunk(
        @Path("id") uploadId: String,
        @Path("index") index: Int,
        @Header("X-Chunk-SHA256") sha256: String?,
        @Body body: RequestBody,
    ): ChunkResult

    @GET("uploads/{id}/status")
    suspend fun uploadStatus(@Path("id") uploadId: String): UploadOut

    @POST("uploads/{id}/complete")
    suspend fun completeUpload(@Path("id") uploadId: String): UploadCompleteOut

    @POST("uploads/{id}/cancel")
    suspend fun cancelUpload(@Path("id") uploadId: String): UploadOut

    // ---------------- Media ----------------
    @GET("media/photos")
    suspend fun photos(
        @Query("folder_id") folderId: String? = null,
        @Query("sort") sort: String = "created_at",
        @Query("order") order: String = "desc",
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 60,
    ): FileList

    @GET("media/videos")
    suspend fun videos(
        @Query("folder_id") folderId: String? = null,
        @Query("sort") sort: String = "created_at",
        @Query("order") order: String = "desc",
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 60,
    ): FileList

    @GET("media/{id}")
    suspend fun mediaInfo(@Path("id") id: String): FileOut

    // ---------------- Trash ----------------
    @GET("trash")
    suspend fun trash(@Query("page") page: Int = 1, @Query("page_size") pageSize: Int = 100): TrashList

    @POST("trash/{id}/restore")
    suspend fun restore(@Path("id") id: String): RestoreResult

    @POST("trash/{id}/delete")
    suspend fun purge(@Path("id") id: String): PurgeResult

    @HTTP(method = "DELETE", path = "trash")
    suspend fun emptyTrash(): EmptyTrashResult

    // ---------------- Storage, aktivitas, sistem ----------------
    @GET("storage/status")
    suspend fun storageStatus(): StorageStatusOut

    @GET("activity")
    suspend fun activity(@Query("page") page: Int = 1, @Query("page_size") pageSize: Int = 50): ActivityList

    @GET("system/health")
    suspend fun health(): HealthOut

    // ---------------- Admin ----------------
    @GET("system/status")
    suspend fun systemStatus(): SystemStatusOut

    @GET("system/statistics")
    suspend fun statistics(): StatisticsOut

    @GET("storage/ssd")
    suspend fun ssd(): DeviceOut

    @GET("storage/hdd")
    suspend fun hdd(): DeviceOut

    @POST("storage/backup")
    suspend fun startBackup(@Body body: BackupStartRequest): BackupJobOut

    @GET("storage/backup")
    suspend fun backups(@Query("page") page: Int = 1, @Query("page_size") pageSize: Int = 20): BackupJobList

    @GET("storage/backup/{id}")
    suspend fun backup(@Path("id") id: Int): BackupJobOut

    @GET("admin/users")
    suspend fun users(
        @Query("search") search: String? = null,
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 100,
    ): UserList

    @POST("admin/users")
    suspend fun createUser(@Body body: AdminUserCreate): UserOut

    @PATCH("admin/users/{id}")
    suspend fun updateUser(@Path("id") id: Int, @Body body: AdminUserUpdate): UserOut

    @DELETE("admin/users/{id}")
    suspend fun deleteUser(@Path("id") id: Int): MessageResponse

    @GET("admin/activity")
    suspend fun allActivity(
        @Query("user_id") userId: Int? = null,
        @Query("page") page: Int = 1,
        @Query("page_size") pageSize: Int = 50,
    ): ActivityList
}
