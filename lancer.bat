
@echo off
title NEXUS - Windows Control Center
cd /d "%~dp0"

echo.
echo  NEXUS // WINDOWS CONTROL CENTER
echo  -------------------------------
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 agent.py
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python agent.py
    goto end
)

echo Python est introuvable.
echo Installe Python 3 depuis https://www.python.org/downloads/windows/
echo Coche "Add Python to PATH" pendant l'installation.
pause

:end
