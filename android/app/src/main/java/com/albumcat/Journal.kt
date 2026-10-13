package com.albumcat

import android.content.Context
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Remembers what each run changed, so it can be undone.
 *
 * The important field is `prev`: the photo's XMP packet *as it was before we
 * touched it* (or null when the photo had none). Undo puts that back verbatim
 * rather than trying to subtract our tags, because "remove what I added" goes
 * wrong the moment a tag was already there for someone else's reason.
 *
 * `wrote` is stored too: before undoing, we check the file still holds the
 * packet we wrote. If it does not, the photo was edited by something else since
 * and we leave it alone instead of clobbering that change.
 */
class Journal(context: Context) {

    private val file = File(context.filesDir, "journal.jsonl")

    data class Row(
        val run: String,
        val uri: String,
        val prev: ByteArray?,
        val wrote: ByteArray?,
    )

    fun newRunId(): String =
        SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())

    @Synchronized
    fun record(run: String, uri: String, prev: ByteArray?, wrote: ByteArray) {
        val row = JSONObject().apply {
            put("ts", System.currentTimeMillis())
            put("run", run)
            put("uri", uri)
            put("prev", prev?.let { Base64.encodeToString(it, Base64.NO_WRAP) } ?: JSONObject.NULL)
            put("wrote", Base64.encodeToString(wrote, Base64.NO_WRAP))
        }
        file.appendText(row.toString() + "\n", Charsets.UTF_8)
    }

    @Synchronized
    fun read(run: String? = null): List<Row> {
        if (!file.exists()) return emptyList()
        val out = ArrayList<Row>()
        file.forEachLine(Charsets.UTF_8) { line ->
            if (line.isBlank()) return@forEachLine
            try {
                val o = JSONObject(line)
                if (run != null && o.optString("run") != run) return@forEachLine
                out += Row(
                    run = o.optString("run"),
                    uri = o.optString("uri"),
                    prev = decodeOrNull(o, "prev"),
                    wrote = decodeOrNull(o, "wrote"),
                )
            } catch (_: Exception) {
                // a half-written line must not make the whole journal unreadable
            }
        }
        return out
    }

    /** Run ids, newest first, with how many files each touched. */
    fun runs(): List<Pair<String, Int>> {
        val counts = LinkedHashMap<String, Int>()
        for (row in read()) counts[row.run] = (counts[row.run] ?: 0) + 1
        return counts.entries.sortedByDescending { it.key }.map { it.key to it.value }
    }

    /**
     * One undo record per photo, keeping the *earliest* row -- that is the state
     * before the run started. Later rows for the same file in the same run are
     * duplicates of a retry.
     */
    fun undoPlan(run: String): List<Row> {
        val first = LinkedHashMap<String, Row>()
        for (row in read(run)) first.putIfAbsent(row.uri, row)
        return first.values.toList()
    }

    @Synchronized
    fun forget(run: String): Int {
        if (!file.exists()) return 0
        val keep = ArrayList<String>()
        var dropped = 0
        file.forEachLine(Charsets.UTF_8) { line ->
            if (line.isBlank()) return@forEachLine
            val isOurs = try { JSONObject(line).optString("run") == run } catch (_: Exception) { false }
            if (isOurs) dropped++ else keep += line
        }
        file.writeText(keep.joinToString("\n").let { if (it.isEmpty()) "" else it + "\n" }, Charsets.UTF_8)
        return dropped
    }

    private fun decodeOrNull(o: JSONObject, key: String): ByteArray? {
        if (o.isNull(key)) return null
        val s = o.optString(key, "")
        if (s.isEmpty()) return null
        return try { Base64.decode(s, Base64.NO_WRAP) } catch (_: Exception) { null }
    }
}
