@echo off
rem ---------------------------------------------------------------------------
rem  Invoice extraction web app, port 8000.
rem
rem  Belongs to the INVOICE pipeline only, and names nothing outside it -- not
rem  the other pipeline's folder, venv, port or module. That is enforced by a
rem  test, so that removing the other pipeline is a deletion and never an edit.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 1

if not exist "logs" mkdir "logs"

rem pythonw, not python: it has no console attached, so no Ctrl+C can reach
rem it. A console-attached server is killable by a Ctrl+C sent to any process
rem sharing that console -- which is exactly how both apps died once already,
rem when an unrelated background command in another window was stopped.
rem
rem -u keeps the log useful while the server is still running.
"%ROOT%\.venv\Scripts\pythonw.exe" -u main.py >> "%ROOT%\logs\invoice-server.log" 2>&1
