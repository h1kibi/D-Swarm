@echo off
rem Wrapper: run build-base.sh under Git Bash (spawn-safe from tools that
rem cannot handle spaces in the executable path).
"C:\Program Files\Git\usr\bin\bash.exe" "%~dp0build-base.sh"
echo BASE_BUILD_RC=%ERRORLEVEL%
