@echo off
echo [INFO] Activating environment...

:: Activate virtual environment
call venv\Scripts\activate

:: Set MongoDB URI
set MONGODB_URI=mongodb+srv://nivedanv14_db_user:nive14@cluster0.hsieonu.mongodb.net/?appName=Cluster0

echo [INFO] Starting sensor reader...

:: Start sensor script in background
start "" python sensor_config.py

echo [INFO] Starting CrustaScope backend...

:: Run backend (this blocks)
python app.py

echo.
echo [INFO] Backend stopped.
pause