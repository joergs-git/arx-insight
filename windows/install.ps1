# ARX Insight - Windows installer (plug and play, for non-technical users)
# ---------------------------------------------------------------------------
# Double-click "Install ARX Insight.bat" in the folder above; it runs this
# script. The script installs everything that is missing and starts the app:
#   1. Python (via winget, or points you to the download if needed)
#   2. a private virtual environment + the two Python packages
#   3. the Firebird client library (downloaded automatically if not present)
#   4. finds your ARX database automatically (asks you to pick it if it can't)
#   5. creates a Desktop shortcut and launches the app in your browser
# Nothing here touches the ARX app itself; the database is only ever read.

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent          # program folder (parent of \windows)
# Everything that must SURVIVE a re-download lives here, outside the program folder:
$App  = Join-Path $env:LOCALAPPDATA "ARXInsight"  # venv, Firebird client, settings, goals
New-Item -ItemType Directory -Force -Path $App | Out-Null
function Say($m){ Write-Host "  $m" -ForegroundColor Cyan }
function Warn($m){ Write-Host "  $m" -ForegroundColor Yellow }

Write-Host ""
Write-Host "===== ARX Insight - Setup =====" -ForegroundColor Green
Write-Host ""

# --- 1. Python -------------------------------------------------------------
function Find-Python {
    foreach ($c in @("py -3","python","python3")) {
        try {
            $exe = $c.Split(" ")[0]; $arg = ($c.Split(" ") | Select-Object -Skip 1)
            $v = & $exe @arg --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match "Python 3") { return ($c) }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "Python is not installed. Trying to install it automatically (winget)..."
    try {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
    } catch { Warn "winget could not run." }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $py = Find-Python
}
if (-not $py) {
    Warn "Python still not found. Please install Python 3 from https://www.python.org/downloads/"
    Warn "IMPORTANT: tick 'Add python.exe to PATH' in the installer, then run this setup again."
    Start-Process "https://www.python.org/downloads/"
    Read-Host "Press Enter to close"; exit 1
}
Say "Python found: $py"

# --- 2. virtual environment + packages ------------------------------------
$pyExe = $py.Split(" ")[0]; $pyArg = ($py.Split(" ") | Select-Object -Skip 1)
$venv = Join-Path $App ".venv"                     # persistent, survives re-download
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Say "Creating a private environment (one time)..."
    & $pyExe @pyArg -m venv "$venv"
    Say "Installing required packages (anthropic, firebird-driver)..."
    & $vpy -m pip install --upgrade pip --quiet
    & $vpy -m pip install -r (Join-Path $Root "requirements.txt") --quiet
    Say "Packages ready."
} else {
    Say "Private environment already present - reusing it."
}

# --- 3. Firebird client library -------------------------------------------
$fbDir = Join-Path $App "firebird"                 # persistent, survives re-download
$fbDll = Join-Path $fbDir "fbclient.dll"
if (Test-Path $fbDll) {
    Say "Firebird client already present - reusing it."
} else {
    Say "Setting up the Firebird database client..."
    # Try to reuse the one that ships with the ARX app first.
    $found = Get-ChildItem "$env:LOCALAPPDATA\Packages\Arx.App*" -Recurse -Filter fbclient.dll -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        New-Item -ItemType Directory -Force -Path $fbDir | Out-Null
        Copy-Item $found.FullName $fbDll -Force
        Say "Reused the ARX Firebird client."
    } else {
        Say "Downloading the Firebird client (one time, ~15 MB)..."
        $url = "https://github.com/FirebirdSQL/firebird/releases/download/v5.0.2/Firebird-5.0.2.1613-0-windows-x64.zip"
        $zip = Join-Path $env:TEMP "arx_firebird.zip"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $zip
            $tmp = Join-Path $env:TEMP "arx_fb_extract"
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            New-Item -ItemType Directory -Force -Path $fbDir | Out-Null
            # copy the whole Firebird tree (fbclient + plugins + intl + firebird.conf)
            Copy-Item (Join-Path $tmp "*") $fbDir -Recurse -Force
            Say "Firebird client installed."
        } catch {
            Warn "Could not download the Firebird client automatically."
            Warn "The app can still use the client that ships with ARX if it is running."
        }
    }
}

# --- 4. locate the ARX database -------------------------------------------
function Find-Db {
    # 1) the common ARX data location: Documents\ARX\Resources\DB.FDB4
    $direct = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "ARX\Resources\DB.FDB4"
    if (Test-Path $direct) { return (Get-Item $direct) }
    # 2) search the usual roots (bounded, newest wins)
    $roots = @(
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "ARX"),
        "$env:LOCALAPPDATA\Packages\Arx.App*",
        "$env:APPDATA\ARX", "$env:PROGRAMDATA\ARX",
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "")
    )
    foreach ($r in $roots) {
        $hits = Get-ChildItem $r -Recurse -Filter "DB.FDB4" -ErrorAction SilentlyContinue
        if ($hits) { return ($hits | Sort-Object LastWriteTime -Descending | Select-Object -First 1) }
    }
    return $null
}
$db = Find-Db
if ($db) {
    Say "Found your ARX database:"
    Say "  $($db.FullName)"
    $dbPath = $db.FullName
} else {
    Warn "Could not find the ARX database automatically. Please pick the file DB.FDB4."
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Filter = "ARX database (DB.FDB4)|DB.FDB4|All files (*.*)|*.*"
    $dlg.Title = "Select your ARX database file (DB.FDB4)"
    if ($dlg.ShowDialog() -eq "OK") { $dbPath = $dlg.FileName } else { Warn "No file selected."; Read-Host "Press Enter to close"; exit 1 }
}

# --- 5. write config.json (db path) ---------------------------------------
$cfgPath = Join-Path $App "config.json"            # persistent settings (survive re-download)
# Preserve any existing settings (e.g. an API key the user set in the app).
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { $cfg = [PSCustomObject]@{} }
} else { $cfg = [PSCustomObject]@{} }
$cfg | Add-Member -NotePropertyName db -NotePropertyValue $dbPath -Force
# Write UTF-8 WITHOUT a BOM (Python's json reader chokes on a BOM otherwise).
[System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
Say "Saved settings."

# --- 6. Desktop shortcut ---------------------------------------------------
try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath("Desktop"),"ARX Insight.lnk"))
    $lnk.TargetPath = Join-Path $Root "Start ARX Insight.bat"
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = "shell32.dll,171"
    $lnk.Save()
    Say "Created a 'ARX Insight' shortcut on your Desktop."
} catch { Warn "Could not create a Desktop shortcut (not critical)." }

# --- 7. launch -------------------------------------------------------------
Write-Host ""
Write-Host "===== Setup complete - starting ARX Insight =====" -ForegroundColor Green
Write-Host ""
$env:ARX_DATA_DIR = $App
$env:ARX_FBCLIENT = $fbDll
$env:FIREBIRD = $fbDir
& $vpy (Join-Path $Root "arx_app.py")
