## 这个 PR 做了什么

<!-- 一句话说清；如果是修 bug，写清「改之前会怎样、改之后怎样」 -->

## 踩过哪几条硬规矩

- [ ] **没有改写照片文件**（标签只进 Aves 自己的 `appTags` 表；`metadata_edition.dart` 那道闸门没拆）
- [ ] **没有提交权重和 APK**（`models/`、`dist/`、`*.apk` 都该被 `.gitignore` 挡住）
- [ ] **`tools/*.cmd` 仍是纯 ASCII**（cmd.exe 按 OEM cp936 读批处理，中文会在后面某行炸）
- [ ] 没把 `.tmp/`、`.venv/`、`samples/` 里的临时东西带进来

## 改在哪

<!-- 大致勾一下，方便我按块看 -->
- [ ] `src/albumcat/` PC 侧
- [ ] `tools/` 脚本
- [ ] `plugins/` 插件
- [ ] `aves/` 手机侧 fork（说明碰了哪个 Dart / Kotlin 文件）
- [ ] 文档

## 自己验过什么

```
python tools\check_all.py          # 6 项，不用设备也不用模型
python -m pytest                   # 59 条
```

<!-- 手机侧如果动了，补一句 dart analyze / 构建 / verify_aves_tags.py 的结果 -->

- [ ] `tools/check_all.py` 全绿
- [ ] 真机上跑过（如果改的是手机侧）

## 备注

<!-- 有取舍、有没做的地方、有想让我特别注意的地方，都写这里 -->
