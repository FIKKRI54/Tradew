$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create venv and install requirements first; see README.md.'
}

# Start the API server in a separate job
$apiProcess = Start-Job -ScriptBlock {
    Set-Location $using:projectRoot
    & "$using:projectRoot\venv\Scripts\python.exe" api_server.py
}

# Wait until the API really answers instead of assuming startup succeeded.
$apiReady = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:5000/api/status' -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200 -and $response.Headers['Content-Type'] -like 'application/json*') {
            $apiReady = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 250
    }
}

if (-not $apiReady) {
    $apiOutput = Receive-Job $apiProcess
    Stop-Job $apiProcess -ErrorAction SilentlyContinue
    Remove-Job $apiProcess -Force -ErrorAction SilentlyContinue
    throw "Scanner API did not start. $apiOutput"
}

# Start the Vite dev server
$env:PATH += ";C:\Program Files\nodejs"
Push-Location "$projectRoot\frontend"
try {
    npm.cmd run dev
} finally {
    Stop-Job $apiProcess -ErrorAction SilentlyContinue
    Remove-Job $apiProcess -Force -ErrorAction SilentlyContinue
    Pop-Location
}
