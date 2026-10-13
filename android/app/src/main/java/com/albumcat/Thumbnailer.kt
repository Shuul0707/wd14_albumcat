package com.albumcat

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Rect
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream
import java.io.InputStream

/**
 * Turns a photo into the 448px thumbnail the PC's model wants.
 *
 * The model (WD14 ViT) only ever sees 448x448, so sending the original 3-8 MB
 * photo across the LAN would be pure waste. A thumbnail is 30-60 KB, which is
 * what makes "no photo ever leaves the phone" true in practice.
 *
 * The shape matches what the PC would do anyway: scale the whole image down,
 * then centre it on a white square. Same letterboxing on both sides means the
 * model sees the same picture either way.
 */
object Thumbnailer {

    const val SIZE = 448
    const val QUALITY = 85

    /** Decode no bigger than this on the long edge before the final resize. */
    private const val DECODE_CEILING = 896

    class DecodeFailed(message: String) : Exception(message)

    fun thumbnail(resolver: ContentResolver, uri: Uri): ByteArray {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        // `open()` throws rather than letting a null stream flow into an `?:`.
        //
        // This used to read `openInputStream(uri)?.use { decodeStream(...) } ?: throw`,
        // which is wrong: `use` returns the *lambda's* result, and with
        // `inJustDecodeBounds` the decode deliberately returns null. So the
        // `?:` fired on every single photo and the app reported "cannot open"
        // for files it had just opened successfully. The desktop harness
        // stubs Thumbnailer out entirely, so nothing caught it; only running
        // it on a real device did.
        open(resolver, uri).use { BitmapFactory.decodeStream(it, null, bounds) }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) throw DecodeFailed("not an image: $uri")

        val opts = BitmapFactory.Options().apply {
            inSampleSize = sampleSize(bounds.outWidth, bounds.outHeight)
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        val decoded = open(resolver, uri).use { BitmapFactory.decodeStream(it, null, opts) }
            ?: throw DecodeFailed("cannot decode $uri")

        val oriented = applyExifRotation(resolver, uri, decoded)
        try {
            val square = letterbox(oriented)
            try {
                val out = ByteArrayOutputStream(64 * 1024)
                square.compress(Bitmap.CompressFormat.JPEG, QUALITY, out)
                return out.toByteArray()
            } finally {
                if (square !== oriented) square.recycle()
            }
        } finally {
            if (oriented !== decoded) oriented.recycle()
            decoded.recycle()
        }
    }

    /**
     * Opens a photo, failing loudly if the provider hands back nothing.
     *
     * The point of the separate function is that callers write
     * `open(...).use { ... }` with nothing after the block, so the nullable
     * result of the *decode* can never be mistaken for a failed *open*.
     */
    private fun open(resolver: ContentResolver, uri: Uri): InputStream =
        resolver.openInputStream(uri) ?: throw DecodeFailed("cannot open $uri")

    private fun sampleSize(w: Int, h: Int): Int {        var sample = 1
        while (maxOf(w, h) / (sample * 2) >= SIZE && maxOf(w, h) / sample > DECODE_CEILING) {
            sample *= 2
        }
        return sample
    }

    /**
     * Phones store most photos in the sensor's orientation and record the real
     * one in EXIF. The model has no idea about any of that, so bake the rotation
     * in before tagging or a portrait photo arrives sideways.
     */
    private fun applyExifRotation(resolver: ContentResolver, uri: Uri, src: Bitmap): Bitmap {
        val orientation = try {
            resolver.openInputStream(uri)?.use { ExifInterface(it).getAttributeInt(
                ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL) }
                ?: ExifInterface.ORIENTATION_NORMAL
        } catch (_: Exception) {
            ExifInterface.ORIENTATION_NORMAL
        }

        val m = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_ROTATE_90 -> m.postRotate(90f)
            ExifInterface.ORIENTATION_ROTATE_180 -> m.postRotate(180f)
            ExifInterface.ORIENTATION_ROTATE_270 -> m.postRotate(270f)
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> m.postScale(-1f, 1f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> m.postScale(1f, -1f)
            ExifInterface.ORIENTATION_TRANSPOSE -> { m.postRotate(90f); m.postScale(-1f, 1f) }
            ExifInterface.ORIENTATION_TRANSVERSE -> { m.postRotate(270f); m.postScale(-1f, 1f) }
            else -> return src
        }
        return Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
    }

    private fun letterbox(src: Bitmap): Bitmap {
        val out = Bitmap.createBitmap(SIZE, SIZE, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(out)
        canvas.drawColor(Color.WHITE)

        val scale = minOf(SIZE.toFloat() / src.width, SIZE.toFloat() / src.height)
        val w = (src.width * scale).toInt().coerceAtLeast(1)
        val h = (src.height * scale).toInt().coerceAtLeast(1)
        val dst = Rect((SIZE - w) / 2, (SIZE - h) / 2, (SIZE - w) / 2 + w, (SIZE - h) / 2 + h)
        val paint = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
        canvas.drawBitmap(src, null, dst, paint)
        return out
    }

    /** Cheap check so we can skip the expensive path on obviously wrong files. */
    fun looksLikeJpegStream(input: InputStream): Boolean {
        val head = ByteArray(3)
        val n = input.read(head)
        return n == 3 && head[0] == 0xFF.toByte() && head[1] == 0xD8.toByte()
    }
}
