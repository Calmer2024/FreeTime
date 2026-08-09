@echo off
chcp 65001 >nul
echo ==========================================
echo   FreeTime 桌面版一键构建
echo ==========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 检查 Node.js
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

:: 检查虚拟环境
if exist "%~dp0\..\.venv\Scripts\activate.bat" (
    echo [INFO] 激活虚拟环境...
    call "%~dp0\..\.venv\Scripts\activate.bat"
) else (
    echo [WARN] 未找到虚拟环境，使用系统 Python
)

:: 检查 PyInstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 安装 PyInstaller...
    pip install pyinstaller
)

:: 运行构建
echo.
echo [INFO] 开始构建...
python "%~dp0\build.py"

echo.
echo ==========================================
echo   构建完成！
echo ==========================================
pause
