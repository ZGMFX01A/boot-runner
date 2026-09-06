@echo off
chcp 65001 >nul
echo ====================================================
echo        Boot Runner - 一键打包为 Windows EXE
echo ====================================================
python build_exe.py
pause
