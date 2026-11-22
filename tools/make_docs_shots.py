"""生成文档用截图 docs/screenshots/*.png（必须用真实平台跑，别加 offscreen）。

offscreen 平台没有中文字体，抓出来全是方块，所以这里不设 QT_QPA_PLATFORM。
样本视频现场用 ffmpeg 的 lavfi 合成，不读用户文件、不联网：
四条不同来源的片子（横屏 1080p / 4K / 手机竖拍 / 已经很小的），外加一个 mkv 容器，
跑一遍真实压缩后截图，保证截图里的数字都是程序当时真算出来的。

跑法：python tools/make_docs_shots.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402
from core.ffmpeg_tool import find_tools  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def build_samples(base: Path) -> Path:
    d = base / "示例视频"
    d.mkdir(parents=True, exist_ok=True)
    ffmpeg, _ = find_tools()

    def run(args):
        subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", *args], check=True)

    def make(name, size, secs, fps, crf, audio=True, freq=440):
        args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={secs}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                 "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        run(args + [str(d / name)])

    # 真实工作里最常见的一批：横屏课录、4K 宣传片、手机竖拍、压过一遍的小视频
    make("课堂录像-第03讲.mp4", "1920x1080", 40, 30, 20, freq=330)
    make("产品宣传片-4K.mp4", "3840x2160", 10, 30, 18, freq=520)
    make("手机竖拍-生日.mp4", "720x1280", 12, 30, 20, freq=660)
    make("微信小视频.mp4", "480x854", 8, 25, 24, freq=880)
    # mkv 容器：用来看「统一输出 mp4」与容器开销
    make("会议录屏.mkv", "1280x720", 20, 30, 22, freq=440)
    return d


def shot(widget, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(OUT / name))


def wait(w, ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="docs_vshots_"))
    print("生成样本…")
    samples = build_samples(base)
    for p in sorted(samples.iterdir()):
        print(f"  {p.name}: {p.stat().st_size / 1048576:.1f} MB")

    app = QApplication(sys.argv)
    m.configure_app(app)
    # 弹窗一律吞掉：截图时不需要它们挡住窗口
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    m.QDesktopServices.openUrl = staticmethod(lambda *a, **k: True)

    print("渲染窗口…")
    empty = m.MainWindow()
    empty.show()
    wait(empty, 500)
    shot(empty, "00-空白窗口.png")
    empty.close()

    w = m.MainWindow()
    w.resize(1180, 760)
    w.show()
    w.paths = [str(samples)]
    w.preview_selection()
    w.drop_label.setText(f"已选文件夹：{samples}")
    w.target.setValue(25)
    # 等后台把时长 / 分辨率读完，截图里才不是一排「—」
    for _ in range(40):
        if w.prober is None or not w.prober.isRunning():
            break
        wait(w, 100)
    wait(w, 200)
    shot(w, "01-加入列表.png")

    # 指定尺寸：勾掉「保持原尺寸」，填 1280×720，截一张说明「尺寸可以点着选」
    w.keep_size.setChecked(False)
    w.ratio.setChecked(True)
    w.w_spin.setValue(1280)
    wait(w, 250)
    shot(w, "08-指定尺寸.png")
    w.keep_size.setChecked(True)  # 回到默认，后面的压缩仍按原尺寸跑
    wait(w, 150)

    # 压缩中：趁 worker 还在跑抓一张
    during = {"ok": False}

    def snap_running():
        # 等到至少 20% 再抓：0% 那张只能看到一排「待压缩」，
        # 说明不了「压缩中」是什么样子
        if w.worker is not None and w.worker.isRunning() and w.progress.value() >= 20:
            shot(w, "02-压缩中.png")
            during["ok"] = True
        elif w.worker is not None and w.worker.isRunning():
            QTimer.singleShot(300, snap_running)

    t0 = time.perf_counter()
    w.start()
    QTimer.singleShot(400, snap_running)
    loop = QEventLoop()
    w.worker.done.connect(lambda *a: loop.quit())
    QTimer.singleShot(600000, loop.quit)
    loop.exec()
    print(f"压缩完成，耗时 {time.perf_counter() - t0:.0f}s；压缩中截图：{during['ok']}")
    wait(w, 400)
    for r in range(w.table.rowCount()):
        print("   ", " | ".join(w.table.item(r, c).text()[:40] for c in (0, 1, 3, 5, 6)))
    shot(w, "03-压缩结果.png")

    # 多选两行，展示「删除选中」按钮亮起
    w.table.selectRow(0)
    sm = w.table.selectionModel()
    sm.select(w.table.model().index(2, 0),
              sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    wait(w, 200)
    shot(w, "04-删除选中.png")

    # 详情页：取第一条有损的
    for r in range(w.table.rowCount()):
        it = w.table.item(r, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        if it is not None and it.ok and not it.lossless:
            dlg = m.DetailDialog(it, w)
            dlg.show()
            wait(dlg, 400)
            shot(dlg, "05-详情.png")
            dlg.close()
            break

    u = m.UsageDialog(w)
    u.show()
    wait(u, 400)
    shot(u, "06-使用说明.png")
    u.close()

    a = m.AboutDialog(w)
    a.show()
    wait(a, 400)
    shot(a, "07-关于.png")
    a.close()
    w.close()

    print("截图已写入：", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
