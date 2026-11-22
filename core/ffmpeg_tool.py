"""ffmpeg / ffprobe 的定位、探测与调用。

只做三件事：找到运行时装在哪儿、问清源片是什么规格、跑一条命令并回报进度。
压缩策略（码率怎么算、降几档分辨率）不在这里，放 core/compressor.py。

路径查找顺序按「离程序最近」排：打包目录 → exe 同级 → 源码树 → PATH → 本机开发路径。
这样开发时不用配环境变量，打包后也不会去调用用户机器上可能不存在的 ffmpeg。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 打包成窗口程序后，每次调 ffmpeg 都不许闪黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FFmpegMissing(RuntimeError):
    """找不到 ffmpeg：消息要直接告诉用户下一步做什么，不是甩一个路径错误。"""


@dataclass
class VideoInfo:
    """源片规格。码率单位统一 kbps，时长统一秒，尺寸取「看到的方向」。"""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str
    v_bitrate: float
    a_bitrate: float
    fmt: str
    has_audio: bool
    rotated: bool = False

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def total_bitrate(self) -> float:
        return self.v_bitrate + self.a_bitrate


_TOOLS: tuple[Path, Path] | None = None


def _candidates() -> list[Path]:
    """按优先级列出可能的 ffmpeg 所在目录。"""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):  # PyInstaller 解包目录
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs += [base / "ffmpeg" / "bin", base / "ffmpeg", base]
        exe_dir = Path(sys.executable).parent
        dirs += [exe_dir / "ffmpeg" / "bin", exe_dir / "ffmpeg", exe_dir]
    here = Path(__file__).resolve().parent.parent
    dirs += [here / "ffmpeg" / "bin", here / "ffmpeg"]
    env = os.environ.get("VIDEO_COMPRESS_FFMPEG")
    if env:
        p = Path(env)
        dirs.insert(0, p.parent if p.is_file() else p)
    # 本机开发用：优先项目自带的 ffmpeg，无需外部绝对路径
    return dirs


def find_tools() -> tuple[Path, Path]:
    """返回 (ffmpeg, ffprobe) 绝对路径；找不到抛 FFmpegMissing。"""
    global _TOOLS
    if _TOOLS is not None:
        return _TOOLS
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    probe_exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for d in _candidates():
        f = d / exe
        if f.is_file():
            p = d / probe_exe
            if not p.is_file():
                p = Path(shutil.which("ffprobe") or "")
            if not p or not str(p) or not Path(p).is_file():
                continue
            _TOOLS = (f, Path(p))
            return _TOOLS
    for name in (exe, probe_exe):
        if shutil.which(name) is None:
            raise FFmpegMissing(
                "找不到 ffmpeg（压缩视频需要它）。请把 ffmpeg 文件夹放在程序旁边，"
                "或重新下载完整安装包——不要只拷贝 exe 一个文件。")
    _TOOLS = (Path(shutil.which(exe)), Path(shutil.which(probe_exe)))
    return _TOOLS


def _run_capture(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _parse_rate(text: str) -> float:
    """把 ffprobe 的 '30000/1001' 这类分数字符串转成浮点帧率。"""
    if not text or "/" not in text:
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0
    num, _, den = text.partition("/")
    try:
        n, d = float(num), float(den)
    except ValueError:
        return 0.0
    return n / d if d else 0.0


def _rotation(stream: dict) -> bool:
    """手机竖拍常存成横向 + 旋转标记，拍出来的画幅与像素尺寸不一致。"""
    for sd in stream.get("side_data_list") or []:
        try:
            if abs(float(sd.get("rotation", 0))) % 180 == 90:
                return True
        except (TypeError, ValueError):
            pass
    try:
        return abs(float((stream.get("tags") or {}).get("rotate", 0))) % 180 == 90
    except (TypeError, ValueError):
        return False


def probe(path: Path) -> VideoInfo:
    """读源片规格。读不出来就抛 ValueError，由调用方记成「跳过」并说明原因。"""
    _, ffprobe = find_tools()
    rc, out, err = _run_capture([
        str(ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path)])
    if rc != 0:
        raise ValueError(f"读不出视频信息（可能已损坏或不是视频）：{(err or '').strip()[:120]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ValueError(f"读取视频信息失败：{exc}") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams
                  if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
                 None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("这个文件里没有视频画面（可能是纯音频），本工具不处理")

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError("读不出视频时长，无法推算目标码率")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotated = _rotation(video)
    if rotated:
        width, height = height, width

    v_bitrate = 0.0
    try:
        v_bitrate = float(video.get("bit_rate") or 0) / 1000
    except (TypeError, ValueError):
        pass
    a_bitrate = 0.0
    if audio is not None:
        try:
            a_bitrate = float(audio.get("bit_rate") or 0) / 1000
        except (TypeError, ValueError):
            pass
    if v_bitrate <= 0:  # 容器没写流码率时用总码率减去音频，别当成 0 去算
        try:
            total = float(fmt.get("bit_rate") or 0) / 1000
        except (TypeError, ValueError):
            total = 0.0
        v_bitrate = max(0.0, total - a_bitrate)

    return VideoInfo(
        path=Path(path), duration=duration, width=width, height=height,
        fps=_parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or ""),
        vcodec=(video.get("codec_name") or "").lower(),
        acodec=((audio or {}).get("codec_name") or "").lower(),
        v_bitrate=v_bitrate, a_bitrate=a_bitrate,
        fmt=(fmt.get("format_name") or "").lower(),
        has_audio=audio is not None, rotated=rotated)


def run_ffmpeg(args: list[str], total_seconds: float = 0.0,
               on_progress=None, cancel: threading.Event | None = None) -> tuple[int, str]:
    """跑一条 ffmpeg 命令，边跑边报进度；返回 (退出码, 最后几行错误)。

    进度来自 `-progress pipe:1` 的 out_time_us：它比解析 "frame=..." 那种
    带 \\r 的输出可靠，也不会因为 loglevel 变化而失效。
    """
    ffmpeg, _ = find_tools()
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1"] + args
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW)
    tail: deque[str] = deque(maxlen=60)

    def drain() -> None:
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    tail.append(line)
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    killed = False
    try:
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set() and not killed:
                proc.terminate()
                killed = True
                continue
            raw = raw.strip()
            if not raw or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            if key in ("out_time_us", "out_time_ms") and total_seconds > 0:
                # 注意：ffmpeg 的 out_time_ms 实际也是微秒，历史命名遗留
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                if on_progress is not None:
                    on_progress(max(0.0, min(1.0, seconds / total_seconds)))
    finally:
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # 极端情况下别把界面卡死
            proc.kill()
            rc = proc.wait()
        reader.join(timeout=3)
    return rc, "\n".join(tail)


def describe_failure(rc: int, stderr: str) -> str:
    """把 ffmpeg 的英文报错翻成一句人话 + 下一步动作。"""
    text = (stderr or "").lower()
    if "no space left" in text:
        return "磁盘空间不够，删点东西或换个输出位置再试"
    if "permission denied" in text:
        return "没有写入权限，换个文件夹，或先把同名文件关掉"
    if "invalid data found" in text or "moov atom not found" in text:
        return "这个视频文件可能不完整或已损坏，换一个文件试试"
    if "unknown encoder" in text:
        return "当前 ffmpeg 缺少编码器（H.264），请用完整安装包覆盖安装"
    detail = (stderr or "").strip().splitlines()
    last = detail[-1][:160] if detail else f"ffmpeg 退出码 {rc}"
    return f"编码失败：{last}"


def version_text() -> str:
    """用于自检与「关于」——确认打包后用的到底是哪个 ffmpeg。"""
    ffmpeg, _ = find_tools()
    rc, out, _ = _run_capture([str(ffmpeg), "-version"])
    first = (out or "").splitlines()[0] if out else ""
    return first if rc == 0 else f"读不到版本信息（{ffmpeg}）"


@lru_cache(maxsize=1)
def cfr_args() -> tuple[str, ...]:
    """「按恒定帧率输出」的参数，两遍编码必须带上，且两遍必须一致。

    不带它的后果（2026-11-10 实测踩到）：mkv / 手机录像这类时间戳不规整的源，
    pass1 写 `-f null`、pass2 写 mp4，两遍产出的帧数会差几帧；x264 一旦发现
    「2nd pass has more frames than 1st pass」就放弃码率控制、退回固定 QP=41，
    结果体积比目标大一大截，或者干脆报 Video encoding failed。
    这句话就是让两遍输出帧数严格对齐，代价是可变帧率源被补成恒定帧率。

    ffmpeg 5 起把 -vsync 改名为 -fps_mode；打包用的 4.x 构建只认旧名字，
    所以按版本号挑，而不是写死一个。
    """
    rc, out, _ = _run_capture([str(find_tools()[0]), "-version"])
    first = (out or "").splitlines()[0] if out else ""
    m = re.search(r"ffmpeg version (\d+)\.", first)
    major = int(m.group(1)) if m else 0  # N-90553 这类 git 构建按老版本处理
    return ("-fps_mode", "cfr") if major >= 5 else ("-vsync", "cfr")
