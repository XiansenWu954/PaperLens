"""Orchestrate the PaperLens quick quality gate (manual §3.2 / §7 Phase 2).

Runs the deterministic quick-gate sequence, captures each step's output into a
run-record evidence directory, and writes a summary using the candidate-release
report template (manual §9). Real-model, Docker, and browser gates are recorded
as NOT RUN here — this command only owns the deterministic quick gate.

Evidence layout (manual §4):

    backend/eval/reports/<run_id>/
        manifest.json      # run_id, sha, worktree, env, config (no secrets)
        backend-tests.txt  # full backend regression output
        eval-results/*.json
        logs/gate.log      # per-step PASS/FAIL/NOT-RUN + durations
        summary.md         # report-template summary

All captured output is run through a redaction pass before being written, so
no API keys or Authorization headers can leak into reports (manual §5.8).
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

BACKEND_DIR = Path(__file__).resolve().parents[3]
PROJECT_DIR = Path(__file__).resolve().parents[4]
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

# Secrets that must never appear in a report (manual §5.8).
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"(?i)authorization:\s*[A-Za-z0-9\-_\.=]+"),
    re.compile(r"(?i)(DEEPSEEK_API_KEY|DJANGO_SECRET_KEY)\s*=\s*\S+"),
]


def redact(text: str) -> str:
    """Replace anything matching a secret pattern with a redaction marker."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


class Command(BaseCommand):
    help = "Run the deterministic quick quality gate and write a run-record evidence bundle."

    def add_arguments(self, parser):
        parser.add_argument(
            "--frontend",
            action="store_true",
            help="Also run the frontend build + Vitest suite (skipped by default if Node is absent).",
        )

    def handle(self, *args, **options):
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M')}-{_git_short_sha()}"
        run_dir = REPORTS_DIR / run_id
        (run_dir / "eval-results").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)

        gate_log = run_dir / "logs" / "gate.log"
        steps: list[dict] = []

        def run_step(name: str, cmd: list[str], *, env: dict | None = None, timeout: int = 600, cwd_override: str | None = None) -> dict:
            """Run one gate step, capture output, classify PASS/FAIL/NOT-RUN."""
            started = time.time()
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n[{name}] {' '.join(cmd)}"))
            record: dict[str, object] = {
                "name": name,
                "command": cmd,
                "status": "RUNNING",
                "duration_ms": 0,
                "stdout_file": None,
            }
            full_env = {**os.environ, **(env or {})}
            work_dir = cwd_override or str(BACKEND_DIR)
            # On Windows, npm/node resolve to .CMD shims that need shell=True
            # to be invoked. shell=True is safe here: every arg is a fixed
            # command we control, not user input.
            use_shell = os.name == "nt" and any(
                str(c).lower().endswith((".cmd", ".bat")) or str(c) in ("npm", "node")
                for c in cmd
            )
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=work_dir,
                    env=full_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=use_shell,
                )
                record["status"] = "PASS" if proc.returncode == 0 else "FAIL"
                record["returncode"] = proc.returncode
                blob = redact((proc.stdout or "") + ("\n--STDERR--\n" + (proc.stderr or "")))
                out_file = run_dir / "eval-results" / f"{name}.txt"
                out_file.write_text(blob, encoding="utf-8")
                record["stdout_file"] = str(out_file.relative_to(REPORTS_DIR))
                # Surface a short tail on the console for quick triage.
                tail = "\n".join(blob.splitlines()[-8:])
                self.stdout.write(tail)
                if proc.returncode != 0:
                    self.stdout.write(self.style.ERROR(f"[{name}] FAIL (rc={proc.returncode})"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"[{name}] PASS"))
            except FileNotFoundError as exc:
                record["status"] = "NOT RUN"
                record["reason"] = f"tooling missing: {exc}"
                self.stdout.write(self.style.WARNING(f"[{name}] NOT RUN: {exc}"))
            except subprocess.TimeoutExpired:
                record["status"] = "FAIL"
                record["reason"] = f"timeout after {timeout}s"
                self.stdout.write(self.style.ERROR(f"[{name}] FAIL (timeout)"))
            record["duration_ms"] = int((time.time() - started) * 1000)
            steps.append(record)
            return record

        # --- Deterministic quick gate (manual §3.2) ---
        py = sys.executable
        fake_env = {"PAPERLENS_EMBEDDING_PROVIDER": "fake"}
        run_step("django_check", [py, "manage.py", "check"])
        run_step("makemigrations_check", [py, "manage.py", "makemigrations", "--check", "--dry-run"])
        run_step("backend_tests", [
            py, "manage.py", "test",
            "api", "realtime", "papers", "datasources", "rag",
            "citation", "agent", "mcp_server", "eval", "--noinput",
        ])
        run_step("evaluate_intents", [py, "manage.py", "evaluate_intents"], env=fake_env)
        run_step("evaluate_project_agent", [py, "manage.py", "evaluate_project_agent"], env=fake_env)
        run_step("evaluate_agent_quality", [py, "manage.py", "evaluate_agent_quality", "--write-report"], env=fake_env)
        run_step("evaluate_rag_quality", [py, "manage.py", "evaluate_rag_quality", "--write-report"], env=fake_env)
        run_step("evaluate_pdf_rag", [py, "manage.py", "evaluate_pdf_rag", "--write-report"], env=fake_env)
        run_step("pip_check", [py, "-m", "pip", "check"])

        # Frontend (optional — needs Node).
        if options["frontend"]:
            npm = _which("npm")
            if npm:
                fe_dir = str(PROJECT_DIR / "frontend")
                run_step("frontend_build", [npm, "run", "build"], cwd_override=fe_dir)
                run_step("frontend_test", [npm, "run", "test"], cwd_override=fe_dir)

        # --- Verdict ---
        verdict = "PASS" if all(s["status"] == "PASS" for s in steps) else "FAIL"

        gate_log.write_text(
            redact(json.dumps(steps, indent=2, default=str, ensure_ascii=False)),
            encoding="utf-8",
        )
        _write_manifest(run_dir, run_id, steps, verdict)
        _write_summary(run_dir, run_id, steps, verdict, include_frontend=options["frontend"])

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== GATE VERDICT: {verdict} ==="))
        self.stdout.write(f"Run record: {run_dir}")
        if verdict != "PASS":
            raise SystemExit(1)


# Frontend steps run in the frontend directory rather than the backend one.
def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _git_short_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() or "nosh"
    except Exception:
        return "nosh"


def _worktree_status() -> dict:
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return {
            "clean": len(porcelain.strip()) == 0,
            "changed_files": len([ln for ln in porcelain.splitlines() if ln.strip()]),
        }
    except Exception as exc:
        # §31.1/§32.4: gate artifacts never carry raw exception text.
        from eval.safe_error import exception_record

        return {"clean": None, **exception_record(exc)}


def _write_manifest(run_dir: Path, run_id: str, steps: list[dict], verdict: str) -> None:
    manifest = {
        "run_id": run_id,
        "git_sha": _git_short_sha(),
        "worktree": _worktree_status(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "node": _node_version(),
        },
        "database": {
            "engine": settings.DATABASES["default"]["ENGINE"],
            "name": str(settings.DATABASES["default"].get("NAME", "")),
        },
        "configuration": {
            "embedding_provider": getattr(settings, "PAPERLENS_EMBEDDING_PROVIDER", "unknown"),
            "embedding_model": getattr(settings, "PAPERLENS_EMBEDDING_MODEL", "unknown"),
            "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "live_llm": getattr(settings, "PROJECT_CHAT_LIVE_LLM", None),
        },
        "gates_not_run": {
            "docker_integration": "needs Docker Desktop running (manual §7 Phase 3)",
            "real_model_release": "needs DEEPSEEK_API_KEY + BGE-M3 download (manual §7 Phase 4)",
            "browser_e2e": "needs running backend + Playwright (manual §7 Phase 5)",
        },
        "verdict": verdict,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    (run_dir / "manifest.json").write_text(
        redact(json.dumps(manifest, indent=2, ensure_ascii=False, default=str)),
        encoding="utf-8",
    )


def _node_version() -> str | None:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return None


def _write_summary(run_dir: Path, run_id: str, steps: list[dict], verdict: str, *, include_frontend: bool) -> None:
    lines = [
        "# PaperLens Quick-Gate Run Record",
        "",
        f"- Run ID: `{run_id}`",
        f"- Git SHA: `{_git_short_sha()}`",
        f"- Worktree clean: {_worktree_status().get('clean')}",
        f"- Generated: {datetime.utcnow().isoformat()}Z",
        "",
        "## Verdict",
        f"**{verdict}**",
        "",
        "## Gates",
        "| Gate | Result | Duration |",
        "|---|---|---:|",
    ]
    for s in steps:
        lines.append(f"| {s['name']} | {s['status']} | {s.get('duration_ms', 0)} ms |")
    lines += [
        "",
        "## Not run this gate",
        "- Docker integration gate — needs Docker Desktop running.",
        "- Real-model release gate — needs DEEPSEEK_API_KEY + BGE-M3.",
        "- Browser E2E gate — needs running backend + Playwright.",
        "",
        "See `docs/internal/gate-runbook.md` to execute those gates.",
        "",
        "## Raw output",
        "Per-step captured (redacted) output is under `eval-results/`; "
        "the step log is `logs/gate.log`; environment/config (no secrets) "
        "is `manifest.json`.",
    ]
    if not include_frontend:
        lines.append("")
        lines.append("_Frontend build/test skipped (re-run with --frontend)._")
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
