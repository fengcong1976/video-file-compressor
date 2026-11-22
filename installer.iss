; 视频文件压缩 · 安装包脚本（Inno Setup 6）
;
; 编译：先运行 build_exe.bat 生成 dist\视频文件压缩\，再执行
;   "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
; 产物：dist\安装包\视频文件压缩-1.1.0-安装包.exe
;
; 关键一条：PrivilegesRequired=lowest
; 装到 %LOCALAPPDATA%\Programs\视频文件压缩，注册表只写 HKCU，
; 全程不请求管理员权限，所以安装时不会弹 UAC，也不需要用户点「是」。
;
; 另一条同样重要：ffmpeg 是随包分发的（_internal\ffmpeg\bin），
; 用户机器上不需要预装 ffmpeg，也不会去调 PATH 里的其它版本。

#define MyAppName "视频文件压缩"
#define MyAppVersion "1.1.2"
#define MyAppPublisher "深圳市创炫互动网络科技有限公司"
#define MyAppURL "https://www.mrheedu.com"
#define MyAppExeName "视频文件压缩.exe"

[Setup]
RedirectionGuard=no
; AppId 一旦发布就不能改，否则新版本会被当成另一个软件、无法覆盖升级
AppId={{B5808542-2AE4-4BC6-B58F-FE3094BA3657}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion=1.1.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}

; 每个用户装到自己的目录：无管理员权限也能装，不弹 UAC
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

; 程序是 64 位（Python 3.13 x64），32 位系统直接挡掉
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 整个文件夹约 116MB（其中 ffmpeg 运行时 54MB），留够磁盘检查的余量
ExtraDiskSpaceRequired=0
OutputDir=installer_output
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-安装包
SetupIconFile=resources\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装前若程序正在运行，自动结束它，免得文件占用装不上
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "dist\{#MyAppName}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyAppName}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载后残留的空目录一并清掉（用户自己放进来的文件不动）
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
