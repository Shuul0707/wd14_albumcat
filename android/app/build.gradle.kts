plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.albumcat"
    compileSdk = 35
    // Pinned: AGP 8.7.2 otherwise asks for build-tools 34.0.0, and the SDK we
    // installed (tools/setup_android.py) carries 35.0.0. 35 >= 34, so this is
    // simply the version that is actually on disk.
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "com.albumcat"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "0.1.2"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
        buildConfig = true
    }
}

dependencies {
    // Walking the SAF tree the user picks. Tiny, and saves a lot of plumbing.
    implementation("androidx.documentfile:documentfile:1.0.1")
    // EXIF orientation only -- AndroidX ExifInterface cannot read or write XMP,
    // which is exactly why this app splices the XMP APP1 segment by hand.
    implementation("androidx.exifinterface:exifinterface:1.3.7")
}
