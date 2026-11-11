@echo off
REM Start resume-agent API and open Swagger UI.
REM ASCII only, CRLF line endings.
cd /d %~dp0..
echo ================================================
echo  resume-agent  API  starting ...
echo  Swagger UI : http://127.0.0.1:8000/docs
echo  Close this window to stop the server.
echo ================================================
start "" http://127.0.0.1:8000/docs
.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000
pause
