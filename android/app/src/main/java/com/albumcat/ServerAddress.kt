package com.albumcat

import java.net.URI

/**
 * Turns whatever the user typed into a base URL, or explains why it cannot.
 *
 * This exists because "填了地址却还被要求填地址" was a real report, and the old
 * check -- `if (!base.startsWith("http"))` in MainActivity -- was the cause:
 *
 *  * it is **case-sensitive**, so an IME that capitalised the first letter
 *    ("Http://192.168.1.10:8899") failed it while the box plainly held an
 *    address. The user then retypes the same thing and gets the same error.
 *  * a Chinese IME types full-width `：` and `／`. Those pass the prefix test
 *    and then blow up deep inside `URL()`, with a message that names no cause.
 *  * it never echoed *what it saw*, so "请填写电脑端地址" read as "the box is
 *    empty" even when the box was full -- which is exactly the wrong thing to
 *    tell someone who just filled it in.
 *
 * So: repair what is unambiguous (case, full-width characters, a missing or
 * malformed scheme, a missing port), and quote the offending text back when
 * something genuinely cannot be read.
 *
 * Pure JDK on purpose -- `tools/check_android_client.py` compiles this file on
 * the desktop JVM and asserts the table of cases at the bottom of this file.
 */
object ServerAddress {

    /** `albumcat serve --port` defaults to this, so a bare IP is completed to it. */
    const val DEFAULT_PORT = 8899

    /** Raised with a message meant to be shown to the user as-is. */
    class BadAddress(message: String) : IllegalArgumentException(message)

    private val SCHEME = Regex("^([A-Za-z][A-Za-z0-9+.\\-]*):(//)?(.*)$", RegexOption.DOT_MATCHES_ALL)

    /** Characters that are fine in `scheme://host:port`. */
    private const val OK_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.:-_[]%@"

    fun normalize(raw: String): String {
        val cleaned = clean(raw)
        if (cleaned.isEmpty()) {
            throw BadAddress(
                "还没填电脑端地址。电脑上跑 tools\\albumcat.cmd serve，" +
                    "用它打印出来的地址，例如 http://192.168.1.10:$DEFAULT_PORT"
            )
        }

        val withScheme = addScheme(cleaned)
        val uri = try {
            URI(withScheme)
        } catch (e: Exception) {
            throw BadAddress("这个地址看不懂：`$cleaned`${oddCharHint(cleaned, withScheme)}")
        }

        val scheme = (uri.scheme ?: "").lowercase()
        if (scheme != "http" && scheme != "https") {
            throw BadAddress("只支持 http:// 和 https://，这里是 `$scheme://`：`$cleaned`")
        }

        val host = uri.host
        if (host.isNullOrEmpty()) {
            throw BadAddress("这个地址里没有主机名：`$cleaned`${oddCharHint(cleaned, withScheme)}")
        }

        val port = if (uri.port in 1..65535) uri.port else DEFAULT_PORT
        // URI.getHost() hands back a literal IPv6 address *with* its brackets,
        // so re-bracketing blindly yields `http://[[::1]]:8899`.
        val bare = host.removePrefix("[").removeSuffix("]")
        val bracketed = if (bare.contains(':')) "[$bare]" else bare
        return "$scheme://$bracketed:$port"
    }

    /**
     * Could this text have been meant as the PC address?
     *
     * Needed because the address can end up in the *token* box: the address box
     * is the first row of the form, which is what a system bar covers first, and
     * a user who can only see one field fills in that one. `normalize()` alone is
     * too permissive to answer this -- it happily completes a bare word to
     * `http://reimu:8899` -- so ask for something endpoint-shaped: a dotted host,
     * a bracketed IPv6, `localhost`, or an explicit port.
     */
    fun looksLikeAddress(raw: String): Boolean {
        val cleaned = clean(raw)
        if (cleaned.isEmpty()) return false
        try {
            normalize(cleaned)
        } catch (e: BadAddress) {
            return false
        }
        if (PORT.find(cleaned) != null) return true
        val host = try {
            URI(normalize(cleaned)).host.orEmpty()
        } catch (e: Exception) {
            return false
        }
        return host.contains('.') || host.contains(':') || host == "localhost"
    }

    /** `:8899`, but not the colon in an IPv6 address. */
    private val PORT = Regex(":\\d{1,5}(?:[/?#]|$)")

    /**
     * Strip what cannot be meant, and fold full-width characters to ASCII.
     *
     * The full-width fold matters more than it looks: `：` (U+FF1A) and `／`
     * (U+FF0F) are what a Chinese IME produces, and they are visually almost
     * identical to `:` and `/` -- a difference nobody can spot by staring at
     * the field, and one that makes the URL parse fail with no clue.
     */
    private fun clean(raw: String): String {
        val sb = StringBuilder(raw.length)
        for (c in raw) {
            val code = c.code
            when {
                // Full-width ！ (U+FF01) .. ～ (U+FF5E) sit exactly 0xFEE0 above ASCII.
                code in 0xFF01..0xFF5E -> sb.append((code - 0xFEE0).toChar())
                // Ideographic / non-breaking space -> ordinary space, trimmed later.
                c == '\u3000' || c == '\u00A0' -> sb.append(' ')
                // Control characters, zero-width joiners, a byte-order mark: drop.
                code < 0x20 || code == 0x7F -> {}
                code in 0x200B..0x200F || code == 0xFEFF -> {}
                else -> sb.append(c)
            }
        }
        return sb.toString().trim()
    }

    /**
     * Give the string a scheme, without mistaking `localhost:8899` for one.
     *
     * `localhost` parses as a syntactically valid scheme, so "there is a colon"
     * is not enough to decide. Only `//` after the colon, or a scheme we
     * actually support, counts as the user having written one.
     */
    private fun addScheme(s: String): String {
        val m = SCHEME.find(s)
        if (m != null) {
            val scheme = m.groupValues[1].lowercase()
            val hadSlashes = m.groupValues[2] == "//"
            if (hadSlashes || scheme == "http" || scheme == "https") {
                // trimStart('/') matters for the typo `http:/1.2.3.4:8899`, where
                // `//` is not present so the slash stays in the remainder and the
                // naive join produces `http:///1.2.3.4:8899` -- an empty host.
                return "$scheme://" + m.groupValues[3].trimStart('/')
            }
        }
        return "http://$s"
    }

    /**
     * Name the first character that has no business being in an address.
     *
     * This is the whole point of the class: instead of repeating "请填写地址",
     * say which character is wrong and what its code point is, so a full-width
     * colon or a stray quote is findable in a two-line message.
     */
    private fun oddCharHint(original: String, attempted: String): String {
        val bad = attempted.indexOfFirst { it !in OK_CHARS }
        if (bad < 0) return ""
        val c = attempted[bad]
        val code = c.code
        val name = when {
            code in 0xFF01..0xFF5E -> "，这是全角字符，应改成半角"
            c == ' ' -> "，地址中间有空格"
            code < 0x20 -> "，地址里有换行或制表符"
            else -> ""
        }
        return "\n第 ${bad + 1} 个字符是 `$c` (U+${code.toString(16).uppercase().padStart(4, '0')})$name"
    }
}
