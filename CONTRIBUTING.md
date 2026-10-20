# 参与开发

> 欢迎 PR、issue，以及「我照 README 跑不起来」这类反馈 —— 最后一种最有价值。

仓库有两半，各自能单独跑：

- **电脑侧**：Python（`src/albumcat/`，命令行与服务端）＋ 插件（`plugins/`）
- **手机侧**：Aves 的 fork（`aves/`，Flutter + Kotlin），改动清单见 [aves/FORK.md](aves/FORK.md)

`tools/` 里两个方向都有东西：PC 侧的命令入口与自检、手机侧的构建与设备验证。

## 三条硬规矩

1. **不改照片文件。** 标签只写进 Aves 自己的数据库（`appTags` 表）。`aves/lib/model/entry/extensions/metadata_edition.dart`
   里那道闸门（纯 app 标签不进 `editIptcValues` / `editTagsXmp`）不要拆 —— 这是整个项目的前提，
   项目最初的动机就是「不想为了让相册能分类而改写照片」。
2. **不许把权重和安装包提交进仓库。** `models/`、`dist/`、`samples/` 都已在 `.gitignore` 里；
   APK 走 [Releases](https://github.com/Shuul0707/wd14_albumcat/releases)。
3. **`tools/*.cmd` 必须是纯 ASCII。** cmd.exe 按 OEM 代码页（中文 Windows 上是 cp936）读批处理文件，
   里面出现中文会在**后面某一行**报错，排查起来极其难受。中文写进 `.py`、`.ps1` 或 `.md`。

## 环境

**电脑侧**（Python 3.11+）：

```bat
pip install -e ".[dml,dev]"    REM Windows + 任意 DX12 显卡（本项目用的）
pip install -e ".[cuda,dev]"   REM NVIDIA + CUDA 12.x
pip install -e ".[cpu,dev]"    REM 别的机器，慢但能跑
python tools\download_model.py REM 模型 356 MB，默认走 hf-mirror
python -m albumcat doctor      REM 自检：模型、依赖、推理后端
```

`[dml]` / `[cuda]` / `[cpu]` 只能选一个：三者提供同一个 `onnxruntime` 模块，装两个就互相踩。

**手机侧**：JDK 21 + Flutter 3.47.6 + Android SDK 37（`compileSdk 37`、build-tools 36.0.0），
`tools\aves_build.ps1` 一条命令出包。国内网络的镜像已经写进仓库与本地 Gradle init 脚本，
细节和坑见 [aves/FORK.md](aves/FORK.md)。

**平台**：脚本以 Windows 为主（`.cmd` / `.ps1`、自己找 adb、`netsh` 防火墙检查）。Linux / macOS 上
`python -m albumcat ...` 和 pytest 都能跑，构建与设备脚本需要自己翻译 —— 这类 PR 很欢迎。

## 改完怎么验

```bat
python -m pytest              REM 单元测试（规则 / 标签字段 / 配置 / 类型），不到一秒
python tools\check_repo.py    REM 仓库卫生：ASCII .cmd、疑似密钥、文档链接、.gitignore
tools\check_all.cmd           REM 两条快的 + 四条端到端的全跑（要模型）
```

- `check_all` 里最贵的那条会**真的起一个 `albumcat serve`** 跑模型，验 `/plan_batch` 的 HTTP 契约；
  没有模型时会明确报错，不会假装通过。
- 改了手机侧还要：`dart analyze <改过的文件>` → `tools\aves_build.ps1 -Platform android-x64` →
  装到模拟器 → `python tools\verify_aves_tags.py verify`（11 项：探针文件 SHA256/mtime 不变、
  `appTags` 逐文件对得上）。
- **换设备、换 Aves 版本，验的东西就要重验一遍。** 只在模拟器上验过的事，别在 PR 里说成「真机验过」。

## 代码风格

- Python 3.11+，4 空格缩进，双引号，行宽约 100；文件开头 `from __future__ import annotations`。
- `src/albumcat/` 的注释与 docstring 用**英文**（方便下游看代码），`tools/` 与 `docs/` 用**中文**（方便本项目维护）。
- 新的纯逻辑顺手加一条测试：`tests/` 按模块分文件，函数用 `def test_xxx`，一组相关的用 `class TestXxx`。
  不需要联网、模型或设备的那部分逻辑都应该能被 pytest 覆盖。
- 不轻易加运行时依赖。现在只有 `numpy` / `pillow` / `pandas` / `opencv-python-headless` / `requests`
  加一个 `onnxruntime` 变体；服务端是纯标准库 HTTP，别为了少写十行代码把它换掉。

## 提交与 PR

- 提交信息一行式即可，中文或英文都行，动词开头：`docs: …`、`fix: …`、`feat: …`、
  `perf: …`、`test: …`；需要解释的写进 PR 描述，别写成十行正文。
- 一个提交一件事；不要把格式化（或无关文件的换行符变化）混进功能改动。
- PR 里写清「改了什么 / 怎么验的」，把 `check_all` 或 `verify_aves_tags` 的结论贴上来 ——
  一个能复现的「我之前是坏的、现在是好的」比一百行解释有用。

## 常见改动的入口

| 想做的事 | 从哪下手 |
|---|---|
| 加一个插件（比如对某个 IP 的角色精确识别） | [插件](docs/PLUGINS.md)，`plugins/` 下有示例；`albumcat train` 训 `feature` 头 |
| 调打标规则（阈值、每图角色上限、denylist） | `config.toml` + `src/albumcat/rules.py`（改完跑 pytest） |
| 改手机端标签怎么显示 / 怎么筛选 | `aves/lib/model/app_tags.dart`、`entry.dart`、`tag.dart` |
| 给手机端加一种取图方式 | `aves/.../decoding/ThumbnailFetcher.kt` + `channel/calls/DebugHandler.kt` + `lib/services/android_debug_service.dart` |
| 跟着 Aves 上游升级 | [aves/FORK.md](aves/FORK.md) 的「上游升级怎么跟」 |
| 让它跑在别的平台上 | 见上文「平台」，`.cmd` / `.ps1` 里的路径与 `netsh` 是要点 |

## 许可

提交进本仓库的代码按 **MIT** 发布（见 [LICENSE](LICENSE)）—— 提交即表示你同意以这个许可发布。
`aves/` 下的代码是 Aves 的 fork，沿用上游 **BSD 3-Clause**（`aves/LICENSE`），改它要同时遵守那份。

CI 在 `.github/workflows/ci.yml`：每次 push / PR 跑 pytest、`check_repo.py` 和 `albumcat --help`，
用的是 CPU 后端（GitHub 的 runner 没有 DX12）。Flutter 侧的分析是手动触发的 `flutter.yml`，
因为拉一遍 Flutter SDK 的分钟数比它抓到的 bug 值钱。
