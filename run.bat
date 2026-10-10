@echo off
cd /d "%~dp0"
echo Installing Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 py -m pip install -r requirements.txt
echo Starting quiz server...
python server\server.py --tunnel
if errorlevel 1 py server\server.py --tunnel
pause
