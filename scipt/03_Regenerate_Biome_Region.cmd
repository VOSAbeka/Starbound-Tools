@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  start "" pythonw.exe "%~dp0regenerate_biome_gui.py"
  exit /b 0
)
if exist "D:\Softwares\Python\pythonw.exe" (
  start "" "D:\Softwares\Python\pythonw.exe" "%~dp0regenerate_biome_gui.py"
  exit /b 0
)
python "%~dp0regenerate_biome_gui.py"
