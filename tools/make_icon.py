"""生成应用图标 resources/app.png / app.ico（纯 Pillow 绘制，无外部素材、无网络）。

图形含义：蓝底圆角方块上是一个播放窗口（圆角边框 + 播放三角），
下方一枚向下的压缩箭头——对应「视频文件压缩」：把片子压到能发出去的体积。

重新生成：
    python tools/make_icon.py
打包时把 resources/app.ico 设为 exe 图标即可。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "resources"
RES.mkdir(exist_ok=True)
S = 512


def rounded_mask(box: tuple[int, int, int, int], radius: int) -> Image.Image:
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def build() -> Image.Image:
    # 1) 蓝色渐变圆角底（与站内其它蓝色系图标同一套色）
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    top, bottom = (59, 130, 255), (24, 52, 148)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    grad.putalpha(rounded_mask((12, 12, S - 12, S - 12), 112))

    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.alpha_composite(grad)
    d = ImageDraw.Draw(canvas)

    # 2) 播放窗口的投影
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (92, 96, 420, 330), radius=44, fill=(0, 0, 0, 95))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    # 3) 白色播放窗口边框（空心，露出蓝底，小尺寸下也不糊成一团）
    d.rounded_rectangle((88, 92, 424, 326), radius=44, outline=(255, 255, 255, 250),
                        width=26)

    # 4) 播放三角
    d.polygon([(212, 156), (212, 262), (300, 209)], fill=(255, 255, 255, 255))

    # 5) 向下的压缩箭头：竖杆 + 箭头，表示「把体积压下去」
    cx = S // 2
    d.rounded_rectangle((cx - 20, 344, cx + 20, 416), radius=12,
                        fill=(255, 255, 255, 255))
    d.polygon([(cx - 64, 400), (cx + 64, 400), (cx, 474)], fill=(255, 255, 255, 255))
    return canvas


def main() -> None:
    icon = build()
    icon.save(RES / "app.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    icon.save(RES / "app.ico", sizes=sizes)
    print("已生成：", RES / "app.png")
    print("已生成：", RES / "app.ico", "（含 16–256 共 7 档）")


if __name__ == "__main__":
    main()
