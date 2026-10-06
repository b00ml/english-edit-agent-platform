param(
    [string]$RuntimeRoot = "",
    [int]$Port = 16580,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) { $RuntimeRoot = Join-Path $workspace '.local-eval/ocr-2026-10-05' }
$RuntimeRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$exe = Join-Path $RuntimeRoot 'mineru-venv/Scripts/mineru-api.exe'
$stateDir = Join-Path $RuntimeRoot 'background-service'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$manifest = Join-Path $stateDir 'process.json'
if ($Stop) {
    if (-not (Test-Path -LiteralPath $manifest)) { throw 'No owned OCR service manifest' }
    $state = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    $all = Get-CimInstance Win32_Process
    $root = $all | Where-Object ProcessId -eq $state.pid
    if (-not $root) { Write-Output 'OCR service is not running'; exit 0 }
    if ($root.ExecutablePath -ne $exe) { throw 'Process identity changed; do not stop an unrelated service' }
    $ids = [System.Collections.Generic.List[int]]::new()
    $ids.Add([int]$state.pid)
    for ($i=0; $i -lt $ids.Count; $i++) {
        foreach ($child in ($all | Where-Object ParentProcessId -eq $ids[$i])) {
            if (-not $ids.Contains([int]$child.ProcessId)) { $ids.Add([int]$child.ProcessId) }
        }
    }
    foreach ($id in $ids) { Stop-Process -Id $id -ErrorAction SilentlyContinue }
    Write-Output 'Stopped only the owned OCR service process tree; artifacts retained'
    exit 0
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'OCR port is occupied; will not replace an existing service' }
$entry = Get-Content -LiteralPath (Join-Path $workspace '.env') | Where-Object { $_ -match '^RAG_OCR_API_KEY=' } | Select-Object -Last 1
$key = if ($entry) { $entry.Substring($entry.IndexOf('=')+1).Trim().Trim('"').Trim("'") } else { '' }
if ($key.Length -lt 32) { throw 'Set a private RAG_OCR_API_KEY (at least 32 characters); never use a public unauthenticated service' }
$env:MINERU_HOME = Join-Path $RuntimeRoot 'mineru-home'
$env:MINERU_MODEL_SMALL_BACKEND = 'onnx'
$env:MINERU_INTRA_OP_NUM_THREADS = '2'
$env:MINERU_INTER_OP_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '2'
$env:HF_HUB_OFFLINE = '1'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$p = Start-Process -FilePath $exe -ArgumentList '--tier','basic','--host','0.0.0.0','--port',$Port,'--no-flash','--no-advanced','--disable-image-analysis','--preload-models','--api-key',$key,'--log-level','warning' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir "$stamp-stdout.log") -RedirectStandardError (Join-Path $stateDir "$stamp-stderr.log") -PassThru
@{pid=$p.Id;port=$Port;runtime=$RuntimeRoot;started_at=(Get-Date).ToString('s')} | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding utf8
Write-Output ('Started owned local OCR service PID=' + $p.Id + '; wait for authenticated /v1/health; core Docker services untouched')
