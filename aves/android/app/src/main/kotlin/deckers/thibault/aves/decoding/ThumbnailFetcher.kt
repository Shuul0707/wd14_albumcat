package deckers.thibault.aves.decoding

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import android.util.Size
import androidx.annotation.RequiresApi
import androidx.core.graphics.scale
import com.bumptech.glide.Glide
import com.bumptech.glide.load.DecodeFormat
import com.bumptech.glide.load.engine.DiskCacheStrategy
import com.bumptech.glide.request.RequestOptions
import com.bumptech.glide.signature.ObjectKey
import deckers.thibault.aves.channel.streams.darttoplatform.ByteSink
import deckers.thibault.aves.glide.AvesAppGlideModule
import deckers.thibault.aves.glide.MultiPageImage
import deckers.thibault.aves.model.VideoThumbnailMethod
import deckers.thibault.aves.storage.StorageUtils
import deckers.thibault.aves.utils.BitmapUtils
import deckers.thibault.aves.utils.BitmapUtils.applyExifOrientation
import deckers.thibault.aves.utils.ContextUtils.devicePixelRatio
import deckers.thibault.aves.utils.LogUtils
import deckers.thibault.aves.utils.MimeTypes
import deckers.thibault.aves.utils.MimeTypes.SVG
import deckers.thibault.aves.utils.MimeTypes.isVideo
import deckers.thibault.aves.utils.MimeTypes.needRotationAfterContentResolverThumbnail
import deckers.thibault.aves.utils.MimeTypes.needRotationAfterGlide
import deckers.thibault.aves.utils.UriUtils.tryParseId
import java.io.ByteArrayInputStream
import kotlin.math.min
import kotlin.math.roundToInt

class ThumbnailFetcher internal constructor(
    private val context: Context,
    private val uri: Uri,
    private val pageId: Int?,
    private val decoded: Boolean,
    private val mimeType: String,
    private val dateModifiedMillis: Long,
    private val rotationDegrees: Int,
    private val isFlipped: Boolean,
    widthDip: Double?,
    heightDip: Double?,
    private val videoMethods: List<VideoThumbnailMethod>?,
    private val result: ByteSink,
) {
    private val density = context.devicePixelRatio()
    private val defaultSize = (DEFAULT_SIZE_DIP * density).roundToInt()
    private val width: Int = if (widthDip?.takeIf { it > 0 } != null) (widthDip * density).roundToInt() else defaultSize
    private val height: Int = if (heightDip?.takeIf { it > 0 } != null) (heightDip * density).roundToInt() else defaultSize

    private fun isLowQualityFetch() = width == defaultSize || height == defaultSize

    private fun isCustomFetch(): Boolean {
        // use custom Glide models for videos / SVG / TIFF / multi-page images
        if (isVideo(mimeType) || mimeType == SVG || mimeType == MimeTypes.TIFF) return true
        if (pageId != null && MultiPageImage.isSupported(mimeType)) return true
        return false
    }

    suspend fun fetch() {
        var bitmap: Bitmap? = null
        var exception: Exception? = null

        try {
            if (isLowQualityFetch() && !isCustomFetch() && !isFlipped) {
                // Fetch low quality thumbnails when size is not specified.
                // As of Android 11, the Media Store content resolver may return a thumbnail
                // that is automatically rotated according to EXIF orientation, but not flipped,
                // so we skip this step for flipped entries.
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    bitmap = getByResolver()
                } else if (StorageUtils.isMediaStoreContentUri(uri)) {
                    bitmap = getByMediaStore()
                }
            }
        } catch (e: Exception) {
            exception = e
        }

        // fallback if the native methods failed or for higher quality thumbnails
        if (bitmap == null) {
            try {
                bitmap = getByGlide()
            } catch (e: Exception) {
                exception = e
            }
        }

        if (bitmap != null) {
            if (bitmap.width > width && bitmap.height > height) {
                // rescale when the resulting bitmap is larger than requested
                val scalingFactor: Double = min(bitmap.width.toDouble() / width, bitmap.height.toDouble() / height)
                val dstWidth = (bitmap.width / scalingFactor).roundToInt()
                val dstHeight = (bitmap.height / scalingFactor).roundToInt()
                val reduction = 1f - (dstWidth * dstHeight).toFloat() / (bitmap.width * bitmap.height)
                if (reduction > RESCALE_REDUCTION_THRESHOLD) {
                    Log.d(
                        LOG_TAG, "rescale thumbnail for mimeType=$mimeType uri=$uri width=$width height=$height" +
                                ", with bitmap byteCount=${bitmap.byteCount} size=${bitmap.width}x${bitmap.height}, to target=${dstWidth}x${dstHeight}" +
                                ", reduced by ${(reduction * 100).roundToInt()}%)"
                    )
                    bitmap = bitmap.scale(dstWidth, dstHeight)
                }
            }

            if (bitmap.byteCount > BITMAP_SIZE_DANGER_THRESHOLD) {
                result.error(
                    "getThumbnail-large", "thumbnail bitmap dangerously large" +
                            " for mimeType=$mimeType uri=$uri pageId=$pageId width=$width height=$height" +
                            ", with bitmap byteCount=${bitmap.byteCount} size=${bitmap.width}x${bitmap.height} config=${bitmap.config?.name}", null
                )
                return
            }
        }

        // do not recycle bitmaps fetched from `ContentResolver` or Glide as their lifecycle is unknown
        val bytes = BitmapUtils.getBytes(bitmap, recycle = false, decoded = decoded, applyGainmap = false, mimeType = mimeType)
        if (bytes == null) {
            var errorDetails: String? = exception?.message
            if (errorDetails?.isNotEmpty() == true) {
                errorDetails = errorDetails.split(Regex("\n"), 2).first()
            }
            result.error("getThumbnail-null", "failed to get thumbnail for mimeType=$mimeType uri=$uri", errorDetails)
        } else {
            result.streamBytes(ByteArrayInputStream(bytes))
        }
    }

    @RequiresApi(api = Build.VERSION_CODES.Q)
    fun getByResolver(): Bitmap? {
        val resolver = context.contentResolver
        var bitmap: Bitmap? = resolver.loadThumbnail(uri, Size(width, height), null)
        if (needRotationAfterContentResolverThumbnail(mimeType)) {
            bitmap = applyExifOrientation(context, bitmap, rotationDegrees, isFlipped)
        }
        return bitmap
    }

    fun getByMediaStore(): Bitmap? {
        val contentId = uri.tryParseId() ?: return null
        val resolver = context.contentResolver
        return if (isVideo(mimeType)) {
            @Suppress("DEPRECATION")
            MediaStore.Video.Thumbnails.getThumbnail(resolver, contentId, MediaStore.Video.Thumbnails.MINI_KIND, null)
        } else {
            @Suppress("DEPRECATION")
            var bitmap = MediaStore.Images.Thumbnails.getThumbnail(resolver, contentId, MediaStore.Images.Thumbnails.MINI_KIND, null)
            // from Android 10 (API 29), returned thumbnail is already rotated according to EXIF orientation
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q && bitmap != null) {
                bitmap = applyExifOrientation(context, bitmap, rotationDegrees, isFlipped)
            }
            bitmap
        }
    }

    fun getByGlide(): Bitmap? {
        // add signature to ignore cache for images which got modified but kept the same URI
        var options = RequestOptions()
            .format(DecodeFormat.PREFER_ARGB_8888)
            .signature(ObjectKey("$dateModifiedMillis-$rotationDegrees-$isFlipped-$width-$pageId"))
            .override(width, height)
        if (isVideo(mimeType)) {
            options = options.diskCacheStrategy(DiskCacheStrategy.RESOURCE)
        }

        val target = Glide.with(context)
            .asBitmap()
            .apply(options)
            .load(AvesAppGlideModule.getModel(context, uri, mimeType, pageId, videoMethods = videoMethods))
            .submit(width, height)

        return try {
            var bitmap = target.get()
            if (needRotationAfterGlide(mimeType, pageId)) {
                bitmap = applyExifOrientation(context, bitmap, rotationDegrees, isFlipped)
            }
            bitmap
        } finally {
            Glide.with(context).clear(target)
        }
    }

    /**
     * Albumcat: decode the file ourselves instead of asking the system for a thumbnail.
     *
     * Glide/MediaStore/resolver all hand back a bitmap they produced for their own purposes
     * (ARGB_8888, their own disk cache, MINI_KIND square thumbnails) and we then encode it
     * again. Here we read the bounds first, decode with an `inSampleSize` that lands just above
     * the requested extent, scale once to the exact target, and stay on RGB_565: half the bytes
     * of ARGB_8888 and no alpha channel, so a PNG screenshot is encoded as JPEG rather than PNG.
     */
    fun getByDecoding(): Bitmap? {
        val target = maxOf(width, height)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

        // the largest power-of-two sample size that still keeps both sides at or above the target
        var sample = 1
        while (min(bounds.outWidth, bounds.outHeight) / (sample * 2) >= target) {
            sample *= 2
        }

        // RGB_565 is half the bytes of ARGB_8888 and is all WD14 needs, but it has no alpha
        // channel: a PNG with transparency would decode with the premultiplied black showing
        // through. So for formats that can carry alpha, decode in ARGB_8888 and flatten onto
        // white -- the way a gallery shows a character cut-out -- before downscaling.
        val mayHaveAlpha = MimeTypes.canHaveAlpha(mimeType)
        val options = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = if (mayHaveAlpha) Bitmap.Config.ARGB_8888 else Bitmap.Config.RGB_565
        }
        val raw = context.contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, options)
        } ?: return null
        val decoded = if (mayHaveAlpha && raw.hasAlpha()) {
            Bitmap.createBitmap(raw.width, raw.height, Bitmap.Config.RGB_565).also { flattened ->
                Canvas(flattened).apply {
                    drawColor(Color.WHITE)
                    drawBitmap(raw, 0f, 0f, null)
                }
                raw.recycle()
            }
        } else {
            raw
        }

        val longest = maxOf(decoded.width, decoded.height)
        val scaled = if (longest > target) {
            val factor = target.toDouble() / longest
            Bitmap.createScaledBitmap(
                decoded,
                maxOf(1, (decoded.width * factor).roundToInt()),
                maxOf(1, (decoded.height * factor).roundToInt()),
                true,
            ).also { if (it !== decoded) decoded.recycle() }
        } else {
            decoded
        }

        // Glide applies the EXIF orientation for us; when we decode ourselves we do it here.
        return if (rotationDegrees != 0 || isFlipped) {
            applyExifOrientation(context, scaled, rotationDegrees, isFlipped) ?: scaled
        } else {
            scaled
        }
    }

    companion object {
        private val LOG_TAG = LogUtils.createTag<ThumbnailFetcher>()
        private const val BITMAP_SIZE_DANGER_THRESHOLD = 20 * (1 shl 20) // MiB
        private const val DEFAULT_SIZE_DIP: Double = 64.0
        private const val RESCALE_REDUCTION_THRESHOLD: Float = .15f
    }
}