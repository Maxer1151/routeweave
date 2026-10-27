plugins {
    id("com.android.application")
}

android {
    namespace = "com.vpnswitch"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.vpnswitch"
        minSdk = 29
        targetSdk = 35
        versionCode = 3
        versionName = "1.2"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
}
