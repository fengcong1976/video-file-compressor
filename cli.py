"""命令行入口：批量压视频到指定体积。

    python cli.py ./相册 --target 25
    python cli.py a.mp4 b.mov --target 100 --out ./out
    python cli.py ./素材 --target 25 --open
    python cli.py ./素材 --lossless-only --target 25
    python cli.py ./素材 --target 25 --size 1280x720
    python cli.py ./素材 --target 25 --size 1080x1080 --stretch
    python cli.py --version
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from core.about import DISCLOSURE, VERSION_LINE
from core.compliance import NetworkAudit
from core.compressor import DEFAULT_TARGET_MB, OUT_DIRNAME, compress_batch
from core.ffmpeg_tool import FFmpegMissing, version_text


def parse_size(text: str) -> tuple[int, int]:
    """把 1280x720 / 1280×720 / 1280*720 解析成 (宽, 高)。"""
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX×*]\s*(\d{2,5})\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"尺寸要写成「宽x高」，例如 1280x720，收到的是 {text!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if w < 16 or h < 16:
        raise argparse.ArgumentTypeError("宽高至少 16 像素")
    return w, h


def human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="视频文件压缩",
        description="把视频压到指定体积以内（本地处理，不上传、不覆盖原片）。")
    p.add_argument("paths", nargs="*", help="视频文件或文件夹（可多个）")
    p.add_argument("-t", "--target", type=float, default=DEFAULT_TARGET_MB,
                   metavar="MB", help=f"目标体积，单位 MB（默认 {DEFAULT_TARGET_MB}）")
    p.add_argument("-o", "--out", default=None,
                   help=f"统一输出目录（默认每段视频旁的 {OUT_DIRNAME}）")
    p.add_argument("--lossless-only", action="store_true",
                   help="仅无损：只换容器不重编码；压不到就说压不到")
    p.add_argument("--size", type=parse_size, default=None, metavar="宽x高",
                   help="输出尺寸，如 1280x720；默认等比缩到框内、不放大（不填 = 保持原尺寸）")
    p.add_argument("--stretch", action="store_true",
                   help="配合 --size：精确输出到该宽高，比例会变（表单强制尺寸时才用）")
    p.add_argument("--open", action="store_true", help="压完打开结果文件夹")
    p.add_argument("--version", action="store_true", help="显示版本信息")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION_LINE)
        print(f"更多免费软件：www.mrheedu.com")
        print(DISCLOSURE)
        try:
            print(f"ffmpeg：{version_text()}")
        except FFmpegMissing as exc:
            print(f"ffmpeg：{exc}")
        return 0
    if not args.paths:
        build_parser().print_help()
        return 2
    if args.target <= 0:
        print("目标体积要大于 0（单位 MB）", file=sys.stderr)
        return 2
    if args.stretch and not args.size:
        print("--stretch 要和 --size 一起用，例如 --size 1080x1080 --stretch",
              file=sys.stderr)
        return 2
    if args.size and args.lossless_only:
        print("--size 与 --lossless-only 不能同时用：仅无损只换容器，改不了尺寸",
              file=sys.stderr)
        return 2

    audit = NetworkAudit().__enter__()
    try:
        total = {"n": 0}

        def on_item(item):
            total["n"] += 1
            if item.skipped:
                flag = "跳过"
            elif item.ok:
                flag = "达标·无损" if item.lossless else "达标·有损"
            else:
                flag = "未达标"
            note = (item.note or "").split("；")[0][:60]
            print(f"\r{' ' * 40}\r[{flag}] {Path(item.src).name}"
                  f"  {human(item.src_bytes)}"
                  + (f" → {human(item.dst_bytes)}" if item.dst_bytes else " → —")
                  + (f" ｜ {note}" if note else ""))

        cancel = threading.Event()
        report = compress_batch(
            args.paths, args.target, out_dir=args.out,
            lossless_only=args.lossless_only,
            target_size=args.size, keep_ratio=not args.stretch,
            on_item=on_item, cancel=cancel, progress_cb=lambda f: None)
    except FFmpegMissing as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n已中断：已经压好的结果保留在原片旁的 out 里。")
        return 130
    finally:
        audit.__exit__(None, None, None)

    print(f"\n{audit.summary()}")

    size_txt = ("保持原尺寸" if not args.size else
                f"{args.size[0]}×{args.size[1]}"
                + ("（等比缩到框内）" if not args.stretch else "（精确拉伸，比例会变）"))
    print(f"\n目标：≤ {args.target:g} MB ｜ 尺寸：{size_txt} ｜ 共 {report.total} 段 ｜ "
          f"达标 {len(report.succeeded)} ｜ 未达标 {len(report.failed)} ｜ "
          f"跳过 {len(report.skipped)} ｜ 无损 {len(report.lossless)} ｜ "
          f"有损 {len(report.lossy)}")
    print("-" * 78)
    for item in report.items:
        if item.skipped:
            head = "[跳过·—]"
        elif item.ok:
            head = "[达标·无损]" if item.lossless else "[达标·有损]"
        else:
            head = "[未达标·—]"
        res = f"{human(item.dst_bytes)}" if item.dst_bytes else "—"
        print(f"{head} {Path(item.src).name}")
        print(f"       {human(item.src_bytes)} → {res} ｜ "
              f"时长 {item.duration:.0f}s ｜ 分辨率 "
              f"{item.size_before[0]}×{item.size_before[1]}"
              + (f" → {item.size_after[0]}×{item.size_after[1]}" if item.resized else "")
              + f" ｜ 原片未改动：{'是' if item.src_unchanged else '否'}"
              + f" ｜ {item.elapsed * 1000:.0f} ms")
        if item.quality_loss:
            print(f"       画质：{item.quality_loss}")
        if item.note:
            print(f"       {item.note}")
    print("-" * 78)
    print(f"合计：{human(report.src_bytes_sum)} → {human(report.dst_bytes_sum)} ｜ "
          f"耗时 {report.elapsed:.1f} s")

    # 报告写在结果目录里；统一 --out 时只有一个，就近落盘时每个目录各写一份
    dirs = report.out_dirs or ([args.out] if args.out else [])
    for d in dirs:
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            p = Path(d) / "report.json"
            p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
            print(f"报告：{p}")
        except OSError as exc:
            print(f"报告写入失败：{exc}", file=sys.stderr)

    if args.open and dirs:
        target = dirs[0]
        try:
            os.startfile(target)  # noqa: S606 - 本地打开文件夹，不联网
        except (AttributeError, OSError):
            subprocess.Popen(["explorer", target])
        print(f"结果位置：{target}")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
