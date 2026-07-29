Set-Location "E:\rtk-bin\quotation-poc"
$envLine = Get-Content .env | Where-Object { $_ -match '^GROQ_API_KEY=' }
$env:GROQ_API_KEY = ($envLine -replace '^GROQ_API_KEY=', '').Trim()
# --reload restarts the server automatically when a .py file changes, so code
# edits take effect without killing and relaunching this window. Watching only
# app/ keeps it from restarting on every data/ or static/ write (the DB and its
# WAL files change constantly and would otherwise trigger reload loops).
& ".\venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app
