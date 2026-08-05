@echo off
rem Wrapper: run build.sh under Git Bash (spawn-safe from tools that cannot
rem handle spaces in the executable path).
"C:\Program Files\Git\usr\bin\bash.exe" "%~dp0build.sh" %*
echo WORKER_BUILD_RC=%ERRORLEVEL%
