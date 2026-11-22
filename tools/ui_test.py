"""界面自检：把「拖进来 → 列表 → 删除 → 压缩 → 取消 → 关窗口」整条链路跑一遍。

与 verify.py 分工：verify.py 验引擎（不吃界面），这里验界面（真的建 QMainWindow、
真的点按钮、真的跑 QThread），两者都跑才算验收完整。

跑法：python tools/ui_test.py            （约 1 分钟，会生成 _ui_samples/ 与截图）
      QT_QPA_PLATFORM=offscreen python tools/ui_test.py   # 无桌面时
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402
from core import compressor as C  # noqa: E402
from core.ffmpeg_tool import find_tools  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

SAMPLES = ROOT / "_ui_samples"
SHOTS = ROOT / "_ui_shots"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}" + (f"（{detail}）" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL  {name}" + (f"（{detail}）" if detail else ""))
    return cond


def build_samples() -> None:
    if SAMPLES.exists():
        for f in SAMPLES.rglob("*"):
            if f.is_file():
                f.unlink()
    SAMPLES.mkdir(parents=True, exist_ok=True)
    ffmpeg, _ = find_tools()

    def run(args):
        subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", *args], check=True)

    # 三条短视频：6 秒 1280×720，源片 2MB 上下——必须真的重编码才够得着 1MB，
    # 否则会走「原片已达标直接复制」，测不到有损披露那条路径
    for i, (freq, crf) in enumerate(((440, 18), (660, 19), (880, 20))):
        run(["-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=25:duration=6",
             "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=6",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-shortest", str(SAMPLES / f"clip{i}.mp4")])
    # 一条 1080p：用来测取消（编码要几秒，来得及掐）
    run(["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(SAMPLES / "long.mp4")])


class Harness:
    """把弹窗都拦下来：无桌面环境下弹窗会一直等，人不在就等于卡死。"""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.answer = QMessageBox.StandardButton.Yes
        self._orig = {}

    def __enter__(self):
        self._orig = {
            "information": QMessageBox.information,
            "warning": QMessageBox.warning,
            "critical": QMessageBox.critical,
            "question": QMessageBox.question,
            "openUrl": m.QDesktopServices.openUrl,
        }
        QMessageBox.information = staticmethod(
            lambda *a, **k: self.messages.append(("info", a[2] if len(a) > 2 else "")))
        QMessageBox.warning = staticmethod(
            lambda *a, **k: self.messages.append(("warn", a[2] if len(a) > 2 else "")))
        QMessageBox.critical = staticmethod(
            lambda *a, **k: self.messages.append(("crit", a[2] if len(a) > 2 else "")))
        QMessageBox.question = staticmethod(lambda *a, **k: self.answer)
        # 别真的去开资源管理器
        m.QDesktopServices.openUrl = staticmethod(lambda *a, **k: True)
        return self

    def __exit__(self, *exc):
        QMessageBox.information = self._orig["information"]
        QMessageBox.warning = self._orig["warning"]
        QMessageBox.critical = self._orig["critical"]
        QMessageBox.question = self._orig["question"]
        m.QDesktopServices.openUrl = self._orig["openUrl"]
        return False


def wait_done(w, timeout_ms: int = 180000) -> None:
    """等 Worker 结束；不靠固定 sleep，测试才稳定。

    已经跑完（线程不再运行）就直接返回——此时 done 可能早于本次 connect 发出，
    再等下去只能等到超时。
    """
    if w.worker is None or not w.worker.isRunning():
        return
    loop = QEventLoop()
    w.worker.done.connect(lambda *a: loop.quit())
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def shot(widget, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    widget.grab().save(str(SHOTS / name))


def main() -> int:
    print("== 准备 ==")
    build_samples()
    for p in sorted(SAMPLES.glob("*.mp4")):
        print(f"  {p.name}: {p.stat().st_size / 1024:.0f} KB")

    app = QApplication(sys.argv)
    m.configure_app(app)

    with Harness() as h:
        print("\n== 1. 启动状态 ==")
        w = m.MainWindow()
        w.show()
        app.processEvents()
        check("ffmpeg 可用", w.ffmpeg_ok, w.ffmpeg_msg or "找到内置运行时")
        check("窗口标题含产品名", m.APP_NAME in w.windowTitle(), w.windowTitle())
        check("开始按钮可用", w.start_btn.isEnabled())
        check("取消按钮初始置灰", not w.cancel_btn.isEnabled())
        check("删除 / 清空初始置灰",
              not w.delete_btn.isEnabled() and not w.clear_btn.isEnabled())
        check("默认目标 25MB", w.target.value() == 25, str(w.target.value()))
        check("表格 7 列且列名正确",
              [w.table.horizontalHeaderItem(c).text() for c in range(7)]
              == ["文件", "时长", "原体积", "输出体积", "分辨率", "结果", "说明"])
        shot(w, "01-空窗口.png")

        print("\n== 2. 拖入列表（不压缩就能确认选对没有）==")
        w.paths = [str(SAMPLES)]
        files = w.preview_selection()
        app.processEvents()
        check("扫到 4 个视频（含 1080p 那条）", len(files) == 4, f"{len(files)} 个")
        check("表格行数一致", w.table.rowCount() == 4, str(w.table.rowCount()))
        check("首列是文件名", w.table.item(0, 0).text().endswith(".mp4"),
              w.table.item(0, 0).text())
        check("原体积已显示", "KB" in w.table.item(0, 2).text()
              or "MB" in w.table.item(0, 2).text(), w.table.item(0, 2).text())
        check("结果列是「待压缩」", w.table.item(0, 5).text() == "待压缩")
        check("清空按钮变可用", w.clear_btn.isEnabled())
        check("状态栏说明已选数量", "已选 4 个视频" in w.net_label.text(),
              w.net_label.text())
        # 后台预读：时长与原始分辨率应在几百毫秒内回填到「待压缩」行
        for _ in range(50):
            if w.prober is None or not w.prober.isRunning():
                break
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()
        check("后台预读完并回填了时长", w.table.item(0, 1).text() != "—",
              w.table.item(0, 1).text())
        check("回填了原始分辨率", "×" in w.table.item(0, 4).text(),
              w.table.item(0, 4).text())
        shot(w, "02-加入列表.png")

        print("\n== 2b. 输出尺寸：保持原尺寸 / 指定尺寸 ==")
        check("默认「保持原尺寸」已勾选", w.keep_size.isChecked())
        check("保持原尺寸时宽高与比例都置灰",
              not w.w_spin.isEnabled() and not w.h_spin.isEnabled()
              and not w.ratio.isEnabled())
        check("保持原尺寸时 target_size 为 None", w.target_size() is None)
        check("提示写明「分辨率不变」", "分辨率不变" in w.size_hint.text(),
              w.size_hint.text())
        src_dims = w.table.item(0, 4).text()
        want = ([int(x) for x in src_dims.split("×")] if "×" in src_dims else [])
        w.keep_size.setChecked(False)
        app.processEvents()
        check("取消勾选后宽高可编辑",
              w.w_spin.isEnabled() and w.h_spin.isEnabled())
        check("自动填入第一条视频的尺寸",
              bool(want) and [w.w_spin.value(), w.h_spin.value()] == want,
              f"表格 {src_dims} → 输入框 {w.w_spin.value()}×{w.h_spin.value()}")
        check("提示改成等比缩到框内", "等比" in w.size_hint.text(), w.size_hint.text())
        w.w_spin.setValue(640)
        app.processEvents()
        want_h = max(16, round(640 / (want[0] / want[1]))) if want else 0
        check("改一带二：高按第一条视频的比例联动", w.h_spin.value() == want_h,
              f"宽 640 → 高 {w.h_spin.value()}（应为 {want_h}）")
        check("target_size 返回 (640, 高)", w.target_size() == (640, w.h_spin.value()),
              str(w.target_size()))
        w.ratio.setChecked(False)
        app.processEvents()
        check("取消约束比例：提示改成「会拉伸」并标红",
              "拉伸" in w.size_hint.text() and "#b3411c" in w.size_hint.styleSheet(),
              w.size_hint.text())
        w.ratio.setChecked(True)
        app.processEvents()
        shot(w, "02b-指定尺寸.png")
        w.lossless_only.setChecked(True)
        app.processEvents()
        check("勾仅无损：强制回「保持原尺寸」并锁住宽高",
              w.keep_size.isChecked() and not w.w_spin.isEnabled())
        check("勾仅无损后 target_size 为 None", w.target_size() is None)
        check("勾仅无损：提示写明不改尺寸", "仅无损不改尺寸" in w.size_hint.text(),
              w.size_hint.text())
        w.lossless_only.setChecked(False)
        app.processEvents()
        check("取消仅无损：恢复用户原来的尺寸选择",
              not w.keep_size.isChecked() and w.w_spin.isEnabled())
        check("取消仅无损后尺寸重新可用", w.target_size() is not None)
        w.keep_size.setChecked(True)  # 回到默认，后面的压缩仍按原尺寸
        app.processEvents()

        print("\n== 3. 场景预设 / 目标体积 ==")
        w.preset.setCurrentIndex(1)  # 微信 25MB
        check("选预设自动填数值", w.target.value() == 25, str(w.target.value()))
        w.target.setValue(1)
        w.preset.setCurrentIndex(0)
        check("选回占位项不改数值", w.target.value() == 1, str(w.target.value()))

        print("\n== 4. 删除选中 / 清空列表 ==")
        w.table.selectRow(0)
        app.processEvents()
        check("选中行后删除按钮可用", w.delete_btn.isEnabled())
        w.delete_selected()
        app.processEvents()
        check("删掉 1 行后剩 3 行", w.table.rowCount() == 3, str(w.table.rowCount()))
        check("待压清单同步为 3", len(w.preview_files) == 3, str(len(w.preview_files)))
        check("状态栏写明原片未改动", "原片未改动" in w.net_label.text(),
              w.net_label.text())
        w.paths = [str(SAMPLES)]
        again = w.preview_selection()
        check("重扫不会把删掉的那条拉回来", len(again) == 3,
              f"{len(again)} 个：{[p.name for p in again]}")
        h.answer = QMessageBox.StandardButton.No
        w.clear_list()
        check("清空时选「否」：列表不变", w.table.rowCount() == 3)
        h.answer = QMessageBox.StandardButton.Yes
        w.clear_list()
        app.processEvents()
        check("选「是」：行数与清单归零",
              w.table.rowCount() == 0 and not w.preview_files)
        check("清空后拖拽区回到初始提示", w.drop_label.text() == m.EMPTY_HINT,
              w.drop_label.text())
        check("磁盘上的样本仍在", len(list(SAMPLES.glob("*.mp4"))) == 4)

        print("\n== 5. 真压一遍（3 条 → 1MB）==")
        w.paths = [str(SAMPLES)]
        w.preview_selection()
        # 把 1080p 那条删掉，只压三条短视频，测试快
        for r in range(w.table.rowCount()):
            if w.table.item(r, 0).text() == "long.mp4":
                w.table.selectRow(r)
        w.delete_selected()
        w.target.setValue(1)
        # 顺手指定尺寸：这一轮同时验「指定尺寸」在真实界面链路里生效
        w.keep_size.setChecked(False)
        w.w_spin.setValue(640)
        app.processEvents()
        check("开压前已切到指定尺寸", w.target_size() == (640, 360),
              str(w.target_size()))
        t0 = time.perf_counter()
        w.start()
        check("开压后开始按钮置灰、取消可用",
              (not w.start_btn.isEnabled()) and w.cancel_btn.isEnabled())
        check("开压后目标体积与尺寸控件都置灰",
              not w.target.isEnabled() and not w.w_spin.isEnabled()
              and not w.keep_size.isEnabled())
        wait_done(w)
        app.processEvents()
        cost = time.perf_counter() - t0
        check("进度条到 100", w.progress.value() == 100, str(w.progress.value()))
        check("结束后按钮状态复原",
              w.start_btn.isEnabled() and not w.cancel_btn.isEnabled())
        check("目标体积可再编辑", w.target.isEnabled())
        check("尺寸控件在结束后恢复可用",
              w.keep_size.isEnabled() and w.w_spin.isEnabled(), "")
        rows = [[w.table.item(r, c).text() for c in range(7)]
                for r in range(w.table.rowCount())]
        for row in rows:
            print("   ", " | ".join(row))
        check("三条都有结果", len(rows) == 3)
        check("时长列填上了", all("秒" in row[1] for row in rows),
              "、".join(row[1] for row in rows))
        check("全部达标", all(row[5].startswith("达标") for row in rows),
              "、".join(row[5] for row in rows))
        rep = w.report
        check("输出体积不超过 1MB",
              bool(rep) and all(i.dst_bytes <= 1048576 for i in rep.items),
              "、".join(f"{i.dst_bytes / 1048576:.2f}MB" for i in (rep.items if rep else [])))
        check("说明列写了画质变化", all("码率" in row[6] for row in rows))
        check("全部按指定尺寸输出（短边 ≤ 360）",
              bool(rep) and all(min(i.size_after) <= 360 for i in rep.items if i.ok),
              "、".join(str(i.size_after) for i in (rep.items if rep else [])))
        check("没有一条被放大",
              bool(rep) and all(i.size_after[0] <= 1280 and i.size_after[1] <= 720
                                for i in rep.items if i.ok))
        check("报告记下目标尺寸 640×360 且是等比",
              bool(rep) and all(i.requested_size == (640, 360) and i.size_keep_ratio
                                for i in rep.items))
        check("分辨率列显示「原 → 新」", all("→" in row[4] for row in rows),
              "、".join(row[4] for row in rows))
        check("状态栏汇总含达标数", "达标 3 / 3" in w.net_label.text(),
              w.net_label.text())
        check("结果目录已记录", bool(w.result_dirs()), str(w.result_dirs()))
        out_files = sorted((SAMPLES / "out").glob("*.mp4"))
        check("out 里有 3 个结果", len(out_files) == 3,
              "、".join(p.name for p in out_files))
        check("压缩耗时合理（< 90s）", cost < 90, f"{cost:.1f}s")
        shot(w, "03-压缩结果.png")

        print("\n== 6. 详情页（有损披露字段）==")
        item = w.table.item(0, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        check("行里存了结果对象", item is not None)
        dlg = m.DetailDialog(item, w)
        rows_detail = item.disclosure()
        keys = [k for k, _ in rows_detail]
        for need in ("是否重编码", "分辨率", "尺寸设定", "视频码率", "音频", "帧率",
                     "编码器", "体积", "损失说明", "原片是否改动", "耗时"):
            check(f"披露字段「{need}」", need in keys)
        vals = dict(rows_detail)
        check("披露写明重编码过", "是" in vals["是否重编码"], vals["是否重编码"])
        check("披露给出码率前后", "→" in vals["视频码率"], vals["视频码率"])
        check("披露给出分辨率", "×" in vals["分辨率"], vals["分辨率"])
        check("披露写明这次指定的尺寸", "640×360" in vals["尺寸设定"], vals["尺寸设定"])
        check("披露写明原片未改动", "未改动" in vals["原片是否改动"],
              vals["原片是否改动"])
        dlg.show()
        app.processEvents()
        shot(dlg, "05-详情.png")
        dlg.close()
        # 双击表格行 → 打开详情：用假对话框验信号确实连到了 show_detail
        seen = {}
        orig_dlg = m.DetailDialog

        class FakeDlg:
            def __init__(self, it, parent=None):
                seen["item"] = it

            def exec(self):
                seen["shown"] = True

        m.DetailDialog = FakeDlg
        try:
            w.table.cellDoubleClicked.emit(0, 0)
        finally:
            m.DetailDialog = orig_dlg
        check("双击表格行打开详情", seen.get("shown") is True
              and seen.get("item") is not None)

        print("\n== 7. 取消：掐掉正在编码的那条 ==")
        # 第 4 步把长片删进 excluded 了；重新选一个新文件等于新一批，排除要清掉
        w.paths = [str(SAMPLES / "long.mp4")]
        w.excluded.clear()
        w.preview_selection()
        w.target.setValue(1)
        t0 = time.perf_counter()
        w.start()
        QTimer.singleShot(1500, w.cancel)
        wait_done(w)
        app.processEvents()
        check("取消在 30 秒内生效", time.perf_counter() - t0 < 30,
              f"{time.perf_counter() - t0:.1f}s")
        check("状态栏写明已取消", "已取消" in w.net_label.text(), w.net_label.text())
        rep = w.report
        check("报告标记 cancelled", rep is not None and rep.cancelled)
        if rep and rep.items:
            check("取消那条不出片", not rep.items[0].ok and not rep.items[0].dst,
                  rep.items[0].note)
            check("说明写「已取消」", "取消" in rep.items[0].note, rep.items[0].note)
        ghosts = [p for p in (SAMPLES / "out").glob("long*")]
        if ghosts:
            check("没留下半截文件", all(p.stat().st_size > 0 for p in ghosts),
                  f"{len(ghosts)} 个残留")
        else:
            check("没留下半截文件", True, "无残留")

        print("\n== 8. 压缩中关窗口 ==")
        w.target.setValue(1)
        w.start()
        check("重新开压", w.worker is not None)
        h.answer = QMessageBox.StandardButton.No
        w.close()
        check("选「否」时窗口不关", w.isVisible())
        h.answer = QMessageBox.StandardButton.Yes
        w.close()
        check("选「是」时能正常退出（QThread 已收尾）",
              not w.isVisible() and (w.worker is None or not w.worker.isRunning()))

        print("\n== 9. 找不到 ffmpeg 时的界面 ==")
        orig = m.find_tools
        m.find_tools = lambda: (_ for _ in ()).throw(
            m.FFmpegMissing("找不到 ffmpeg（压缩视频需要它）。测试用提示。"))
        try:
            w2 = m.MainWindow()
            check("开始按钮置灰", not w2.start_btn.isEnabled())
            check("拖拽区写清原因", "找不到 ffmpeg" in w2.drop_label.text(),
                  w2.drop_label.text())
            check("状态栏也提示缺 ffmpeg", "缺少 ffmpeg" in w2.net_label.text())
            w2.close()
        finally:
            m.find_tools = orig

        print("\n== 10. 使用说明 / 关于 ==")
        w3 = m.MainWindow()
        u = m.UsageDialog(w3)
        a = m.AboutDialog(w3)
        u.show()
        a.show()
        app.processEvents()
        shot(u, "06-使用说明.png")
        shot(a, "07-关于.png")
        u.close()
        a.close()
        w3.close()

    print("\n" + "=" * 62)
    print(f"结论：{'全部通过' if _failed == 0 else '有失败项'} —— 共 {_passed + _failed} 项，"
          f"通过 {_passed}，失败 {_failed}")
    print(f"截图：{SHOTS}")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
