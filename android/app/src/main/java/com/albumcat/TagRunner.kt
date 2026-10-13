package com.albumcat

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import java.io.ByteArrayOutputStream
import java.io.IOException

/**
 * The whole phone-side run: walk the folder the user picked, ask the PC what
 * each photo is, splice the answer into the photo's own XMP, journal it.
 *
 * Nothing is moved, renamed or re-encoded -- the only bytes that change are
 * inside one metadata block (a JPEG's APP1 segment, a PNG's iTXt chunk).
 */
class TagRunner(
    private val context: Context,
    private val client: TaggerClient,
    private val journal: Journal,
) {

    companion object {
        /**
         * Formats whose XMP we know how to write: JPEG's APP1 segment and PNG's
         * iTXt chunk. PNG is here because an Android screenshot *is* a PNG, and
         * screenshots are exactly the images worth sorting by character.
         *
         * HEIC and friends stay out for now: HEIC means rewriting the MP4
         * `meta` box's `iinf`/`iloc`/`iref` tables, a different and much
         * riskier job. They are counted and reported, never silently dropped.
         */
        private val WRITABLE_SUFFIXES = setOf("jpg", "jpeg", "jpe", "png")

        /**
         * Images we walk past. They are *counted*, never silently dropped: an
         * album that is half HEIC would otherwise look like an empty album, and
         * "找到 12 张" with no explanation is unguessable.
         */
        private val OTHER_IMAGE_SUFFIXES = setOf(
            "heic", "heif", "webp", "gif", "bmp", "avif", "dng", "tif", "tiff",
        )

        private const val MAX_FILES = 100_000
        private const val MAX_DEPTH = 24
        private val MARKER = "x:xmptk=\"albumcat\""

        /** `abortReason` when the user, not a failure, ended the run. */
        const val STOPPED = "手动停止"
    }

    /**
     * Set by the UI thread, read by the worker thread between photos.
     *
     * Volatile, not synchronised: the worst an unsynchronised read could do is
     * cost one extra photo, and the check sits on the hot path.
     */
    @Volatile
    private var stopRequested = false

    /** What a folder walk found: the work, plus what it had to pass over. */
    data class Scan(
        val files: List<DocumentFile>,
        val otherImages: Int = 0,
        val truncated: Boolean = false,
    )

    sealed class Event {
        data class Scanning(val found: Int, val otherImages: Int = 0, val truncated: Boolean = false) : Event()
        data class Item(val index: Int, val total: Int, val name: String, val line: String, val bad: Boolean = false) : Event()
        data class Message(val text: String) : Event()
    }

    data class Options(
        val dryRun: Boolean = true,
        val skipAlreadyTagged: Boolean = true,
        val limit: Int = 0,
        /**
         * Stop the run after this many failures in a row.
         *
         * The dominant real-world failure is the server going away mid-run
         * (terminal closed, laptop slept, WiFi roamed). Without this the app
         * grinds through the remaining thousands of photos, failing each one
         * after a full timeout, and the user watches a broken-looking progress
         * bar for an hour instead of being told what happened.
         */
        val maxConsecutiveFailures: Int = 8,
    )

    data class Result(
        val uri: String,
        val name: String,
        val characters: List<Pair<String, Double>>,
        val hierarchical: List<String>,
        val needsReview: Boolean,
        val written: Boolean,
        val note: String = "",
        val bad: Boolean = false,
    )

    data class Summary(
        val runId: String,
        val total: Int,
        val processed: Int,
        val written: Int,
        val skipped: Int,
        val failed: Int,
        val dryRun: Boolean,
        val results: List<Result>,
        val otherImages: Int = 0,
        val truncated: Boolean = false,
        val aborted: Boolean = false,
        val abortReason: String = "",
    )

    private val resolver: ContentResolver get() = context.contentResolver

    // ------------------------------------------------------------------ #

    fun collect(tree: Uri): Scan {
        stopRequested = false
        val root = DocumentFile.fromTreeUri(context, tree) ?: return Scan(emptyList())
        val tally = Tally()
        walk(root, tally, 0)
        return Scan(tally.files, tally.otherImages, tally.truncated)
    }

    private class Tally {
        val files = ArrayList<DocumentFile>()
        var otherImages = 0
        var truncated = false
    }

    private fun walk(dir: DocumentFile, tally: Tally, depth: Int) {
        // A 20 000-file tree takes long enough to walk that "I pressed stop"
        // has to mean something here too, not only in the write loop.
        if (stopRequested) return
        if (depth > MAX_DEPTH) { tally.truncated = true; return }
        if (tally.files.size > MAX_FILES) { tally.truncated = true; return }
        val children = try { dir.listFiles() } catch (_: Exception) { return }
        for (child in children) {
            if (stopRequested) return
            val name = child.name ?: continue
            if (name.startsWith(".")) continue          // .nomedia, .thumbnails, ...
            if (child.isDirectory) {
                walk(child, tally, depth + 1)
                continue
            }
            when (suffixOf(name)) {
                in WRITABLE_SUFFIXES -> tally.files += child
                in OTHER_IMAGE_SUFFIXES -> tally.otherImages++
            }
        }
    }

    private fun suffixOf(name: String): String = name.substringAfterLast('.', "").lowercase()

    // ------------------------------------------------------------------ #

    fun run(tree: Uri, options: Options, onEvent: (Event) -> Unit): Summary {
        stopRequested = false
        val runId = journal.newRunId()
        onEvent(Event.Message("扫描文件夹…"))
        val scan = collect(tree)
        val files = scan.files
        onEvent(Event.Scanning(files.size, scan.otherImages, scan.truncated))

        val target = if (options.limit > 0) files.take(options.limit) else files
        val results = ArrayList<Result>(target.size)
        var written = 0
        var skipped = 0
        var failed = 0
        var attempted = 0
        var consecutive = 0
        var aborted = false
        var abortReason = ""
        // Paths waiting to be announced to the media store, so Aves knows to
        // re-read those photos. Flushed every Rescan.BATCH and once at the end.
        val rescan = ArrayList<String>(Rescan.BATCH)

        for ((index, doc) in target.withIndex()) {
            // The run ends *between* photos, never inside one. What matters is
            // that this check is inside the loop at all -- a stop pressed while
            // photo N is in flight still lets N finish and be journaled, and
            // the loop exits before N+1. (Verified by deleting this check: eight
            // assertions in tools/check_android_runner.py go red, and the run
            // writes all five photos while claiming nothing happened.)
            if (stopRequested) {
                aborted = true
                abortReason = STOPPED
                break
            }
            attempted++
            val name = doc.name ?: "(unnamed)"
            val uri = doc.uri
            try {
                val thumb = Thumbnailer.thumbnail(resolver, uri)
                val plan = client.plan(
                    jpeg = thumb,
                    id = uri.toString(),
                    name = name,
                    size = doc.length(),
                    dateTaken = doc.lastModified(),
                )
                if (!plan.ok) throw IOException(plan.error ?: "server said no")
                // The server answered, so whatever failed before is not an
                // ongoing outage.
                consecutive = 0

                if (options.dryRun) {
                    results += Result(
                        uri = uri.toString(), name = name,
                        characters = plan.characters, hierarchical = plan.hierarchical,
                        needsReview = plan.needsReview, written = false,
                    )
                    onEvent(Event.Item(index + 1, target.size, name, describe(plan) + " (预览)", bad = false))
                    continue
                }

                val original = readAll(uri)
                // Decided by the file's own header, not by its name: a ".jpg"
                // that is really a PNG is rare but it happens, and guessing
                // wrong here would corrupt the photo.
                val format = try { Xmp.photoFormat(original) } catch (e: Xmp.XmpException) {
                    throw IOException("不是可改写的格式：${e.message}")
                }
                // `prev` is the undo blob, which is *not* the same thing for
                // both formats -- a PNG's iTXt payload carries compression and
                // language fields that a packet cannot express. Reading tags out
                // of it therefore goes through blockPacket().
                val prev = Xmp.findBlock(original, format)
                val prevPacket = Xmp.blockPacket(prev, format)
                val existing = prevPacket?.let { try { Xmp.parsePacket(it) } catch (_: Exception) { null } }

                if (options.skipAlreadyTagged && prevPacket != null &&
                    String(prevPacket, Charsets.UTF_8).contains(MARKER)
                ) {
                    skipped++
                    results += Result(
                        uri = uri.toString(), name = name,
                        characters = plan.characters, hierarchical = plan.hierarchical,
                        needsReview = plan.needsReview, written = false, note = "已打过标签，跳过",
                    )
                    onEvent(Event.Item(index + 1, target.size, name, "已打过标签，跳过"))
                    continue
                }

                val (flat, hier, rating) =
                    Xmp.merge(existing, plan.flat, plan.hierarchical, plan.rating.takeIf { it > 0 })
                val packet = Xmp.buildPacket(
                    flat = flat, hierarchical = hier, rating = rating,
                    preserve = Xmp.foreignParts(prevPacket),
                )
                val updated = Xmp.spliceBlock(original, format, packet)

                // Journal *before* the write, not after.
                //
                // `writeAll` truncates the file and then streams the new bytes
                // in. If the app is killed, the battery dies, or the phone runs
                // out of space inside that window, the photo is left half
                // written -- and if the journal entry for it did not exist yet,
                // the original bytes are gone for good. Recording first means
                // there is always a way back.
                //
                // A row for a write that then failed is harmless: `undo` would
                // put back exactly the bytes that are still there. And
                // `undoPlan` keeps the *earliest* row per photo, so a retry
                // cannot overwrite the pre-run original.
                journal.record(runId, uri.toString(), prev, packet)
                writeVerified(uri, updated, original)
                written++
                Rescan.pathOf(uri)?.let { rescan += it }
                if (rescan.size >= Rescan.BATCH) {
                    Rescan.scanPaths(context, rescan)
                    rescan.clear()
                }

                results += Result(
                    uri = uri.toString(), name = name,
                    characters = plan.characters, hierarchical = hier,
                    needsReview = plan.needsReview, written = true,
                )
                onEvent(Event.Item(index + 1, target.size, name, describe(plan) + " → 已写入", bad = false))
            } catch (e: Exception) {
                failed++
                consecutive++
                val why = e.message ?: e.javaClass.simpleName
                results += Result(
                    uri = uri.toString(), name = name,
                    characters = emptyList(), hierarchical = emptyList(),
                    needsReview = false, written = false, note = why, bad = true,
                )
                onEvent(Event.Item(index + 1, target.size, name, "失败：$why", bad = true))
                if (consecutive >= options.maxConsecutiveFailures) {
                    aborted = true
                    abortReason = why
                    onEvent(Event.Message(
                        "连续 $consecutive 张都失败了，已停下来（最后一条：$why）。" +
                        "常见原因：电脑端的 albumcat serve 关了、电脑休眠了、" +
                        "或者手机和电脑不在同一个 WiFi 上。修好后可以再跑一次，" +
                        "已经写好的照片会被自动跳过。"
                    ))
                    break
                }
            }
        }

        Rescan.scanPaths(context, rescan)

        return Summary(
            runId = runId,
            total = target.size,
            processed = attempted,
            written = written,
            skipped = skipped,
            failed = failed,
            dryRun = options.dryRun,
            results = results,
            otherImages = scan.otherImages,
            truncated = scan.truncated,
            aborted = aborted,
            abortReason = abortReason,
        )
    }

    fun requestStop() {
        stopRequested = true
    }

    /** True once [requestStop] has been called for the run in progress. */
    fun isStopRequested(): Boolean = stopRequested

    private fun describe(plan: PlanResult): String {
        val who = if (plan.characters.isEmpty()) "（没认出角色）"
        else plan.characters.joinToString("、") { "${it.first} ${(it.second * 100).toInt()}%" }
        val extra = if (plan.needsReview) " · 待复核" else ""
        return who + extra
    }

    // ------------------------------------------------------------------ #

    /** Put back the XMP every photo had before the run. Returns how many we restored. */
    fun undo(runId: String, onEvent: (Event) -> Unit): Int {
        stopRequested = false
        val rows = journal.undoPlan(runId)
        var restored = 0
        var untouched = 0
        val rescan = ArrayList<String>(Rescan.BATCH)
        rows.forEachIndexed { index, row ->
            if (stopRequested) return@forEachIndexed
            val uri = Uri.parse(row.uri)
            val name = uri.lastPathSegment ?: row.uri
            try {
                val current = readAll(uri)
                // Re-derived from the file, so the journal needs no format
                // column -- and a photo whose format somehow changed since the
                // run is restored in whatever format it is now.
                val format = Xmp.photoFormat(current)
                val now = Xmp.blockPacket(Xmp.findBlock(current, format), format)
                if (row.wrote != null && (now == null || !now.contentEquals(row.wrote))) {
                    untouched++
                    onEvent(Event.Item(index + 1, rows.size, name, "之后又被改过，未动它", bad = true))
                    return@forEachIndexed
                }
                // row.prev is the undo blob, so it must go back through
                // spliceBlock; re-wrapping it by hand would drop a PNG's chunk
                // fields.
                writeVerified(uri, Xmp.spliceBlock(current, format, row.prev), current)
                restored++
                Rescan.pathOf(uri)?.let { rescan += it }
                if (rescan.size >= Rescan.BATCH) {
                    Rescan.scanPaths(context, rescan)
                    rescan.clear()
                }
                onEvent(Event.Item(index + 1, rows.size, name, "已还原"))
            } catch (e: Exception) {
                untouched++
                onEvent(Event.Item(index + 1, rows.size, name, "还原失败：${e.message}", bad = true))
            }
        }
        Rescan.scanPaths(context, rescan)
        if (restored > 0) journal.forget(runId)
        onEvent(Event.Message("还原 $restored 张，跳过 $untouched 张"))
        return restored
    }

    // ------------------------------------------------------------------ #

    private fun readAll(uri: Uri): ByteArray {
        val out = ByteArrayOutputStream(1 shl 20)
        resolver.openInputStream(uri)?.use { it.copyTo(out, 1 shl 16) }
            ?: throw IOException("打不开文件")
        return out.toByteArray()
    }

    /**
     * Write [bytes], then read the file back and prove it really holds them.
     *
     * SAF providers are inconsistent: some silently ignore a write, some report
     * success after storing a truncated stream. Verifying costs one extra read
     * of a file we already had in memory, and the alternative is a corrupted
     * photo discovered weeks later. If the check fails, [original] goes back
     * before the error propagates.
     */
    private fun writeVerified(uri: Uri, bytes: ByteArray, original: ByteArray) {
        writeAll(uri, bytes)
        val readBack = try { readAll(uri) } catch (_: Exception) { null }
        if (readBack != null && readBack.contentEquals(bytes)) return

        val restored = runCatching { writeAll(uri, original) }.isSuccess
        throw IOException(
            if (restored) "写入后读回校验不一致，已还原原文件"
            else "写入后读回校验不一致，且还原失败——这张照片可能已损坏"
        )
    }

    /**
     * SAF providers disagree about which modes they honour, so try the ones
     * that actually truncate, in order, before giving up.
     */
    private fun writeAll(uri: Uri, bytes: ByteArray) {
        var last: Exception? = null
        for (mode in listOf("rwt", "wt", "w")) {
            try {
                val stream = resolver.openOutputStream(uri, mode) ?: continue
                stream.use { it.write(bytes); it.flush() }
                return
            } catch (e: Exception) {
                last = e
            }
        }
        throw last ?: IOException("这个文件夹不允许写入")
    }
}
