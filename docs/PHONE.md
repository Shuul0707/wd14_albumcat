# 手机端

> Aves fork 怎么用、连不上先看哪里、没有 Wi-Fi 时插数据线、打标中途断了会怎样。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 手机上怎么用

1. 电脑上起服务：`tools\albumcat.cmd serve`（默认 `:8899`；用 `--token` 时 App 里填同一串）。
2. 装 `dist\aves-fork-libre-debug-arm64.apk`（真机）或 `-x64`（模拟器）。
3. Aves 里授权照片访问 → 右上角 ✨（`AI 打标签`）→ 填电脑地址（如 `http://192.168.1.10:8899`）→
   **开始打标签**。App 只对**当前集合里可见的**照片取缩略图，按 8 张一批、批内 4 路并发取图发给电脑。
   （手机和电脑不在同一个局域网？插数据线跑一次 `tools\phone_link.cmd`，地址不用改，
   见下面「[没有 Wi-Fi 的时候](#没有-wi-fi-的时候插数据线备用通道)」。）
4. 标签随后出现在 Aves 的 `Tags` 页（层级形式 `角色|Shameimaru Aya`），点一下就筛出这个角色的照片。

设置项在 `lib/model/settings/modules/ai_tagging.dart`：地址、令牌、批大小 `8`、批内并发 `4`、最低分 `0.35`、
是否连通用标签一起打 `false`。取缩略图走 Aves 自己的 debug MethodChannel
（`deckers.thibault/aves/debug` 的 `getThumbnail`，release 里也无条件注册）。
**默认那条路是「自己解图」**：App 直接用 `BitmapFactory` 解原文件（`inJustDecodeBounds` 先读尺寸 →
power-of-two 采样 → `RGB_565` → 只缩到长边 448 **物理像素** → 按 EXIF 转正 → JPEG `quality = 88`），
`getByDecoding()` 解不出来才退回 glide → mediaStore → resolver 那三条系统路
（`ThumbnailMethod.decode` 排在 `android_debug_service.dart:175` 那个枚举的第一位）。
原图的像素不会被改，手机上也没有任何缩略图缓存可依赖——每轮都是真解码，所以计时稳定。
透明底的 PNG 会多一步**白底压平**（`ARGB_8888` 解 → 白底合成 → `RGB_565`），所以立绘不会变成黑底。
「448 物理像素」的坑在 `extentDip = 448 / devicePixelRatio`：`ThumbnailFetcher.kt:52` 拿到的 `widthDip`
还要乘一次屏幕密度，早先直接传 512 DIP，在 2.6 倍密度屏上就是 1344 px（面积 9 倍）。
`quality` 是本项目给 `BitmapUtils.getBytes()` / `DebugHandler.getThumbnail` 新加的旋钮（默认仍是 Aves 原本的 100）。
批内 4 路并发取图，取一张送一张（不等整批取完再上传）。

这条链路每一步都量过（安卓 15 模拟器，24 张，同一台电脑同一个服务端）：
**4.45 秒 → 3.25 秒 → 2.83 秒**（185 → 135 → 95 ms/张），传的字节 5162 KB → 2943 KB → **1034 KB**（-80%），
三段分别是取图 3.6 → 2.2 s、上传 0.5 → 0.2 s、电脑端缓存命中 1–2 ms。
代价也量过两轮：① 512 DIP/q100 → 448/q88，标签 **16/17 逐字相同**，
唯一的差异是一张**界面截图**（网格里的小缩略图被当成角色），从一个临界角色掉成一个；
② 系统取图（glide，q88/448，2943 KB）→ 自己解图（1034 KB），标签 **17/17 逐字相同**，
真实照片与 4 张探针图零差异（`tools\verify_aves_tags.py verify` 11/11 PASS）。

## 没有 Wi-Fi 的时候：插数据线（备用通道）

手机和电脑不在同一个局域网时不用折腾网络，一根数据线就够：

```
tools\albumcat.cmd serve       REM 电脑端，照旧
tools\phone_link.cmd           REM 另一个窗口：把手机的 127.0.0.1:8899 接到电脑的 8899
```

`tools\phone_link.cmd` 做的事只有一件：`adb reverse tcp:8899 tcp:8899`——它让**手机自己的回环地址**
落到电脑上，于是 App 连 `http://127.0.0.1:8899` 就等于连电脑。顺带一个好处：这条流量到电脑时是从
loopback 进来的，**完全不经过 Windows 防火墙**，所以局域网那次「包被丢掉」的坑在这里根本不存在。
`-r` 撤掉隧道，`-s SERIAL` 在多设备时挑一台，也可以传别的端口（`tools\phone_link.cmd 9000`）。

App 这边**不需要改地址**：`AiTaggingService.resolveReachable()` 先探你填的地址（3 秒超时），
探不通才换 `http://127.0.0.1:8899`（`lib/services/ai_tagging_service.dart` 的 `usbBase`）。
日志里会写明走了哪条：`电脑端在数据线上：http://127.0.0.1:8899` 还是 `电脑端：http://192.168.1.10:8899`。
前提是手机开了 **USB 调试**、线是数据线、电脑在手机上被授权过（`adb devices` 显示 `device`
而不是 `unauthorized`）。拔线或重开 adb，隧道就没了，插回去再跑一次脚本即可。

验过的方式：在模拟器上用 `iptables` 把手机发往电脑 `10.0.2.2:8899` 的出站流量 `REJECT` 掉
（等价于「这里没有局域网」），25 张照样跑完——**19 张写了标签、0 失败**，界面日志写着
`电脑端在数据线上：http://127.0.0.1:8899`；删掉规则再跑一轮，日志变回 `电脑端：http://10.0.2.2:8899`、
3.1 s，说明回退没有把原路拖慢。⚠️ 这条验证跑在模拟器上，**adb 的传输是 loopback 而不是真 USB**，
所以真机上还需要确认的只有一件事：手机端 adbd 接受 `reverse`（任何开了 USB 调试的现代安卓都接受）。

## 打标跑到一半断了会怎样

**已经打好的标签不会丢**——这是 2026-10-13 专门改过的一件事。原先整轮结果攒在内存里、**跑完才落盘**，
所以拔线、手机被杀、电脑端消失，代价是整轮标签全白打；现在**一批一落盘**（每批 8 张，电脑回一批就写一次
Aves 的 `appTags`），中断只会丢掉正在飞的那几批。

- 单批失败**自动重试一次**；连着 **3** 批都失败才停下，提示是
  「连着 3 批都没成功，先停下来。已经打好的标签都留在手机里了。」
- 其余情况都正常收尾并报数：`完成：6 张写了标签，2 张没认出来，14 张失败。`；
  真出错时日志会补一句 `已经存下来的：N 张写了标签…`，一眼能看出保住了多少。
- 剩下的图不用手动挑：勾着「跳过已经有标签的图片」（默认就勾着）再点一次开始，只补没打上的。

怎么验的：一个可复现的 A/B。手机连的不是真服务端，而是 `tools\slow_proxy.py`——一个转发的代理，
**第 1 批之后一律返回 503**（等价于「电脑端没了」，比掐网线更容易复现）。同样的中断点、
电脑端同样回完 8 张：改前 `appTags` **一行没多**（只剩那 4 行探针），改后**多出 6 行**
（回完的 8 张里有 2 张没认出来），而且整轮是以 `完成：` 收尾而不是 `出错：`。
配套脚本还有 `.tmp\clear_apptags.py`（清出待打标的子集）与
`.tmp\bench_aves_run.py --keep-skip-tagged`（驱动界面跑一轮、不碰那个勾选框）。

---

## 手机上：让 App 自己跑（XMP 路线的完整形态）

电脑端 `tag` 适合处理已经在电脑上的照片。要处理**手机相册里的**照片，用手机 App：
它自己扫描相册、把缩略图发给电脑、把返回的标签写回照片。电脑在这条链路里只做它擅长的那一件事——推理。

```bat
REM 1) 电脑端开服务。启动时会打印局域网地址，例如 http://192.168.1.10:8899
tools\albumcat.cmd serve

REM 2) 构建手机 App
tools\android_build.cmd assembleDebug
REM    产物：android\app\build\outputs\apk\debug\app-debug.apk
REM    连上手机（开 USB 调试）直接装：
E:\Android\sdk\platform-tools\adb.exe install -r android\app\build\outputs\apk\debug\app-debug.apk
```

App 里的顺序：填电脑地址 → `测试连接` → `选择相册文件夹`（系统弹一次授权，之后一直有效）
→ 保持「只预览，不写入」勾着跑一遍 → 满意了取消勾选再跑。

### 地址填什么，填错了会怎样

填 `serve` 启动时打印的那行，形如 `http://192.168.83.124:8899`。`albumcat doctor` 也会
把这一行直接打出来（`phone should use: …`），不必自己猜哪张网卡。

地址栏是有意做宽容的，下面这些都会被自动修正成同一个地址，改动了什么会写进下方日志：

| 你填的 | 实际用的 |
|---|---|
| `Http://192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `192.168.83.124` | `http://192.168.83.124:8899` |
| `192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `http:/192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `http://192.168.83.124：8899`（全角冒号） | `http://192.168.83.124:8899` |
| `http://192.168.83.124:8899/health`（从浏览器粘的） | `http://192.168.83.124:8899` |

真的读不懂时会指名道姓——告诉你第几个字符是什么、码点多少，而不是笼统一句「请填写地址」。

这份宽容是踩坑换来的。早先的检查是 `startsWith("http")`，它**大小写敏感**：输入法把首字母
大写成 `Http://` 就会被判为"没填地址"，而框里明明有地址——看到「请填写电脑端地址」的人
只会把同一串再打一遍，然后得到同一个错误。还有两处更隐蔽的：中文输入法的全角 `：`／`／`
能过前缀检查、再死在 `URL()` 深处且不说明原因；而地址以前**只在点「开始」或选完文件夹时
才保存**，中途进程被系统回收（或只是转屏）就会让 `onCreate` 用旧值把输入框刷回去，空框里
显示着灰色示例 `http://192.168.1.10:8899`，看着就像是填好的。现在每敲一个字就存一次。
逻辑都在 `ServerAddress.kt`，它不 import 任何安卓类，所以那张表能在电脑上逐行断言
（`tools\check_android_client.py`）。

界面上两个输入框现在各有一行**常驻标签**（「电脑端地址」/「令牌（电脑端没设 --token 就留空）」）。
这不是装饰：早先只有占位符，而占位符一有字就消失——两个都填了字的框长得一模一样，
再加上最上面那个框曾经被系统栏整个压住（见「技术要点」），填错栏位是迟早的事。
真填错了也不要紧：**地址框为空、而令牌框里那段文字看着像个端点**（带点的主机名、`localhost`、
或带端口）时，`ServerAddress.looksLikeAddress()` 会把它当地址用、清空令牌栏，并在日志里
说明「地址是从「令牌」那一栏读到的」。反过来，真正的令牌不会被误当成地址——那会静默丢掉令牌、
随后认证失败，比原来的 bug 更糟，所以这一条有 15 个用例专门守着。

### 手机连不上电脑？先看防火墙

**回环流量不受 Windows 防火墙管辖**，所以在电脑上 `http://127.0.0.1:8899/health` 秒回，
完全不能说明手机连得上。而防火墙的入站规则是**按可执行文件路径**匹配的：这台机器上早就
有好几条 python 的放行规则，但它们指向的都是别的解释器，项目虚拟环境里的那个一份都没有。

查：

```bat
tools\albumcat.cmd doctor
REM   phone (route A)
REM     phone should use: http://192.168.1.23:8899
REM     [WARN] no inbound rule covers python.exe on TCP 8899 ...
```

`tools\albumcat.cmd serve` 启动时也会打同样的 WARNING（`--host 127.0.0.1` 时不打，因为那种绑定本来就不给手机用）。

修：

```bat
tools\allow_firewall.cmd
REM 端口不是 8899 时：tools\allow_firewall.cmd 9000
```

它会弹 UAC，然后加一条**只针对本虚拟环境解释器 + 只针对这一个端口**的入站放行。
不想用脚本就照抄 `doctor` 打印出来的那条 `netsh` 命令。
撤销：`netsh advfirewall firewall delete rule name="albumcat serve"`。

如果懒得先查，也可以**直接在手机上点「测试连接」**：App 会把失败原因分好类再说出来，
防火墙丢包会明确提示你去跑 `tools\allow_firewall.cmd`，而不是甩一坨 Java 异常
（见 `NetDiagnosis.kt`，以及「技术要点」里那两条反直觉的教训）。

三点容易踩的：规则用 `profile=any`，因为这台机器同时挂了 VPN（Private）和 Wi-Fi（Public）
两个配置文件，Windows 按包从哪个网卡进来挑配置文件，写死一个就是赌运气；
规则里的解释器路径必须是**展开后的**绝对路径（`tools\..\.venv\...` 这种带 `..` 的写法
未必能和进程映像路径匹配上，而不匹配的表现就是"规则加了但没用"）；另外
`netsh` 的输出语言**会随有没有控制台而变**（同一条查询在 shell 里回英文、在代码里回中文），
所以 `src\albumcat\firewall.py` 是按中英双语解析的。

**照片不出手机。** 传过去的是手机上就地缩放的 448px JPEG 缩略图（约 30–60 KB），
电脑不落盘、用完即弃。原始照片位置不变、像素不重编码——只在 JPEG 的 APP1 段
（或 PNG 的 `iTXt` 块）里插一段 XMP，其余字节（JPEG 是 `SOS` 之后的熵编码数据，
PNG 是 IDAT 块）逐字节原样搬运。

App 里也有「撤销上一次写入」，和电脑端 `undo` 是同一套思路：拿运行前记下的旧 XMP 包还原。
写回前会先比对文件的当前内容是否还是我们上次写进去的那份，被别人改过就不动它。

### 手机上跑之前，先在电脑上彩排一遍

真机上出了问题很难看出是哪一环。`tools\simulate_phone.py` 把 App 除 SAF 之外**整条链路**
在电脑上跑一遍：用 Python 复刻 `Thumbnailer` 的缩放和白边、调真正的 `TaggingService`（就是
`serve` 跑的那份代码）、再用 **App 自己的 `Xmp.kt`**（经 JVM）做 merge/build/splice 写进真实照片。

```bat
.venv\Scripts\python.exe tools\simulate_phone.py
```

每张照片验六件事：缩略图认出的角色对不对、标签能被**别人的** XMP 解析器（Pillow）读到、
像素逐字节没变、重复跑不堆积标签、写入了 App 用来跳过的标记、撤销后 SHA-256 与原件相同。
它跑的是 `samples\` 里所有的 JPEG **和 PNG**——包括那张 1.53 MB 的真实安卓截屏，
因为截屏走的是完全不同的代码路径（iTXt 块而不是 APP1 段）。
在真机上再出错，就只剩 SAF 文件读写这一段了。

上面那个脚本是从 Python 一侧发请求的，App 自己的 HTTP 客户端仍然没被执行过——而它的典型
失败方式是「字段名写错 → 收到空数组 → 跑完了但什么都没发布」，在手机上极难定位。
`tools\check_android_client.py` 就是补这一段：它把 **App 真实的 `TaggerClient.kt` 和
`Journal.kt`** 拿到桌面 JVM 上编译运行（`android.util.Base64` / `android.content.Context`
用两个小垫片顶上），自己起一个 `albumcat serve`，然后：

```bat
.venv\Scripts\python.exe tools\check_android_client.py
```

- 用真实服务端验 `health()` / `plan()`：每一项都和「Python 解析同一个响应」逐字段比对，
  确认标签是**真的读到了**而不是静默变成空数组；
- 验 `Journal` 的撤销语义：同一张图重试后只留**最早**那行（那才是运行前的状态）、
  `prev` 逐字节可还原、原本没有 XMP 的记成 `null` 而不是空数组、半行垃圾不会让整个日志报废。

（这几个脚本的 JVM 管道是共用的，见 `tools\kotlin_jvm.py`。）

嫌一个一个跑麻烦的话，有一条命令跑全部自检（现在 10 项，从便宜到贵排序——
明显的坏掉几秒内就暴露，而不是等 ONNX 模型加载两次）：

```bat
tools\check_all.cmd
tools\check_all.cmd --list          REM 看看有哪些
tools\check_all.cmd -k runner       REM 只跑名字里带 runner 的
```

而**决定你照片命运的那份代码是 `TagRunner.kt`**：什么时候写、什么时候跳过、写失败怎么办、
半路断线了怎么办、写完通知谁——全在它里面。它此前没有被任何东西执行过，而它的每一种失败
都是「照片被写坏」或「几千张在无声地失败」的形状。`tools\check_android_runner.py` 把它
连同 `Xmp.kt` / `Journal.kt` / `Rescan.kt` / `DocumentPaths.kt` / `TaggerClient.kt` 一起
拉到桌面 JVM 上，用一套内存里的假相册（假的 SAF 树、假的字节存储、假的媒体扫描器）真跑：

```bat
.venv\Scripts\python.exe tools\check_android_runner.py
```

六个场景，56 项断言：

| 场景 | 验的是什么 |
|---|---|
| `dry` | 一个乱七八糟的文件夹（嵌套子目录、`.nomedia`、`.txt`、HEIC）里认出 4 张能打的图，另外 1 张 HEIC 被计数而不是被无视，且一个字节都没写 |
| `write` | 写 4 张（**3 张 JPEG + 1 张 PNG**）→ 逐字节变大且带上 `albumcat` 标记和 `角色\|` 层级路径 → **重跑全部跳过且字节完全不变** → 撤销后 SHA-256 与原件相同、标记消失、日志清空 |
| `batch` | 70 张一次跑完，`Rescan` 只调 **2** 次 `scanFile`（64 + 6），不是 70 次跨进程往返 |
| `abort` | 服务端不可达时，连续失败 8 张就**停下并说明原因**（而不是把几千张挨个超时一遍），且一张照片都没被动过 |
| `failwrite` | 中间那张每次写入都失败（磁盘满的形状）时，**它的日志记录已经先写下去了**——所以哪怕 App 正好死在写入中途，原始字节也还有救 |
| `stop` | 5 张跑到第 3 张时按「停止」：正好处理 3 张、算作**用户按停而不是出错**、没轮到的 2 张**一个字节都没动**、写盘次数正好 3 次、已写好的 3 张照样通知媒体库、撤销把这 5 张全部逐字节还原 |

`failwrite` 那条不是凑数的：把 `TagRunner.kt` 里的顺序换回「先写照片、再记日志」，这一条立刻
变成 `JOURNAL == 2` 而失败（其余 37 条照过）。**知道一个测试真的会失败，才算验过。**

`stop` 这条也是：把 `TagRunner.kt` 里那个循环内的停止检查**删掉**，8 条断言立刻变红
（5 张全被写完，而界面却准备报告「已停止」）。但**把检查从循环体开头挪到循环体末尾却测不出来**——
两种写法都会在下一张开始前退出。这条测试锁住的是「检查在循环**内**，
而不是循环之后」：停在哪一张飞到一半时按下，那一张仍然会写完并进日志，而不是被丢下半途而废。

PNG 那张也不是凑数的。App 写到 PNG 时走的是一整条**不同的**代码路径
（iTXt 块、CRC32、不是 APP1 段），而「路径走错了」这种东西很难靠文字断言发现——
所以驱动还会把每张被改过的文件的**前 8 字节**打出来（PNG 必须始终以 `89504e47 0d0a1a0a` 开头，
一旦有人把 JPEG 的 APP1 段塞进 PNG，这里立刻露馅），并把原字节全部 dump 到磁盘，
再用 Pillow 打开、逐像素比对。

它还会先直接问服务端「这几张图分别是什么」，再拿回答去核对照片里实际写进去的东西——
所以「Aves 要读的 `dc:subject` 里有层级路径」这条是按**真实识别结果**验的，
而不是靠假设某张样图里一定有角色。

### 但假相册顶掉的那一层，恰好藏过一个真 bug

`check_android_runner.py` 为了能在桌面跑，**替换**了 App 里唯二两处真正碰安卓系统的地方：
`Thumbnailer`（解码照片并缩到 448px）和 `ContentResolver`（按 SAF URI 读写字节日志）。
凡是只活在这一层里的 bug，它一个都看不见。真有一个：

```kotlin
// Thumbnailer.kt -- 看起来是「流没打开就报错」，其实是「解码没返回就报错」
resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
    ?: throw DecodeFailed("cannot open $uri")
```

`?.use { ... }` 返回的是 **lambda 的结果**，不是流。而测尺寸那一趟
（`inJustDecodeBounds = true`）按设计**就是返回 null**——所以 `?:` 每次都触发，
每张照片都以「打不开」告终，而**它其实刚刚成功打开了**。桌面套件 56/56 全绿，
真机上 4/4 失败。修法是先把流取出来（`private fun open(...)`），
再 `open(...).use { ... }` 且块后什么都不接。

教训不是「Kotlin 的 `?.use` 有坑」，而是：**一个把平台层换成假实现的测试，
测不到那一层里的错**。所以还需要一个不替换任何东西的测试。

### 真机烟测：`tools\smoke_device.py`

```bat
REM 电脑端服务必须开着，App 要问它每张图是什么
tools\albumcat.cmd serve --port 8899
REM 另开一个窗口
.venv\Scripts\python.exe tools\smoke_device.py --serial emulator-5554 --port 8899
```

它在一个真的安卓系统上把 App 完整跑一轮，全程无人值守（约 60 秒，18 项断言）：

0. **先看版式**：用 `uiautomator` 取每个带文字控件的矩形，断言**两两不重叠**、地址框与令牌框各有
   一行常驻标签、屏幕上没有第二条「相册分类」标题栏、最上面的元素不在屏幕顶边上——这几条就是下面
   那个 ActionBar 遮挡 bug 的探针（见「技术要点」）。**"有没有文字"和"看不看得见"是两回事**：
   出问题的那版里每个按钮的文字都还搜得到，变的只有矩形坐标；
1. 推 4 张干净原图（3 JPEG + 1 PNG）进 `/sdcard/DCIM/AlbumcatSmoke/`，装 APK；
2. 用 root 直接写 App 的 prefs 指向电脑（省掉手打地址），再**用 uiautomator 走一遍真的
   SAF 拾取器**并授权——是按屏幕上的文字找控件，不是写死的坐标；
3. 点「开始」，断言日志说「写入 4 张、失败 0 张」，且 4 个文件的 SHA-256 **全部变了**；
4. 再点一次「开始」，断言「跳过 4 张」且**每个字节都没动**；
5. 点「撤销上一次写入」，断言 4 个文件的 SHA-256 与**最开始那组逐个相同**。

第三步是关键：它断言的是**文件真的被我改了**，不是「日志说改了」。
`--server` 还能把一份**故意写歪**的地址塞进去（比如用户报错时的 `Http://10.0.2.2:8899`），
此时多一条断言：日志里必须出现「地址已按 …使用」——即 App 是**修好了再用**，
而不是碰巧接受。这台机器上的实测输出：

```
-- setup --
  [PASS] the app installs
-- layout: the form must not be covered --
  [PASS] no two labelled elements overlap (nothing is drawn over the form)
  [PASS] both input boxes carry a visible label, not just a placeholder
  [PASS] no framework ActionBar above the form
  [PASS] the form starts below the top edge, clear of the status bar   最上面的元素 top=170
-- run: tag the photos --
  [PASS] wrote 4 photos   完成：写入 4 张，跳过 0 张，失败 0 张，用时 4.7 秒（运行号 20261012-171850）
  [PASS] the odd address was repaired, not silently ignored   完成：写入 4 张，跳过 0 张，失败 0 张，用时 4.7 秒（运行号 20261012-171850）
  [PASS] every photo was actually rewritten   4/4 changed
-- run again: must skip and change nothing --
  [PASS] re-run left every byte alone
-- undo: must restore the originals --
  [PASS] undo restored every photo byte-for-byte   4/4 identical
----------------------------------------------------------------------
All 18 checks passed -- the app works on a real Android system.
```

它**不在 `check_all.cmd` 里**，因为它需要一台设备；电脑侧的 10 项自检保持无依赖。

关于设备：实在的手机只要开了 USB 调试就行（预置 prefs 那步需要 root，非 root 设备上
就自己在界面上填一次地址）。本机用的是安卓模拟器——`E:\Android\sdk\emulator` 加一份
`system-images;android-35;google_apis;x86_64`，配一个 `--no-window` 的 AVD，WHPX
硬件加速下约 40 秒开机，`adb root` 可用。

### Aves 那一端，在设备上也是验过的

模拟器上装了 Aves（F-Droid 的 libre 构建），把带哨兵标签的照片推进相册，
让它索引完，然后**直接把它的数据库拉出来看**——比截图可靠得多：

```
adb root
adb pull /data/data/deckers.thibault.aves.libre/databases/metadata.db
```

`metadata` 表里那两行（列名就叫 `xmpSubjects`，分隔符是 `;`）：

```
AVES-PROBE-1234;Hakurei Reimu;Touhou Project;1girl;角色|AVES-PROBE-1234;角色|Hakurei Reimu;作品|Touhou Project   rating=4
AVES-PROBE-1234;Shameimaru Aya;Touhou Project;1girl;角色|AVES-PROBE-1234;角色|Shameimaru Aya;作品|Touhou Project   rating=4
```

一行是 PNG、一行是 JPEG，内容一样。这同时证明了三件事：Aves 读的确实是 `dc:subject`；
层级路径**原样到达**（它不会拆成树，`角色|X` 就是它眼里一个带竖线的普通标签，但点它能筛出
所有 X 的照片）；`xmp:Rating` 也读到了。真实标注的 4 张照片跑完后重开 Aves，它的库里同样
出现了我们写进去的那几串标签。

注意 libre 构建的**包名不是 `deckers.thibault.aves`，而是 `deckers.thibault.aves.libre`**。

---

[← 回到 README](../README.md)
