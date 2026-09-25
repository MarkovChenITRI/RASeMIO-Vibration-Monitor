# Smoke test run inside Windows Sandbox.
# Simulates a user who downloads only the EXE.
# ASCII only: Windows PowerShell 5.1 in the sandbox reads this file as ANSI.
# Result is written to C:\out\result.txt, which maps to sandbox-out on the host.

$ErrorActionPreference = 'Continue'
$result = "C:\out\result.txt"
$downloads = "C:\Users\WDAGUtilityAccount\Downloads"
$exe = Join-Path $downloads 'WR503-GPST-Monitor.exe'

function Log($text) {
    $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $text
    Add-Content -Path $result -Value $line -Encoding UTF8
    Write-Host $line
}

Set-Content -Path $result -Value "WR503-GPST-Monitor sandbox smoke test  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { Log "1. Python present in sandbox: YES (environment not clean)" }
else { Log "1. Python present in sandbox: NO (expected)" }

Copy-Item 'C:\out\WR503-GPST-Monitor.exe' $downloads -ErrorAction SilentlyContinue
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 2)
    Log "2. EXE copied to Downloads: OK, $mb MB"
} else {
    Log "2. EXE copied to Downloads: FAILED"
}

Log "3. Starting EXE"
Start-Process -FilePath $exe -WorkingDirectory $downloads
Start-Sleep -Seconds 30

$proc = Get-Process -Name 'WR503-GPST-Monitor' -ErrorAction SilentlyContinue
if ($proc) { Log "4. Process alive after 30s: YES" } else { Log "4. Process alive after 30s: NO (startup failed)" }

$db = Join-Path $downloads 'wr503_status.sqlite3'
if (Test-Path $db) {
    Log ("5. Database beside EXE: YES, " + (Get-Item $db).Length + " bytes")
} else {
    Log "5. Database beside EXE: NO"
}

$fallback = Join-Path $env:LOCALAPPDATA 'WR503-GPST-Monitor\wr503_status.sqlite3'
if (Test-Path $fallback) { Log "6. Fallback path used: YES (expected only when EXE folder is read-only)" }
else { Log "6. Fallback path used: NO" }

$cfg = Join-Path $downloads 'wr503_app_config.json'
if (Test-Path $cfg) { Log "7. Config auto-created: YES" } else { Log "7. Config auto-created: NO (created on exit)" }

$csv = Get-ChildItem 'C:\sensor' -Filter *.csv -ErrorAction SilentlyContinue | Select-Object -First 1
if ($csv) { Log ("8. Test CSV visible: " + $csv.Name) } else { Log "8. Test CSV visible: NOT FOUND" }

Log ""
Log "Automatic checks done. Now confirm manually in the app window:"
Log "  import the CSV from C:\sensor, run the metrics, export PNG and both CSV files."
