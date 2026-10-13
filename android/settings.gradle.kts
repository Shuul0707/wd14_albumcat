// Repositories are the Aliyun mirrors, and that is deliberate.
//
// On this machine:
//   * plugins.gradle.org terminates TLS with a self-signed certificate, so
//     Gradle's plugin-marker lookup dies with a certificate error.
//   * repo.maven.apache.org times out (45s+, measured).
//   * dl.google.com works, but maven.aliyun.com/repository/google is the same
//     content at ~0.1s.
//
// So every artifact here comes from Aliyun, with google() kept as the fallback
// for anything the mirrors have not picked up yet.
pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
    }
}

rootProject.name = "AlbumCategories"
include(":app")
