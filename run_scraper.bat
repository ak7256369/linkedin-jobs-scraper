@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py linkedin_ai_job_filter.py --scrape "https://www.linkedin.com/jobs/search/?currentJobId=4483715326&f_TPR=r86400&f_WT=2&geoId=103644278&keywords=senior%%20ai%%20remote&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON&refresh=true"
) else (
    python linkedin_ai_job_filter.py --scrape "https://www.linkedin.com/jobs/search/?currentJobId=4483715326&f_TPR=r86400&f_WT=2&geoId=103644278&keywords=senior%%20ai%%20remote&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON&refresh=true"
)
pause
