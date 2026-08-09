@echo off
chcp 65001 >nul 2>&1
title 微信小程序反编译工具
setlocal enabledelayedexpansion

REM ============================================================
REM  微信小程序一键解密+反编译工具 (整合版)
REM  支持模式:
REM    1. 拖拽 .wxapkg 到本 bat 上
REM    2. 菜单选择已缓存的小程序
REM    3. 手动输入文件路径
REM ============================================================

set "TOOL_DIR=%~dp0"
set "UNPACK_DIR=%TOOL_DIR%wxappUnpacker"
set "DECRYPT_DIR=%TOOL_DIR%pc_wxapkg_decrypt"
set "WECHAT_CACHE=C:\Users\38364\Documents\WeChat Files\Applet"

REM === 拖拽模式: 有参数 ===
if not "%~1"=="" (
    set "WXAPKG=%~1"
    goto :PROCESS
)

REM === 交互模式: 显示菜单 ===
:MENU
cls
echo.
echo  ================================================
echo   微信小程序反编译工具
echo  ================================================
echo.
echo   [1] 从微信缓存中选择小程序
echo   [2] 手动输入文件路径
echo   [3] 打开微信缓存目录
echo   [4] 打开工具目录
echo   [0] 退出
echo.
set /p "CHOICE=请选择 (0-4): "

if "%CHOICE%"=="0" exit /b
if "%CHOICE%"=="3" ( explorer "%WECHAT_CACHE%" & goto :MENU )
if "%CHOICE%"=="4" ( explorer "%TOOL_DIR%" & goto :MENU )
if "%CHOICE%"=="2" goto :MANUAL_INPUT
if "%CHOICE%"=="1" goto :LIST_CACHE
echo 无效选择! & timeout /t 2 >nul & goto :MENU

REM === 列出缓存中的小程序 ===
:LIST_CACHE
cls
echo.
echo  正在扫描微信缓存目录...
echo.
echo  编号  修改时间              大小(KB)  AppID              文件名
echo  ----  -------------------  --------  -----------------  --------
set /a IDX=0
set "FILE_LIST="
if not exist "%WECHAT_CACHE%" (
    echo  缓存目录不存在: %WECHAT_CACHE%
    echo  请先在微信中打开小程序以生成缓存
    echo.
    pause
    goto :MENU
)
for /f "delims=" %%F in ('dir /s /b /o-d "%WECHAT_CACHE%\*.wxapkg" 2^>nul') do (
    set /a IDX+=1
    set "FILE_!IDX!=%%F"
    for %%A in ("%%F") do set "FNAME=%%~nA"
    REM 提取 wxid
    set "FPATH=%%~dpF"
    set "WXID_!IDX!=unknown"
    for %%P in ("!FPATH:\=" "!") do (
        set "SEG=%%~P"
        if "!SEG:~0,2!"=="wx" set "WXID_!IDX!=!SEG!"
    )
    REM 获取文件信息
    for %%A in ("%%F") do set "FSIZE=%%~zA"
    set /a FSIZEKB=!FSIZE!/1024
    for %%A in ("%%F") do set "FTIME=%%~tA"
    set "FNAME=!FNAME:~0,18!"
    echo   [!IDX!]  !FTIME!  !FSIZEKB!KB      !WXID_!IDX!!FNAME!
)
if %IDX%==0 (
    echo.
    echo  未找到任何 wxapkg 文件!
    echo  请先在微信中打开小程序.
    echo.
    pause
    goto :MENU
)
echo.
set /p "SEL=请输入编号 (1-%IDX%) 选择, 或按 0 返回: "
if "%SEL%"=="0" goto :MENU
if not defined FILE_%SEL% (
    echo 无效编号! & timeout /t 2 >nul & goto :LIST_CACHE
)
call set "WXAPKG=%%FILE_%SEL%%%"
call set "WXID=%%WXID_%SEL%%%"
goto :PROCESS_WITH_WXID

REM === 手动输入 ===
:MANUAL_INPUT
cls
echo.
echo  请输入 .wxapkg 文件完整路径
echo  ^(也可将文件拖拽到此窗口后按回车^)
echo.
set /p "WXAPKG=路径: "
if "!WXAPKG!"=="" goto :MENU
REM 去除可能的引号和尾部空格
set "WXAPKG=!WXAPKG:"=!"

REM === 处理主流程 ===
:PROCESS
REM 从路径提取 wxid
set "PDIR=%~dp1"
set "WXID="
for %%P in ("%PDIR:\=" "%") do (
    set "SEG=%%~P"
    if "!SEG:~0,2!"=="wx" set "WXID=!SEG!"
)

:PROCESS_WITH_WXID
if not exist "%WXAPKG%" (
    echo.
    echo  [错误] 文件不存在: %WXAPKG%
    echo.
    pause
    goto :MENU
)

echo.
echo  ================================================
echo  输入文件: %WXAPKG%
echo  AppID:    %WXID%
echo  ================================================
echo.

REM === 步骤1: 检测是否加密 ===
powershell -NoProfile -Command "$b=[IO.File]::ReadAllBytes('%WXAPKG%')[0..5]; $s=[Text.Encoding]::ASCII.GetString($b); Write-Output $s" > "%TEMP%\_wxmagic.txt" 2>nul
set "MAGIC="
set /p "MAGIC=" < "%TEMP%\_wxmagic.txt" 2>nul
del "%TEMP%\_wxmagic.txt" 2>nul

if "%MAGIC%"=="V1MMWX" (
    echo  [步骤1/2] 文件已加密, 正在解密...
    echo.
    if "%WXID%"=="" (
        echo  [警告] 未能从路径自动提取 AppID
        echo  请手动输入 AppID ^(wx开头18位, 如 wx1234567890abcdef^):
        set /p "WXID=AppID: "
        if "!WXID!"=="" (
            echo  [错误] 未提供 AppID, 无法解密
            pause & goto :MENU
        )
    )
    set "DEC_FILE=%TEMP%\_dec_%RANDOM%.wxapkg"
    cd /d "%DECRYPT_DIR%"
    python main.py --wxid "%WXID%" -f "%WXAPKG%" -o "%DEC_FILE%"
    if !ERRORLEVEL! neq 0 (
        echo.
        echo  [错误] 解密失败! 请检查:
        echo    1. Python 是否已安装
        echo    2. pycryptodome 是否已安装 ^(pip install pycryptodome^)
        echo    3. AppID 是否正确: %WXID%
        echo.
        pause & goto :MENU
    )
    echo.
    echo  [步骤1/2] 解密成功!
    set "TARGET=%DEC_FILE%"
) else (
    echo  [步骤1/2] 文件未加密, 跳过解密
    set "TARGET=%WXAPKG%"
)

REM === 步骤2: 反编译 ===
echo.
echo  [步骤2/2] 正在反编译...
echo.
cd /d "%UNPACK_DIR%"
node wuWxapkg.js "%TARGET%"
set "RC=!ERRORLEVEL!"

REM === 清理临时文件 ===
if exist "%DEC_FILE%" del "%DEC_FILE%" 2>nul

REM === 找到输出目录 ===
set "OUTDIR="
for %%A in ("%TARGET%") do set "OUTDIR=%%~dpnA"

echo.
echo  ================================================
if exist "%OUTDIR%" (
    echo   反编译完成!
    echo   输出目录: %OUTDIR%
    echo  ================================================
    echo.
    echo  [1] 打开输出目录
    echo  [2] 用微信开发者工具打开
    echo  [3] 返回菜单
    echo  [0] 退出
    echo.
    set /p "AFTER=选择: "
    if "!AFTER!"=="1" explorer "%OUTDIR%"
    if "!AFTER!"=="2" (
        explorer "%OUTDIR%"
        echo  请在微信开发者工具中: 导入项目 -^> 选择该目录
    )
    if "!AFTER!"=="3" goto :MENU
) else (
    if !RC!==0 (
        echo   反编译完成! 请在 wxapkg 同级目录查看输出
    ) else (
        echo   反编译结束但有部分错误
        echo   ^(通常是插件路径含冒号, 不影响主体文件^)
        echo   输出目录: %OUTDIR%
    )
    echo  ================================================
)
echo.
pause
goto :MENU
