# Daily fetch + publish for the Skagit housing dashboard.
# Registered in Windows Task Scheduler as "Skagit Housing Daily Fetch".
# Fetches fresh listings, then pushes updated data to GitHub so the
# public GitHub Pages site refreshes.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Start-Transcript -Path (Join-Path $PSScriptRoot "update.log") -Append

try {
    git pull --rebase origin main
    python fetch_listings.py
    if ($LASTEXITCODE -ne 0) { throw "fetch_listings.py failed (exit $LASTEXITCODE)" }

    git add data/listings.db data/listings.json
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -m "Daily data update $(Get-Date -Format yyyy-MM-dd)"
        git push origin main
    }
    else {
        Write-Output "No data changes to publish."
    }
}
finally {
    Stop-Transcript
}
