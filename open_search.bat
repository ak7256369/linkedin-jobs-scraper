@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py linkedin_ai_job_filter.py --open-search
) else (
    python linkedin_ai_job_filter.py --open-search
)
pause
