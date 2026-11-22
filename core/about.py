"""软件身份信息：关于页、使用说明、命令行 --version、打包版本资源共用这一处定义。"""
from __future__ import annotations

from version import VERSION as APP_VERSION

APP_NAME = "视频文件压缩"
APP_BUILD = "2026-11-10"
COMPANY = "深圳市创炫互动网络科技有限公司"

WEBSITE = "www.mrheedu.com"
HOMEPAGE = "https://www.mrheedu.com"

DISCLOSURE = (
    "视频压缩必然重新编码、必然有损；「原片不动」指原文件字节未被改写，"
    "不代表画质不变。逐条的码率与分辨率变化见结果表的「说明」列，"
    "双击任意一行可以看完整披露。"
)

USAGE_SHORT = [
    "把视频或文件夹拖进窗口，填目标体积（默认 25MB，够发微信）",
    "列表里选错的行，选中后点「删除选中」去掉；「清空列表」只清清单，不删任何文件",
    "点「开始压缩」，结果在原片旁边的 out 文件夹，压完自动打开；原片始终不动",
    "默认只压体积、画面尺寸一点不动；体积实在不够才自动降一档分辨率，"
    "降了会写在「说明」里，双击那一行能看完整详情",
    "要指定尺寸就取消「保持原尺寸」再填宽 × 高：默认等比缩到框内（不变形、不放大），"
    "先缩尺寸再压体积；压不到会如实说不达标，不会偷偷换成别的尺寸",
    "支持 mp4 / mov / mkv / avi / flv / wmv / webm 等常见格式；压到画质地板还超标就"
    "不出片，会告诉你最小能到多少",
]

VERSION_LINE = f"{APP_NAME} {APP_VERSION}（{APP_BUILD} 构建）· {COMPANY}"
VERSION_TEXT = f"{VERSION_LINE}\n更多免费软件：{WEBSITE}\n{DISCLOSURE}"
