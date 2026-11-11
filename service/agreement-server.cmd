@echo off
rem ---------------------------------------------------------------------------
rem  Agreement extraction web app, port 8100.
rem
rem  Belongs to the AGREEMENT pipeline only. To remove that pipeline entirely:
rem      powershell -File service\uninstall.ps1 -Pipeline agreement
rem      rmdir /s /q agreement_extractor
rem      del service\agreement-server.cmd
rem  The invoice app is untouched by all three.
rem
rem  FLAGS_use_mkldnn must be set BEFORE python starts, because paddle reads it
rem  at import. With oneDNN on, PaddlePaddle fails every inference call through
rem  its PIR executor -- per page, not at startup -- so the app would look
rem  healthy and OCR every page to nothing.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0.."
set "FLAGS_use_mkldnn=0"
set "GLOG_minloglevel=2"
cd /d "%ROOT%\agreement_extractor" || exit /b 1

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

rem pythonw, not python: no console, so no Ctrl+C can reach it. Both apps were
rem killed once by a Ctrl+C meant for an unrelated background command, which a
rem console-attached process in the same session will happily accept.
"%ROOT%\.venv313\Scripts\pythonw.exe" -u -m scripts.serve >> "%ROOT%\logs\agreement-server.log" 2>&1
