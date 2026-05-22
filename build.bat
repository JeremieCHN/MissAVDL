@echo off
chcp 65001 >nul
echo =========================================
echo MissAVDL 打包脚本
echo =========================================
echo.

REM 检查 Python 是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

echo [1/5] 检查并安装依赖...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo [2/5] 安装 PyInstaller...
pip install pyinstaller
if %errorlevel% neq 0 (
    echo [错误] PyInstaller 安装失败
    pause
    exit /b 1
)

echo.
echo [3/5] 清理旧的打包文件...
if exist "build" (
    rmdir /s /q "build"
    echo 已删除 build 目录
)
if exist "dist" (
    rmdir /s /q "dist"
    echo 已删除 dist 目录
)

echo.
echo [4/5] 开始打包...
pyinstaller --clean MissAVDL.spec
if %errorlevel% neq 0 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo [5/5] 打包完成！
echo.
echo =========================================
echo 可执行文件位置: dist\MissAVDL.exe
echo =========================================
echo.

REM 询问是否打开输出目录
set /p open_dist="是否打开输出目录? (Y/N): "
if /i "%open_dist%"=="Y" (
    explorer dist
)

echo.
pause
