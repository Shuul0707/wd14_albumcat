package com.albumcat

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * One screen: point it at the PC, pick the folder, press go.
 *
 * Everything is built in code -- the app is a utility, a layout XML file per
 * widget would be more code than it saves.
 */
class MainActivity : Activity() {

    private val prefs by lazy { getSharedPreferences("albumcat", MODE_PRIVATE) }

    private lateinit var serverField: EditText
    private lateinit var tokenField: EditText
    private lateinit var folderLabel: TextView
    private lateinit var dryRunBox: CheckBox
    private lateinit var skipBox: CheckBox
    private lateinit var statusLabel: TextView
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var runButton: Button
    private lateinit var undoButton: Button
    private lateinit var testButton: Button
    private lateinit var stopButton: Button

    private var treeUri: Uri? = null
    @Volatile private var busy = false

    /** The runner currently working, so the stop button has something to poke. */
    @Volatile private var activeRunner: TagRunner? = null

    /**
     * Set by [client] when the typed address had to be repaired.
     *
     * Emitted by the caller rather than by [client] itself: `start()` clears the
     * log immediately after calling it, so a notice logged in there would be
     * wiped before anyone could read it.
     */
    private var addressNotice: String? = null

    /**
     * The log keeps only the last [LOG_MAX] lines.
     *
     * A real album is thousands of photos and one line each. `TextView.append`
     * re-measures the whole buffer every time, so an uncapped log gets slower
     * and slower and holds every line in memory -- on a 5 000-photo run that is
     * a visible stall by the end. The full record is in the journal file and in
     * the server's own log; this pane is for watching, not for keeping.
     */
    private val logLines = ArrayList<String>()
    private companion object {
        const val LOG_MAX = 400
        const val LOG_DROP = 100

        /**
         * Which build this is, printed on screen.
         *
         * Bumped by hand with every APK handed to a user. "Did you install the
         * new one?" stops being a guess when the answer is rendered by the app
         * itself: a screenshot is then evidence of the build behind it.
         *
         * This exists because a report could not be reproduced: the screenshot
         * showed a layout with no title line and no token field, while its
         * toast text existed only in the current source. Nothing in the repo
         * could tell the two builds apart, so neither could I.
         */
        const val BUILD_STAMP = "b11 · 2026-10-13"
    }

    private val pickFolder = 1001

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        restore()

        // Persist as the user types.
        //
        // This is the fix for "我填了地址，还是提示我填地址". The address used to
        // be saved only by "开始" and by the folder picker, so anything that
        // recreated the activity in between -- a rotation, a low-memory process
        // kill while the picker was open -- ran restore() and refilled the box
        // with the *previous* value, i.e. empty. The empty box then showed the
        // grey placeholder "http://192.168.1.10:8899", which looks filled in.
        val autosave = object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
            override fun afterTextChanged(s: Editable?) = save()
        }
        serverField.addTextChangedListener(autosave)
        tokenField.addTextChangedListener(autosave)
        dryRunBox.setOnCheckedChangeListener { _, _ -> save() }
        skipBox.setOnCheckedChangeListener { _, _ -> save() }
    }

    // ------------------------------------------------------------------ #
    // UI

    private fun buildUi(): View {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        root.addView(title("相册分类 · 按角色给照片打标签"))

        root.addView(hint("电脑端先运行：  tools\\albumcat.cmd serve"))
        root.addView(hint(BUILD_STAMP))

        // Both boxes get a visible label, not just a placeholder. A placeholder
        // vanishes the moment the box has text, so a filled box and an empty one
        // look alike -- and when a system bar covered this row, the token box
        // was the only field a user could see, which is exactly how the address
        // ended up there. A label stays put and says what the box is for.
        root.addView(hint("电脑端地址"))
        serverField = EditText(this).apply {
            hint = "http://192.168.1.10:8899"
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        root.addView(serverField)

        root.addView(hint("令牌（电脑端没设 --token 就留空）"))
        tokenField = EditText(this).apply {
            hint = "没设就留空"
            setSingleLine()
        }
        root.addView(tokenField)

        testButton = Button(this).apply {
            text = "测试连接"
            setOnClickListener { testConnection() }
        }
        root.addView(testButton)

        root.addView(spacer(pad))

        folderLabel = TextView(this).apply {
            text = "未选择文件夹"
            setTextColor(Color.DKGRAY)
        }
        root.addView(folderLabel)
        root.addView(Button(this).apply {
            text = "选择相册文件夹（只需一次）"
            setOnClickListener { pickTree() }
        })

        dryRunBox = CheckBox(this).apply {
            text = "只预览，不写入（建议第一次先勾上）"
            isChecked = true
        }
        skipBox = CheckBox(this).apply {
            text = "跳过已经打过标签的照片"
            isChecked = true
        }
        root.addView(dryRunBox)
        root.addView(skipBox)

        runButton = Button(this).apply {
            text = "开始"
            setOnClickListener { start() }
        }
        root.addView(runButton)

        stopButton = Button(this).apply {
            text = "停止"
            isEnabled = false
            setOnClickListener {
                val r = activeRunner
                if (r == null) toast("现在没有在跑的任务")
                else {
                    r.requestStop()
                    log("已请求停止 —— 正在处理的那张会写完，之后不再继续。")
                }
            }
        }
        root.addView(stopButton)

        undoButton = Button(this).apply {
            text = "撤销上一次写入"
            setOnClickListener { undo() }
        }
        root.addView(undoButton)

        statusLabel = TextView(this).apply {
            setPadding(0, pad, 0, pad)
            typeface = Typeface.DEFAULT_BOLD
        }
        root.addView(statusLabel)

        logView = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
        }
        // The log takes the leftover height and scrolls on its own. It cannot
        // live inside another ScrollView -- a ScrollView hands its child
        // unbounded height, so the weight below would collapse to nothing.
        logScroll = ScrollView(this).apply {
            addView(logView, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
        }
        root.addView(logScroll)

        // The window is drawn edge to edge -- targetSdk 35 forces it, asked for
        // or not -- so without this the status bar sits on top of the first rows
        // of the form. That is not hypothetical: it is what hid the address box
        // on a real phone and made "填了地址还被要求填地址" look like the user's
        // mistake. The theme opts out where it can (values-v35), and this
        // listener is what keeps it correct where it cannot, e.g. API 36.
        val padLeft = root.paddingLeft
        val padTop = root.paddingTop
        val padRight = root.paddingRight
        val padBottom = root.paddingBottom
        root.setOnApplyWindowInsetsListener { v, insets ->
            v.setPadding(
                padLeft,
                padTop + insets.systemWindowInsetTop,
                padRight,
                padBottom + insets.systemWindowInsetBottom,
            )
            insets
        }

        return root
    }

    private fun title(t: String) = TextView(this).apply {
        text = t
        textSize = 18f
        typeface = Typeface.DEFAULT_BOLD
        setPadding(0, 0, 0, 8)
    }

    private fun hint(t: String) = TextView(this).apply {
        text = t
        textSize = 12f
        setTextColor(Color.GRAY)
    }

    private fun spacer(px: Int) = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(1, px)
    }

    private fun log(line: String) {
        runOnUiThread {
            logLines += line
            if (logLines.size > LOG_MAX) {
                val drop = logLines.size - LOG_MAX + LOG_DROP
                repeat(drop) { logLines.removeAt(0) }
                logLines.add(0, "… （只保留最近的 $LOG_MAX 行，完整记录在电脑端的日志里）")
            }
            logView.text = logLines.joinToString("\n")
            statusLabel.text = line
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun toast(text: String) = runOnUiThread { Toast.makeText(this, text, Toast.LENGTH_SHORT).show() }

    // ------------------------------------------------------------------ #
    // state

    private fun restore() {
        serverField.setText(prefs.getString("server", ""))
        tokenField.setText(prefs.getString("token", ""))
        dryRunBox.isChecked = prefs.getBoolean("dryRun", true)
        skipBox.isChecked = prefs.getBoolean("skip", true)
        prefs.getString("tree", null)?.let { saved ->
            treeUri = Uri.parse(saved)
            folderLabel.text = "文件夹：" + (treeUri?.lastPathSegment ?: saved)
        }
    }

    private fun save() {
        val e = prefs.edit()
            .putString("server", serverField.text.toString().trim())
            .putString("token", tokenField.text.toString().trim())
            .putBoolean("dryRun", dryRunBox.isChecked)
            .putBoolean("skip", skipBox.isChecked)
        // Written only when known. The autosave watcher above fires while the
        // user types, long before a folder is picked, and putting null here
        // would erase the persisted SAF grant -- which the app cannot rebuild
        // on its own, only the user can, by picking the folder again.
        treeUri?.let { e.putString("tree", it.toString()) }
        e.apply()
    }

    /**
     * The address as the app will actually use it, with the field rewritten to
     * match so what you see is what it dials.
     *
     * Repairing silently would be worse than failing: the old check rejected a
     * capitalised `Http://` while the box visibly held an address, so the user
     * retyped the same thing and got the same error. Say what was changed, or
     * say which character is wrong -- see ServerAddress.kt.
     *
     * The notice is stashed rather than logged here, because `start()` clears
     * the log right after calling this: logging on the spot would print the one
     * line explaining the address and then immediately wipe it. Callers emit it
     * themselves, after their own clear.
     */
    private fun client(): TaggerClient {
        val typed = serverField.text.toString().trim()
        // An empty field is not the same as "never filled in". Every keystroke
        // is already persisted, so a saved value is a better answer than
        // telling someone who is looking at a filled-in box that it is empty.
        val saved = prefs.getString("server", "").orEmpty().trim()
        val token = tokenField.text.toString().trim()
        // The address box is the first field in the form, i.e. the first thing a
        // system bar, a tall ActionBar or a small screen hides. If it reads
        // empty while the *token* box holds something address-shaped, believe
        // the token box and say so: filling in the only field you can see is a
        // reasonable thing to do, and this is not the user's mistake.
        val misplaced = typed.isEmpty() && saved.isEmpty() &&
            ServerAddress.looksLikeAddress(token)
        val pick = when {
            misplaced -> token
            typed.isEmpty() -> saved
            else -> typed
        }
        val base = try {
            ServerAddress.normalize(pick)
        } catch (e: ServerAddress.BadAddress) {
            // Name what the field actually held. Without it, "you did not fill
            // it in" is indistinguishable from a misread, and that cost a whole
            // round trip on a bug that could not be reproduced here.
            val seen = if (typed.isEmpty()) "空" else "「$typed」"
            throw IllegalStateException("${e.message}\n（输入框里读到：$seen）")
        }
        addressNotice = when {
            misplaced -> "地址是从「令牌」那一栏读到的（`$base`），令牌栏已清空"
            base != pick -> "地址已按 `$base` 使用（你填的是 `$pick`）"
            typed.isEmpty() -> "输入框当时是空的，用了上次保存的 `$base`"
            else -> null
        }
        if (base != typed) serverField.setText(base)
        if (misplaced) tokenField.setText("")
        return TaggerClient(base, tokenField.text.toString().trim())
    }

    private fun setBusy(on: Boolean) {
        busy = on
        runOnUiThread {
            runButton.isEnabled = !on
            undoButton.isEnabled = !on
            testButton.isEnabled = !on
            stopButton.isEnabled = on
        }
    }

    // ------------------------------------------------------------------ #
    // actions

    private fun pickTree() {
        startActivityForResult(
            Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
                addFlags(
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                        Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                )
            },
            pickFolder,
        )
    }

    @Deprecated("plain Activity, no androidx.activity here")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != pickFolder || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        try {
            contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (e: SecurityException) {
            log("注意：系统没给长期权限，重启 App 后可能要重新选一次（${e.message}）")
        }
        treeUri = uri
        folderLabel.text = "文件夹：${uri.lastPathSegment ?: uri}"
        save()
        log("已选择：$uri")
    }

    /** A toast truncates, and the address errors carry a second line. */
    private fun addressProblem(e: Exception) {
        val msg = e.message ?: "地址不对"
        if (msg.contains('\n')) {
            log("⚠ $msg")
            toast(msg.lineSequence().first())
        } else {
            toast(msg)
        }
    }

    private fun testConnection() {
        val c = try { client() } catch (e: Exception) { addressProblem(e); return }
        addressNotice?.let { log(it) }
        setBusy(true)
        Thread {
            try {
                val health = c.health()
                val model = health.optJSONObject("model")
                log("连接成功 ✓")
                log("  模型：${model?.optString("session_model") ?: "?"}")
                log("  后端：${model?.optJSONArray("providers")}")
                log("  插件：${health.optJSONArray("plugins")}")
                log("  嵌入维度：${model?.optInt("embedding_dim", 0)}")
            } catch (e: Exception) {
                // The raw exception is four near-identical lines for four
                // different mistakes; see NetDiagnosis.kt.
                diagnoseNetworkError(e).lines().forEach { log(it) }
            } finally {
                setBusy(false)
            }
        }.start()
    }

    private fun start() {
        if (busy) return
        val uri = treeUri
        if (uri == null) { toast("先选一个文件夹"); return }
        val c = try { client() } catch (e: Exception) { addressProblem(e); return }

        save()
        logView.text = ""
        logLines.clear()
        // After the clear, not before: see the addressNotice declaration.
        addressNotice?.let { log(it) }
        val options = TagRunner.Options(
            dryRun = dryRunBox.isChecked,
            skipAlreadyTagged = skipBox.isChecked,
        )
        setBusy(true)
        val runner = TagRunner(this, c, Journal(this))
        activeRunner = runner

        Thread {
            try {
                val started = System.currentTimeMillis()
                val summary = runner.run(uri, options) { event ->
                    when (event) {
                        is TagRunner.Event.Scanning -> {
                            log("找到 ${event.found} 张能打标签的图片（JPEG / PNG）")
                            if (event.otherImages > 0) {
                                log(
                                    "另有 ${event.otherImages} 张 HEIC/WebP 等格式的图片被跳过 —— " +
                                    "现在只写 JPEG 和 PNG 的 XMP（见 README「已知限制」）"
                                )
                            }
                            if (event.truncated) log("文件夹太大或嵌套太深，只扫了前面一部分")
                            if (event.found == 0) {
                                log("这个文件夹里没有 JPEG 或 PNG。换一个文件夹，或者把手机相机设成 JPEG。")
                            }
                        }
                        is TagRunner.Event.Message -> log(event.text)
                        is TagRunner.Event.Item ->
                            log("[${event.index}/${event.total}] ${event.name}  ${event.line}")
                    }
                }
                val secs = (System.currentTimeMillis() - started) / 1000.0
                log("")
                log(
                    if (summary.dryRun) "预览完成：共 ${summary.total} 张，用时 ${"%.1f".format(secs)} 秒"
                    else "完成：写入 ${summary.written} 张，跳过 ${summary.skipped} 张，失败 ${summary.failed} 张，" +
                        "用时 ${"%.1f".format(secs)} 秒（运行号 ${summary.runId}）"
                )
                if (summary.aborted) {
                    if (summary.abortReason == TagRunner.STOPPED) {
                        // Stopping is a normal thing to do on a big album, not a
                        // failure -- say so, and say that nothing was lost.
                        log("已停止：处理了 ${summary.processed} / ${summary.total} 张。")
                        log("停下时正在写的那张已经写完并校验过，没有半写的照片。")
                        if (summary.written > 0) {
                            log("已写好的 ${summary.written} 张是有效的；下次再跑会跳过它们。")
                        }
                    } else {
                        log("⚠ 半路停了：处理 ${summary.processed} / ${summary.total} 张。")
                        if (summary.written > 0) {
                            log("已写好的 ${summary.written} 张是有效的，不用重来；修好之后直接再跑一次，它们会被跳过。")
                        }
                    }
                }
                val withChars = summary.results.count { it.characters.isNotEmpty() }
                log("认出角色的：$withChars / ${summary.results.size}")
                val review = summary.results.count { it.needsReview }
                if (review > 0) log("需要人工复核的：$review 张（分数在中间区间，没有自动打标）")
            } catch (e: Exception) {
                log("出错：${e.message}")
                diagnoseNetworkError(e).lines().forEach { log(it) }
            } finally {
                activeRunner = null
                setBusy(false)
            }
        }.start()
    }

    private fun undo() {
        val runs = try { Journal(this).runs() } catch (e: Exception) { emptyList() }
        if (runs.isEmpty()) { toast("没有可撤销的记录"); return }
        val (runId, count) = runs.first()
        log("撤销运行 $runId（$count 张）…")
        val runner = TagRunner(this, TaggerClient("http://127.0.0.1", ""), Journal(this))
        setBusy(true)
        activeRunner = runner
        Thread {
            try {
                runner.undo(runId) { event ->
                    when (event) {
                        is TagRunner.Event.Item ->
                            log("[${event.index}/${event.total}] ${event.name}  ${event.line}")
                        is TagRunner.Event.Message -> log(event.text)
                        is TagRunner.Event.Scanning -> Unit
                    }
                }
            } catch (e: Exception) {
                log("撤销出错：${e.message}")
            } finally {
                activeRunner = null
                setBusy(false)
            }
        }.start()
    }
}
