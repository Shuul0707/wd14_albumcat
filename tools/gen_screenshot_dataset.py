#!/usr/bin/env python3
"""生成 UI 截图样本：状态栏 + 标题栏 + 内容区 + 底部导航。

为什么合成而不是下载：公开的截图数据集动辄几个 GB，而截图这一类真正需要的
特征只有三样 —— 规整的分栏、大块纯色、系统栏上的小字。这三样用 PIL 完全
可以造出来，还能控制数量、保证四类平衡。

三种版式轮换，避免模型只记住一种：
    列表      设置页 / 聊天列表
    网格      相册 / 应用抽屉
    详情      文章页 / 商品页

用法::

    python tools/gen_screenshot_dataset.py --out data/eval/截图 --count 150
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "C:/Windows/Fonts/msyh.ttc"
FONTH = "C:/Windows/Fonts/msyhbd.ttc"

PALETTES = [
    ("#FFFFFF", "#F2F2F7", "#007AFF", "#1C1C1E"),   # iOS 浅色
    ("#121212", "#1E1E1E", "#BB86FC", "#E1E1E1"),   # Material 深色
    ("#FAFAFA", "#F5F5F5", "#FF6B35", "#212121"),   # 暖橙
    ("#F8FAFC", "#EFF6FF", "#2563EB", "#0F172A"),   # 冷蓝
]

WORDS = ["设置", "相册", "消息", "推荐", "收藏", "我的", "最近", "全部",
         "通知", "隐私", "存储", "通用", "账号", "帮助", "关于", "搜索"]


def font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(FONTH if bold else FONT, size)
    except Exception:
        return ImageFont.load_default()


def status_bar(d: ImageDraw.ImageDraw, w: int, bg: str, fg: str, rng: random.Random):
    d.rectangle([0, 0, w, 44], fill=bg)
    d.text((18, 12), f"{rng.randint(8, 23)}:{rng.choice(['05','18','42','59'])}", font=font(17, True), fill=fg)
    # 电量 + 信号（几个小方块，示意而非写实）
    for i in range(4):
        d.rectangle([w - 78 + i * 7, 30 - i * 4, w - 74 + i * 7, 32], fill=fg)
    d.rectangle([w - 40, 16, w - 20, 30], outline=fg, width=2)
    d.rectangle([w - 38, 19, w - 30, 27], fill=fg)


def tab_bar(d: ImageDraw.ImageDraw, w: int, h: int, bg: str, accent: str, fg: str, rng: random.Random):
    top = h - 62
    d.rectangle([0, top, w, h], fill=bg)
    d.line([0, top, w, top], fill=fg + "22" if len(fg) == 7 else fg, width=1)
    n = 4
    for i in range(n):
        cx = int(w * (i + 0.5) / n)
        color = accent if i == rng.randrange(n) else fg
        d.rounded_rectangle([cx - 12, top + 14, cx + 12, top + 34], radius=5, outline=color, width=2)
        d.text((cx - 14, top + 38), rng.choice(WORDS), font=font(11), fill=color)


def layout_list(im, d, w, h, c, rng):
    bg, card, accent, fg = c
    d.rectangle([0, 0, w, h], fill=bg)
    status_bar(d, w, bg, fg, rng)
    d.text((20, 58), rng.choice(["设置", "消息", "通知", "全部会话"]), font=font(26, True), fill=fg)
    y = 104
    for _ in range(rng.randint(5, 9)):
        d.rounded_rectangle([16, y, w - 16, y + 64], radius=10, fill=card)
        d.ellipse([28, y + 16, 60, y + 48], fill=accent + "55" if len(accent) == 7 else accent)
        d.text((72, y + 14), rng.choice(WORDS) + rng.choice(["列表", "中心", "管理", "记录"]), font=font(16, True), fill=fg)
        d.text((72, y + 38), rng.choice(["查看详情", "共 128 项", "2 分钟前", "已同步"]), font=font(12), fill=fg + "99" if len(fg) == 7 else fg)
        y += 74
        if y > h - 90:
            break
    tab_bar(d, w, h, bg, accent, fg, rng)


def layout_grid(im, d, w, h, c, rng):
    bg, card, accent, fg = c
    d.rectangle([0, 0, w, h], fill=bg)
    status_bar(d, w, bg, fg, rng)
    d.text((20, 58), rng.choice(["相册", "应用", "收藏", "图片"]), font=font(26, True), fill=fg)
    cols, top = 3, 104
    gap = 8
    size = (w - 40 - gap * (cols - 1)) // cols
    for r in range(4):
        for cidx in range(cols):
            x = 20 + cidx * (size + gap)
            y = top + r * (size + gap) + r * 16
            if y + size > h - 80:
                break
            shade = rng.choice([accent + "44", accent + "66", card])
            d.rounded_rectangle([x, y, x + size, y + size], radius=8, fill=shade if len(shade) == 9 else shade)
    tab_bar(d, w, h, bg, accent, fg, rng)


def layout_detail(im, d, w, h, c, rng):
    bg, card, accent, fg = c
    d.rectangle([0, 0, w, h], fill=bg)
    status_bar(d, w, bg, fg, rng)
    # 顶部大图占位
    d.rectangle([0, 44, w, 44 + int(h * 0.28)], fill=accent + "33" if len(accent) == 7 else accent)
    y = 44 + int(h * 0.28) + 20
    d.text((20, y), rng.choice(["详情", "文章", "商品", "介绍"]), font=font(24, True), fill=fg)
    y += 44
    for _ in range(rng.randint(6, 11)):
        width = rng.choice([w - 40, w - 80, w - 120, w - 60])
        d.rounded_rectangle([20, y, 20 + width, y + 12], radius=6, fill=fg + "33" if len(fg) == 7 else fg)
        y += 26
        if y > h - 90:
            break
    tab_bar(d, w, h, bg, accent, fg, rng)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/eval/截图")
    ap.add_argument("--count", type=int, default=150)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    layouts = [layout_list, layout_grid, layout_detail]
    sizes = [(720, 1280), (750, 1334), (1080, 1920), (828, 1792)]

    print(f"生成 {args.count} 张截图 → {out}")
    for i in range(1, args.count + 1):
        w, h = rng.choice(sizes)
        im = Image.new("RGB", (w, h), "#FFFFFF")
        d = ImageDraw.Draw(im)
        layout = layouts[i % len(layouts)]
        layout(im, d, w, h, rng.choice(PALETTES), rng)
        im.save(out / f"screenshot-{i:04d}.png", optimize=True)
        if i % 25 == 0:
            print(f"  {i}/{args.count}")
    print(f"完成：{args.count} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
