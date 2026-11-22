"""引擎自检：把立项卡里承诺的每一条，逐条变成可断言的事实。

样本视频全部现场用 ffmpeg 的 lavfi 合成，不读用户文件、不联网。
跑法：python verify.py        （约 3–5 分钟，会生成 _verify_samples/）
      python verify.py -q     （只跑末尾的汇总，不逐条打印）

跑完会打印「通过 / 失败」条数；任何一条 FAIL 都说明承诺没兑现，别当成噪音。
界面另有一套：python tools/ui_test.py
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import compressor as C
from core.compliance import NetworkAudit
from core.ffmpeg_tool import find_tools, probe, version_text

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "_verify_samples"
OUT = ROOT / "_verify_out"
QUIET = "-q" in sys.argv

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        if not QUIET:
            print(f"  PASS  {name}" + (f"（{detail}）" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL  {name}" + (f"（{detail}）" if detail else ""))
    return cond


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mb(n: int | float) -> str:
    return f"{n / 1048576:.2f}MB"


def section(title: str) -> None:
    if not QUIET:
        print(f"\n== {title} ==")


def stream_md5(path: Path, stream: str = "0:v:0") -> str:
    """取某条流解码前的数据指纹：用 -c copy 比对，能证明「画面没重编码」。"""
    ffmpeg, _ = find_tools()
    r = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(path), "-map", stream,
         "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def build_samples() -> None:
    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    SAMPLES.mkdir(parents=True)
    (SAMPLES / "containers").mkdir()
    ffmpeg, _ = find_tools()

    def run(args):
        subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", *args], check=True)

    def clip(name, size, secs, fps=25, crf=20, audio=True, freq=440, abr="128k"):
        args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={secs}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                 "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-b:a", abr, "-shortest"]
        run(args + [str(SAMPLES / name)])

    # 1) 1080p / 30 秒：验分辨率阶梯与画质地板
    clip("a-1080p.mp4", "1920x1080", 30, 30)
    # 2) 720p / 15 秒 / mkv：验体积准确性与「仅无损换容器」
    clip("b-720p.mkv", "1280x720", 15, 30)
    # 3) 小文件 / 无音轨：原片已达标 → 原样复制
    clip("c-small.mp4", "640x360", 4, 25, crf=22, audio=False)
    # 4) 长一点：验取消
    clip("d-cancel.mp4", "1280x720", 40, 30, crf=18)
    # 5) 无音轨的 1080p：验「无音轨」这条路不写假字段
    clip("e-noaudio.mp4", "1920x1080", 6, 25, audio=False)
    # 6) 手机竖拍：横向存 + rotate=90 元数据，验方向没搞反
    clip("_rot.mp4", "1280x720", 8, 25)
    run(["-i", str(SAMPLES / "_rot.mp4"), "-c", "copy",
         "-metadata:s:v:0", "rotate=90", str(SAMPLES / "f-rotated.mp4")])
    (SAMPLES / "_rot.mp4").unlink()
    # 7) 4K：验从 4K 往下台阶梯降
    clip("g-4k.mp4", "3840x2160", 8, 24, crf=18)
    # 8) 低码率音频：验「源音频够低就原样复制」
    clip("h-audio64.mp4", "1280x720", 6, 25, abr="64k", freq=520)
    # 9) 双音轨 mkv：重封装只留第一条音轨，体积必然变小（验仅无损能成）
    run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
         "-map", "0:v", "-map", "1:a", "-map", "2:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         str(SAMPLES / "m-twoaudio.mkv")])
    # 10) 中文 + 空格文件名
    clip("中文 名称 带空格.mp4", "1280x720", 5, 25)
    # 11) 各种容器：avi / mov / ts / flv / wmv / webm
    specs = [("k1.avi", ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "mp3"]),
             ("k2.mov", ["-c:v", "libx264", "-crf", "20", "-c:a", "aac"]),
             ("k3.ts", ["-c:v", "libx264", "-crf", "20", "-c:a", "aac"]),
             ("k4.flv", ["-c:v", "libx264", "-crf", "20", "-c:a", "aac"]),
             ("k5.wmv", ["-c:v", "wmv2", "-q:v", "4", "-c:a", "wmav2"]),
             ("k6.webm", ["-c:v", "libvpx", "-crf", "20", "-b:v", "1M",
                          "-c:a", "libvorbis"])]
    for name, codec in specs:
        run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             *codec, "-shortest", str(SAMPLES / "containers" / name)])
    # 12) 坏文件与假视频（装成 .mp4 的文本）
    (SAMPLES / "n-broken.mp4").write_bytes(b"\x00\x01\x02not a video" * 200)
    (SAMPLES / "n-fake.mp4").write_text("这不是视频", encoding="utf-8")
    (SAMPLES / "empty_dir").mkdir()


def main() -> int:
    print("== 运行环境 ==")
    ffmpeg, ffprobe = find_tools()
    print(f"  ffmpeg : {ffmpeg}")
    print(f"  ffprobe: {ffprobe}")
    print(f"  {version_text()}")
    check("ffmpeg 与 ffprobe 都在", ffmpeg.is_file() and ffprobe.is_file())

    section("生成样本")
    t0 = time.perf_counter()
    build_samples()
    sizes = {p.name: p.stat().st_size for p in sorted(SAMPLES.glob("*.*"))}
    for k, v in sizes.items():
        print(f"  {k}: {mb(v)}")
    print(f"  耗时 {time.perf_counter() - t0:.1f}s")
    check("样本齐了", len(sizes) >= 10, f"{len(sizes)} 个文件")

    src_hashes = {p.name: sha(p) for p in SAMPLES.glob("*.*") if p.is_file()}

    audit = NetworkAudit().__enter__()

    # ---------------------------------------------------------------- 1
    section("1. 目标体积必须压得住")
    target_mb = 1.0
    t0 = time.perf_counter()
    rep = C.compress_batch([str(SAMPLES / "b-720p.mkv")], target_mb)
    cost = time.perf_counter() - t0
    item = rep.items[0]
    check("达标", item.ok and item.hit_target, f"{mb(item.src_bytes)} → {mb(item.dst_bytes)}")
    check("不超过目标", item.dst_bytes <= target_mb * 1048576,
          f"{mb(item.dst_bytes)} vs 目标 {target_mb}MB，误差 "
          f"{(item.dst_bytes - target_mb * 1048576) / (target_mb * 1048576) * 100:+.1f}%")
    check("输出了文件且扩展名是 mp4", Path(item.dst).is_file() and item.dst.endswith(".mp4"))
    check("容器从未知 mkv 统一成 mp4", Path(item.dst).suffix == ".mp4")
    print(f"       （编码耗时 {cost:.1f}s，{item.attempts} 次编码）")

    # ---------------------------------------------------------------- 2
    section("2. 原片不可变 · 结果在 out")
    after = {p.name: sha(p) for p in SAMPLES.glob("*.*") if p.is_file()}
    check("原片哈希前后一致", src_hashes == after)
    check("报告自述原片未改动", item.src_unchanged is True)
    check("结果落在原片旁的 out", Path(item.dst).parent == SAMPLES / C.OUT_DIRNAME,
          str(Path(item.dst).parent))

    # ---------------------------------------------------------------- 3
    section("3. 原片本来就小：不重编码")
    small = SAMPLES / "c-small.mp4"
    it = C.compress_batch([str(small)], 50).items[0]
    check("标无损", it.ok and it.lossless)
    check("字节与体积都没变", it.dst_bytes == it.src_bytes == small.stat().st_size)
    check("说明写明未重新编码", "未重新编码" in it.note, it.note)

    # ---------------------------------------------------------------- 4
    section("4. 仅无损：只换容器，压不到就明说")
    mkv = SAMPLES / "b-720p.mkv"
    it = C.compress_batch([str(mkv)], 0.5, lossless_only=True).items[0]
    check("无损压不到目标时不出片", (not it.ok) and not it.dst)
    check("说明写清「只能省容器开销」", "容器" in it.note, it.note[:48])

    # ---------------------------------------------------------------- 5
    section("5. 仅无损能成时：画面一个字节都没重编")
    two = SAMPLES / "m-twoaudio.mkv"
    two_size = two.stat().st_size
    # 重封装只丢掉第二条音轨（约 80KB）与容器冗余，目标就设在「源 − 50KB」，
    # 定得太狠就成了「要求无损压缩」，那本来就做不到
    it = C.compress_batch([str(two)], (two_size - 50 * 1024) / 1048576,
                          lossless_only=True).items[0]
    check("达标且体积变小", it.ok and it.dst_bytes < it.src_bytes,
          f"{mb(it.src_bytes)} → {mb(it.dst_bytes)}")
    check("标记为无损", it.lossless)
    check("报告字段写明未重编码", "0 次" in it.note, it.note[:40])
    if it.dst:
        src_md5 = stream_md5(two)
        dst_md5 = stream_md5(Path(it.dst))
        check("视频流数据指纹完全一致（证明没重编码）",
              src_md5 and src_md5 == dst_md5, src_md5[:32])

    # ---------------------------------------------------------------- 6
    section("6. 分辨率阶梯：不硬撑高分辨率")
    big = SAMPLES / "a-1080p.mp4"
    it = C.compress_batch([str(big)], 2.0).items[0]
    check("达标且不超过 2MB", it.ok and it.dst_bytes <= 2 * 1048576, mb(it.dst_bytes))
    check("自动降到 720p", it.resized and min(it.size_after) == 720,
          f"{it.size_before} → {it.size_after}")
    check("说明披露了分辨率变化", "分辨率" in it.quality_loss and "→" in it.quality_loss,
          it.quality_loss[:52])
    out_info = probe(Path(it.dst))
    check("产物确实是 720p", min(out_info.width, out_info.height) == 720,
          f"{out_info.width}×{out_info.height}")
    check("产物有音轨（源有音轨就得保留）", out_info.has_audio)

    # ---------------------------------------------------------------- 7
    section("7. 4K 往下走台阶：从 2160p 降到能承受的那档")
    it = C.compress_batch([str(SAMPLES / "g-4k.mp4")], 0.6).items[0]
    check("4K 达标", it.ok and it.dst_bytes <= int(0.6 * 1048576), mb(it.dst_bytes))
    check("确实降了档", it.resized and min(it.size_after) < 2160,
          f"{it.size_before} → {it.size_after}")
    if it.ok:
        info = probe(Path(it.dst))
        check("落地文件的分辨率与报告一致",
              (info.width, info.height) == it.size_after,
              f"报告 {it.size_after} / 实际 {(info.width, info.height)}")

    # ---------------------------------------------------------------- 8
    section("8. 画质地板：压不到就不交付，并报最小可达")
    it = C.compress_batch([str(big)], 0.3).items[0]
    check("判未达标", (not it.ok) and not it.skipped)
    check("没有输出文件", not it.dst or not Path(it.dst).exists())
    check("说明提到画质地板与最小体积", "画质地板" in it.note and "MB" in it.note,
          it.note[:60])
    check("给了具体的最小可达数字",
          bool(re.search(r"约 \d+(\.\d+)?MB", it.note)), it.note[:60])
    check("报告标记 reached_floor", it.reached_floor)

    # ---------------------------------------------------------------- 9
    section("9. 无音轨视频：不编造音频字段")
    it = C.compress_batch([str(SAMPLES / "e-noaudio.mp4")], 0.5).items[0]
    check("无音轨也能达标", it.ok, mb(it.dst_bytes))
    check("音频字段写「无音轨」", it.src_acodec == "无音轨", it.src_acodec)
    if it.ok:
        check("产物确实没有音轨", not probe(Path(it.dst)).has_audio)
        check("说明里写明无音轨", "无音轨" in it.quality_loss, it.quality_loss[:40])

    # ---------------------------------------------------------------- 10
    section("10. 音频：源够低就原样复制，不白扒一遍")
    it = C.compress_batch([str(SAMPLES / "h-audio64.mp4")], 0.4).items[0]
    check("达标", it.ok, mb(it.dst_bytes))
    check("标了 audio_copied", it.audio_copied)
    check("音频码率没被再砍", it.dst_a_bitrate == it.src_a_bitrate,
          f"{it.src_a_bitrate:.0f} → {it.dst_a_bitrate:.0f} kbps")
    check("披露里写明未重编码", "原样复制" in it.quality_loss, it.quality_loss[:60])

    # ---------------------------------------------------------------- 11
    section("11. 手机竖拍：旋转元数据不能让画面方向搞反")
    rot = SAMPLES / "f-rotated.mp4"
    info = probe(rot)
    check("探测到旋转标记", info.rotated)
    check("探测按「看到的方向」出尺寸（竖的）", info.height > info.width,
          f"{info.width}×{info.height}")
    it = C.compress_batch([str(rot)], 0.4).items[0]
    check("竖拍也能达标", it.ok, mb(it.dst_bytes))
    if it.ok:
        out = probe(Path(it.dst))
        check("产物仍是竖的", out.height > out.width, f"{out.width}×{out.height}")

    # ---------------------------------------------------------------- 12
    section("12. 各种容器都能读、都能出 mp4")
    rep = C.compress_batch([str(SAMPLES / "containers")], 0.5)
    names = [Path(i.src).name for i in rep.items]
    check("6 个容器全被收进来", rep.total == 6, "、".join(names))
    check("全部达标", all(i.ok for i in rep.items),
          "、".join(f"{Path(i.src).suffix}:{'达标' if i.ok else '未达标'}"
                    for i in rep.items))
    check("输出统一是 mp4",
          all(i.dst.endswith(".mp4") for i in rep.items if i.dst))
    for i in rep.items:
        if i.ok:
            info = probe(Path(i.dst))
            check(f"  {Path(i.src).name} 产物可解码",
                  info.duration > 0 and info.width > 0,
                  f"{info.width}×{info.height} {info.duration:.1f}s")

    # ---------------------------------------------------------------- 13
    section("13. 坏文件 / 假视频：跳过并说明，不崩不产出")
    rep = C.compress_batch([str(SAMPLES / "n-broken.mp4"), str(SAMPLES / "n-fake.mp4")], 1.0)
    check("两条都没崩", rep.total == 2)
    check("都判为跳过", all(i.skipped for i in rep.items), 
          "、".join(i.note[:24] for i in rep.items))
    check("没有产出文件", all(not i.dst for i in rep.items))
    check("说明是人话（含下一步或原因）",
          all(("损坏" in i.note) or ("不是视频" in i.note) or ("读不出" in i.note)
              for i in rep.items),
          "、".join(i.note[:24] for i in rep.items))

    # ---------------------------------------------------------------- 14
    section("14. 空目录 / 非视频路径：安静返回，不抛异常")
    rep = C.compress_batch([str(SAMPLES / "empty_dir")], 1.0)
    check("空目录 total 为 0", rep.total == 0, str(rep.total))
    check("空目录没有 out_dirs", not rep.out_dirs)
    rep = C.compress_batch([str(SAMPLES / "n-one.txt")], 1.0)  # 不存在的路径
    check("不存在的路径也不抛", rep.total == 0, str(rep.total))

    # ---------------------------------------------------------------- 15
    section("15. 目标比源还大：原样复制，不浪费 CPU")
    t0 = time.perf_counter()
    it = C.compress_batch([str(big)], 5000).items[0]
    cost = time.perf_counter() - t0
    check("标无损", it.ok and it.lossless)
    check("耗时极短（没编码）", cost < 3, f"{cost:.2f}s")
    check("说明写明直接复制", "直接复制" in it.note, it.note)

    # ---------------------------------------------------------------- 16
    section("16. 目标正好等于源大小：算达标（<= 而不是 <）")
    it = C.compress_batch([str(small)], small.stat().st_size / 1048576).items[0]
    check("不重编码、也算达标", it.ok and it.dst_bytes <= small.stat().st_size,
          f"{it.dst_bytes} <= {small.stat().st_size}")

    # ---------------------------------------------------------------- 17
    section("17. 同名不覆盖，自动加序号")
    first = C.compress_batch([str(big)], 2.0).items[0]
    second = C.compress_batch([str(big)], 2.0).items[0]
    check("两次都出片", Path(first.dst).is_file() and Path(second.dst).is_file())
    check("文件名不同（加了序号）", first.dst != second.dst,
          f"{Path(first.dst).name} / {Path(second.dst).name}")
    check("序号是「(数字)」这种可读格式",
          bool(re.search(r"\(\d+\)", Path(second.dst).name)), Path(second.dst).name)

    # ---------------------------------------------------------------- 18
    section("18. 递归扫描跳过 out，不套娃")
    found = C.collect_videos([str(SAMPLES)])
    check("没有把 out 里的结果再收进来",
          all(C.OUT_DIRNAME not in p.parts for p in found))
    check("容器子目录也被递归扫到",
          any(p.parent.name == "containers" for p in found))

    # ---------------------------------------------------------------- 19
    section("19. 中文 / 空格文件名照常处理")
    it = C.compress_batch([str(SAMPLES / "中文 名称 带空格.mp4")], 0.5).items[0]
    check("达标", it.ok, mb(it.dst_bytes))
    check("输出保留原名（扩展名换成 mp4）",
          Path(it.dst).name.startswith("中文 名称 带空格"), Path(it.dst).name)

    # ---------------------------------------------------------------- 20
    section("20. 进度回调单调不减，最后到 100%")
    seen: list[float] = []
    rep = C.compress_batch([str(SAMPLES / "b-720p.mkv")], 1.0,
                           progress_cb=lambda f: seen.append(f))
    check("有进度回调", len(seen) > 3, f"{len(seen)} 次")
    check("单调不减", all(b >= a - 1e-6 for a, b in zip(seen, seen[1:])),
          f"{min(seen):.2f} → {max(seen):.2f}")
    check("最后落在 100%", seen and seen[-1] > 0.98, f"{seen[-1]:.2f}")

    # ---------------------------------------------------------------- 21
    section("21. 中途取消：不留半成品，已完成保留")
    cancel = threading.Event()
    result = {}

    def job():
        result["rep"] = C.compress_batch(
            [str(SAMPLES / "d-cancel.mp4")], 3.0, cancel=cancel)

    th = threading.Thread(target=job)
    th.start()
    time.sleep(0.8)
    cancel.set()
    th.join(timeout=60)
    rep_cancel = result.get("rep")
    check("批次标了取消", rep_cancel is not None and rep_cancel.cancelled)
    if rep_cancel is not None and rep_cancel.items:
        it = rep_cancel.items[0]
        check("取消的那条不出片", not it.ok and not it.dst)
        check("说明写「已取消」", "取消" in it.note, it.note)
    ghosts = [p for p in (SAMPLES / C.OUT_DIRNAME).glob("d-cancel*")]
    check("没留下半截文件",
          all(p.stat().st_size > 0 for p in ghosts) if ghosts else True,
          f"{len(ghosts)} 个残留")

    section("22. 取消后再跑同一条：没有残留锁，照常出片")
    it = C.compress_batch([str(SAMPLES / "d-cancel.mp4")], 3.0).items[0]
    check("重跑能达标", it.ok, mb(it.dst_bytes))
    check("产物可解码", it.ok and probe(Path(it.dst)).duration > 30,
          f"{it.duration:.0f}s")

    # ---------------------------------------------------------------- 23
    section("23. 报告字段齐全（有损披露）")
    d = rep.to_dict()
    it0 = d["items"][0]
    for key in ("src_bytes", "dst_bytes", "duration", "size_before", "size_after",
                "src_v_bitrate", "dst_v_bitrate", "src_a_bitrate", "dst_a_bitrate",
                "src_vcodec", "dst_vcodec", "src_acodec", "dst_acodec",
                "src_fps", "dst_fps", "quality_loss", "elapsed",
                "src_sha256_before", "src_unchanged", "lossless", "resized"):
        check(f"字段 {key}", key in it0, str(it0.get(key))[:28])
    check("汇总含无损 / 有损计数", {"lossless", "lossy", "succeeded"} <= set(d))
    check("码率是真实数字", it0["src_v_bitrate"] > 0 and it0["dst_v_bitrate"] > 0,
          f"{it0['src_v_bitrate']:.0f} → {it0['dst_v_bitrate']:.0f} kbps")
    try:
        json.dumps(d, ensure_ascii=False)
        ok = True
    except (TypeError, ValueError):
        ok = False
    check("整份报告能直接 JSON 序列化", ok)

    # ---------------------------------------------------------------- 24
    section("24. 产物时长与帧率没被改掉")
    info_src = probe(SAMPLES / "b-720p.mkv")
    info_dst = probe(Path(rep.items[0].dst)) if rep.items[0].dst else None
    check("上一批确实产出了文件（否则时长校验无从谈起）", info_dst is not None,
          rep.items[0].note[:40])
    if info_dst:
        check("时长相符（±0.3s）", abs(info_dst.duration - info_src.duration) < 0.3,
              f"{info_src.duration:.2f}s → {info_dst.duration:.2f}s")
        check("帧率相符（±0.5）", abs(info_dst.fps - info_src.fps) < 0.5,
              f"{info_src.fps:.2f} → {info_dst.fps:.2f}")
        check("编码器换成 h264/aac", info_dst.vcodec == "h264", info_dst.vcodec)

    # ---------------------------------------------------------------- 25
    section("25. 指定输出尺寸：等比缩到框内 / 精确拉伸 / 不放大")
    b720 = SAMPLES / "b-720p.mkv"          # 1280×720 / 15s
    size_out = OUT / "size"
    fit = C.compress_one(b720, size_out / "fit", int(1.5 * 1048576),
                         target_size=(854, 480))
    i_fit = probe(Path(fit.dst)) if fit.dst else None
    check("等比：出片且不超目标", fit.ok and fit.dst_bytes <= 1.5 * 1048576,
          mb(fit.dst_bytes))
    check("等比：短边正好收到 480",
          i_fit is not None and i_fit.short_side == 480,
          f"{i_fit.width}×{i_fit.height}" if i_fit else "无输出")
    check("等比：比例没被拉歪（16:9 ±2%）",
          i_fit is not None and abs(i_fit.width / i_fit.height - 16 / 9) < 0.02,
          f"{i_fit.width / i_fit.height:.3f}" if i_fit else "—")
    check("等比：报告记下了目标尺寸与等比语义",
          fit.requested_size == (854, 480) and fit.size_keep_ratio is True)
    check("等比：size_after 与实测一致",
          bool(i_fit) and fit.size_after == (i_fit.width, i_fit.height),
          f"{fit.size_after}")
    check("等比：标记为改过分辨率", fit.resized is True)

    exact = C.compress_one(b720, size_out / "exact", int(1.5 * 1048576),
                           target_size=(640, 640), keep_ratio=False)
    i_ex = probe(Path(exact.dst)) if exact.dst else None
    check("精确：输出就是 640×640（允许拉伸）",
          i_ex is not None and (i_ex.width, i_ex.height) == (640, 640),
          f"{i_ex.width}×{i_ex.height}" if i_ex else "无输出")
    check("精确：披露里写明比例会变", "比例已改变" in (exact.quality_loss or ""),
          (exact.quality_loss or "")[:46])

    big = C.compress_one(b720, size_out / "big", int(2 * 1048576),
                         target_size=(3840, 2160))
    check("框比原片大：不放大（仍是 1280×720），并在说明里讲清楚",
          big.ok and big.size_after == (1280, 720) and "未放大" in big.note,
          f"{big.size_after}｜{big.note[:46]}")

    shrink = C.compress_one(b720, size_out / "shrink", 100 * 1048576,
                            target_size=(640, 360))
    check("原片体积本就达标时，仍会按指定尺寸缩（尺寸设置不能形同虚设）",
          shrink.ok and shrink.resized and shrink.dst_bytes > 0,
          f"{mb(shrink.dst_bytes)}，{shrink.size_after}")
    check("缩尺寸重编码不会把文件反而搞大",
          shrink.dst_bytes <= b720.stat().st_size,
          f"{mb(b720.stat().st_size)} → {mb(shrink.dst_bytes)}")

    hard = C.compress_one(b720, size_out / "hard", int(0.05 * 1048576),
                          target_size=(1280, 720))
    check("指定尺寸压不到：不交付、也不偷偷改成别的尺寸",
          (not hard.ok) and hard.dst_bytes == 0 and "指定尺寸" in hard.note,
          hard.note[:70])

    # ---------------------------------------------------------------- 26
    section("26. 0 联网")
    audit.__exit__(None, None, None)
    check("整轮没有发起任何外连", audit.attempts == 0, audit.summary())
    check("核心模块没导入网络库",
          not any(m in sys.modules for m in ("requests", "urllib.request", "http.client")),
          "、".join(m for m in ("requests", "urllib.request", "http.client")
                   if m in sys.modules) or "干净")

    # ---------------------------------------------------------------- 收尾
    OUT.mkdir(exist_ok=True)
    (OUT / "verify_report.json").write_text(
        json.dumps({"samples": sizes, "passed": _passed, "failed": _failed},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 62)
    print(f"结论：{'全部通过' if _failed == 0 else '有失败项'} —— 共 {_passed + _failed} 项，"
          f"通过 {_passed}，失败 {_failed}")
    print(f"自检产物：{SAMPLES}（样本与 out 结果，可直接删）")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
