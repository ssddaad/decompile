@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

rem ---- 查找一个带 tkinter 的 Python 解释器 ----
set "PYEXE="
call :findpy "%LocalAppData%\Programs\Python\Python313\python.exe"
call :findpy "%LocalAppData%\Programs\Python\Python312\python.exe"
call :findpy "%LocalAppData%\Programs\Python\Python311\python.exe"
call :findpy "%LocalAppData%\Programs\Python\Python310\python.exe"
call :findpy "%LocalAppData%\Programs\Python\Python39\python.exe"
call :findpy "%USERPROFILE%\miniconda3\python.exe"
call :findpy "%USERPROFILE%\anaconda3\python.exe"
call :findpy "C:\ProgramData\miniconda3\python.exe"
call :findpy "C:\ProgramData\anaconda3\python.exe"

if not defined PYEXE (
    echo 未找到带 tkinter 的 Python，尝试使用系统默认 python...
    set "PYEXE=python"
)

"%PYEXE%" wxapp_decompiler.py
echo.
echo 按任意键关闭...
pause >nul
goto :eof

:findpy
if defined PYEXE goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import tkinter" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
goto :eof
