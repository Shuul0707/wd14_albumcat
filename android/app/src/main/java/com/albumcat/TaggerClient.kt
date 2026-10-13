package com.albumcat

import android.util.Base64
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL

/** What the PC decided the photo's tags should be. */
data class PlanResult(
    val ok: Boolean,
    val flat: List<String> = emptyList(),
    val hierarchical: List<String> = emptyList(),
    val rating: Int = 0,
    val needsReview: Boolean = false,
    val characters: List<Pair<String, Double>> = emptyList(),
    val notes: List<String> = emptyList(),
    val ms: Double = 0.0,
    val cached: Boolean = false,
    val error: String? = null,
)

/**
 * Talks to `albumcat serve` on the PC.
 *
 * The PC hands back a *finished* answer -- flat keywords, hierarchical paths,
 * star rating -- rather than raw model scores. That keeps the rules, the
 * thresholds and the plugin registry in one place: retune them on the PC and
 * the phone's behaviour changes with no app rebuild.
 */
class TaggerClient(private val baseUrl: String, private val token: String = "") {

    private fun open(path: String, method: String): HttpURLConnection {
        val url = URL(baseUrl.trimEnd('/') + path)
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 8000
        conn.readTimeout = 180_000
        if (token.isNotEmpty()) conn.setRequestProperty("X-Albumcat-Token", token)
        return conn
    }

    private fun readAll(conn: HttpURLConnection, ok: Int): String {
        val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.use { BufferedReader(InputStreamReader(it, Charsets.UTF_8)).readText() } ?: ""
        if (conn.responseCode != ok && conn.responseCode !in 200..299) {
            throw java.io.IOException("HTTP ${conn.responseCode}: ${text.take(300)}")
        }
        return text
    }

    fun health(): JSONObject {
        val conn = open("/health", "GET")
        try {
            return JSONObject(readAll(conn, 200))
        } finally {
            conn.disconnect()
        }
    }

    fun plan(
        jpeg: ByteArray,
        id: String,
        name: String,
        size: Long,
        dateTaken: Long,
    ): PlanResult {
        val body = JSONObject().apply {
            put("id", id)
            put("name", name)
            put("size", size)
            put("date_taken", dateTaken)
            put("image", Base64.encodeToString(jpeg, Base64.NO_WRAP))
        }.toString()

        val conn = open("/plan", "POST")
        try {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val text = readAll(conn, 200)
            return parse(JSONObject(text))
        } finally {
            conn.disconnect()
        }
    }

    private fun parse(o: JSONObject): PlanResult {
        if (!o.optBoolean("ok", false)) {
            return PlanResult(ok = false, error = o.optString("error", "unknown error"))
        }
        val chars = ArrayList<Pair<String, Double>>()
        o.optJSONArray("characters")?.let { arr ->
            for (i in 0 until arr.length()) {
                val c = arr.getJSONObject(i)
                chars += c.optString("label", c.optString("key")) to c.optDouble("score", 0.0)
            }
        }
        return PlanResult(
            ok = true,
            flat = o.optJSONArray("flat").toStringList(),
            hierarchical = o.optJSONArray("hierarchical").toStringList(),
            rating = o.optInt("rating", 0),
            needsReview = o.optBoolean("needs_review", false),
            characters = chars,
            notes = o.optJSONArray("notes").toStringList(),
            ms = o.optDouble("ms", 0.0),
            cached = o.optBoolean("cached", false),
        )
    }

    private fun org.json.JSONArray?.toStringList(): List<String> {
        if (this == null) return emptyList()
        val out = ArrayList<String>(length())
        for (i in 0 until length()) out += optString(i)
        return out
    }
}
