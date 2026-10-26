@echo off
setlocal
for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "runner_vs=%%i"
if not defined runner_vs exit /b 1
call "%runner_vs%\Common7\Tools\VsDevCmd.bat" -no_logo -arch=x64 -host_arch=x64
if errorlevel 1 exit /b %errorlevel%
if not exist build mkdir build
if not exist dist mkdir dist
cl /nologo /W4 /WX /O2 /MT /utf-8 /DUNICODE /D_UNICODE packaging\windows_background.c /Fo:build\windows_background.obj /Fe:dist\codex-runner-background.exe /link /SUBSYSTEM:WINDOWS /DYNAMICBASE /NXCOMPAT
if errorlevel 1 exit /b %errorlevel%
cl /nologo /W4 /WX /O2 /MT /utf-8 /DUNICODE /D_UNICODE tests\fixtures\windows_console_probe.c /Fo:build\windows_console_probe.obj /Fe:build\windows_console_probe.exe /link /SUBSYSTEM:CONSOLE
exit /b %errorlevel%
