# Installs PostgreSQL for the current user, no admin rights needed, and creates
# the app's database. Safe to run again: finished steps are skipped.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_local_postgres.ps1
param(
    [string]$Version = '16.15-5',
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'redline-postgres')
)
$ErrorActionPreference = 'Stop'

$backendDir = Join-Path $PSScriptRoot '..\backend'
$envFile = Join-Path $backendDir '.env'
$bin = Join-Path $Root 'pgsql\bin'
$data = Join-Path $Root 'data'
$log = Join-Path $Root 'postgres.log'
New-Item -ItemType Directory -Force $Root | Out-Null

if (-not (Test-Path (Join-Path $bin 'pg_ctl.exe'))) {
    $zip = Join-Path $Root 'binaries.zip'
    $url = "https://get.enterprisedb.com/postgresql/postgresql-$Version-windows-x64-binaries.zip"
    Write-Output "Downloading $url"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Write-Output 'Unpacking'
    Expand-Archive -Path $zip -DestinationPath $Root -Force
    Remove-Item $zip
}

# The password stays in backend/.env. It's only ever used on this machine.
$match = if (Test-Path $envFile) { Select-String -Path $envFile -Pattern 'postgres:([^@]+)@localhost' } else { $null }
if ($match) {
    $password = $match.Matches[0].Groups[1].Value
} else {
    $bytes = New-Object byte[] 18
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes) -replace '[+/=]', 'x'
    Add-Content -Path $envFile -Encoding ascii -Value "DATABASE_URL=postgresql+psycopg://postgres:$password@localhost:5432/redline"
}

if (-not (Test-Path (Join-Path $data 'PG_VERSION'))) {
    Write-Output 'Initialising the database cluster'
    $pwFile = Join-Path $Root 'pw.tmp'
    Set-Content -Path $pwFile -Value $password -NoNewline -Encoding ascii
    try {
        & (Join-Path $bin 'initdb.exe') -D $data -U postgres -E UTF8 --locale=C -A scram-sha-256 --pwfile=$pwFile | Out-Null
        if ($LASTEXITCODE) { throw "initdb failed with exit code $LASTEXITCODE" }
    } finally {
        Remove-Item $pwFile -ErrorAction SilentlyContinue
    }
}

& (Join-Path $bin 'pg_ctl.exe') -D $data status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Output 'Starting PostgreSQL on localhost:5432'
    # Started detached, otherwise this shell would own the server's output handles.
    Start-Process -FilePath (Join-Path $bin 'pg_ctl.exe') -WindowStyle Hidden -ArgumentList @(
        '-D', "`"$data`"", '-l', "`"$log`"", '-o', '"-p 5432 -c listen_addresses=localhost"', 'start'
    )
    $ready = $false
    foreach ($attempt in 1..30) {
        & (Join-Path $bin 'pg_isready.exe') -h localhost -p 5432 -q
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "PostgreSQL did not start. See $log" }
}

$env:PGPASSWORD = $password
$exists = & (Join-Path $bin 'psql.exe') -h localhost -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='redline'"
if ($exists -ne '1') {
    & (Join-Path $bin 'createdb.exe') -h localhost -U postgres redline
    if ($LASTEXITCODE) { throw "createdb failed with exit code $LASTEXITCODE" }
}
Write-Output "PostgreSQL is ready. Binaries: $bin"
