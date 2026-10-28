import java.time.Duration
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Подпись релиза: файл свойств вне репозитория (/keys/pimusic-release.properties в контейнере сборки =
// <каталог сборки>/keys на хосте; ключ создаёт build/android-native-inner.sh при первой сборке).
val signingProps = Properties().apply {
    val f = file(System.getenv("PIMUSIC_SIGNING_PROPERTIES") ?: "/keys/pimusic-release.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "io.github.ikriu.pimusic"
    compileSdk = 36
    buildToolsVersion = "36.0.0" // = ANDROID_BUILD_TOOLS в apps/build/env.sh

    defaultConfig {
        applicationId = "io.github.ikriu.pimusic"
        minSdk = 26
        targetSdk = 36
        versionCode = 10000 // major*10000 + minor*100 + patch; увеличивать на каждый выпуск (по нему видно обновление)
        versionName = "1.0.0"
    }

    signingConfigs {
        create("release") {
            if (signingProps.getProperty("storeFile") != null) {
                storeFile = file(signingProps.getProperty("storeFile"))
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
    }
    buildTypes {
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
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}", "/META-INF/*.version", "DebugProbesKt.bin")
    }
    // воспроизводимость: без отметок времени/путей сборки в APK
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
        unitTests.isIncludeAndroidResources = true // Robolectric + Compose UI-тесты
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        freeCompilerArgs.addAll(
            "-opt-in=androidx.compose.material3.ExperimentalMaterial3Api",
            "-opt-in=androidx.compose.material3.ExperimentalMaterial3ExpressiveApi",
            "-opt-in=androidx.media3.common.util.UnstableApi",
        )
    }
}

// Без ключа release не собирается (а не подписывается тихо debug-ключом)
gradle.taskGraph.whenReady {
    val wantsRelease = allTasks.any { it.project == project && it.name.contains("Release") && it.name.startsWith("assemble") }
    if (wantsRelease && signingProps.getProperty("storeFile") == null) {
        throw GradleException("Нет ключа подписи release (PIMUSIC_SIGNING_PROPERTIES или /keys/pimusic-release.properties)")
    }
}

// Robolectric без сети: образ android-all берётся из Gradle (verification-metadata) и кладётся в каталог для offline-режима
val robolectricRuntime: Configuration by configurations.creating { isTransitive = false }
val robolectricDeps = tasks.register<Copy>("robolectricDeps") {
    from(robolectricRuntime)
    into(layout.buildDirectory.dir("robolectric-deps"))
}
tasks.withType<Test>().configureEach {
    dependsOn(robolectricDeps)
    systemProperty("robolectric.offline", "true")
    systemProperty("robolectric.dependency.dir", layout.buildDirectory.dir("robolectric-deps").get().asFile.absolutePath)
    systemProperty("robolectric.logging.enabled", "false")
    // скриншоты всех экранов (Roborazzi): ./gradlew testDebugUnitTest -Pscreenshots=true → app/build/shots/
    val shots = (project.findProperty("screenshots") ?: "false").toString()
    systemProperty("pimusic.screenshots", shots)
    systemProperty("roborazzi.test.record", shots)
    systemProperty("pimusic.shotcfg", (project.findProperty("shotcfg") ?: "").toString()) // подстрока конфигурации, напр. 360x800-light
    maxHeapSize = "2560m"
    timeout.set(Duration.ofMinutes(20)) // зависший тест не держит сборку часами
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.foundation)
    implementation(libs.compose.animation)
    implementation(libs.compose.icons)
    implementation(libs.material3)
    implementation(libs.graphics.shapes)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.process)
    implementation(libs.core.ktx)
    implementation(libs.core.splashscreen)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.session)
    implementation(libs.media3.datasource.okhttp)
    implementation(libs.media3.database)
    implementation(libs.coil.compose)
    implementation(libs.coil.network)
    implementation(libs.okhttp)
    implementation(libs.serialization.json)
    implementation(libs.coroutines.android)

    testImplementation(libs.junit)
    testImplementation(libs.coroutines.test)
    testImplementation(libs.robolectric)
    testImplementation(libs.roborazzi)
    testImplementation(libs.roborazzi.compose)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test)
    debugImplementation(libs.compose.ui.test.manifest) // ComponentActivity для createComposeRule (только debug, не в APK релиза)
    robolectricRuntime(libs.robolectric.android.all)
}
