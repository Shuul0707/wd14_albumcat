package com.albumcat

/**
 * Maps SAF document ids onto filesystem paths.
 *
 * `MediaScannerConnection` only speaks in filesystem paths, while SAF only
 * speaks in document ids, so `primary:DCIM/Camera/IMG_1.jpg` has to become
 * `/storage/emulated/0/DCIM/Camera/IMG_1.jpg` before the scanner can be told
 * about it.
 *
 * Deliberately free of Android imports -- the same reason `Xmp.kt` only uses
 * the JDK -- so it can be compiled and exercised on a plain JVM without a
 * device, see `tools/check_android_client.py`.
 */
object DocumentPaths {

    private const val PRIMARY_ROOT = "/storage/emulated/0"

    /**
     * `primary:DCIM/Camera/IMG_1.jpg` -> `/storage/emulated/0/DCIM/Camera/IMG_1.jpg`
     *
     * Returns null for anything that is not a `<volume>:<relative path>`
     * document id, because guessing a wrong path would make the scanner scan
     * something else entirely.
     */
    fun forDocumentId(documentId: String): String? {
        val colon = documentId.indexOf(':')
        // Needs a non-empty volume before the colon and a non-empty path after.
        if (colon <= 0 || colon == documentId.length - 1) return null

        val volume = documentId.substring(0, colon)
        val relative = documentId.substring(colon + 1)

        // A relative document id never starts with "/"; an absolute path is not
        // something that can be appended to a mount point.
        if (relative.startsWith("/")) return null
        // The volume is a single path component ("primary", "1A2B-3C4D").
        if (volume.contains('/')) return null

        val root = if (volume.equals("primary", ignoreCase = true)) {
            PRIMARY_ROOT
        } else {
            // Removable volumes are mounted under a name that matches the
            // document id's volume prefix.
            "/storage/$volume"
        }
        return "$root/$relative"
    }
}
