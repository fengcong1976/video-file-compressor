# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（onedir）。

用法：pyinstaller --noconfirm --clean "build.spec"（或直接双击 build_exe.bat）
产物：dist\\视频文件压缩\\视频文件压缩.exe（整个文件夹拷走即可免安装运行）

与「图片文件压缩」那份 spec 的差别只有一条，但这条很关键：
**ffmpeg 运行时按数据文件整份拷进去（_internal\\ffmpeg\\bin）**，不走 PyInstaller
的依赖分析。ffmpeg.exe 依赖十几个同目录的 DLL，交给依赖分析去猜容易漏；
按 datas 原样搬运，路径与开发时一致（core/ffmpeg_tool.py 查 base/ffmpeg/bin）。

刻意做的三件事：
1. 只打界面真用到的库。numpy / yaml / psutil / requests 是环境里装着的第三方包
   被钩子顺手牵进来的，本项目一行都没 import，排除后省几十 MB。
2. 去掉 Qt 自带翻译与 opengl32sw.dll：界面文案全是自写中文、没装 QTranslator；
   QWidget 走光栅渲染，软件 OpenGL 兜底用不上。
3. 不启用 UPX：压缩版 exe 容易被杀软误报，宁可体积大一点。
"""

import re
from pathlib import Path

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('resources', 'resources'),       # 图标：程序内 app_icon() 按相对路径找
        ('ffmpeg/bin', 'ffmpeg/bin'),     # ffmpeg.exe / ffprobe.exe + 依赖 DLL
        ('ffmpeg/LICENSE.txt', 'ffmpeg'), # 分发 ffmpeg 要带许可证
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest', 'pdb',
        'numpy', 'yaml', 'psutil', 'charset_normalizer',
        'requests', 'urllib3', 'setuptools', 'pkg_resources',
        'pytest', 'IPython', 'matplotlib',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 只筛「没用的资源」，不动代码：这两项删掉不影响功能，只影响体积
a.binaries = [b for b in a.binaries if 'opengl32sw' not in b[0].lower()]

# PyInstaller 会把 datas 里的 ffmpeg.exe 也做一遍依赖分析，于是 avcodec 等 DLL
# 又被复制到 _internal 根目录一份（实测多出 57MB）。ffmpeg.exe 与这些 DLL 在
# ffmpeg\bin 里本来就在同一目录，Windows 优先从 exe 所在目录找，根目录那份没用。
_FFMPEG_DLL = re.compile(
    r'^(avcodec|avdevice|avfilter|avformat|avutil|postproc|swresample|swscale)-\d+\.dll$',
    re.IGNORECASE)


def _is_root_copy(dest: str) -> bool:
    """dest 不带目录 = 直接躺在 _internal 根目录的那份副本。"""
    return '/' not in str(dest).replace('\\', '/')


# 只删根目录副本，ffmpeg/bin/ 里的那份必须留着——那是 ffmpeg.exe 真正要加载的。
# 2026-11-10 踩过：一开始按文件名全删，结果 ffmpeg.exe 缺 DLL 起不来，
# 打包自检直接 FAIL 在「包内 ffmpeg 能编码」这一条上。
a.binaries = [b for b in a.binaries
              if not (_FFMPEG_DLL.match(Path(b[0]).name) and _is_root_copy(b[0]))]
a.datas = [d for d in a.datas
           if 'translations' not in d[0].replace('\\', '/').lower()]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='视频文件压缩',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口程序：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='tools/pyinstaller_version.txt',
    icon=['resources/app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='视频文件压缩',
)
