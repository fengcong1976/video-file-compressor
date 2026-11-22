"""用真实安装向导走一遍，验证完成页不再出现「查阅 README 文档」勾选项。

不静默：静默安装看不到完成页，这个要求就没法验证。
步骤：启动安装包 → 一路回车（默认按钮是「下一步」）→ 到完成页 → 枚举控件文字并截图
     → 关掉安装程序 → 打印结论。

跑法：python tools/installer_ui_check.py "dist\\安装包\\视频文件压缩-1.1.0-安装包.exe"
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32
ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / "_ui_shots"
SHOT.mkdir(exist_ok=True)

VK_RETURN = 0x0D
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
BM_CLICK = 0x00F5


def top_windows(title_part: str) -> list[int]:
    """找标题里带关键字的顶层窗口（Inno 向导的类是 TWizardForm）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def children_text(hwnd: int, visible_only: bool = True) -> list[tuple[str, str]]:
    """列出子控件的 (类名, 文字)。只取可见的：向导用隐藏控件占位，
    不筛掉的话「下一步」在完成页也还在列表里，就永远判断不出完成。"""
    out: list[tuple[str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if visible_only and not user32.IsWindowVisible(h):
            return True
        cn = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cn, 64)
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        text = buf.value.strip()
        if text:
            out.append((cn.value, text))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def find_button(hwnd: int, keywords: tuple[str, ...]) -> int:
    hit = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if not user32.IsWindowVisible(h):
            return True
        cn = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cn, 64)
        if cn.value != "TNewButton":
            return True
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if any(k in buf.value for k in keywords):
            hit.append(h)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return hit[0] if hit else 0


def rect_of(hwnd: int):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/installer_ui_check.py <安装包路径>")
        return 2
    setup = Path(sys.argv[1]).resolve()
    if not setup.is_file():
        print(f"找不到安装包：{setup}")
        return 2

    print(f"启动安装向导：{setup.name}")
    proc = subprocess.Popen([str(setup)])
    time.sleep(2.0)

    hwnd = 0
    for _ in range(40):
        wins = top_windows("视频文件压缩")
        if wins:
            hwnd = wins[0]
            break
        time.sleep(0.25)
    if not hwnd:
        print("FAIL 没找到安装向导窗口")
        return 1
    print(f"窗口句柄 {hwnd}，标题 ok")

    done_hwnd = 0
    shots = 0
    for step in range(90):          # 90 × 2s ≈ 3 分钟，够装完 116MB
        if not user32.IsWindow(hwnd):
            print("FAIL 安装向导提前消失")
            return 1
        texts = [t for _, t in children_text(hwnd)]
        done_btn = find_button(hwnd, ("完成",))
        # 向导的前进按钮在不同页叫「下一步」或「安装」
        next_btn = find_button(hwnd, ("下一步", "安装(&I)", "安装"))
        if done_btn:
            done_hwnd = hwnd
            break
        if shots < 3:
            ImageGrab.grab(bbox=rect_of(hwnd)).save(
                SHOT / f"installer-01-第{shots + 1}页.png")
            shots += 1
        if next_btn:
            user32.SendMessageW(next_btn, BM_CLICK, 0, 0)
        time.sleep(2.0)

    if not done_hwnd:
        print("FAIL 没走到完成页（可能卡在某一页）")
        return 1

    time.sleep(0.8)
    texts = children_text(done_hwnd)
    print("\n完成页控件：")
    for cn, t in texts:
        print(f"  [{cn}] {t}")
    ImageGrab.grab(bbox=rect_of(done_hwnd)).save(SHOT / "installer-02-完成页.png")

    blob = " ".join(t for _, t in texts).lower()
    bad = [k for k in ("readme", "查阅", "自述") if k in blob]
    print(f"\n完成后截图：{SHOT / 'installer-02-完成页.png'}")
    if bad:
        print(f"FAIL 完成页仍出现 readme 相关项：{bad}")
        ret = 1
    else:
        print("PASS 完成页没有「查阅 README」勾选项")
        ret = 0

    # 关掉安装程序：点「完成」；[Run] 里默认勾了「立即运行」，会把程序带起来
    btn = find_button(done_hwnd, ("完成",))
    if btn:
        user32.SendMessageW(btn, BM_CLICK, 0, 0)
    time.sleep(4.0)
    if top_windows("视频文件压缩"):
        # 安装向导或刚启动的程序还开着，测试到此为止，收掉不留后台进程
        subprocess.run(["taskkill", "/IM", "视频文件压缩.exe", "/F"],
                       capture_output=True)
        try:
            proc.terminate()
        except OSError:
            pass
    return ret


if __name__ == "__main__":
    sys.exit(main())
