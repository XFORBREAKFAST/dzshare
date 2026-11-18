@echo off
title DzShare
cd /d "%~dp0"
where python >nul 2>nul || (echo Python belum terinstall. Download di https://python.org ^(centang "Add to PATH"^) & pause & exit /b)
python dzshare.py %*
pause
