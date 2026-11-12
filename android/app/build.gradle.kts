import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Nomor versi dari CI (GITHUB_RUN_NUMBER) agar setiap APK bisa dipasang menimpa versi lama.
val ciBuildNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

// Kunci penandatanganan rilis: dari environment (secret CI) bila ada, selain itu kunci
// debug tetap yang disimpan di repo (agar update APK tetap bisa dipasang di atas versi lama).
val releaseSigning = Properties().apply {
    val path = System.getenv("ANDROID_KEYSTORE_PATH")
    if (path != null && file(path).exists()) {
        setProperty("storeFile", path)
        setProperty("storePassword", System.getenv("ANDROID_KEYSTORE_PASSWORD") ?: "")
        setProperty("keyAlias", System.getenv("ANDROID_KEY_ALIAS") ?: "")
        setProperty("keyPassword", System.getenv("ANDROID_KEY_PASSWORD") ?: "")
    }
}

// Build lokal di Raspberry Pi (ARM64): aapt2 versi Debian belum bisa membaca android.jar API 35,
// jadi kompilasi lokal memakai API 34 hanya untuk pemeriksaan cepat. Properti ini diset di
// ~/.gradle/gradle.properties mesin lokal, TIDAK di repo; APK resmi dibuat di CI dengan API 35.
val localArm64 = providers.gradleProperty("penyimpanan.localArm64").isPresent

android {
    namespace = "id.serverpenyimpanan.android"
    compileSdk = if (localArm64) 34 else 35

    defaultConfig {
        applicationId = "id.serverpenyimpanan.android"
        minSdk = 29
        targetSdk = 35
        versionCode = ciBuildNumber
        versionName = "1.0.$ciBuildNumber"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        getByName("debug") {
            storeFile = rootProject.file("signing/debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
        create("release") {
            if (releaseSigning.containsKey("storeFile")) {
                storeFile = file(releaseSigning.getProperty("storeFile"))
                storePassword = releaseSigning.getProperty("storePassword")
                keyAlias = releaseSigning.getProperty("keyAlias")
                keyPassword = releaseSigning.getProperty("keyPassword")
            } else {
                storeFile = rootProject.file("signing/debug.keystore")
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
        unitTests.all {
            // Tes integrasi dengan server asli hanya berjalan bila SP_SERVER_URL diisi (di CI).
            it.environment("SP_SERVER_URL", System.getenv("SP_SERVER_URL") ?: "")
            it.environment("SP_ADMIN_PASSWORD", System.getenv("SP_ADMIN_PASSWORD") ?: "")
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.splashscreen)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.icons.extended)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.work.runtime)
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.androidx.media3.ui)
    implementation(libs.androidx.media3.okhttp)
    implementation(libs.androidx.security.crypto)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)
    implementation(libs.retrofit)
    implementation(libs.retrofit.kotlinx.serialization)
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)
    debugImplementation(libs.androidx.compose.ui.tooling)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}

if (localArm64) {
    // Library androidx terbaru meminta compileSdk 35; pada build lokal API 34 cek ini dilewati.
    tasks.matching { it.name.startsWith("check") && it.name.endsWith("AarMetadata") }.configureEach { enabled = false }
}
