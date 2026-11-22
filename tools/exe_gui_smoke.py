"""外部冒烟：把「安装后的 exe」当黑盒点开，确认窗口真能起来、能正常关。

与 tools/ui_test.py 的分工：ui_test 直接 import main 建窗口，验的是代码逻辑；
这里不 import 任何项目代码，只启动 exe、用 Win32 找到它自己的顶层窗口、
截一张图、发 WM_CLOSE，验的是「打包 + 安装之后仍然是个能打开的窗口程序」。

顺带查两条打包才会出问题的东西：
  1. PE 子系统必须是 WINDOWS(2)，否则双击会先弹一个黑框；
  2. 进程起来后 3 秒内必须出现标题含「视频文件压缩」的可见窗口。

跑法：python tools/exe_gui_smoke.py "C:\\...\\视频文件压缩.exe"
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "_ui_shots"
SW_RESTORE = 9
WM_CLOSE = 0x0010

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""))
    return cond


def subsystem_of(exe: Path) -> int:
    """读 PE 头里的 Subsystem：2 = GUI，3 = 控制台。"""
    data = exe.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe:pe + 4] != b"PE\0\0":
        return -1
    opt = pe + 24  # COFF 头 20 字节 + 签名 4 字节
    magic = int.from_bytes(data[opt:opt + 2], "little")
    return int.from_bytes(data[opt + 68:opt + 70], "little") if magic == 0x20B else -1


def find_window(pid: int, timeout: float = 15.0):
    """在进程的顶层可见窗口里找标题带「视频文件压缩」的那个。"""
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        found.append((hwnd, buf.value))
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(enum_proc, 0)
        for hwnd, title in found:
            if "视频文件压缩" in title:
                return hwnd, title
        time.sleep(0.3)
    return None, ""


def grab(hwnd: int, out: Path) -> tuple[int, int]:
    """PrintWindow 抓窗口位图（PW_RENDERFULLCONTENT=2，能抓到 DWM 合成后的内容）。"""
    from PIL import Image

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)
    gdi32.SelectObject(mem, old)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h  # 负高度 = 自上而下，省得自己翻转
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return w, h


def main() -> int:
    exe = Path(sys.argv[1] if len(sys.argv) > 1 else
               ROOT / "dist" / "视频文件压缩" / "视频文件压缩.exe")
    print(f"目标：{exe}")
    if not check("exe 存在", exe.is_file()):
        return 1

    sub = subsystem_of(exe)
    check("PE 子系统是 GUI（不弹黑框）", sub == 2, f"subsystem={sub}")

    proc = subprocess.Popen([str(exe)])
    try:
        hwnd, title = find_window(proc.pid)
        if not check("窗口已出现", bool(hwnd), f"标题「{title}」" if hwnd else "15 秒内没找到窗口"):
            return 1
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(1.2)  # 让首帧画完再抓，否则截到半张白
        shot = SHOTS / "exe-安装后启动.png"
        w, h = grab(hwnd, shot)
        check("窗口尺寸合理", w > 600 and h > 400, f"{w}×{h}")
        check("截图已保存", shot.is_file() and shot.stat().st_size > 10000,
              f"{shot.stat().st_size // 1024}KB → {shot.relative_to(ROOT)}")

        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            code = proc.wait(timeout=10)
            check("窗口正常关闭并退出", True, f"退出码 {code}")
        except subprocess.TimeoutExpired:
            check("窗口正常关闭并退出", False, "10 秒内没退出")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    print(f"\n结论：{'全部通过' if _failed == 0 else f'{_failed} 项失败'}"
          f"（通过 {_passed}，失败 {_failed}）")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
