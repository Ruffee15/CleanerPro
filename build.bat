@echo off
setlocal
cd /d "%~dp0"
echo ================================================
echo   Building Cleaner Pro v1.0 Free (.exe)
echo ================================================
echo Working folder: %cd%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Install it from https://www.python.org/downloads/
    echo During install, check the box "Add python.exe to PATH"
    goto :fail
)

if not exist "cleaner_pro.pyw" (
    echo ERROR: cleaner_pro.pyw not found in this folder:
    echo %cd%
    goto :fail
)

echo Checking PyInstaller...
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: could not install PyInstaller ^(pip failed^).
    goto :fail
)

rem The old EXE must not be mistaken for the result of this build
if exist "dist\CleanerPro.exe" del /f /q "dist\CleanerPro.exe"
if exist "dist\CleanerPro.exe" (
    echo ERROR: cannot remove old dist\CleanerPro.exe ^(is it running?^)
    goto :fail
)

echo.
echo Building EXE, this may take a minute...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "CleanerPro" --icon icon.ico "cleaner_pro.pyw"
if errorlevel 1 (
    echo ERROR: PyInstaller failed, see the messages above.
    goto :fail
)
if not exist "dist\CleanerPro.exe" (
    echo ERROR: PyInstaller finished but dist\CleanerPro.exe was not created.
    goto :fail
)

echo.
echo ================================================
echo Done! File is here: %cd%\dist\CleanerPro.exe
echo SHA-256:
certutil -hashfile "dist\CleanerPro.exe" SHA256 | findstr /v /c:"hash" /c:"CertUtil"
echo ================================================
if not defined CP_NO_PAUSE pause
exit /b 0

:fail
echo.
echo BUILD FAILED.
if not defined CP_NO_PAUSE pause
exit /b 1
