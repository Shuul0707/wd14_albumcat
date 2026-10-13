# 手机端

> Aves fork 怎么用、连不上先看哪里、没有 Wi-Fi 时插数据线、打标中途断了会怎样。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 手机上怎么用

1. 电脑上起服务：`tools\albumcat.cmd serve`（默认 `:8899`；用 `--token` 时 App 里填同一串）。
2. 装 fork 版 APK：arm64 给真机、x64 给模拟器（本仓库不附 APK，构建见
   [开发与验证](DEVELOPMENT.md#构建)）。
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
手机上也没有任何缩略图缓存可依赖——每轮都是真解码，所以计时稳定。
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

> 早期那个把标签写进照片的 Kotlin App——连同它的地址归一化、整条链路彩排和
> `tools\smoke_device.py` 真机烟测——已经不是现在的做法，都搬到了 [LEGACY](LEGACY.md)。

## 连接排错与真机验证

### 手机连不上电脑？先看防火墙

**回环流量不受 Windows 防火墙管辖**，所以在电脑上 `http://127.0.0.1:8899/health` 秒回，
完全不能说明手机连得上。而防火墙的入站规则是**按可执行文件路径**匹配的：这台机器上早就
有好几条 python 的放行规则，但它们指向的都是别的解释器，项目虚拟环境里的那个一份都没有。

查：

```bat
tools\albumcat.cmd doctor
REM   phone
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

三点容易踩的：规则用 `profile=any`，因为这台机器同时挂了 VPN（Private）和 Wi-Fi（Public）
两个配置文件，Windows 按包从哪个网卡进来挑配置文件，写死一个就是赌运气；
规则里的解释器路径必须是**展开后的**绝对路径（`tools\..\.venv\...` 这种带 `..` 的写法
未必能和进程映像路径匹配上，而不匹配的表现就是"规则加了但没用"）；另外
`netsh` 的输出语言**会随有没有控制台而变**（同一条查询在 shell 里回英文、在代码里回中文），
所以 `src\albumcat\firewall.py` 是按中英双语解析的。

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