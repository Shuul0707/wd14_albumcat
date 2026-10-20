package deckers.thibault.aves.channel.calls

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.nio.LongBuffer
import java.util.concurrent.Executors
import kotlin.math.sqrt

/**
 * Chinese-CLIP *text* tower, on the device.
 *
 * The image tower stays on the PC: it is a ViT that takes 1-3 s per photo and
 * would need 6-14 hours for a ten-thousand-photo library. The text tower only
 * ever sees one short sentence per *query*, which is why it fits here — and
 * why semantic search can work with the computer switched off.
 *
 * Input/output are fixed-shape on purpose (`[1, 52]` int64 in, `[1, 512]`
 * float out), so the graph has no dynamic-shape ops and the mobile runtime has
 * nothing to re-allocate between queries.
 *
 * The tokenizer is a minimal BERT WordPiece implementation that mirrors
 * `src/albumcat/semantic.py::BertTokenizer` exactly (CJK split per character,
 * full-width folding, `##` continuation, `[CLS]`/`[SEP]` reserved). Getting
 * these two out of sync would produce vectors that still look plausible but
 * rank nonsense, so the two implementations are meant to be read side by side.
 *
 * Channel: `deckers.thibault/aves/text_encoder`
 *
 *   dir()             -> String                  app-private folder for the model
 *   status()          -> {loaded, dim, dir}
 *   load()            -> {ok, dim, dir} | {ok: false, error}
 *   encode({text})    -> {ok, dim, vector: FloatArray} | {ok: false, error}
 *   unload()          -> {ok}
 */
class TextEncoderHandler(private val context: Context) : MethodChannel.MethodCallHandler {

    companion object {
        const val CHANNEL = "deckers.thibault/aves/text_encoder"

        private const val TAG = "TextEncoderHandler"
        private const val MAX_TOKENS = 52
        private const val MAX_CHARS_PER_WORD = 100
        private const val MODEL_FILE = "text_encoder.onnx"
        private const val VOCAB_FILE = "vocab.txt"

        private val ASCII_PUNCTUATION = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~".toSet()
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    // Encoding is ~200 ms and must never block the platform thread.
    private val executor = Executors.newSingleThreadExecutor()

    private var env: OrtEnvironment? = null
    private var session: OrtSession? = null
    private var vocab: Map<String, Int> = emptyMap()
    private var padId = 0
    private var unkId = 100
    private var clsId = 101
    private var sepId = 102
    private var dim = 0
    private var folder: String? = null

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "dir" -> result.success(semanticDir().absolutePath)
            "status" -> result.success(status())
            "load" -> run(result) { load() }
            "unload" -> run(result) {
                unload()
                mapOf<String, Any>("ok" to true)
            }
            "encode" -> {
                val text = call.argument<String>("text")?.trim().orEmpty()
                run(result) { encode(text) }
            }
            else -> result.notImplemented()
        }
    }

    // -- plumbing ---------------------------------------------------------- #

    private fun run(result: MethodChannel.Result, block: () -> Map<String, Any>) {
        executor.execute {
            val payload = try {
                block()
            } catch (t: Throwable) {
                Log.w(TAG, "call failed", t)
                mapOf<String, Any>("ok" to false, "error" to (t.message ?: t.javaClass.simpleName))
            }
            mainHandler.post { result.success(payload) }
        }
    }

    /** App-private storage: no permission, wiped on uninstall, not user-visible. */
    private fun semanticDir(): File {
        val dir = File(context.filesDir, "semantic")
        if (!dir.exists()) dir.mkdirs()
        return dir
    }

    private fun status(): Map<String, Any> = mapOf(
        "loaded" to (session != null),
        "dim" to dim,
        "dir" to (folder ?: semanticDir().absolutePath),
        "model" to MODEL_FILE,
    )

    // -- load -------------------------------------------------------------- #

    private fun load(): Map<String, Any> {
        val dir = semanticDir()
        val modelFile = File(dir, MODEL_FILE)
        val vocabFile = File(dir, VOCAB_FILE)

        if (!modelFile.exists()) {
            return mapOf("ok" to false, "error" to "缺少 $MODEL_FILE（先从电脑端下载）")
        }
        if (!vocabFile.exists()) {
            return mapOf("ok" to false, "error" to "缺少 $VOCAB_FILE（先从电脑端下载）")
        }

        unload()

        val environment = OrtEnvironment.getEnvironment()
        val options = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(2)
            setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        }
        val loaded = environment.createSession(modelFile.absolutePath, options)

        val table = HashMap<String, Int>(60000)
        var index = 0
        vocabFile.bufferedReader(Charsets.UTF_8).useLines { lines ->
            for (line in lines) {
                table[line.trimEnd('\r')] = index
                index++
            }
        }
        if (table.isEmpty()) {
            loaded.close()
            return mapOf("ok" to false, "error" to "$VOCAB_FILE 是空的")
        }

        padId = table["[PAD]"] ?: 0
        unkId = table["[UNK]"] ?: 100
        clsId = table["[CLS]"] ?: 101
        sepId = table["[SEP]"] ?: 102

        val output = loaded.outputInfo.values.firstOrNull()?.info as? TensorInfo
        dim = output?.shape?.lastOrNull()?.toInt() ?: 0

        env = environment
        session = loaded
        vocab = table
        folder = dir.absolutePath

        Log.i(TAG, "text tower loaded: dim=$dim, vocab=${table.size}")
        return mapOf("ok" to true, "dim" to dim, "dir" to dir.absolutePath)
    }

    private fun unload() {
        try {
            session?.close()
        } catch (t: Throwable) {
            Log.w(TAG, "close failed", t)
        }
        session = null
        dim = 0
    }

    // -- encode ------------------------------------------------------------ #

    private fun encode(text: String): Map<String, Any> {
        if (text.isEmpty()) return mapOf("ok" to false, "error" to "查询是空的")

        val current = session ?: return mapOf("ok" to false, "error" to "文本塔还没加载")
        val environment = env ?: return mapOf("ok" to false, "error" to "ONNX 环境未初始化")

        val (ids, mask) = buildInputs(text)
        val shape = longArrayOf(1, MAX_TOKENS.toLong())

        OnnxTensor.createTensor(environment, LongBuffer.wrap(ids), shape).use { idTensor ->
            OnnxTensor.createTensor(environment, LongBuffer.wrap(mask), shape).use { maskTensor ->
                OnnxTensor.createTensor(environment, LongBuffer.wrap(LongArray(MAX_TOKENS)), shape).use { typeTensor ->
                    val feed = HashMap<String, OnnxTensor>()
                    for (name in current.inputNames) {
                        when {
                            name.contains("input_ids") -> feed[name] = idTensor
                            name.contains("attention_mask") -> feed[name] = maskTensor
                            name.contains("token_type_ids") -> feed[name] = typeTensor
                        }
                    }
                    if (feed.isEmpty()) {
                        return mapOf("ok" to false, "error" to "模型输入名不认识：${current.inputNames}")
                    }

                    current.run(feed).use { output ->
                        val value = output[0]
                        if (value !is OnnxTensor) {
                            return mapOf("ok" to false, "error" to "模型输出不是张量")
                        }
                        val buffer = value.floatBuffer
                        val vector = FloatArray(buffer.remaining())
                        buffer.get(vector)

                        // L2 normalise here, not in Dart: the PC side does the
                        // same (semantic.py::_l2_normalize), and a vector that
                        // is normalised on one side only ranks by magnitude.
                        var norm = 0f
                        for (v in vector) norm += v * v
                        norm = sqrt(norm)
                        if (norm > 0f) {
                            for (i in vector.indices) vector[i] /= norm
                        }

                        return mapOf("ok" to true, "dim" to vector.size, "vector" to vector)
                    }
                }
            }
        }
    }

    // -- tokenizer --------------------------------------------------------- #

    private fun buildInputs(text: String): Pair<LongArray, LongArray> {
        val pieces = ArrayList<Int>(MAX_TOKENS)
        pieces.add(clsId)

        outer@ for (word in basicTokenize(text)) {
            for (piece in wordPiece(word)) {
                pieces.add(vocab[piece] ?: unkId)
                // keep one slot for [SEP]: a truncated sequence without the
                // closing token changes the pooled output
                if (pieces.size >= MAX_TOKENS - 1) break@outer
            }
        }
        pieces.add(sepId)

        val ids = LongArray(MAX_TOKENS) { padId.toLong() }
        val mask = LongArray(MAX_TOKENS)
        for ((i, id) in pieces.withIndex()) {
            if (i >= MAX_TOKENS) break
            ids[i] = id.toLong()
            mask[i] = 1L
        }
        return ids to mask
    }

    /** clean -> full-width fold -> CJK per character -> punctuation split -> lowercase */
    private fun basicTokenize(text: String): List<String> {
        val words = ArrayList<String>()
        val buffer = StringBuilder()

        fun flush() {
            if (buffer.isNotEmpty()) {
                words.add(buffer.toString())
                buffer.setLength(0)
            }
        }

        for (raw in text) {
            val ch = foldFullWidth(raw)
            when {
                ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' -> flush()
                isCjk(ch) -> {
                    flush()
                    words.add(ch.toString())
                }
                isPunctuation(ch) -> {
                    flush()
                    words.add(ch.toString())
                }
                else -> buffer.append(ch)
            }
        }
        flush()

        return words.map { it.lowercase() }
    }

    private fun wordPiece(word: String): List<String> {
        if (word.length > MAX_CHARS_PER_WORD) return listOf("[UNK]")

        val pieces = ArrayList<String>()
        var start = 0
        while (start < word.length) {
            var end = word.length
            var found: String? = null
            while (start < end) {
                var chunk = word.substring(start, end)
                if (start > 0) chunk = "##$chunk"
                if (vocab.containsKey(chunk)) {
                    found = chunk
                    break
                }
                end--
            }
            if (found == null) return listOf("[UNK]")
            pieces.add(found)
            start = end
        }
        return pieces
    }

    private fun foldFullWidth(ch: Char): Char {
        val code = ch.code
        return when {
            code == 0x3000 -> ' '
            code in 0xFF01..0xFF5E -> (code - 0xFEE0).toChar()
            else -> ch
        }
    }

    private fun isCjk(ch: Char): Boolean {
        val code = ch.code
        return code in 0x4E00..0x9FFF ||
            code in 0x3400..0x4DBF ||
            code in 0xF900..0xFAFF ||
            code in 0x3040..0x30FF
    }

    private fun isPunctuation(ch: Char): Boolean {
        if (ch in ASCII_PUNCTUATION) return true
        return when (Character.getType(ch).toByte()) {
            Character.CONNECTOR_PUNCTUATION,
            Character.DASH_PUNCTUATION,
            Character.START_PUNCTUATION,
            Character.END_PUNCTUATION,
            Character.INITIAL_QUOTE_PUNCTUATION,
            Character.FINAL_QUOTE_PUNCTUATION,
            Character.OTHER_PUNCTUATION,
            -> true

            else -> false
        }
    }
}
