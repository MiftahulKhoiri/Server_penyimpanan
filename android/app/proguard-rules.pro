# kotlinx.serialization: pertahankan serializer kelas DTO.
-keepattributes *Annotation*, InnerClasses, Signature, Exceptions
-dontnote kotlinx.serialization.**
-keepclassmembers @kotlinx.serialization.Serializable class ** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class id.serverpenyimpanan.android.**$$serializer { *; }
-keepclassmembers class id.serverpenyimpanan.android.** {
    *** Companion;
}

# Retrofit: interface API dipakai lewat refleksi.
-keep,allowobfuscation interface id.serverpenyimpanan.android.data.api.ServerApi
-keep,allowobfuscation,allowshrinking class kotlin.coroutines.Continuation
-keep,allowobfuscation,allowshrinking class retrofit2.Response
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
