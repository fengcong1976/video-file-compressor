"""按目标体积压视频：码率反推、两遍编码、分辨率阶梯、逐条报告。

为什么不用单遍 CRF：CRF 决定的是「画质」，不是「体积」。同一 CRF 在不同片子上
能差出三五倍，用户要的是「不超过 25MB」，不是「差不多清楚」。所以这里走码率路线：
先用目标体积反推总码率，再两遍编码，最后复称体积，超了就修一次。

量出来的两个事实（2026-11-10，60 秒 1080p 源片 63MB / 8809kbps → 目标 5MB）：
  · 不留余量：5.07MB（超 1.4%），单遍按码率估会稳定超出一点；
  · 留 3% 余量：4.92MB（差 −1.5%），耗时 23.4s。
所以余量必须留 —— 超 1% 就过不了「不超过 25MB」那道门。

输出尺寸有两种：默认「保持原尺寸」，由程序按目标体积自动决定要不要降档；
也可以指定「宽 × 高」，此时只按这个尺寸编一档（等比缩到框内不拉伸不放大，
或按表单要求精确拉伸），压不到就如实说不达标，不偷偷改成别的尺寸。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ffmpeg_tool import (FFmpegMissing, VideoInfo, cfr_args, describe_failure,
                          probe, run_ffmpeg)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm",
              ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
OUT_DIRNAME = "out"        # 结果就近落在原片旁边的这个子目录
OUT_EXT = ".mp4"           # 统一成 mp4：微信、邮件、各平台都认
DEFAULT_TARGET_MB = 25

# 画质地板：按分辨率分档。低于这条线，画面已经不是「糊一点」而是「看不清」，
# 宁可不出片也不交付。数字来自常用码率表（1080p 约 2M 起步，480p 约 300–500k），
# 取的是「还能看」的下限，不是「好看」的推荐值。
MIN_KBPS_BY_SHORT_SIDE = ((1000, 600.0), (700, 400.0), (0, 200.0))
AUDIO_KBPS = 96
# 码率 / 像素 低于这个值就提示「画面会偏软」，但仍然出片（有些内容本来就压得动）
SOFT_BPP = 0.03
# 目标体积的 97% 当预算：剩下 3% 留给容器开销与码率控制的误差
SAFETY = 0.97
# 同一分辨率下体积仍超标时，按实测比例修正再编一次；两次还不行就降一档
MAX_RETRY = 2
PRESET = "veryfast"
# 分辨率阶梯：原片 → 720 → 480（只降不升）
LADDER = (None, 720, 480)
# 超过这个体积不再整份读两遍做哈希，改用「大小 + 修改时间」复核
FULL_HASH_LIMIT = 2 * 1024 * 1024 * 1024


@dataclass
class ItemReport:
    src: str
    target_bytes: int = 0
    src_bytes: int = 0
    duration: float = 0.0
    size_before: tuple = ()
    size_after: tuple = ()
    src_v_bitrate: float = 0.0
    dst_v_bitrate: float = 0.0
    src_a_bitrate: float = 0.0
    dst_a_bitrate: float = 0.0
    src_vcodec: str = ""
    dst_vcodec: str = ""
    src_acodec: str = "无音轨"
    dst_acodec: str = ""
    src_fps: float = 0.0
    dst_fps: float = 0.0
    audio_copied: bool = False
    fmt_in: str = ""
    dst: str = ""
    dst_bytes: int = 0
    ok: bool = False
    hit_target: bool = False
    lossless: bool = False
    skipped: bool = False
    resized: bool = False
    # 用户指定的目标尺寸（空 = 保持原尺寸）；size_keep_ratio 决定是等比缩到框内还是精确拉伸
    requested_size: tuple = ()
    size_keep_ratio: bool = True
    note: str = ""
    quality_loss: str = ""
    elapsed: float = 0.0
    attempts: int = 0
    src_sha256_before: str = ""
    src_unchanged: bool | None = None
    src_check: str = "sha256"

    @property
    def reached_floor(self) -> bool:
        """是否因为画质地板而放弃（界面据此说「不是失败，是不值得」）。"""
        return (not self.ok) and (not self.skipped) and "画质地板" in self.note

    @property
    def reencoded(self) -> bool:
        """画面有没有重新编码过。仅无损与「原片已达标」这两种都没重编。"""
        return self.ok and not self.lossless

    def _size_request_text(self) -> str:
        if not self.requested_size:
            return "保持原尺寸（按目标体积自动决定是否降档）"
        box = f"{self.requested_size[0]}×{self.requested_size[1]}"
        if self.size_keep_ratio:
            return f"{box}（等比缩到框内，不拉伸、不放大）"
        return f"{box}（精确到该尺寸，比例会变）"

    def disclosure(self) -> list:
        """有损披露：立项卡要求逐条给出的字段，界面详情页与报告共用一份。"""
        def dims(d):
            return f"{d[0]}×{d[1]}" if d else "—"

        if not self.ok:
            rows = [("是否重编码", "否（未交付结果）")]
        elif self.lossless:
            rows = [("是否重编码", "否（只换容器或原样复制）")]
        else:
            rows = [("是否重编码", "是（H.264 两遍编码）")]
        rows += [
            ("分辨率", f"{dims(self.size_before)} → {dims(self.size_after)}"
                       if self.resized else f"{dims(self.size_before)}（未变）"),
            ("尺寸设定", self._size_request_text()),
            ("视频码率", f"{self.src_v_bitrate:.0f} kbps → {self.dst_v_bitrate:.0f} kbps"
                         if self.dst_v_bitrate else "—"),
            ("音频", ("无音轨" if self.src_acodec == "无音轨" else
                      f"{self.src_a_bitrate:.0f} kbps {self.src_acodec} → "
                      f"{self.dst_a_bitrate:.0f} kbps {self.dst_acodec or self.src_acodec}"
                      + ("（原样复制，未重编码）" if self.audio_copied else ""))),
            ("帧率", f"{self.src_fps:.2f} → {self.dst_fps or self.src_fps:.2f}"
                     if self.src_fps else "—"),
            ("编码器", "H.264 (libx264) + AAC" if self.reencoded
                       else f"{self.dst_vcodec or self.src_vcodec}（未重编码）"),
            ("体积", f"{self.src_bytes / 1048576:.1f} MB → "
                     f"{self.dst_bytes / 1048576:.1f} MB" if self.dst_bytes
                     else f"{self.src_bytes / 1048576:.1f} MB（未产出）"),
            ("损失说明", self.quality_loss or self.note or "—"),
            ("原片是否改动", "未改动（SHA-256 前后一致）" if self.src_unchanged
                             else "无法核实"),
            ("耗时", f"{self.elapsed:.1f} s（{self.attempts} 次编码）"),
        ]
        return rows


@dataclass
class BatchReport:
    target_kb: float = 0.0
    items: list = field(default_factory=list)
    elapsed: float = 0.0
    cancelled: bool = False
    lossless_only: bool = False
    target_size: tuple = ()       # 空 = 保持原尺寸
    size_keep_ratio: bool = True
    out_dirs: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> list:
        return [i for i in self.items if i.ok]

    @property
    def failed(self) -> list:
        return [i for i in self.items if not i.ok and not i.skipped]

    @property
    def skipped(self) -> list:
        return [i for i in self.items if i.skipped]

    @property
    def lossless(self) -> list:
        return [i for i in self.items if i.ok and i.lossless]

    @property
    def lossy(self) -> list:
        return [i for i in self.items if i.ok and not i.lossless]

    @property
    def src_bytes_sum(self) -> int:
        return sum(i.src_bytes for i in self.items)

    @property
    def dst_bytes_sum(self) -> int:
        return sum(i.dst_bytes for i in self.items if i.ok)

    def to_dict(self) -> dict:
        return {
            "target_kb": self.target_kb,
            "elapsed": round(self.elapsed, 2),
            "cancelled": self.cancelled,
            "lossless_only": self.lossless_only,
            "target_size": list(self.target_size),
            "size_keep_ratio": self.size_keep_ratio,
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "lossless": len(self.lossless),
            "lossy": len(self.lossy),
            "src_bytes": self.src_bytes_sum,
            "dst_bytes": self.dst_bytes_sum,
            "out_dirs": list(self.out_dirs),
            "items": [asdict(i) for i in self.items],
        }


# ---------------------------------------------------------------- 文件与命名

def collect_videos(paths) -> list[Path]:
    """收集要处理的视频；递归时跳过已生成的 out，免得把结果再压一遍。"""
    found: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        batch: list[Path] = []
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                    continue
                if OUT_DIRNAME in f.parts:
                    continue
                batch.append(f)
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            batch.append(p)
        for f in batch:
            key = str(f.resolve()).lower()
            if key not in seen:
                seen.add(key)
                found.append(f)
    return found


def _reserve_path(out_dir: Path, filename: str):
    """原子占名：抢不到就换 `名字 (1).mp4`，绝不覆盖已有文件。"""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    i = 0
    while True:
        p = out_dir / (filename if i == 0 else f"{stem} ({i}){suffix}")
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
            return p
        except FileExistsError:
            i += 1
            if i > 9999:
                raise RuntimeError(f"输出目录里同名文件过多：{filename}")


# ---------------------------------------------------------------- 体积与码率

def target_bitrate_kbps(target_bytes: int, duration: float, audio_k: float) -> float:
    """目标体积 → 视频码率。留 3% 给容器开销，音频码率先扣掉。"""
    if duration <= 0:
        return 0.0
    return target_bytes * 8 / duration / 1000 * SAFETY - audio_k


def pick_audio_kbps(info: VideoInfo) -> float:
    """音频码率取 min(96, 源)，源本来就低就别再砍一遍。"""
    if not info.has_audio:
        return 0.0
    if info.a_bitrate and info.a_bitrate < AUDIO_KBPS:
        return max(48.0, round(info.a_bitrate))
    return float(AUDIO_KBPS)


def _audio_args(info: VideoInfo, audio_k: float) -> tuple[list[str], bool]:
    """音频参数：源已经是 mp4 装得下的编码且码率不高，就原样复制。

    128k 的 AAC 再压到 96k 听感差别很小、却要多扒一遍编码，不如直接 copy：
    更快、也不掉一点音质。opus / vorbis 这类 mp4 装不进去，只能转 AAC。
    """
    if not info.has_audio:
        return ["-an"], False
    if info.acodec in ("aac", "mp3") and 0 < info.a_bitrate <= audio_k + 1:
        return ["-c:a", "copy"], True
    return ["-c:a", "aac", "-b:a", f"{audio_k:.0f}k"], False


def _scale_filter(info: VideoInfo, limit: int | None) -> tuple[str | None, tuple]:
    """按短边封顶算 scale 滤镜；返回 (滤镜或 None, 目标尺寸)。只降不升。"""
    if limit is None or info.short_side <= limit:
        return None, (info.width, info.height)
    if info.width >= info.height:  # 横屏：高封顶，宽按比例偶数化
        return (f"scale=-2:{limit}",
                (round(info.width * limit / info.height / 2) * 2, limit))
    # 竖屏（手机竖拍）：宽封顶。注意这里是「=」，写成「:」会被 ffmpeg 当成
    # 不存在的滤镜名 scale:480:-2，整条竖拍视频直接报错——2026-11-10 实测踩到。
    return (f"scale={limit}:-2",
            (limit, round(info.height * limit / info.width / 2) * 2))


def _even(n: float) -> int:
    """yuv420p 的宽高必须是偶数；向下取偶，宁可少两像素也不让编码失败。"""
    return max(2, int(round(n)) // 2 * 2)


def _box_filter(info: VideoInfo, box: tuple,
                keep_ratio: bool) -> tuple[str | None, tuple]:
    """按用户指定的尺寸缩放；返回 (滤镜或 None, 目标尺寸)。

    keep_ratio=True（默认）：等比缩到 box 这个框内，长边贴边、短边等比让位，
    不拉伸也不放大（原片比框小的原样输出）。
    keep_ratio=False：精确输出到该宽高，比例会被改变（表单强制尺寸时才这么用）。
    """
    bw, bh = max(2, int(box[0])), max(2, int(box[1]))
    if keep_ratio:
        ratio = min(bw / info.width, bh / info.height)
        if ratio >= 1.0:  # 只降不升：放大不会多出细节，只会更糊更慢
            return None, (info.width, info.height)
        w, h = _even(info.width * ratio), _even(info.height * ratio)
    else:
        w, h = _even(bw), _even(bh)
    if (w, h) == (_even(info.width), _even(info.height)):
        return None, (info.width, info.height)
    return f"scale={w}:{h}", (w, h)


def min_video_kbps(short_side: int) -> float:
    """该分辨率下「还能看」的视频码率下限。

    固定一个 300kbps 是不对的：1080p 跑到 300kbps 已经是满屏色块，
    而 480p 跑到 300kbps 只是偏软。地板必须跟着分辨率走，否则「降一档再试」
    永远不会被触发——阈值不随分辨率变化，降档就白降了。
    """
    for threshold, kbps in MIN_KBPS_BY_SHORT_SIDE:
        if short_side >= threshold:
            return kbps
    return MIN_KBPS_BY_SHORT_SIDE[-1][1]


def floor_bytes(duration: float, audio_k: float, short_side: int = 480) -> int:
    """画质地板下「最小可达体积」——用来告诉用户「这条最多只能到多少 MB」。"""
    return int(duration * (min_video_kbps(short_side) + audio_k) * 1000 / 8 * 1.01)


# ---------------------------------------------------------------- 单条处理

def _hash_or_stat(src: Path) -> tuple[str, str]:
    size = src.stat().st_size
    if size <= FULL_HASH_LIMIT:
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest(), "sha256"
    st = src.stat()
    return f"{st.st_size}-{int(st.st_mtime)}", "size+mtime"


def compress_one(src: Path, out_dir: Path, target_bytes: int,
                 lossless_only: bool = False, on_progress=None,
                 cancel: threading.Event | None = None,
                 target_size: tuple | None = None,
                 keep_ratio: bool = True) -> ItemReport:
    """压一条视频。任何异常都收进报告，不往外抛——批量里一条坏了不能拖垮整批。

    target_size=(宽, 高) 时按用户指定的尺寸输出；None 时保持原尺寸，由程序按
    目标体积自动决定要不要降档。keep_ratio 只对 target_size 生效。
    """
    t0 = time.perf_counter()
    src = Path(src)
    r = ItemReport(src=str(src), target_bytes=int(target_bytes),
                   requested_size=tuple(target_size) if target_size else (),
                   size_keep_ratio=bool(keep_ratio))
    tmp_dir = None
    reserved: Path | None = None
    try:
        stat = src.stat()
        r.src_bytes = stat.st_size
        r.src_sha256_before, r.src_check = _hash_or_stat(src)

        info = probe(src)
        r.duration = info.duration
        r.size_before = (info.width, info.height)
        r.src_v_bitrate = info.v_bitrate
        r.src_a_bitrate = info.a_bitrate
        r.src_vcodec = info.vcodec
        r.src_acodec = info.acodec or "无音轨"
        r.src_fps = info.fps
        r.fmt_in = info.fmt
        audio_k = pick_audio_kbps(info)
        audio_args, audio_copied = _audio_args(info, audio_k)

        # 1) 原片已经够小、且不需要改尺寸：原样复制，不重编码，标无损
        #    （指定了更小的尺寸时不能直接复制：那样尺寸设置等于没生效）
        need_scale = bool(target_size) and _box_filter(info, target_size, keep_ratio)[0] is not None
        if r.src_bytes <= target_bytes and not need_scale:
            out_dir.mkdir(parents=True, exist_ok=True)
            reserved = _reserve_path(out_dir, src.stem + OUT_EXT)
            shutil.copyfile(src, reserved)
            r.dst, r.dst_bytes = str(reserved), reserved.stat().st_size
            r.ok = r.hit_target = r.lossless = True
            r.dst_vcodec, r.size_after = info.vcodec, (info.width, info.height)
            r.dst_v_bitrate, r.dst_a_bitrate = info.v_bitrate, info.a_bitrate
            r.dst_acodec, r.dst_fps, r.audio_copied = r.src_acodec, info.fps, True
            r.note = "原片已达标，直接复制，未重新编码"
            return _finish(src, r, t0)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 2) 仅无损：只换容器（-c copy），编码数据一个字节不动
        #    与「指定尺寸」互斥：-c copy 没法缩放，界面已把两者锁成一边。
        if lossless_only:
            reserved = _reserve_path(out_dir, src.stem + OUT_EXT)
            rc, err = run_ffmpeg(
                ["-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                 "-c", "copy", "-movflags", "+faststart", str(reserved)],
                info.duration, on_progress, cancel)
            if rc != 0:
                _drop(reserved)
                reserved = None
                r.note = "已取消" if (cancel is not None and cancel.is_set()) \
                    else describe_failure(rc, err)
                return _finish(src, r, t0)
            size = reserved.stat().st_size
            if size <= target_bytes:
                r.dst, r.dst_bytes = str(reserved), size
                r.ok = r.hit_target = r.lossless = True
                r.dst_vcodec = info.vcodec
                r.size_after = (info.width, info.height)
                r.dst_v_bitrate, r.dst_a_bitrate = info.v_bitrate, info.a_bitrate
                r.dst_acodec, r.dst_fps, r.audio_copied = r.src_acodec, info.fps, True
                r.note = (f"仅无损：换成 mp4 容器，画面重新编码过 0 次；"
                          f"{r.src_bytes / 1048576:.1f}MB → {size / 1048576:.1f}MB")
                return _finish(src, r, t0)
            _drop(reserved)
            reserved = None
            r.note = (f"仅无损只能省容器开销：换容器后仍 {size / 1048576:.1f}MB，"
                      f"超过目标 {target_bytes / 1048576:.1f}MB。要更小必须重编码，"
                      "请取消「仅无损」再压一次")
            return _finish(src, r, t0)

        # 3) 重编码：用户指定了尺寸就只按那一档，否则逐档试（原片 → 720 → 480）
        tmp_dir = Path(tempfile.mkdtemp(prefix="vcmp_"))
        budget = target_bytes * SAFETY
        video_k = target_bitrate_kbps(budget, info.duration, audio_k)
        # 体积本来就达标、只是要改尺寸：按「像素数比例」折算原片码率，
        # 这样画质与原片相当，体积也不会因为缩了分辨率反而变大。
        keep_quality = r.src_bytes <= target_bytes
        limit_bytes = min(target_bytes, r.src_bytes) if keep_quality else target_bytes
        last_err = ""
        lowest_short = info.short_side
        best_size = 0  # 压到地板时实际能到多小：报告里给的是实测值，不是估算
        if target_size:
            # 点名了尺寸就只编这一档：擅自再降档等于没听用户的
            plans = [_box_filter(info, target_size, keep_ratio)]
        else:
            plans = []
            for rung in LADDER:
                vf, dims = _scale_filter(info, rung)
                if rung is not None and (vf is None or dims == r.size_before):
                    continue  # 原片本来就比这一档小，跳过（不许放大）
                plans.append((vf, dims))
        if keep_quality and target_size:
            out_w, out_h = plans[0][1]
            pixel_ratio = (out_w * out_h) / max(1, info.width * info.height)
            video_k = min(video_k, max(1.0, (info.v_bitrate or video_k) * pixel_ratio))
        fixed_floor_hit = False  # 指定尺寸下要达标就会低于画质下限
        for vf, dims in plans:
            lowest_short = min(lowest_short, min(dims))
            if video_k < min_video_kbps(min(dims)):
                if target_size:
                    fixed_floor_hit = True
                    break  # 不偷偷改成别的尺寸，交给下面如实说明
                continue  # 自动档：这一档码率低于地板，画面必然糊，换更低的档
            attempt_k = video_k
            for attempt in range(1, MAX_RETRY + 1):
                if cancel is not None and cancel.is_set():
                    r.note = "已取消"
                    return _finish(src, r, t0)
                r.attempts += 1
                reserved = _reserve_path(out_dir, src.stem + OUT_EXT)
                ok, size, err = _encode_two_pass(
                    src, reserved, info, vf, attempt_k, audio_args, tmp_dir,
                    on_progress, cancel)
                last_err = err
                if not ok:
                    _drop(reserved)
                    reserved = None
                    # ffmpeg 是被取消掐掉的：别把「编码失败」当成结论写进报告
                    if cancel is not None and cancel.is_set():
                        r.note = "已取消"
                        return _finish(src, r, t0)
                    break
                if size <= limit_bytes:
                    r.hit_target = True
                    break
                best_size = min(best_size, size) if best_size else size
                _drop(reserved)  # 超标就不交付，按实测比例修正后再来
                reserved = None
                if attempt == MAX_RETRY:
                    break
                attempt_k = max(min_video_kbps(min(dims)),
                                attempt_k * (limit_bytes * 0.99) / size * 0.97)
            if reserved is not None and r.hit_target:
                r.dst, r.dst_bytes = str(reserved), size
                r.ok = True
                r.dst_vcodec = "h264"
                r.size_after = dims
                r.dst_v_bitrate = (size * 8 / info.duration / 1000) - audio_k
                r.dst_a_bitrate = info.a_bitrate if audio_copied else audio_k
                r.dst_acodec = r.src_acodec if audio_copied else ("aac" if info.has_audio else "无音轨")
                r.dst_fps = info.fps
                r.audio_copied = audio_copied
                r.resized = dims != r.size_before
                r.lossless = False
                parts = []
                if r.resized:
                    how = ("按指定尺寸精确缩放，比例已改变，画面会拉伸"
                           if target_size and not keep_ratio
                           else "重采样，细节按比例减少")
                    parts.append(f"分辨率 {r.size_before[0]}×{r.size_before[1]} → "
                                 f"{dims[0]}×{dims[1]}（{how}）")
                else:
                    parts.append("分辨率未变")
                parts.append(f"码率 {info.total_bitrate:.0f} → "
                             f"{r.dst_v_bitrate + r.dst_a_bitrate:.0f} kbps")
                if info.has_audio:
                    parts.append(f"音频 {info.a_bitrate:.0f} → {r.dst_a_bitrate:.0f} kbps"
                                 + ("（原样复制，未重编码）" if audio_copied else " AAC"))
                else:
                    parts.append("无音轨")
                parts.append("重新编码：细节与运动场景锐度下降，快速画面可能出现轻微抖动")
                r.quality_loss = "有损：" + "；".join(parts)
                soft = r.dst_v_bitrate / max(1.0, dims[0] * dims[1] * max(info.fps, 1) / 1000)
                extra = "" if r.attempts == 1 else f"（第 {r.attempts} 次编码才够）"
                # 指定了尺寸却没缩：说一句「没放大」，免得用户以为设置没生效
                if target_size and not r.resized:
                    extra += (f"；目标尺寸 {int(target_size[0])}×{int(target_size[1])} "
                              f"不小于原片，未放大")
                if keep_quality:
                    r.note = f"原片体积已达标，只按指定尺寸重编码{extra}"
                elif soft < SOFT_BPP:
                    r.note = (f"已压到目标大小{extra}；"
                              f"码率偏低，静态内容没问题，快速运动画面会偏软")
                else:
                    r.note = f"已压到目标大小{extra}"
                return _finish(src, r, t0)

        # 4) 压不到：如实说不交付，并给出「最小能到多少」
        floor_k = min_video_kbps(lowest_short)
        want_mb = target_bytes / 1048576
        # 目标小于 1MB 时写 MB 会变成「0.0MB」，换成 KB 才看得懂
        want_txt = (f"{target_bytes / 1024:.0f}KB" if target_bytes < 1048576
                    else f"{want_mb:.1f}MB")
        if last_err and "取消" not in r.note:
            r.note = describe_failure(-1, last_err)
        elif not r.note:
            if target_size:
                out_w, out_h = plans[-1][1]
                here = f"{out_w}×{out_h}"
                # 框比原片大才会走到「不放大」这句话；框正好等于原片时不算
                no_upscale = (keep_ratio and min(int(target_size[0]) / info.width,
                                                 int(target_size[1]) / info.height) > 1.0)
                head = (f"指定的 {int(target_size[0])}×{int(target_size[1])} "
                        f"不小于原片、不放大，实际按 {here} 处理：" if no_upscale
                        else f"在指定尺寸 {here} 下，")
                if video_k <= 0 or fixed_floor_hit:
                    if video_k <= 0:
                        why = (f"{want_txt} 连音频轨道本身（{audio_k:.0f}kbps）都放不下，"
                               "画面没法编")
                    else:
                        why = (f"要压到 {want_txt} 只能给 {video_k:.0f}kbps，低于该分辨率"
                               f"的画质下限（{floor_k:.0f}kbps），不交付比这更糊的结果")
                    r.note = (f"{head}{why}。"
                              "可调高目标体积、把尺寸改小，或先把视频剪短再压")
                elif best_size:
                    r.note = (
                        f"{head}重试 {MAX_RETRY} 次仍超目标：实测最小 "
                        f"{best_size / 1048576:.2f}MB > {want_txt}。"
                        "可调高目标体积、把尺寸改小，或先把视频剪短再压")
                else:
                    r.note = (f"{head}压不到 {want_txt}。"
                              "可调高目标体积、把尺寸改小，或先把视频剪短再压")
                return _finish(src, r, t0)
            if best_size:
                # 真的编过、复称过：用实测最小值，比按码率估出来的更可信
                got = best_size / 1048576
                r.note = (f"压到画质地板（短边 {lowest_short}p、{floor_k:.0f}kbps 下限）"
                          f"实测仍有 {got:.2f}MB，超过目标 {want_txt}，"
                          "不交付比这更糊的结果。可调高目标体积，或先把视频剪短再压")
            else:
                min_mb = floor_bytes(info.duration, audio_k, lowest_short) / 1048576
                r.note = (f"压到画质地板（短边 {lowest_short}p、{floor_k:.0f}kbps）"
                          f"仍有约 {min_mb:.1f}MB，超过目标 "
                          f"{want_txt}，不交付比这更糊的结果。"
                          "可调高目标体积，或先把视频剪短再压")
        return _finish(src, r, t0)
    except FFmpegMissing as exc:
        r.skipped, r.note = True, str(exc)
        return _finish(src, r, t0)
    except ValueError as exc:
        r.skipped, r.note = True, str(exc)
        return _finish(src, r, t0)
    except OSError as exc:
        r.note = f"读写失败：{exc.strerror or exc}（检查文件是否被占用或磁盘是否满）"
        return _finish(src, r, t0)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 取消 / 异常退出时别留半截文件
        if reserved is not None and not r.ok:
            _drop(reserved)


def _drop(path: Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def _encode_two_pass(src: Path, dst: Path, info: VideoInfo, vf: str | None,
                     video_k: float, audio_args: list[str], tmp_dir: Path,
                     on_progress, cancel) -> tuple[bool, int, str]:
    """两遍编码：pass1 只分析不产出，pass2 才写文件。返回 (成功, 字节数, 错误)。"""
    log = str(tmp_dir / f"pass_{threading.get_ident()}")
    scale = ["-vf", vf] if vf else []
    # 两遍都要、且必须一致：否则帧数对不上，x264 会放弃码率控制（见 cfr_args 注释）
    fps = list(cfr_args())
    rate = max(1.0, video_k)
    common = ["-c:v", "libx264", "-preset", PRESET, "-pix_fmt", "yuv420p",
              "-b:v", f"{rate:.0f}k",
              # 不封峰值时，短片（几秒）会被 I 帧拉高到超目标 15%；VBV 封住上限
              "-maxrate", f"{rate * 1.05:.0f}k", "-bufsize", f"{rate * 2:.0f}k"]

    def report(base: float, span: float):
        if on_progress is None:
            return None
        return lambda frac: on_progress(base + span * frac)

    rc, err = run_ffmpeg(
        ["-y", "-i", str(src), *scale, *fps, *common, "-pass", "1",
         "-passlogfile", log, "-an", "-f", "null", "-"],
        info.duration, report(0.0, 0.45), cancel)
    if rc != 0:
        return False, 0, err

    rc, err = run_ffmpeg(
        ["-y", "-i", str(src), *scale, *fps, *common, "-pass", "2",
         "-passlogfile", log, *audio_args, "-map", "0:v:0", "-map", "0:a:0?",
         "-sn", "-dn", "-movflags", "+faststart", str(dst)],
        info.duration, report(0.45, 0.55), cancel)
    if rc != 0:
        return False, 0, err
    try:
        return True, dst.stat().st_size, ""
    except OSError as exc:
        return False, 0, str(exc)


def _finish(src: Path, r: ItemReport, t0: float) -> ItemReport:
    """收尾：复核原片没被动过，记耗时。这条证据每次都要留。"""
    try:
        after, _ = _hash_or_stat(src)
        r.src_unchanged = (after == r.src_sha256_before)
    except OSError:
        r.src_unchanged = None
    r.elapsed = time.perf_counter() - t0
    return r


# ---------------------------------------------------------------- 批量

def compress_batch(paths, target_mb: float, out_dir=None,
                   lossless_only: bool = False, on_item=None,
                   on_progress=None, cancel: threading.Event | None = None,
                   progress_cb=None, target_size: tuple | None = None,
                   keep_ratio: bool = True) -> BatchReport:
    """按顺序逐条压。视频编码吃满 CPU，并发只会让进度条失真、失败难定位。

    target_size=(宽, 高) 时按指定尺寸输出；None（默认）保持原尺寸、按目标体积
    自动决定降不降档。keep_ratio 只对 target_size 生效。
    """
    t0 = time.perf_counter()
    target_bytes = int(target_mb * 1024 * 1024)
    files = collect_videos(paths)
    report = BatchReport(target_kb=target_mb * 1024, lossless_only=lossless_only)
    report.target_size = tuple(target_size) if target_size else ()
    report.size_keep_ratio = bool(keep_ratio)

    highest = [0.0]

    def per_item(frac: float) -> None:
        if progress_cb is not None and files:
            value = (len(report.items) + frac) / len(files)
            # 重试或降档时这一条的 frac 会从头再来，进度条不能因此倒退
            highest[0] = max(highest[0], value)
            progress_cb(highest[0])

    for i, src in enumerate(files):
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
        dest = Path(out_dir) if out_dir else src.parent / OUT_DIRNAME
        item = compress_one(src, dest, target_bytes, lossless_only,
                            on_progress=per_item, cancel=cancel,
                            target_size=target_size, keep_ratio=keep_ratio)
        report.items.append(item)
        if item.ok and str(dest) not in report.out_dirs:
            report.out_dirs.append(str(dest))
        if on_item is not None:
            on_item(item)
        # 取消可能发生在最后一段身上：这时循环已经结束，靠「下一轮开头」是标不上的
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
    report.elapsed = time.perf_counter() - t0
    return report


def undo_outputs(report: BatchReport) -> int:
    """按报告删掉本次新生成的文件（只给自检与工程清理用，不是产品功能）。"""
    removed = 0
    for item in report.items:
        if not item.dst:
            continue
        try:
            Path(item.dst).unlink()
            removed += 1
        except OSError:
            pass
    return removed
