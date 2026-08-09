@echo off
chcp 65001 >nul
REM ============================================
REM  wxappUnpacker 一键反编译脚本
REM  用法: 拖拽 .wxapkg 到本 .bat 上, 或双击输入路径
REM  注意: PC 微信缓存是加密的(V1MMWX头), 需先用解密工具
REM ============================================
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
if "%~1"=="" (
    echo 请输入要反编译的 .wxapkg 文件路径:
    set /p "WXAPKG="
) else (
    set "WXAPKG=%~1"
)
if "%WXAPKG%"=="" ( echo [错误] 未提供文件路径 & pause & exit /b 1 )
if not exist "%WXAPKG%" ( echo [错误] 文件不存在: %WXAPKG% & pause & exit /b 1 )
echo.
echo ========================================
echo  开始反编译: %WXAPKG%
echo ========================================
echo.
node wuWxapkg.js "%WXAPKG%"
if %ERRORLEVEL%==0 (
    echo.
    echo [成功] 反编译完成! 输出在与 wxapkg 同级目录
) else (
    echo.
    echo [失败] 若报 Magic number 错误, 说明文件加密, 需先用解密工具
)
echo.
pause
