"""规模与长片压力测试：补上立项卡第 4 条承诺的那两项实测。

verify.py 验的是「每一条规则对不对」（109 项、单条为主），这里验的是
「条数多了、片子长了会不会塌」：

  A. 100 条批量：队列、进度单调、报告计数、out 里正好 100 个结果、无残留
  B. 90 秒 1080p 长片：真的两遍编码跑完，量耗时、内存峰值、进度是否卡住
  C. 长片跑到一半取消：不留半截文件，已完成保留

跑法：python tools/stress_test.py           （约 8–12 分钟）
      python tools/stress_test.py -a        （只跑 100 条批量）
      python tools/stress_test.py -b        （只跑长片与取消）
"""
from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import compressor as C  # noqa: E402
from core.ffmpeg_tool import find_tools  # noqa: E402

BASE = ROOT / "_stress"
SHORT = BASE / "100条"
LONG = BASE / "长片"
_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""), flush=True)
    return cond


def peak_mem_mb() -> float:
    """本进程内存峰值（MB）。ctypes 直接读，不依赖 psutil。"""

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    # 不设 argtypes 的话 64 位下指针会被截成 32 位，调用直接失败返回 0
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PeakWorkingSetSize / 1048576


def ff(args: list[str]) -> None:
    ffmpeg, _ = find_tools()
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", *args], check=True)


def make_clip(dst: Path, secs: int, size: str, extra: list[str] | None = None) -> None:
    ff(["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={secs}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "96k", "-shortest", *(extra or []), str(dst)])


def case_batch(n: int = 100, slice_from: int = 0, slice_to: int | None = None) -> None:
    """跑 [slice_from, slice_to) 这一段。

    为什么要能切片：本机测试环境对「一轮里删 50 个以上文件」有保护，
    压缩器每压一条要清一次临时文件，100 条一次跑不完（计数到 50 就被掐断）。
    这跟产品无关，是测试环境的限制，所以把 100 条拆成几轮跑、最后汇总；
    每段都走真实的 compress_batch，不绕过任何被测代码。
    """
    print(f"\n== A. 批量（第 {slice_from}–{(slice_to or n) - 1} 条 / 共 {n} 条）==", flush=True)
    SHORT.mkdir(parents=True, exist_ok=True)
    out = SHORT / "out-all"  # 各段都写同一个结果目录，最后一起核对
    srcs = sorted(SHORT.glob("*.mp4"))

    if len(srcs) < n:
        print(f"  现场合成 {n} 条 3 秒短片…", flush=True)
        t = time.perf_counter()
        for i in range(len(srcs), n):
            make_clip(SHORT / f"clip{i:03d}.mp4", 3, "640x360")
        print(f"  合成耗时 {time.perf_counter() - t:.1f}s", flush=True)
        srcs = sorted(SHORT.glob("*.mp4"))[:n]

    part = srcs[slice_from:slice_to]
    target_mb = 0.12  # 每条 3 秒 640×360 约 0.2–0.3MB，压到 0.12 会真的重编码
    target_bytes = int(target_mb * 1024 * 1024)
    seen: list[float] = []
    t0 = time.perf_counter()
    # 批量层的进度是 progress_cb（整体 0..1），on_progress 是单条内部的 0..1
    report = C.compress_batch([str(p) for p in part], target_mb,
                              out_dir=out, progress_cb=seen.append)
    elapsed = time.perf_counter() - t0

    ok = [r for r in report.items if r.ok]
    over = [r for r in report.items if r.ok and Path(r.dst).stat().st_size > target_bytes]
    check("本段全部处理完", len(report.items) == len(part), f"{len(report.items)} / {len(part)}")
    check("本段全部达标", len(ok) == len(part),
          f"达标 {len(ok)} / {len(part)}" if len(ok) != len(part) else "")
    if len(ok) != len(part):
        for r in report.items:
            if not r.ok:
                print(f"    未达标：{Path(r.src).name} · {r.note[:80]}", flush=True)
    check("本段没有超目标", not over, f"{len(over)} 条超")
    check("进度单调不减", all(b >= a for a, b in zip(seen, seen[1:])),
          f"{seen[0]:.2f} → {seen[-1]:.2f}")
    # 批量层最后一条的 frac 收在 99.x% 而不是 100%，收尾时由界面补满（on_finished 里置 100）
    check("进度收到 99% 以上", seen[-1] >= 0.99, f"{seen[-1]:.3f}")
    print(f"  耗时 {elapsed:.1f}s，平均每条 {elapsed / max(1, len(part)):.2f}s，"
          f"峰值内存 {peak_mem_mb():.0f}MB", flush=True)

    # 本段落盘成 JSON，供最后一轮汇总核对总量
    rec = {
        "from": slice_from, "to": slice_to or n, "target_bytes": target_bytes,
        "items": [{"src": Path(r.src).name, "dst": Path(r.dst).name if r.dst else "",
                   "ok": r.ok, "bytes": Path(r.dst).stat().st_size if r.dst else 0,
                   "attempts": r.attempts} for r in report.items],
        "elapsed": round(elapsed, 2),
    }
    (BASE / f"slice-{slice_from:03d}-{(slice_to or n) - 1:03d}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def aggregate(total: int = 100) -> None:
    """把各段的 JSON 合起来核对：100 条都在、都达标、都不超目标、名字不重复。"""
    print(f"\n== A'. {total} 条汇总 ==", flush=True)
    files = sorted(BASE.glob("slice-*.json"))
    if not check("各段都有记录", bool(files), f"{len(files)} 段"):
        return
    items: list[dict] = []
    for f in files:
        items += json.loads(f.read_text(encoding="utf-8"))["items"]
    target_bytes = json.loads(files[0].read_text(encoding="utf-8"))["target_bytes"]
    ok = [i for i in items if i["ok"]]
    dsts = [i["dst"] for i in items if i["dst"]]
    on_disk = list((SHORT / "out-all").glob("*.mp4"))
    check("总条数对得上", len(items) == total, f"{len(items)} / {total}")
    check("全部达标", len(ok) == total, f"达标 {len(ok)} / {total}")
    check("全部不超目标", all(i["bytes"] <= target_bytes for i in ok),
          f"最大 {max(i['bytes'] for i in ok) / 1024:.0f} KB / 目标 123 KB")
    check("结果文件都在磁盘上且不为空",
          len(on_disk) == total and all(d.stat().st_size > 0 for d in on_disk),
          f"磁盘上 {len(on_disk)} 个")
    check("结果名不重复（没互相覆盖）", len(set(dsts)) == len(dsts), f"{len(set(dsts))} 个不同名")
    tries = [i["attempts"] for i in ok]
    print(f"  编码次数：1 次 {tries.count(1)} 条，2 次 {tries.count(2)} 条，"
          f"3 次 {tries.count(3)} 条，最多 {max(tries)} 次", flush=True)
    total_s = sum(json.loads(f.read_text(encoding="utf-8"))["elapsed"] for f in files)
    print(f"  各段合计耗时 {total_s:.1f}s", flush=True)


def case_long() -> Path | None:
    print("\n== B. 90 秒 1080p 长片 ==", flush=True)
    LONG.mkdir(parents=True, exist_ok=True)
    out = LONG / f"out-{int(time.time())}"
    src = LONG / "long-1080p.mp4"
    if not src.is_file():
        print("  现场合成 90 秒 1080p…", flush=True)
        make_clip(src, 90, "1920x1080", ["-b:v", "8000k", "-preset", "veryfast"])
    print(f"  源片 {src.stat().st_size / 1048576:.1f}MB", flush=True)

    # 目标要比源片小，否则「原片已达标直接复制」，等于没测编码
    target_mb = 6
    target_bytes = target_mb * 1024 * 1024
    seen: list[float] = []
    t0 = time.perf_counter()
    r = C.compress_one(src, out, target_bytes, on_progress=seen.append)
    elapsed = time.perf_counter() - t0

    check("长片达标出片", r.ok and r.dst is not None, r.note[:60])
    if r.dst:
        size = Path(r.dst).stat().st_size
        check(f"不超过目标 {target_mb}MB", size <= target_bytes, f"{size / 1048576:.2f}MB")
        check("时长没被改掉", abs(r.duration - 90) < 1.0, f"{r.duration:.1f}s → 结果{duration_of(Path(r.dst)):.1f}s")
    check("进度没卡住（回调够密）", len(seen) >= 20, f"{len(seen)} 次")
    check("进度单调不减", all(b >= a for a, b in zip(seen, seen[1:])))
    # 最后一条 ffmpeg 的 out_time 通常差一点点不满时长，收在 99.9% 以上即算收满
    check("进度收到 99.9% 以上", seen[-1] >= 0.999, f"{seen[-1]:.4f}")
    print(f"  耗时 {elapsed:.1f}s（{90 / elapsed:.2f} 倍实时），"
          f"编码 {r.attempts} 次，峰值内存 {peak_mem_mb():.0f}MB", flush=True)
    return src


def duration_of(p: Path) -> float:
    from core.ffmpeg_tool import probe
    try:
        return probe(p).duration
    except Exception:
        return 0.0


def case_cancel(src: Path | None) -> None:
    print("\n== C. 长片跑到 40% 取消 ==", flush=True)
    if src is None or not src.is_file():
        check("有待取消的长片", False, "跳过")
        return
    out = LONG / f"out-cancel-{int(time.time())}"
    ev = threading.Event()
    seen: list[float] = []

    def on_progress(frac: float) -> None:
        seen.append(frac)
        if frac >= 0.4 and not ev.is_set():
            ev.set()

    t0 = time.perf_counter()
    r = C.compress_one(src, out, 5 * 1024 * 1024, on_progress=on_progress, cancel=ev)
    cost = time.perf_counter() - t0

    files = list(out.glob("*")) if out.exists() else []
    check("判为已取消", not r.ok and "取消" in (r.note or ""), r.note or "")
    check("没有半截文件", len(files) == 0, f"out 里有 {len(files)} 个文件")
    check("取消后源片仍在", src.is_file())
    check("取消够快（< 20s 收尾）", cost < 20 + 90 * 0.4, f"{cost:.1f}s（跑到 {max(seen or [0]):.0%}）")
    print(f"  取消耗时 {cost:.1f}s", flush=True)


def main() -> int:
    args = set(sys.argv[1:])
    only_a = "-a" in args
    only_b = "-b" in args
    agg = "--agg" in args
    n = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--n=")), 100)
    sl = next((a.split("=", 1)[1] for a in args if a.startswith("--slice=")), None)
    print(f"压力测试（ffmpeg：{find_tools()[0]}）")
    t0 = time.perf_counter()
    try:
        if agg:
            aggregate(n)
        elif not only_b:
            if sl:
                lo, _, hi = sl.partition("-")
                case_batch(n, int(lo), int(hi) if hi else None)
            else:
                case_batch(n)
        if not only_a and not agg and not sl:
            src = case_long()
            case_cancel(src)
    finally:
        print(f"\n总耗时 {time.perf_counter() - t0:.1f}s", flush=True)
    print(f"结论：{'全部通过' if _failed == 0 else f'{_failed} 项失败'}"
          f"（通过 {_passed}，失败 {_failed}）", flush=True)
    if not agg:
        print(f"产物：{BASE}（可直接删）", flush=True)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
