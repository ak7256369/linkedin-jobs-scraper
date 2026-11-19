@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py linkedin_ai_job_filter.py --input input_jobs.csv --output jobs.csv --rejected rejected_jobs.csv --allow-linkedin-apply
) else (
    python linkedin_ai_job_filter.py --input input_jobs.csv --output jobs.csv --rejected rejected_jobs.csv --allow-linkedin-apply
)
pause
