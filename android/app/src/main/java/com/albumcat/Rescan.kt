package com.albumcat

import android.content.Context
import android.media.MediaScannerConnection
import android.net.Uri
import android.provider.DocumentsContract
import android.util.Log

/**
 * Tell the media store that a photo's bytes changed.
 *
 * Writing through SAF replaces the file's contents but leaves MediaStore's row
 * for that photo exactly as it was, so nothing informs the gallery apps that
 * anything happened. Aves rebuilds an entry's tags only when the media store
 * reports a change for it:
 *
 *     MediaStoreChangeStreamHandler  (ContentObserver on Images/Video)
 *       -> media_store_source.onStoreChanged
 *       -> refreshUris -> refreshEntries(..., force: true)
 *       -> entry.catalog(force: true)      // re-reads dc:subject from the file
 *
 * So without this step a freshly tagged photo can keep showing its old,
 * tag-less metadata until something unrelated forces a re-read.
 *
 * Best effort by design: the folder picker may hand back a provider that is not
 * external storage, in which case there is no filesystem path to hand to the
 * scanner and we simply skip. The write itself already succeeded -- and was
 * read back and verified -- before we get here.
 */
object Rescan {

    private const val TAG = "albumcat"

    /** `DocumentsContract.EXTERNAL_STORAGE_PROVIDER_AUTHORITY`, spelled out. */
    private const val EXTERNAL_STORAGE_AUTHORITY = "com.android.externalstorage.documents"

    /**
     * How many paths to hand the scanner in one go.
     *
     * `MediaScannerConnection.scanFile` takes an array and does one service
     * bind for the whole array, so an unbatched call per photo would mean one
     * binder round trip per photo on a large album.
     */
    const val BATCH = 64

    /**
     * `primary:DCIM/Camera/IMG_1.jpg` -> `/storage/emulated/0/DCIM/Camera/IMG_1.jpg`
     *
     * See [DocumentPaths], which owns the mapping and is testable off-device.
     */
    fun pathForDocumentId(documentId: String): String? = DocumentPaths.forDocumentId(documentId)

    /** The on-disk path behind a SAF document, or null when there isn't one. */
    fun pathOf(uri: Uri): String? {
        if (uri.authority != EXTERNAL_STORAGE_AUTHORITY) return null
        val id = try {
            DocumentsContract.getDocumentId(uri)
        } catch (e: Exception) {
            Log.w(TAG, "cannot read the document id of $uri", e)
            return null
        }
        return pathForDocumentId(id)
    }

    /** Announce that [paths] changed, so gallery apps re-read those photos. */
    fun scanPaths(context: Context, paths: List<String>) {
        if (paths.isEmpty()) return
        try {
            MediaScannerConnection.scanFile(
                context,
                paths.toTypedArray(),
                Array(paths.size) { "image/jpeg" },
                null,
            )
        } catch (e: Exception) {
            // Never fatal: the photos are written and verified either way.
            Log.w(TAG, "media scan failed for ${paths.size} file(s)", e)
        }
    }
}
