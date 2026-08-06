@echo off
REM Abre o Copia_hd com privilegio de administrador (necessario para acessar discos).
setlocal

net session >nul 2>&1
if %errorlevel%==0 goto :rodar

echo Solicitando privilegio de administrador...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:rodar
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python nao encontrado no PATH. Instale o Python 3.10 ou superior.
  pause
  exit /b 1
)
python app.py %*
echo.
pause
