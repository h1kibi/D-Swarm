@echo off
rem Build the P6 desktop shell exe from the desktop/ dir (wails needs it as cwd).
cd /d %~dp0
rem Desktop defaults to `next start`, so package a fresh production deck build.
pushd ..\apps\web\ui
call npm.cmd run build
if errorlevel 1 (
  set BUILD_RC=%ERRORLEVEL%
  popd
  echo UI_BUILD_FAILED=%BUILD_RC%
  exit /b %BUILD_RC%
)
popd
rem Keep the Wails app icon and Windows icon in sync with the tracked SVG source.
magick appicon.svg -background white -density 256 -resize 1024x1024 build\appicon.png
if errorlevel 1 (
  echo ICON_BUILD_FAILED=%ERRORLEVEL%
  exit /b %ERRORLEVEL%
)
magick build\appicon.png -define icon:auto-resize=256,128,64,48,32,16 build\windows\icon.ico
if errorlevel 1 (
  echo ICO_BUILD_FAILED=%ERRORLEVEL%
  exit /b %ERRORLEVEL%
)
C:\Users\Administrator\go\bin\wails.exe build -nocolour -windowsconsole
echo BUILD_RC=%ERRORLEVEL%
