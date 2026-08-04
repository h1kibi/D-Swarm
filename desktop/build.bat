@echo off
rem Build the P6 desktop shell exe from the desktop/ dir (wails needs it as cwd).
cd /d %~dp0
C:\Users\Administrator\go\bin\wails.exe build -nocolour -windowsconsole
echo BUILD_RC=%ERRORLEVEL%
