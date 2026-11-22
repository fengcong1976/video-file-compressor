"""PySide6 桌面界面（需 pip install PySide6；CLI 与自检不依赖它）。

界面把规范的六条标配做成看得见的东西：拖进来 → 填数字 → 开始；逐条报告；
结果落在原片旁的 out 并压完自动打开；状态栏常驻 0 联网自检。

视频与图片的差别在「时间」：一条 1080p 一分钟片子要编十来秒，所以进度条
必须给已用时间与预计剩余，取消必须能真的掐掉正在跑的 ffmpeg，而不是等它跑完。

界面上不出现「码率 / CRF / GOP / 编码器」这类词：用户只需要说清「不超过几 MB」，
怎么分配码率、要不要降分辨率是程序的事，降了会在「说明」里说明白。
原片始终不动、结果只写在独立的 out 文件夹里，所以不设「撤销」按钮。

「关于」只放标准身份信息（应用名 / 版本 / 公司全称 / 网址）；
用法、有损披露与可自己核对的几件事单独放「使用说明」，两个入口不混在一起。
"""
from __future__ import annotations

import html
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSpinBox, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.about import (
    APP_NAME, APP_VERSION, COMPANY, DISCLOSURE, HOMEPAGE, USAGE_SHORT, WEBSITE,
)
from core.compliance import NetworkAudit
from core.compressor import DEFAULT_TARGET_MB, collect_videos, compress_batch
from core.ffmpeg_tool import FFmpegMissing, find_tools, probe, version_text


ICON_PATH = Path(__file__).resolve().parent / "resources" / "app.ico"
APP_USER_MODEL_ID = "Chuangxuan.VideoFileCompress"
# 三处入口（选文件框 / 拖拽 / 状态栏）用同一句口径，免得互相打架。
SUPPORT_TEXT = "mp4 / mov / mkv / avi / flv / wmv / webm / m4v / ts / 3gp"
FILE_FILTER = ("视频 (*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.webm *.m4v *.ts "
               "*.mpg *.mpeg *.3gp)")
PREVIEW_LIMIT = 300
EMPTY_HINT = "把视频或文件夹拖到这里，或点下面的按钮选择"
# 实测编码速度约 0.3–0.4 倍实时，估时取中值；估得不准也只是个参考，不参与决策
SPEED_FACTOR = 0.35


def app_icon() -> QIcon:
    """图标文件缺失时返回空图标，不让界面启动失败。"""
    return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()


def set_windows_app_id() -> None:
    """让 Windows 任务栏显示本软件图标，而不是 python.exe 的图标。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def configure_app(app: QApplication) -> None:
    """默认可读性：入口统一设定，界面里不提供字号开关（规范 v1.3 第 5 条）。

    字放大后按钮高度、输入框高度、进度条高度、表格行高、间距同步跟上，
    否则文字会挤在原来的格子里，比不改更糟。
    """
    base_font = app.font()
    base_font.setPointSize(11)  # Windows 默认是 9pt
    app.setFont(base_font)
    app.setStyleSheet(
        "QPushButton { min-height: 34px; padding: 4px 12px; }"
        "QComboBox, QSpinBox { min-height: 32px; padding: 2px 8px; }"
        "QProgressBar { min-height: 24px; }"
        "QCheckBox { padding: 2px 4px; }"
    )


def human(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def dur_text(seconds: float) -> str:
    """时长：不到一分钟写秒，超过写「X 分 Y 秒」，一眼知道片子多长。"""
    if not seconds:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    return f"{s // 60} 分 {s % 60} 秒"


class AboutDialog(QDialog):
    """「关于」只放身份信息，与站内其它项目的标准对话框一致。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"关于 {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(560, 300)

        root = QVBoxLayout(self)
        row = QHBoxLayout()
        pic = QLabel()
        pix = app_icon().pixmap(72, 72)
        if not pix.isNull():
            pic.setPixmap(pix)
        row.addWidget(pic)

        # 字段顺序与站内其它产品一致：应用名 → 版本 → 公司全称 → 更多免费软件
        info = QLabel(
            f"<h2>{html.escape(APP_NAME)}</h2>"
            f"<p>版本：{html.escape(APP_VERSION)}</p>"
            f"<p>公司：{html.escape(COMPANY)}</p>"
            f"<p>更多免费软件："
            f"<a href='{HOMEPAGE}'>{html.escape(WEBSITE)}</a></p>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        row.addWidget(info, 1)
        root.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self.accept)
        root.addWidget(btns)


class UsageDialog(QDialog):
    """使用说明：几步用法 + 有损提示 + 一句可核对的事实，一屏看完。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"使用说明 · {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(620, 420)

        layout = QVBoxLayout(self)
        steps = "".join(f"<li>{html.escape(x)}</li>" for x in USAGE_SHORT)
        try:
            ff = html.escape(version_text())
        except FFmpegMissing as exc:
            ff = html.escape(str(exc))
        text = QLabel(
            f"<h3>怎么用</h3><ol>{steps}</ol>"
            f"<p><font color='#b3411c'>{html.escape(DISCLOSURE)}</font></p>"
            f"<p style='color:#666'>编码速度约 0.3–0.4 倍实时：一分钟的片子要十几秒，"
            f"长视频请让它跑着。全程本地处理，不联网、不上传。</p>"
            f"<p style='color:#888;font-size:9pt'>内置编码器：{ff}</p>")
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        layout.addWidget(text, 1)

        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class DetailDialog(QDialog):
    """单条结果的详情：立项卡要求的有损披露字段一条不落。

    结果表的列宽有限，「说明」只能放下半句；真要核对「码率从多少降到多少、
    音频有没有重编码、原片有没有被动过」时，双击那一行看这张表。
    """

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"详情 · {Path(item.src).name}")
        self.setWindowIcon(app_icon())
        self.resize(700, 470)
        layout = QVBoxLayout(self)

        head = QLabel(
            f"<b>{html.escape(Path(item.src).name)}</b><br>"
            f"<span style='color:#666'>{html.escape(str(item.src))}</span>")
        head.setWordWrap(True)
        layout.addWidget(head)

        rows = item.disclosure()
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["项目", "实际情况"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(True)
        for r, (k, v) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(k))
            cell = QTableWidgetItem(v)
            cell.setToolTip(v)  # 长说明悬停看全文
            table.setItem(r, 1, cell)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 损失说明会换行，行高必须跟着文字走，否则第二行会被切掉
        table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setMinimumSectionSize(80)
        layout.addWidget(table, 1)

        row = QHBoxLayout()
        if item.dst and Path(item.dst).is_file():
            btn = QPushButton("打开结果所在文件夹")
            btn.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(item.dst).parent))))
            row.addWidget(btn)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class ProbeWorker(QThread):
    """在后台把列表里每条视频的时长与分辨率读出来，逐条回填。

    为什么不在 preview_selection 里同步读：每条要起一次 ffprobe，几百条会把
    界面冻住好几秒。为什么还要读：只看到「39.1 MB」不知道是哪条片子，
    有了时长和分辨率才敢确认没选错——这正是「选完先列清单」的意义。
    """

    probed = Signal(str, float, object)  # 路径、时长、原始尺寸

    def __init__(self, files):
        super().__init__()
        self.files = list(files)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self):
        for p in self.files:
            if self._stop.is_set():
                return
            try:
                info = probe(Path(p))
            except Exception:  # 读不出来就留「—」，压缩时会给出具体原因
                continue
            if self._stop.is_set():
                return
            self.probed.emit(str(p), info.duration, (info.width, info.height))


class Worker(QThread):
    item_done = Signal(object)     # 一条处理完就发一条，表格随进度补齐
    progressed = Signal(float)     # 0..1，整批进度
    # 不叫 finished：那是 QThread 自带的信号，同名不同签名会互相盖住。
    done = Signal(object, str)     # (BatchReport 或 None, 状态摘要)

    def __init__(self, files, target_mb, lossless_only=False,
                 target_size=None, keep_ratio=True):
        super().__init__()
        self.files = files
        self.target_mb = target_mb
        self.lossless_only = lossless_only
        self.target_size = target_size   # (宽, 高) 或 None = 保持原尺寸
        self.keep_ratio = keep_ratio
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """请求取消：正在跑的 ffmpeg 会被掐掉，已压好的保留。"""
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self):
        # 这里漏出去的异常会让 PyQt 直接 abort 整个进程（窗口瞬间消失，
        # 没有弹窗也没有日志），所以整段兜住，失败也要给界面一个交代。
        try:
            with NetworkAudit() as audit:
                report = compress_batch(
                    self.files, self.target_mb, out_dir=None,
                    lossless_only=self.lossless_only,
                    target_size=self.target_size, keep_ratio=self.keep_ratio,
                    on_item=lambda it: self.item_done.emit(it),
                    progress_cb=lambda f: self.progressed.emit(f),
                    cancel=self._cancel)
                report.network_attempts = audit.attempts
                summary = audit.summary()
        except BaseException as exc:  # noqa: BLE001 — 兜底就是为了不让进程消失
            self.done.emit(
                None,
                f"压缩中断：{type(exc).__name__}: {exc}。"
                "已经压好的视频仍在各自的 out 文件夹里，原片未改动。")
            return
        self.done.emit(report, summary)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · 创炫互动")
        self.setWindowIcon(app_icon())
        self.resize(1120, 720)
        self.setAcceptDrops(True)
        self.paths: list[str] = []
        self.report = None
        self.worker = None
        self.row_index: dict[str, int] = {}  # 视频路径 → 表格行号，压缩时原地更新
        self.preview_files: list = []
        # 用户在列表里删掉的行：只影响这批压哪些，原片一个字节都不动。
        self.excluded: set[str] = set()
        self._running = False
        self._t0 = 0.0
        self._last_pct = -1
        self.prober = None  # 后台预读时长 / 分辨率，换一批就停掉上一批
        # 尺寸联动：以清单里第一条视频的宽高比为准；用户手动改过宽高就不再自动填
        self._aspect = 16 / 9
        self._first_dims = None
        self._size_touched = False
        self._filling = False

        # 找不到 ffmpeg 时界面照常打开，但开始按钮置灰并把原因写在拖拽区，
        # 不用等用户点了才弹一个「找不到 ffmpeg」的框。
        try:
            find_tools()
            self.ffmpeg_ok, self.ffmpeg_msg = True, ""
        except FFmpegMissing as exc:
            self.ffmpeg_ok, self.ffmpeg_msg = False, str(exc)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        self.drop_label = QLabel(EMPTY_HINT if self.ffmpeg_ok else self.ffmpeg_msg)
        self.drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_label.setWordWrap(True)
        self.drop_label.setStyleSheet(
            "padding: 28px; border: 1px dashed #888; border-radius: 8px;"
            + ("" if self.ffmpeg_ok else "color: #b3411c;"))
        layout.addWidget(self.drop_label)

        row = QHBoxLayout()
        btn_files = QPushButton("选择视频")
        btn_files.setToolTip(
            f"选一个或多个视频（{SUPPORT_TEXT}）。\n"
            "结果统一输出成 mp4，微信、邮件、各平台都能直接放。")
        btn_files.clicked.connect(self.pick_files)
        btn_dir = QPushButton("选择文件夹")
        btn_dir.setToolTip("选一整个文件夹，里面的视频会连子文件夹一起找出来")
        btn_dir.clicked.connect(self.pick_dir)
        row.addWidget(btn_files)
        row.addWidget(btn_dir)
        row.addWidget(QLabel("目标体积"))
        self.target = QSpinBox()
        self.target.setRange(1, 102400)
        self.target.setValue(DEFAULT_TARGET_MB)
        self.target.setSuffix(" MB")
        self.target.setToolTip(
            "填对方页面写的那个数字：微信发视频约 25MB，邮件附件常 50MB，"
            "平台投稿 100–500MB。程序会保证结果不超过它。")
        row.addWidget(self.target)
        self.lossless_only = QCheckBox("仅无损")
        self.lossless_only.setToolTip(
            "只换容器（如 mkv / ts → mp4），画面重新编码 0 次，体积几乎不会变小。\n"
            "压不到目标就明确告诉你「只能省容器开销」，不会偷偷把画面压糊。")
        self.lossless_only.toggled.connect(self.on_lossless_toggled)
        row.addWidget(self.lossless_only)
        row.addStretch()
        self.start_btn = QPushButton("开始压缩")
        self.start_btn.setEnabled(self.ffmpeg_ok)
        self.start_btn.clicked.connect(self.start)
        row.addWidget(self.start_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip("掐掉正在编码的这一条，不再处理剩下的；已完成的保留")
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        # 场景预设单独一行：下拉本身就是「填目标体积」的快捷方式
        preset_row = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem("常用场景（自动填目标体积）", None)
        self.preset.addItem("微信直接发视频（约 25MB）", 25)
        self.preset.addItem("邮件附件（约 50MB）", 50)
        self.preset.addItem("平台投稿（约 100MB）", 100)
        self.preset.addItem("高清留档（约 500MB）", 500)
        self.preset.setToolTip(
            "不同平台限制不同，这里只是常用起点；请以对方页面提示的数字为准。"
            "选好后仍可在上面的目标体积里改。")
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self._fit_combo_width(self.preset)  # 宽度按最长条目实测，别让末尾被切
        preset_row.addWidget(self.preset)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        # 输出尺寸：默认保持原尺寸；取消勾选后按填的宽高先缩放、再压体积。
        size_row = QHBoxLayout()
        self.keep_size = QCheckBox("保持原尺寸")
        self.keep_size.setChecked(True)
        self.keep_size.setToolTip(
            "默认只压体积，分辨率一点不动；万一这个尺寸下压不到，才按 720 / 480 逐档降。\n"
            "取消勾选后可填目标宽高：先把画面缩到这个尺寸，再压到目标体积。")
        self.keep_size.toggled.connect(self.on_keep_size_toggled)
        size_row.addWidget(self.keep_size)
        self.ratio = QCheckBox("约束比例")
        self.ratio.setChecked(True)
        self.ratio.setToolTip(
            "勾选：等比缩到「宽 × 高」这个框内，长边贴边、短边等比让位，不变形、不放大。\n"
            "取消：按你填的宽高精确输出，比例会变（画面拉伸），表单强制尺寸时才这么用。")
        self.ratio.toggled.connect(self.on_ratio_toggled)
        size_row.addWidget(self.ratio)
        self.w_label = QLabel("宽")
        size_row.addWidget(self.w_label)
        self.w_spin = QSpinBox()
        self.w_spin.setRange(16, 7680)
        self.w_spin.setSingleStep(2)
        self.w_spin.setValue(1920)
        self.w_spin.setSuffix(" px")
        self.w_spin.setMaximumWidth(132)  # 一行放下复选框 + 两个宽高框 + 提示
        self.w_spin.valueChanged.connect(self._on_width_changed)
        size_row.addWidget(self.w_spin)
        size_row.addWidget(QLabel("×"))
        self.h_label = QLabel("高")
        size_row.addWidget(self.h_label)
        self.h_spin = QSpinBox()
        self.h_spin.setRange(16, 7680)
        self.h_spin.setSingleStep(2)
        self.h_spin.setValue(1080)
        self.h_spin.setSuffix(" px")
        self.h_spin.setMaximumWidth(132)
        self.h_spin.valueChanged.connect(self._on_height_changed)
        size_row.addWidget(self.h_spin)
        self.size_hint = QLabel()
        size_row.addWidget(self.size_hint)
        size_row.addStretch()
        self.on_keep_size_toggled(True)  # 初始按「保持原尺寸」设置可编辑与提示
        layout.addLayout(size_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # 列表工具行：删行 / 清空只动「这批压哪些」，不碰磁盘上的文件。
        list_row = QHBoxLayout()
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.setEnabled(False)
        self.delete_btn.setToolTip(
            "把选中的行移出待压缩列表（原片不动，随时可以重新选回来）。\n"
            "按住 Ctrl 点选多行，或按住 Shift 选一段；也可以直接按 Delete 键。")
        self.delete_btn.clicked.connect(lambda: self.delete_selected())
        list_row.addWidget(self.delete_btn)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.setEnabled(False)
        self.clear_btn.setToolTip("清空整个待压缩列表，不会删除、不会改动任何视频文件")
        self.clear_btn.clicked.connect(self.clear_list)
        list_row.addWidget(self.clear_btn)
        list_row.addStretch()
        layout.addLayout(list_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["文件", "时长", "原体积", "输出体积", "分辨率", "结果", "说明"])
        # 整行选中才好认「要删哪几条」；默认是按单元格选，选出来是虚线框。
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        # 单元格只是结果展示，双击进去改字没有意义；双击改成看详情。
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 选中行必须一眼可见：Windows 默认的「失焦高亮」几乎就是白底。
        self.table.setStyleSheet(
            "QTableWidget::item:selected "
            "{ background-color: #0b64c4; color: #ffffff; }")
        self.table.setWordWrap(True)
        # 空表时 ResizeToContents 没有内容可依，列会挤成一堆；给个下限，
        # 表头在还没加片子的状态也保持可读的间距。
        self.table.horizontalHeader().setMinimumSectionSize(80)
        hh = self.table.horizontalHeader()
        for c in range(6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._sync_table_fonts()
        self.table.itemSelectionChanged.connect(self._refresh_list_buttons)
        self.table.cellDoubleClicked.connect(self.show_detail)
        # Delete 键等价于点「删除选中」：手不离键盘也能清掉选错的那几行。
        self.del_shortcut = QShortcut(QKeySequence("Delete"), self.table)
        self.del_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.del_shortcut.activated.connect(lambda: self.delete_selected(hint=False))
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        self.net_label = QLabel("本次尚未运行 · 全程本地处理" if self.ffmpeg_ok
                                else "缺少 ffmpeg：无法压缩")
        bottom.addWidget(self.net_label)
        bottom.addStretch()
        btn_open = QPushButton("打开结果文件夹")
        btn_open.setToolTip("结果就在每个视频旁边的 out 文件夹里；压完会自动打开一次")
        btn_open.clicked.connect(lambda: self.open_result_folder())
        bottom.addWidget(btn_open)
        btn_usage = QPushButton("使用说明")
        btn_usage.setToolTip("怎么用、结果在哪、有损说明、可自己核对的几件事")
        btn_usage.clicked.connect(self.show_usage)
        bottom.addWidget(btn_usage)
        btn_about = QPushButton("关于")
        btn_about.setToolTip("版本 / 公司全称 / 官网网址")
        btn_about.clicked.connect(self.show_about)
        bottom.addWidget(btn_about)
        layout.addLayout(bottom)

    # ------------------------------------------------------------ 字体与宽度
    def _fit_combo_width(self, combo: QComboBox) -> None:
        """下拉框最小宽度按最长条目的实测文字宽算，不交给 Qt 的 sizeHint。

        样式表给 QComboBox 加了左右各 8px 内边距，Qt 的 sizeHint 不把这份
        内边距算进文字区，长条目末尾会被切掉，连下拉箭头一起挤没。
        """
        fm = combo.fontMetrics()
        text_w = max(
            (fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())),
            default=0)
        arrow = combo.style().pixelMetric(
            QStyle.PixelMetric.PM_MenuButtonIndicator, None, combo)
        combo.setMinimumWidth(text_w + arrow + 34)  # 34 = 左右内边距 + 余量

    def _sync_table_fonts(self) -> None:
        """表格正文与两个表头统一到全局字号（11pt）。

        QTableWidget 的正文和表头默认会退回样式里的 9pt：正文比界面其余部分
        小一号，行号列宽又按这个小字号算，换到高 DPI 屏上两位数字就会被切。
        """
        font = QApplication.font()
        for w in (self.table, self.table.horizontalHeader(),
                  self.table.verticalHeader()):
            w.setFont(font)
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + 14)
        self._fit_row_number_width()

    def _fit_row_number_width(self) -> None:
        """行号列宽按实际字号和位数算，避免两位数被切掉一半。"""
        vh = self.table.verticalHeader()
        digits = max(2, len(str(max(1, self.table.rowCount()))))
        fm = vh.fontMetrics()
        vh.setFixedWidth(fm.horizontalAdvance("0") * digits + 16)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_table_fonts()  # 样式刷新会把表头字体打回 9pt，显示后补一次
        self._fit_combo_width(self.preset)  # 样式刷新后字号可能变，宽度要跟着重算

    # ------------------------------------------------------------ 选片
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        self.excluded.clear()  # 换了新一批视频，上一批删掉的行不再挡路
        files = self.preview_selection()
        self.drop_label.setText(f"已加入 {len(self.paths)} 个路径 · 找到 {len(files)} 个视频")

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频", "", FILE_FILTER)
        if files:
            self.paths = files
            self.excluded.clear()
            self.drop_label.setText(f"已选 {len(files)} 个视频 · 点「开始压缩」即可")
            self.preview_selection()

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.paths = [d]
            self.excluded.clear()
            self.drop_label.setText(f"已选文件夹：{d}")
            self.preview_selection()

    def apply_preset(self, index):
        """场景预设：用户不必记住数字，选场景即自动填。"""
        mb = self.preset.itemData(index)
        if mb:
            self.target.setValue(mb)

    # ------------------------------------------------------------ 输出尺寸
    def on_keep_size_toggled(self, checked: bool):
        """勾选时宽高与「约束比例」一并置灰，取消勾选才允许输入目标尺寸。

        仅无损时整行都不该动：-c copy 没法缩放，允许填就是个陷阱。
        """
        editable = not checked and not self.lossless_only.isChecked()
        for w in (self.ratio, self.w_label, self.w_spin, self.h_label, self.h_spin):
            w.setEnabled(editable)
        self._refresh_size_hint()
        if not checked and not self._size_touched:
            # 一取消勾选就把第一条视频的尺寸填进去，用户在此基础上改数字即可
            self._fill_size_from(self._first_dims)

    def on_ratio_toggled(self, checked: bool):
        """勾上「约束比例」时立刻按当前宽把高校回原比例，避免留着扭曲的数字。"""
        self._refresh_size_hint()
        if checked and not self.keep_size.isChecked() and not self._filling:
            self._on_width_changed(self.w_spin.value())

    def _on_width_changed(self, value):
        """改宽：锁了比例就按第一条视频的比例带出高；没锁就只记住用户改过。"""
        if self._filling:
            return
        self._size_touched = True
        if self.ratio.isChecked() and self._aspect > 0:
            self._filling = True
            try:
                self.h_spin.setValue(
                    max(16, min(round(value / self._aspect), self.h_spin.maximum())))
            finally:
                self._filling = False

    def _on_height_changed(self, value):
        """改高：同理，锁了比例就反算宽。"""
        if self._filling:
            return
        self._size_touched = True
        if self.ratio.isChecked() and self._aspect > 0:
            self._filling = True
            try:
                self.w_spin.setValue(
                    max(16, min(round(value * self._aspect), self.w_spin.maximum())))
            finally:
                self._filling = False

    def _fill_size_from(self, dims) -> None:
        if not dims:
            return
        w, h = int(dims[0]), int(dims[1])
        if w <= 0 or h <= 0:
            return
        self._aspect = w / h  # 约束比例以这条视频为基准
        self._filling = True
        try:
            self.w_spin.setValue(max(16, min(w, self.w_spin.maximum())))
            self.h_spin.setValue(max(16, min(h, self.h_spin.maximum())))
        finally:
            self._filling = False

    def _refresh_size_hint(self) -> None:
        if self.lossless_only.isChecked():
            text, color = "仅无损不改尺寸", "#666"
        elif self.keep_size.isChecked():
            text, color = "只压体积，分辨率不变", "#666"
        elif self.ratio.isChecked():
            text, color = "等比缩到框内，不变形、不放大", "#666"
        else:
            text, color = "精确到该宽高，画面会拉伸", "#b3411c"
        self.size_hint.setText(text)
        self.size_hint.setStyleSheet(f"color: {color};")
        # 行内控件多，QLabel 容易被挤成省略号：按文字实际宽度给个下限
        self.size_hint.setMinimumWidth(self.size_hint.sizeHint().width())
        self.size_hint.setToolTip(
            "「约束比例」勾选：宽高按第一条视频的比例联动，等比缩到框内，画面不变形、不放大。\n"
            "「约束比例」取消：按你填的宽高精确输出，比例会被改变，画面会拉伸。\n"
            "两种情况都是先改尺寸，再压到目标体积；压不到会如实说不达标，"
            "不会偷偷换成别的尺寸。")

    def on_lossless_toggled(self, checked: bool):
        """仅无损只换容器（-c copy），改不了尺寸：勾上就把尺寸行整体锁掉。"""
        if checked:
            self._size_off_before_lossless = not self.keep_size.isChecked()
            self.keep_size.setChecked(True)
        elif getattr(self, "_size_off_before_lossless", False):
            self.keep_size.setChecked(False)  # 取消仅无损，恢复用户原来的选择
            self._size_off_before_lossless = False
        self.keep_size.setEnabled(not checked and not self._running)
        self.on_keep_size_toggled(self.keep_size.isChecked())

    def target_size(self):
        """要改尺寸就返回 (宽, 高)；保持原尺寸或仅无损时返回 None。"""
        if self.keep_size.isChecked() or self.lossless_only.isChecked():
            return None
        return (self.w_spin.value(), self.h_spin.value())

    # ------------------------------------------------------------ 清单
    @staticmethod
    def _key(path) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def preview_selection(self) -> list:
        """选中 / 拖入后先把视频列进表格：文件名、原体积、待压缩。

        只读文件大小（stat），不探测时长（每条要起一次 ffprobe，几百条会卡）；
        时长与分辨率在压完后随结果一起补进表里。
        返回完整文件列表，start 直接复用，不再重扫目录。
        """
        files = [p for p in collect_videos(self.paths)
                 if self._key(p) not in self.excluded]
        self.preview_files = files
        self.row_index = {}
        self._first_dims = None  # 换了批次，尺寸基准跟着换
        shown = files[:PREVIEW_LIMIT]
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(shown))
            for r, p in enumerate(shown):
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                values = [p.name, "—", human(size) if size else "—",
                          "—", "—", "待压缩", ""]
                for c, v in enumerate(values):
                    cell = QTableWidgetItem(str(v))
                    if c == 0:
                        cell.setToolTip(str(p))  # 不同文件夹的重名文件，悬停看完整路径
                        cell.setData(Qt.ItemDataRole.UserRole, str(p))
                    self.table.setItem(r, c, cell)
                self.row_index[self._key(p)] = r
        finally:
            self.table.setUpdatesEnabled(True)
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._start_prober(files)
        if files:
            n = f"已选 {len(files)} 个视频 ｜ 还没开始 · 全程本地处理"
            if len(files) > PREVIEW_LIMIT:
                n += f" ｜ 列表只显示前 {PREVIEW_LIMIT} 个，其余照压"
            self.net_label.setText(n)
        elif self.ffmpeg_ok:
            self.net_label.setText(f"没有找到视频 ｜ 支持 {SUPPORT_TEXT}")
        return files

    def _start_prober(self, files) -> None:
        """换一批就停掉上一批的预读，避免旧结果填到新列表上。"""
        self._stop_prober()
        if not files or not self.ffmpeg_ok:
            return
        self.prober = ProbeWorker(files[:PREVIEW_LIMIT])
        self.prober.probed.connect(self.on_probed)
        self.prober.start()

    def _stop_prober(self) -> None:
        if self.prober is None:
            return
        self.prober.stop()
        if self.prober.isRunning():
            self.prober.wait(3000)
        self.prober.deleteLater()
        self.prober = None

    def on_probed(self, path: str, duration: float, dims) -> None:
        """时长与分辨率回填到「待压缩」的行上；已经压出结果的行不再覆盖。"""
        if self._running:
            return
        r = self.row_index.get(self._key(path))
        if r is None or r >= self.table.rowCount():
            return
        result_cell = self.table.item(r, 5)
        if result_cell is None or result_cell.text() != "待压缩":
            return
        self.table.item(r, 1).setText(dur_text(duration))
        dim_cell = self.table.item(r, 4)
        dim_cell.setText(f"{dims[0]}×{dims[1]}")
        # 记下来源尺寸：重跑时分辨率列要能退回这一档，而不是留着「原 → 新」
        dim_cell.setData(Qt.ItemDataRole.UserRole, f"{dims[0]}×{dims[1]}")
        # 第一条的尺寸当「约束比例」的基准，也是取消勾选「保持原尺寸」时的默认值
        if self._first_dims is None and dims[0] > 0 and dims[1] > 0:
            self._first_dims = (dims[0], dims[1])
            self._aspect = dims[0] / dims[1]
            if not self.keep_size.isChecked() and not self._size_touched:
                self._fill_size_from(dims)

    def _row_path(self, row: int):
        """行 → 文件路径：从单元格里取，删过行也不会错位。"""
        cell = self.table.item(row, 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None

    def _rebuild_row_index(self) -> None:
        self.row_index = {}
        for r in range(self.table.rowCount()):
            p = self._row_path(r)
            if p:
                self.row_index[self._key(p)] = r

    def _refresh_list_buttons(self) -> None:
        """删除 / 清空只在「有行、且没在压」时可用，免得压到一半表被抽走。"""
        has_row = self.table.rowCount() > 0
        has_pick = bool(self.table.selectedIndexes())
        self.delete_btn.setEnabled(has_pick and not self._running)
        self.clear_btn.setEnabled(has_row and not self._running)

    def _after_list_changed(self, n: int, verb: str) -> None:
        """删行 / 清空后同步拖拽区、状态栏与进度，不说一句「原片未改动」不放心。"""
        left = len(self.preview_files)
        if left:
            self.drop_label.setText(f"待压缩 {left} 个视频 · 点「开始压缩」即可")
            self.net_label.setText(f"{verb} {n} 个 ｜ 还剩 {left} 个待压缩 ｜ 原片未改动")
        else:
            self.drop_label.setText(EMPTY_HINT)
            self.net_label.setText(f"{verb} {n} 个 ｜ 列表已空 ｜ 原片未改动")
        self.progress.setValue(0)
        self.progress.setFormat("%p%")

    def delete_selected(self, hint: bool = True) -> None:
        """把选中行移出待压缩列表：只是这批不压它们，原片不删不改。

        删掉的路径记进 excluded，之后重新扫描目录也不会再被拉回列表——
        否则「删了又冒出来」比不能删更让人恼火。
        """
        if self._running:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            if hint:
                QMessageBox.information(
                    self, "先选中行",
                    "在列表里点一下要删的那行再点「删除选中」。\n\n"
                    "按住 Ctrl 点击可多选，按住 Shift 可选一段，"
                    "选中后直接按 Delete 键也行。")
            return
        removed = 0
        self.table.setUpdatesEnabled(False)
        try:
            for r in rows:
                p = self._row_path(r)
                if p:
                    self.excluded.add(self._key(p))
                    removed += 1
                self.table.removeRow(r)
        finally:
            self.table.setUpdatesEnabled(True)
        self.preview_files = [p for p in self.preview_files
                              if self._key(p) not in self.excluded]
        self._rebuild_row_index()
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._after_list_changed(removed, "已移除")

    def clear_list(self) -> None:
        """清空整个待压缩列表：不删文件、不动文件，只是这批不压了。"""
        if self._running:
            return
        rows = self.table.rowCount()
        if not rows and not self.paths:
            return
        if rows:
            ret = QMessageBox.question(
                self, "清空列表",
                f"把列表里这 {rows} 个视频都清掉吗？\n\n"
                "只是不再压它们：原片不会被删除、不会被改动，"
                "之后重新选择即可。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.paths = []
        self.excluded.clear()
        self.preview_files = []
        self.row_index = {}
        self.table.setRowCount(0)
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._after_list_changed(rows, "已清空")

    # ------------------------------------------------------------ 压缩
    def cancel(self):
        """掐掉正在编码的这一条：已完成的保留，界面上如实说明。"""
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.net_label.setText("正在停止：掐掉当前这条，已完成的会保留…")

    def _set_running(self, running: bool) -> None:
        if running:
            self._stop_prober()  # 编码要紧，预读先让位
        self._running = running
        self.start_btn.setEnabled(not running and self.ffmpeg_ok)
        self.cancel_btn.setEnabled(running)
        for w in (self.target, self.lossless_only, self.preset):
            w.setEnabled(not running)
        if not running:
            # 跑完把尺寸行的可用状态按「仅无损 / 保持原尺寸」重新算一遍
            self.on_lossless_toggled(self.lossless_only.isChecked())
        else:
            for w in (self.keep_size, self.ratio, self.w_label, self.w_spin,
                      self.h_label, self.h_spin):
                w.setEnabled(False)
        self._refresh_list_buttons()

    def _reset_rows(self, files) -> bool:
        """开压前把已列出的行清回「待压缩」，避免上一批的结果留在表里。"""
        shown = files[:PREVIEW_LIMIT]
        if self.table.rowCount() != len(shown):
            return False
        self.table.setUpdatesEnabled(False)
        try:
            for r in range(len(shown)):
                for c, v in ((3, "—"), (5, "待压缩"), (6, "")):
                    self.table.setItem(r, c, QTableWidgetItem(v))
                # 时长与分辨率是源片属性，不必清；分辨率若显示成「原 → 新」，
                # 退回只显示原分辨率（值存在 UserRole 里）
                cell = self.table.item(r, 4)
                if cell is not None:
                    src_dims = cell.data(Qt.ItemDataRole.UserRole)
                    if src_dims:
                        cell.setText(str(src_dims))
        finally:
            self.table.setUpdatesEnabled(True)
        return True

    def start(self):
        if self._running:
            return
        if not self.ffmpeg_ok:
            QMessageBox.critical(self, "缺少 ffmpeg", self.ffmpeg_msg)
            return
        if not self.paths:
            QMessageBox.information(self, "提示", "请先拖入视频，或选择视频 / 文件夹")
            return
        # 复用上次预览的结果，不再重复扫描同一批目录
        files = self.preview_files
        if not files or not self._reset_rows(files):
            files = self.preview_selection()
        self.report = None
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        if not files:
            if self.excluded:
                QMessageBox.information(
                    self, "列表是空的",
                    "列表里的视频都被删掉了。\n\n"
                    "重新选择视频，或点「清空列表」从头来一遍即可。")
            else:
                QMessageBox.information(
                    self, "没有找到视频",
                    f"选中的位置里没有可处理的视频（{SUPPORT_TEXT}）。")
            return
        size = self.target_size()
        if self.lossless_only.isChecked():
            mode = "仅无损（只换容器）"
        elif size:
            mode = (f"重新编码，缩到 {size[0]}×{size[1]}"
                    + ("（等比，不变形）" if self.ratio.isChecked() else "（精确拉伸）"))
        else:
            mode = "重新编码，保持原尺寸"
        self._set_running(True)
        self._t0 = time.perf_counter()
        self._last_pct = -1
        self.net_label.setText(
            f"正在处理 {len(files)} 个视频（目标 ≤ {self.target.value()}MB，{mode}）…")
        # 传的是「过滤后的文件清单」而不是原始路径：用户删掉的行不该再被压。
        self.worker = Worker(files, float(self.target.value()),
                             self.lossless_only.isChecked(),
                             target_size=size, keep_ratio=self.ratio.isChecked())
        self.worker.item_done.connect(self.add_row)
        self.worker.progressed.connect(self.on_progress)
        self.worker.done.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, frac: float):
        """进度 + 已用时间 + 预计剩余。估时按 0.35 倍实时，只做参考。"""
        pct = max(0, min(100, int(frac * 100)))
        elapsed = time.perf_counter() - self._t0
        if frac <= 0.01:
            text = f"{pct}% · 已用 {elapsed:.0f}s"
        else:
            eta = elapsed * (1 - frac) / frac
            text = f"{pct}% · 已用 {elapsed:.0f}s · 预计还需 {eta:.0f}s"
        self.progress.setFormat(text)
        if pct != self._last_pct:
            self._last_pct = pct
            self.progress.setValue(pct)

    def show_detail(self, row: int, _col: int = 0):
        """双击一行看完整披露：结果表列宽有限，只放得下半句说明。"""
        cell = self.table.item(row, 0)
        item = cell.data(Qt.ItemDataRole.UserRole + 1) if cell is not None else None
        if item is None:
            return
        DetailDialog(item, self).exec()

    @staticmethod
    def _dims_text(item) -> str:
        """分辨率列：降过档显示「原 → 新」，没降只显示原分辨率。"""
        if not item.size_before:
            return "—"
        before = f"{item.size_before[0]}×{item.size_before[1]}"
        if item.resized and item.size_after:
            return f"{before} → {item.size_after[0]}×{item.size_after[1]}"
        return before

    @staticmethod
    def _short_note(item) -> str:
        """说明列用短句：一行放不下就换行，别让整张表被一段话撑成五六行高。

        完整的损失说明（含「重新编码」这类整句）放在悬停提示和双击详情里，
        表格里只留「码率多少降到多少、分辨率有没有降、音频动没动」这三件。
        """
        if not item.ok or item.skipped:
            return item.note or "—"
        if item.lossless:
            return item.note or "未重新编码"
        bits = []
        if item.dst_v_bitrate:
            bits.append(f"码率 {item.src_v_bitrate:.0f} → {item.dst_v_bitrate:.0f} kbps")
        if item.resized and item.size_after:
            bits.append(f"分辨率 {item.size_before[0]}×{item.size_before[1]} → "
                        f"{item.size_after[0]}×{item.size_after[1]}"
                        + ("" if item.size_keep_ratio else "（拉伸）"))
        else:
            bits.append("分辨率未变")
        if item.audio_copied and item.src_acodec != "无音轨":
            bits.append("音频原样复制")
        text = "；".join(bits)
        return f"{text}；{item.note}" if item.note else text

    def add_row(self, item):
        key = self._key(item.src)
        r = self.row_index.get(key)
        if r is None:  # 不在预览列表里（超出预览上限等）才追加
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.row_index[key] = r
            self._fit_row_number_width()
        if item.skipped:
            flag = "跳过"
        elif item.ok:
            flag = "达标·无损" if item.lossless else "达标·有损"
        else:
            flag = "未达标"
        note = self._short_note(item)
        detail = "\n".join(x for x in (item.quality_loss, item.note) if x)
        values = [
            Path(item.src).name,
            dur_text(item.duration),
            human(item.src_bytes) if item.src_bytes else "—",
            human(item.dst_bytes) if item.dst_bytes else "—",
            self._dims_text(item),
            flag,
            note,
        ]
        for c, v in enumerate(values):
            cell = QTableWidgetItem(str(v))
            if c == 0:
                cell.setData(Qt.ItemDataRole.UserRole, str(item.src))
                cell.setData(Qt.ItemDataRole.UserRole + 1, item)  # 详情页用
                cell.setToolTip(f"{item.src}\n双击这一行看完整详情")
            if c == 4 and item.size_before:
                # 供下一次重跑时把「原 → 新」退回原分辨率
                cell.setData(Qt.ItemDataRole.UserRole,
                             f"{item.size_before[0]}×{item.size_before[1]}")
            if c == 6:
                cell.setToolTip(detail or str(v))
            self.table.setItem(r, c, cell)
        self.table.resizeRowToContents(r)

    def on_finished(self, report, summary):
        self._set_running(False)
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.wait(1000)   # run() 已走到末尾，这里只是收尾
            worker.deleteLater()  # 每批一个 QThread，不回收会一直堆着
        if report is None:  # Worker 兜底捕获的异常：必须让用户看见原因
            self.progress.setFormat("中断")
            self.net_label.setText(summary)
            QMessageBox.critical(self, "压缩中断", summary)
            return
        self.report = report
        self.progress.setValue(100 if not report.cancelled else self.progress.value())
        self.progress.setFormat("完成 %p%" if not report.cancelled else "已取消")
        parts = [summary]
        if report.cancelled:
            parts.insert(0, f"已取消（完成 {len(report.items)} 个，其余未处理）")
        parts.append(f"达标 {len(report.succeeded)} / {len(report.items)}")
        if report.skipped:
            parts.append(f"跳过 {len(report.skipped)} 个（原因见说明列）")
        resized = sum(1 for i in report.items if i.resized)
        if resized:
            parts.append(f"改分辨率 {resized} 个")
        if report.failed:
            parts.append("未达标项可调高目标体积，或把尺寸改小、把视频剪短再压")
        if not report.out_dirs:
            parts.append("本次没有生成结果文件")
        self.net_label.setText(" ｜ ".join(parts))
        self.net_label.setToolTip(self.net_label.text())
        if report.out_dirs:
            self.open_result_folder(auto=True)

    # ------------------------------------------------------------ 打开结果
    def result_dirs(self) -> list[Path]:
        if not self.report:
            return []
        return [Path(d) for d in self.report.out_dirs if Path(d).is_dir()]

    def open_result_folder(self, auto: bool = False) -> None:
        """打开结果文件夹：压完自动开一次，也可随时点底部按钮再开。

        多个文件夹混合压缩时只开结果最多的那个，其余仍在各自原片旁边，
        不一口气弹一排资源管理器窗口。
        """
        dirs = self.result_dirs()
        if not dirs:
            if not auto:
                QMessageBox.information(self, "提示", "还没有生成结果；先压缩一次吧")
            return
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(dirs[0])))
        if not ok and not auto:
            QMessageBox.information(self, "打不开结果文件夹", f"请手动打开：{dirs[0]}")
        extra = f"（另有 {len(dirs) - 1} 个结果文件夹）" if len(dirs) > 1 else ""
        if auto:
            self.net_label.setText(self.net_label.text() + f" ｜ 结果已打开{extra}")
            self.net_label.setToolTip(self.net_label.text())

    def show_about(self):
        AboutDialog(self).exec()

    def show_usage(self):
        UsageDialog(self).exec()

    def closeEvent(self, event):
        """压缩中关窗口：先问一句，并让后台线程收尾，避免 QThread 被销毁崩掉。"""
        if self.worker is not None and self.worker.isRunning():
            ret = QMessageBox.question(
                self, "还在压缩",
                "还有视频正在编码。停止并退出吗？\n\n"
                "已经压好的结果会保留在原片旁的 out 文件夹里，原片不受影响；"
                "正在编码的这一条会被掐掉，不会留下半截文件。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            if not self.worker.wait(8000):
                QMessageBox.warning(
                    self, "请稍候",
                    "后台还在收尾，请几秒后再关闭窗口，以免留下半截文件。")
                event.ignore()
                return
        self._stop_prober()
        event.accept()


def run_selftest(out: Path | None = None) -> int:
    """无界面自检：确认「这一份 exe 里」ffmpeg 能用、能压到目标体积。

    为什么打包后还要单独跑一遍：开发机上永远能找到 PATH 里的 ffmpeg，
    exe 却只认自己旁边那份。这条自检现场合成一段 1080p 素材，用包内
    ffmpeg 走完探测 → 两遍编码 → 复称体积，输出写日志文件（窗口程序没有
    控制台，stdout 看不到），安装包验收时直接看这个文件就行。

    用法：视频文件压缩.exe --selftest [日志路径]
    """
    import hashlib
    import tempfile

    from core.compressor import compress_one

    lines: list[str] = []
    failed = 0

    def log(msg: str) -> None:
        lines.append(msg)
        try:  # 源码方式跑的时候顺便打到控制台；打包后这行只是无用的 no-op
            print(msg, flush=True)
        except (OSError, ValueError):
            pass

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if not cond:
            failed += 1
        log(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""))

    log(f"{APP_NAME} {APP_VERSION} 打包自检")
    log(f"  可执行文件：{sys.executable}")
    log(f"  frozen     ：{getattr(sys, 'frozen', False)}")
    log(f"  解包目录   ：{getattr(sys, '_MEIPASS', '（无，源码运行）')}")

    tmp = Path(tempfile.mkdtemp(prefix="vc-selftest-"))
    try:
        try:
            ffmpeg, ffprobe = find_tools()
            log(f"  ffmpeg     ：{ffmpeg}")
            log(f"  ffprobe    ：{ffprobe}")
            check("随包 ffmpeg 已定位", True)
            check("ffprobe 与 ffmpeg 同目录", ffmpeg.parent == ffprobe.parent)
            log(f"  版本       ：{version_text()}")
        except FFmpegMissing as exc:
            check("随包 ffmpeg 已定位", False, str(exc))
            return _write_selftest_log(lines, out, failed)

        # 现场合成素材：8 秒 1080p + 音轨，体积刻意做大，好让压缩真起作用
        sample = tmp / "sample.mp4"
        from core.ffmpeg_tool import run_ffmpeg
        rc, err = run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", "9000k",
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-y", str(sample)])
        check("包内 ffmpeg 能编码（libx264 可用）", rc == 0 and sample.is_file(),
              f"{sample.stat().st_size / 1048576:.2f}MB" if sample.is_file() else err[-120:])
        if rc != 0 or not sample.is_file():
            return _write_selftest_log(lines, out, failed)

        info = probe(sample)
        check("包内 ffprobe 能读出规格", info.duration > 7 and info.width == 1920,
              f"{info.width}×{info.height} / {info.duration:.1f}s")

        target_bytes = 2 * 1024 * 1024
        before = hashlib.sha256(sample.read_bytes()).hexdigest()
        with NetworkAudit() as audit:  # 打包后的 exe 也照样不许外连
            r = compress_one(sample, tmp / "out", target_bytes)
        check("压缩过程 0 次联网", audit.attempts == 0, audit.summary())
        check("压缩成功出片", bool(r.dst) and Path(r.dst).is_file(), r.note or "")
        if r.dst and Path(r.dst).is_file():
            size = Path(r.dst).stat().st_size
            check("输出不超过目标体积", size <= target_bytes,
                  f"{size / 1048576:.2f}MB / 目标 2.00MB")
            check("输出落在独立 out 目录", Path(r.dst).parent.name == "out")
            out_info = probe(Path(r.dst))
            check("输出可被正常播放解析", out_info.duration > 0 and out_info.vcodec == "h264",
                  f"{out_info.width}×{out_info.height} / {out_info.vcodec}")
            check("降分辨率不超过一档以上", out_info.short_side >= 480,
                  f"短边 {out_info.short_side}")
        check("原片字节未被改写",
              hashlib.sha256(sample.read_bytes()).hexdigest() == before)
    except Exception as exc:  # 自检本身不该崩在异常上，崩了也要留下日志
        check("自检过程未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return _write_selftest_log(lines, out, failed)


def _write_selftest_log(lines: list[str], out: Path | None, failed: int) -> int:
    import tempfile
    log_path = out or (Path(tempfile.gettempdir()) / "视频文件压缩-自检日志.txt")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    head = "结论：全部通过" if failed == 0 else f"结论：{failed} 项失败"
    text = "\n".join(lines + ["", "=" * 40, head, ""])
    log_path.write_text(text, encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 打包验收用：不建窗口、不弹任何对话框，结果写日志文件后退出
        idx = sys.argv.index("--selftest")
        target = Path(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else None
        sys.exit(run_selftest(target))

    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(COMPANY)
    app.setWindowIcon(app_icon())

    configure_app(app)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())
