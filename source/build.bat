@echo off
setlocal
cd /d "%~dp0"
py -3 -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name MIDIKeyboardPlayer --distpath ".." --workpath "build" --specpath "build" player.py
if errorlevel 1 goto failed
echo Build completed. Keep samples folder next to MIDIKeyboardPlayer.exe.
pause
exit /b 0
:failed
echo Build failed. Please check the error above.
pause
exit /b 1
