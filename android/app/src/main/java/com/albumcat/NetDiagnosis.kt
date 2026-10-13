package com.albumcat

/**
 * Turn a raw connect failure into something the user can act on.
 *
 * The first thing anyone does with this app on a real phone is mistype an
 * address or forget to open the firewall, and the failure they see is a
 * wall of Java:
 *
 *     java.net.ConnectException: failed to connect to /192.168.1.23:8899
 *       (port 8899) from /192.168.1.5 (port 41234) after 8000ms:
 *       isConnected failed: ETIMEDOUT (Connection timed out)
 *
 * Four completely different mistakes produce four *nearly identical* lines like
 * that, and each one has a different fix:
 *
 *   - `ECONNREFUSED`  the packet arrived, nothing is listening -> start `serve`,
 *                     or the port is wrong
 *   - `ETIMEDOUT`     the packet went out and nothing came back -> almost always
 *                     the Windows firewall on the PC
 *   - `EHOSTUNREACH`  the phone has no route at all -> wrong address, or the
 *     / `ENETUNREACH` phone is not on the same network
 *   - `UnknownHost`   the address is malformed
 *
 * Guessing between those from a phone screen costs a round trip each time, so we
 * classify instead of printing the exception. This file deliberately imports
 * nothing from Android, which is what lets `tools\check_android_client.py`
 * compile it for the desktop JVM and assert the classification against the real
 * strings Android produces.
 */

enum class NetFaultKind {
    /** TCP reached the host; nothing was listening on that port. */
    REFUSED,

    /** Packet sent, no reply inside the connect timeout. */
    TIMEOUT,

    /** No route from this phone to that address. */
    NO_ROUTE,

    /** The address could not be understood/resolved. */
    BAD_ADDRESS,

    /** We got an HTTP response, but not a good one. */
    SERVER_ERROR,

    /** Anything else; the raw message is the only clue. */
    UNKNOWN,
}

/**
 * @param phoneAddress this phone's own address, as Android embeds it in connect
 *   errors ("... from /192.168.1.5 ..."). Null when the message did not carry
 *   one.
 * @param serverAddress the address we tried to reach, likewise parsed. Comparing
 *   the first three octets of the two is the quickest way to spot the classic
 *   "phone is on the WiFi, PC is on Ethernet" mistake.
 */
data class NetFault(
    val kind: NetFaultKind,
    val headline: String,
    val hints: List<String>,
    val raw: String,
    val phoneAddress: String? = null,
    val serverAddress: String? = null,
) {
    /** True when we know both addresses and their /24 differs. */
    val subnetMismatch: Boolean
        get() {
            val a = phoneAddress ?: return false
            val b = serverAddress ?: return false
            return a.substringBeforeLast('.') != b.substringBeforeLast('.')
        }
}

private val FROM_ADDRESS = Regex("""from /(\d{1,3}(?:\.\d{1,3}){3})""")
private val TARGET_ADDRESS = Regex("""failed to connect to /?(\d{1,3}(?:\.\d{1,3}){3})""")

/**
 * The exception and everything it wraps.
 *
 * Android frequently reports the real reason on a *cause* rather than the
 * exception we catch, and the useful one is not always the outermost.
 */
private fun Throwable.chain(): List<Throwable> {
    val out = ArrayList<Throwable>()
    var t: Throwable? = this
    var guard = 0
    while (t != null && guard++ < 16) {
        out += t
        t = t.cause
    }
    return out
}

/**
 * Classify [e] and explain the fix.
 *
 * Two sources of truth, because neither alone is enough:
 *   * the **exception type** is reliable when it is specific
 *     (`NoRouteToHostException`, `UnknownHostException`) but useless for the
 *     most common case -- a dropped packet arrives as a plain
 *     `ConnectException` *and* as `SocketTimeoutException`, sharing types with
 *     the "nothing is listening" case.
 *   * the **message** distinguishes those two ("ECONNREFUSED" vs
 *     "ETIMEDOUT"), but an Android `SocketTimeoutException` often carries no
 *     "timeout" word at all -- just "failed to connect to /10.0.0.5:8899 after
 *     8000ms". Matching text alone files that one under UNKNOWN.
 *
 * Order matters. A refused connection's message also reads "failed to connect
 * to", and an unreachable host can have "Connection timed out" appended, so the
 * specific markers win over the generic ones.
 */
fun diagnoseNetworkError(e: Throwable): NetFault {
    val raw = e.message ?: e.toString()
    val lower = raw.lowercase()
    val phone = FROM_ADDRESS.find(raw)?.groupValues?.get(1)
    val server = TARGET_ADDRESS.find(raw)?.groupValues?.get(1)
    val chain = e.chain()

    fun anyType(vararg names: String) =
        chain.any { t -> names.any { t.javaClass.simpleName == it } }

    fun fault(kind: NetFaultKind, headline: String, vararg hints: String) =
        NetFault(kind, headline, hints.toList(), raw, phone, server)

    val noRoute = anyType("NoRouteToHostException") ||
        "ehostunreach" in lower || "no route to host" in lower ||
        "enetunreach" in lower || "network is unreachable" in lower

    val refused = !noRoute && ("econnrefused" in lower || "connection refused" in lower)

    val addressUnknown = anyType("UnknownHostException") ||
        "unknownhost" in lower || "unable to resolve host" in lower

    val timedOut = anyType("SocketTimeoutException") ||
        "etimedout" in lower || "timed out" in lower || "timeout" in lower

    return when {
        noRoute ->
            fault(
                NetFaultKind.NO_ROUTE,
                "手机没有到那个地址的路",
                "这个地址多半不是那台电脑的。在电脑上跑 tools\\albumcat.cmd serve，" +
                    "用它打印出来的那个地址。",
                "也可能手机和电脑不在同一个网络：让手机连上和电脑同一个 WiFi / 路由器。",
                "手机开着 VPN 的话先关掉再试。",
            )

        refused ->
            fault(
                NetFaultKind.REFUSED,
                "电脑回话了，但那个端口上没人在听",
                "最常见的原因：电脑上的服务没在跑。",
                "在电脑上执行  tools\\albumcat.cmd serve ，再点一次「测试连接」。",
                "如果服务确实在跑，就是端口写错了 —— 地址末尾应当是 :8899。",
            )

        addressUnknown ->
            fault(
                NetFaultKind.BAD_ADDRESS,
                "这个地址没看懂",
                "要填完整地址，形如  http://192.168.1.10:8899",
                "只用 IP，不要用电脑名 —— 手机一般解析不了电脑名。",
                "别漏掉 http:// 和端口号。",
            )

        // Reaching here via `timedOut` and via a bare "failed to connect to"
        // are the same situation for the user: the packets went nowhere.
        timedOut || "failed to connect" in lower ->
            fault(
                NetFaultKind.TIMEOUT,
                "地址看着是对的，但一直没人回话",
                "电脑上的 Windows 防火墙最可能把手机的包丢了 —— 这是本机实测过的情况。",
                "在电脑上（需要管理员）跑  tools\\allow_firewall.cmd ，然后重试。",
                "先确认服务真的在跑：电脑浏览器打开 http://127.0.0.1:8899/health 应能看到 JSON。",
                "还不行就对比上面的两个地址：不在同一个网段就连不通。",
            )

        lower.startsWith("http ") ->
            fault(NetFaultKind.SERVER_ERROR, "服务端返回了错误", raw)

        else -> fault(NetFaultKind.UNKNOWN, "连接失败", raw)
    }
}

/** Render a fault as the lines the log view shows. */
fun NetFault.lines(): List<String> {
    val out = ArrayList<String>()
    out += headline
    hints.forEach { out += "  · $it" }
    if (subnetMismatch) {
        out += "  ! 手机在 $phoneAddress，电脑在 $serverAddress —— 两者不在同一个网段"
    }
    return out
}
