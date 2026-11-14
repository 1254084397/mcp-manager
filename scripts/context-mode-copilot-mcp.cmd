@echo off
setlocal
set "PATH=%ProgramFiles%\Git\cmd;%ProgramFiles%\Git\bin;%ProgramFiles%\nodejs;%APPDATA%\npm;%PATH%"
"%ProgramFiles%\nodejs\node.exe" %*
exit /b %ERRORLEVEL%
