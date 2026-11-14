@echo off
chcp 65001 >nul
rem Сборка CloudMount.exe (нужен Python 3.10+ с галочкой "Add to PATH")
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --onefile --noconsole --name CloudMount ^
  --icon assets\icon.ico --add-data "assets;assets" ^
  --hidden-import pystray._win32 --hidden-import mfusepy ^
  --collect-submodules cloudmount main.py || goto :err
echo.
echo Готово: dist\CloudMount.exe
pause
exit /b 0
:err
echo Ошибка сборки
pause
exit /b 1
