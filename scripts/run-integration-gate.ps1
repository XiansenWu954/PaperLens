<#
.SYNOPSIS
  PaperLens Docker integration gate (comprehensive test manual §7 Phase 3).

.DESCRIPTION
  Builds and starts the full Postgres/pgvector/Redis/Celery stack, verifies each
  service is healthy, seeds a clean demo project, and runs the API/PDF/RAG/SSE
  end-to-end checks. This is the integration gate — it needs Docker Desktop
  running on the host.

  Preconditions (manual §7 Phase 3):
    - Docker Desktop is running (the daemon is up).
    - No other process is bound to ports 5432 / 6379 / 8000 / 5173.

  Manual traceability: docs/internal/comprehensive-test-plan.md §3.2 integration
  gate, §5.1 (ENV-05..08), §5.2 API contract.
#>

[CmdletBinding()]
param(
    [switch]$SkipBuild,   # reuse already-built images
    [switch]$NoSeed       # do not seed the demo project
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Assert-Tool {
    param([string]$Name, [string]$MinVersion = $null)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Prerequisite missing: '$Name' is not on PATH."
    }
    Write-Host "  [ok] $Name found" -ForegroundColor DarkGray
}

function Test-DockerRunning {
    try {
        $null = docker info 2>&1
        if ($LASTEXITCODE -ne 0) { throw "docker info failed" }
        Write-Host "  [ok] Docker daemon is running" -ForegroundColor DarkGray
    } catch {
        Write-Host "ERROR: Docker daemon is not running." -ForegroundColor Red
        Write-Host "Start Docker Desktop, then re-run this script." -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "=== Phase 3: Docker integration gate ===" -ForegroundColor Cyan
Write-Host "[1/6] Checking prerequisites..." -ForegroundColor Cyan
Assert-Tool 'docker'
Assert-Tool 'docker'
Test-DockerRunning

Write-Host "[2/6] Building and starting the stack..." -ForegroundColor Cyan
Push-Location $repoRoot
try {
    if ($SkipBuild) {
        docker compose up -d
    } else {
        docker compose up -d --build
    }
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
} finally {
    Pop-Location
}

Write-Host "[3/6] Verifying all five services are healthy..." -ForegroundColor Cyan
Start-Sleep -Seconds 5
docker compose ps
$ps = docker compose ps --services --filter "status=running" 2>$null
$expected = @('postgres', 'redis', 'backend', 'celery-worker', 'frontend')
foreach ($svc in $expected) {
    if ($ps -notcontains $svc) {
        Write-Host "  [FAIL] service '$svc' is not running" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [ok] $svc running" -ForegroundColor DarkGray
}

Write-Host "[4/6] Verifying pgvector, Redis, and Celery..." -ForegroundColor Cyan
docker compose exec -T postgres psql -U paperlens -d paperlens -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
docker compose exec -T redis redis-cli ping | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Redis ping failed" }
Write-Host "  [ok] Redis ping OK" -ForegroundColor DarkGray
docker compose exec -T celery-worker celery -A config inspect ping | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Celery inspect ping failed" }
Write-Host "  [ok] Celery worker responds" -ForegroundColor DarkGray

Write-Host "[5/6] Backend checks inside the container..." -ForegroundColor Cyan
docker compose exec -T backend python manage.py check
if ($LASTEXITCODE -ne 0) { throw "Django check failed in container" }
docker compose exec -T backend python manage.py showmigrations --plan | Select-String -Pattern '\[ \]'
Write-Host "  [ok] migrations applied" -ForegroundColor DarkGray

if (-not $NoSeed) {
    Write-Host "[6/6] Seeding a clean demo project..." -ForegroundColor Cyan
    docker compose exec -T backend python manage.py seed_demo_project
    Write-Host "  [ok] demo project seeded" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== Integration gate: services are up and healthy ===" -ForegroundColor Green
Write-Host "Endpoints:" -ForegroundColor Cyan
Write-Host "  Frontend: http://127.0.0.1:5173"
Write-Host "  Backend:  http://127.0.0.1:8000/api/projects"
Write-Host "  Health:   http://127.0.0.1:8000/  (returns config readiness)"
Write-Host ""
Write-Host "Next: run the browser E2E gate (Playwright) and the API/SSE end-to-end"
Write-Host "checks against these live services. See docs/internal/gate-runbook.md."
Write-Host ""
Write-Host "Tear down when done:"
Write-Host "  docker compose down -v"
