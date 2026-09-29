# receiver GUI 打包独立 exe（issue #27）
# 用法：仓库根运行  .\build_exe.ps1
# 产物：dist\receiver_gui.exe（onefile 单文件，无控制台窗口，输出锚定 exe 所在目录）
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "未找到 .venv\Scripts\python.exe —— 请在仓库根目录运行本脚本"
}

# PyInstaller 是打包工具、非运行依赖，未装时现装
& .venv\Scripts\python.exe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    .venv\Scripts\python.exe -m pip install pyinstaller
}

.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed `
    --name receiver_gui receiver_gui.pyw

Write-Host "`n产物：$(Resolve-Path dist\receiver_gui.exe)"
