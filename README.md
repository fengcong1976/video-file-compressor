# 视频文件压缩 · v1.1.0

> 免费下载与图文教程：<https://www.mrheedu.com/apps/video-file-compressor>　·　更多免费软件：[www.mrheedu.com](https://www.mrheedu.com)
按《创炫互动 · 新品立项与验收规范》v1.5 立项的第二款。立项依据、竞品对照、验收方式见 [`立项卡.md`](立项卡.md)。

> **一句话**：填 25 就压到 25MB 以内；要固定分辨率就顺手填宽高，片子不出本机。
>
> **口径**：「原片不动」指原文件字节未被改写（SHA-256 前后一致），**不代表画质不变**。视频压缩必然重编码、必然有损；每一条的码率与分辨率变化都写在报告里。

当前进度：**已完成并验收（v1.1.0 加输出尺寸）** —— 核心引擎、命令行、PySide6 界面、无 UAC 安装包、产品文档。
产物：`dist\视频文件压缩\`（免安装版 116MB）、`dist\安装包\视频文件压缩-1.1.0-安装包.exe`（32.6MB）。

产品页与教程：[`docs/产品介绍与功能特点.md`](docs/产品介绍与功能特点.md)、[`docs/使用教程.md`](docs/使用教程.md)。

## 快速开始

核心只依赖 Python 与 ffmpeg；界面另需 PySide6。

```bash
pip install -r requirements.txt

python main.py                               # 图形界面
python cli.py ./相册 --target 25             # 压到 ≤25MB，结果进每段视频旁的 out
python cli.py ./相册 --target 25 --open      # 压完打开结果文件夹
python cli.py a.mp4 b.mov --target 100 --out ./out
python cli.py ./素材 --target 25 --lossless-only   # 仅无损：只换容器，压不到就明说
python cli.py ./素材 --target 25 --size 1280x720   # 等比缩到 1280×720 以内再压
python cli.py ./素材 --target 25 --size 1080x1080 --stretch  # 精确该宽高，比例会变
python cli.py --version                      # 版本、口径、实际用的 ffmpeg
```

## 四套自检（都不读用户文件、不联网）

```bash
python verify.py                    # 引擎 121 项，约 2 分钟
python tools/ui_test.py             # 界面 92 项，约 25 秒（会真的建窗口、点按钮、跑 QThread）
python tools/exe_gui_smoke.py "dist\视频文件压缩\视频文件压缩.exe"   # 打包产物窗口冒烟 6 项
"dist\视频文件压缩\视频文件压缩.exe" --selftest D:\selftest.txt     # 打包产物内部 11 项
```

`--selftest` 是给验收用的：打包后 ffmpeg 只认自己旁边那份，开发机上永远能找到 PATH 里的版本，所以必须让 exe 自己现场合成一段 1080p 素材、用包内 ffmpeg 走完探测 → 两遍编码 → 复称体积，结果写日志文件（窗口程序没有控制台）。

规模与长片另有一套（约 3 分钟）：

```bash
python tools/stress_test.py --slice=0-34    # 100 条批量，分三段跑
python tools/stress_test.py --slice=34-67
python tools/stress_test.py --slice=67-100
python tools/stress_test.py --agg           # 汇总：100/100 达标、名字不重复
python tools/stress_test.py -b              # 90 秒 1080p 长片 + 40% 处取消
```

批量为什么要分段：本机对「一轮里删 50 个以上文件」有保护，而压缩器每压一条都要清临时文件——100 条一次跑到第 49 条就被掐断，且不留 traceback（很容易误判成产品 bug）。分段 + `--agg` 汇总即可，被测代码一行不改。

## 它怎么工作

**不用 CRF，用码率反推。** CRF 决定「画质」，不决定「体积」；同一 CRF 在不同片子上能差三五倍，而用户要的是「不超过 25MB」这个硬数字。所以：

```
总码率(kbps) = 目标字节 × 8 / 时长(秒) / 1000 × 0.97   ← 3% 余量给容器开销
视频码率     = 总码率 − 音频码率
```

然后两遍编码（pass 1 分析、pass 2 输出），出来复称体积；超了就按实测比例修正再编一次，最多两次，再不行降一档分辨率。

**为什么会超。** 实测（60 秒 1080p 源片 63MB / 8809 kbps，目标 5MB）：

| 策略 | 输出 | 结论 |
|---|---|---|
| 不留余量，视频 603k | 5.07 MB | **超 1.4%**，卡在「不超过 5MB」门外 |
| 留 3% 余量 + 两遍，视频 582k（1080p） | 4.92 MB | 差 −1.5%，耗时 23.4 s |
| 同上降到 720p | 4.92 MB | 差 −1.6%，耗时 18.3 s |

编码速度约 **0.3–0.4 倍实时**。

**两遍编码必须带恒定帧率。** mkv / 手机录像这类时间戳不规整的源，pass1 与 pass2 会差几帧；x264 一旦发现「2nd pass has more frames than 1st pass」就放弃码率控制退回固定 QP，体积失控。`cfr_args()` 按 ffmpeg 版本挑 `-fps_mode cfr`（≥5）或 `-vsync cfr`，两遍必须一致。

**分辨率阶梯与画质地板。** 反推的码率低于该分辨率的地板就自动降一档（只降不升）：1080p 600 / 720p 400 / 480p 200 kbps，短边不低于 480p。低于地板仍压不到就**不出片**，只告诉你「最小可达约 X MB」。

> 地板必须分档：一开始用固定 300kbps，结果「降一档再试」永远不会触发——阈值不随分辨率变化，降档等于白降；而 1080p 跑到 300kbps 早已是满屏色块。

**不做什么**：不剪辑、不拼接、不加字幕、不抽帧、不转音频格式、不默认 H.265（留 v1.1）、不用 GPU 编码。各有专款或另议。

## 目录

| 文件 | 作用 |
|---|---|
| `core/ffmpeg_tool.py` | ffmpeg/ffprobe 定位（打包目录 → exe 同级 → PATH → 本机开发路径）、探测（时长/分辨率/码率/编码器/旋转）、带进度与取消的子进程调用 |
| `core/compressor.py` | 目标码率反推、3% 余量、两遍编码、超差修正、分辨率阶梯、画质地板、仅无损重封装、逐条报告、批量、同名原子占名 |
| `core/compliance.py` | 0 联网运行时计数（自检、界面、打包自检共用） |
| `core/about.py` | 身份与文案单一来源：名称、版本、公司、有损披露、使用说明 |
| `main.py` | PySide6 界面 + `--selftest` 打包自检入口 |
| `cli.py` | 命令行：`--target` / `--size` / `--stretch` / `--out` / `--lossless-only` / `--open` / `--version`，写 `report.json` |
| `verify.py` | 引擎自检 121 项（含指定尺寸 6 项） |
| `tools/ui_test.py` | 界面自检 92 项（建真窗口、点真按钮、跑真 QThread、指定尺寸真压） |
| `tools/exe_gui_smoke.py` | 外部冒烟：启动 exe、Win32 找窗口、截图、发 WM_CLOSE，并检查 PE 子系统是 GUI |
| `tools/installer_ui_check.py` | 走真实安装向导：点「下一步 / 安装」到完成页，枚举控件文字并截图，断言没有 README 勾选项 |
| `tools/make_docs_shots.py` | 生成文档截图（真实窗口 + 现场合成素材，数字都是真算的） |
| `docs/` | 产品介绍与功能特点、使用教程、9 张截图 |
| `立项卡.md` | 立项三问、竞品对照、六条标配落地、技术方案与实测、MVP 范围、命名与撞词、风险、已拍板决策 |
| `requirements.txt` | 界面依赖（PySide6）；ffmpeg 作为运行时随包分发，不在此列 |

## 自检覆盖（121 + 92 + 11 + 6，全过）

引擎侧（`verify.py`，12 个现场合成样本）：

```
体积   目标必须压得住 / 超差修正 / 目标≥源直接复制 / 目标=源算达标
原片   SHA-256 前后一致 / 结果落在原片旁 out / 同名加 (1)(2) 不覆盖
阶梯   1080p→720p、4K→720p、竖拍 720×1280→480×854、地板不交付并报最小可达
尺寸   指定 854×480 等比 / 640×640 精确拉伸 / 框比原片大不放大 / 已达标仍缩尺寸 / 压不到不换档
模式   仅无损只换容器（视频流 MD5 一致）/ 源已达标原样复制 / 无音轨不编造字段
兼容   mkv-avi-mov-ts-flv-wmv-webm 六种容器 / 双音轨 / 中文与空格名 / 坏文件跳过
流程   进度单调到 100% / 取消无半截 / 取消后重跑 / 递归跳过 out / 空目录不抛
报告   20+ 字段齐全可序列化 / 时长帧率未被改 / 码率是实测值 / 0 联网
```

界面侧（`tools/ui_test.py`）：拖入 → 清单 → 删除选中 / 清空 → 尺寸联动与仅无损互斥 → 指定 640×360 真压 → 结果表「原 → 新」→ 详情页 11 个披露字段 → 取消 → 压缩中关窗口 → 缺 ffmpeg 时的置灰与说明 → 使用说明 / 关于。

规模侧（`tools/stress_test.py`）：100 条批量 100/100 达标、结果名不重复、报告计数一致；90 秒 1080p 93.6MB → 5.72MB（时长不变）；40% 处取消 15.5s 收尾、out 里 0 个文件。

打包侧（`--selftest`）：随包 ffmpeg/ffprobe 定位、包内能编码（libx264）、包内能探测、0 联网、压到目标、结果进 out、产物可解析、不越档降分辨率、原片未改。
外部冒烟（`tools/exe_gui_smoke.py`）：PE 子系统 = GUI（不弹黑框）、窗口出现、尺寸合理、截图、正常关闭退出。

安装包验收（2026-11-10 实测，v1.1.0）：`python tools/installer_ui_check.py "dist\安装包\视频文件压缩-1.1.0-安装包.exe"` 走完真实向导（欢迎 → 目录 → 准备安装 → 安装 → 完成），完成页只剩「立即运行 视频文件压缩」+「完成」，**不再有「查阅 README 文档」勾选项**；装到 `%LOCALAPPDATA%\Programs`、注册表只写 HKCU、`asInvoker` 清单不弹 UAC；安装后自检 11/11、窗口冒烟 6/6；静默卸载退出码 0，安装目录、开始菜单、卸载注册项零残留。

## 已知限制

- 单条编码按实时 0.3–0.4 倍，1 小时视频要 20 分钟左右；长任务请让它跑着
- 仅无损只能省容器开销（mkv/ts→mp4），想真正变小必须重编码；它也改不了尺寸（`-c copy` 不重采样）
- 指定尺寸时只按该尺寸编一档，不会为了凑达标自动再降档；那个尺寸下压不到就如实说不达标
- 纯 CPU 编码（libx264 veryfast），未做 NVENC/QSV；显卡加速的质量与兼容性差异大，另议
- 双音轨只保留第一条（会在说明里写清）
- 免安装版 116MB，其中 ffmpeg 运行时 54MB（这是「装完就能用、不依赖用户机器」的代价）；安装包 lzma2 压缩后 32.6MB

## 打包

```bat
build_installer.bat     :: 先 PyInstaller 出 dist\视频文件压缩\，再 ISCC 出安装包
```

- `build.spec`：ffmpeg 按 datas 整份搬进 `_internal\ffmpeg\bin`（不走依赖分析，避免漏 DLL）；排除 numpy/yaml/psutil/requests 等被钩子顺手牵进来的包；去掉 Qt 翻译与 `opengl32sw.dll`；不启用 UPX（易被杀软误报）
- **踩过的坑**：PyInstaller 会对 datas 里的 exe 再做一次依赖分析，把 `avcodec` 等 DLL 复制到 `_internal` 根目录（多 57MB）。过滤时要**只删根目录那份**——2026-11-10 一开始按文件名全删，结果 `ffmpeg/bin` 里的也一起没了，打包自检直接 FAIL 在「包内 ffmpeg 能编码」上。现在用 `_is_root_copy()` 判 dest 是否带目录。
- `installer.iss`：`PrivilegesRequired=lowest` + `{autopf}`（解析为 `%LOCALAPPDATA%\Programs`），注册表只写 HKCU，全程不弹 UAC

## 仓库说明

本仓库只包含源码，以下运行时资源体积较大，未纳入仓库：

- ffmpeg/（FFmpeg 运行时）：自行下载 FFmpeg 后放到该目录再运行/打包。

---

## 更多免费软件

本工具由深圳市创炫互动网络科技有限公司开发，官网提供免费安装包、图文教程，以及更多永久免费的 Windows 软件：**[www.mrheedu.com](https://www.mrheedu.com)**
