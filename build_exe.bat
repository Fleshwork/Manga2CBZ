@echo off
REM ---------------------------------------------------------------
REM  Builds "Manga2CBZ.exe" from manga2cbz_gui.py + manga2cbz.py.
REM  Put this file, both .py files and manga2cbz.ico in one folder,
REM  then double-click this file. The exe ends up in the "dist" folder.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause & exit /b 1
)

for %%F in (manga2cbz.py manga2cbz_gui.py manga2cbz.ico) do (
    if not exist "%%F" ( echo Missing %%F - keep all files in the same folder. & pause & exit /b 1 )
)

echo.
echo [1/3] Setting up a private build environment...
if not exist ".build_venv\Scripts\python.exe" %PY% -m venv .build_venv || (pause & exit /b 1)
set "VPY=.build_venv\Scripts\python.exe"

echo [2/3] Installing customtkinter and pyinstaller...
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet customtkinter pyinstaller || (pause & exit /b 1)

echo [3/3] Building the exe (this takes a minute)...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name Manga2CBZ ^
    --icon manga2cbz.ico ^
    --add-data "manga2cbz.ico;." ^
    --collect-data customtkinter ^
    manga2cbz_gui.py || (pause & exit /b 1)

echo.
echo Done:  %~dp0dist\Manga2CBZ.exe
echo You can move that exe anywhere; nothing else is needed to run it.
explorer "%~dp0dist"
pause
