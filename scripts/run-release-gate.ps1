<#
.SYNOPSIS
  PaperLens real-model release gate (comprehensive test manual §7 Phase 4).

.DESCRIPTION
  Runs the real-model evaluation: BGE-M3 dense/sparse/hybrid embedding
  ablation, citation-context classification, PDF parse quality, and the
  interactive end-to-end agent evaluation with DeepSeek as the judge.

  These checks require the real embedding model and a live DeepSeek key, and
  they exercise the external paper sources (DBLP / OpenAlex / arXiv). They are
  intentionally separate from the deterministic quick gate so offline and
  real-model results are never conflated (manual §1.2).

  Preconditions (manual §7 Phase 4):
    - DEEPSEEK_API_KEY is set in backend/.env (or the host environment).
    - FlagEmbedding is installed and the BGE-M3 model can be loaded (first run
      downloads ~2GB into the HF cache).
    - backend/ services are reachable if running evaluate_interactive
      (it consumes the SSE stream from a running server).

  Manual traceability: docs/internal/comprehensive-test-plan.md §3.2 release
  gate, §5.3 (ReAct), §5.4 (Hybrid RAG), §5.5 (PDF).
#>

[CmdletBinding()]
param(
    [switch]$SkipInteractive  # skip evaluate_interactive (no running backend)
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'

function Get-EnvKey {
    # Read from backend/.env if present, else the host environment.
    $envFile = Join-Path $backend '.env'
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^DEEPSEEK_API_KEY=' } | Select-Object -First 1
        if ($line -and $line -ne 'DEEPSEEK_API_KEY=') { return $true }
    }
    return -not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)
}

Write-Host "=== Phase 4: Real-model release gate ===" -ForegroundColor Cyan

Write-Host "[0/3] Checking real-model prerequisites..." -ForegroundColor Cyan
if (-not (Get-EnvKey)) {
    Write-Host "ERROR: DEEPSEEK_API_KEY is not configured." -ForegroundColor Red
    Write-Host "Set it in backend/.env (DEEPSEEK_API_KEY=sk-...) or the host" -ForegroundColor Yellow
    Write-Host "environment before running the release gate." -ForegroundColor Yellow
    exit 1
}
Write-Host "  [ok] DEEPSEEK_API_KEY configured" -ForegroundColor DarkGray

Push-Location $backend
try {
    Write-Host "[1/3] Real BGE-M3 + DeepSeek upgrade-quality eval..." -ForegroundColor Cyan
    python manage.py evaluate_upgrade_quality --write-report
    if ($LASTEXITCODE -ne 0) { throw "evaluate_upgrade_quality failed" }

    if (-not $SkipInteractive) {
        Write-Host "[2/3] Interactive end-to-end agent eval (needs running backend)..." -ForegroundColor Cyan
        Write-Host "  Ensure the backend is running: python manage.py runserver 127.0.0.1:8000 --noreload" -ForegroundColor DarkGray
        python manage.py evaluate_interactive --write-report
        if ($LASTEXITCODE -ne 0) { throw "evaluate_interactive failed" }
    } else {
        Write-Host "[2/3] Skipping interactive eval (--SkipInteractive)" -ForegroundColor DarkGray
    }

    Write-Host "[3/3] Done. Review the written reports under backend/eval/reports/." -ForegroundColor Cyan
    Write-Host "  Copy the relevant metrics into docs/quality-assessment-log.md and"
    Write-Host "  (after a PASS verdict) sync README 'Recent local results'."
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "=== Release gate finished ===" -ForegroundColor Green
Write-Host "Remember: real-model reports MUST be scanned for secrets before any"
Write-Host "public use. Keys never appear in committed output."
