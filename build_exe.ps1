# 接收端与制作端 GUI 打包独立 exe
# 用法：仓库根运行  .\build_exe.ps1
# 产物：dist\receiver_gui.exe、dist\tapemaker_gui.exe（单文件，无控制台窗口）
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "未找到 .venv\Scripts\python.exe —— 请在仓库根目录运行本脚本"
}

# PyInstaller 是打包工具、非运行依赖，未装时现装
& .venv\Scripts\python.exe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    .venv\Scripts\python.exe -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败" }
}

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name receiver_gui `
    --hidden-import dxcam `
    --hidden-import mss `
    --hidden-import winotify `
    receiver_gui.pyw
if ($LASTEXITCODE -ne 0) { throw "打包接收端失败" }

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean tapemaker_gui.spec
if ($LASTEXITCODE -ne 0) { throw "打包制作端失败" }

Write-Host "`n产物：$(Resolve-Path dist\receiver_gui.exe)"
Write-Host "产物：$(Resolve-Path dist\tapemaker_gui.exe)"
