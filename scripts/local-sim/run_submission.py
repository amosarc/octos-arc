#!/usr/bin/env python3
"""ARC-Bench 生产 Runner(run_submission.py)本地重建版。

真实源码在 arc-bench 网站仓库(私有),本机没有 checkout;本文件按三份公开
材料复刻其行为,供 hackathon-local-simulation 在无平台仓库时完成一致的本地
模拟提交:

  1. hackathon-local-simulation/README.md §3(平台时序:解压→pip install→
     需求快照→执行智能体→部署→Playwright)与 §4(环境表);
  2. local_runner.py 引用的属性面——它按名 monkey-patch 以下成员,名字与
     调用约定必须逐字对齐:
       SPEC_PATH / PROJECT_DIR / WEB_APP_PORT / WEB_APP_BASE_URL /
       append_debug_log / append_runner_event / start_background_process /
       run_web_template / write_playwright_config / ensure_test_package /
       run_playwright_tests_with_progress / parse_playwright_results / main
  3. local_submit.py 的 runner-spec.json 结构与回读产物:
       template/.arc/stdout.log、agent-execution.json(duration_seconds)、
       playwright-report.json、execution.debug.log、local-run.json。

与生产 Runner 的差异(已知且有意为之):缺少 traceability 的平台侧上报细节;
部署失败等错误文案按 README 引文逐字复刻,其余日志措辞可能与生产略有出入。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("ARCBENCH_WORKSPACE", "/workspace")).resolve()
SPEC_PATH = WORKSPACE / "runner-spec.json"
PROJECT_DIR = WORKSPACE / "template"
SUBMISSION_DIR = WORKSPACE / "submission"
TESTS_DIR = WORKSPACE / "tests"
REQUIREMENTS_SNAPSHOT = Path("/tmp/arcbench/requirements-source")

WEB_APP_PORT = int(os.environ.get("ARCBENCH_WEB_PORT", "3000"))
WEB_APP_BASE_URL = f"http://127.0.0.1:{WEB_APP_PORT}"

APP_READY_TIMEOUT_SECONDS = 120
# Operator-tunable via the ARCBENCH_AGENT_TIMEOUT env (seconds). The old
# hard 6h ceiling silently cut a no-budget 21-batch bookstack run at batch
# 14 (2026-09-22); long tasks need headroom, so the default stays 6h but
# the sim .env raises it.
AGENT_RUN_TIMEOUT_SECONDS = int(
    os.environ.get("ARCBENCH_AGENT_TIMEOUT", "") or 6 * 60 * 60)

DEBUG_LOG_PATH = WORKSPACE / "execution.debug.log"
_DEBUG_LOCK = threading.Lock()
_EVENTS_LOCK = threading.Lock()

_APP_PROCESS: subprocess.Popen | None = None
_APP_THREADS: list[threading.Thread] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _spec_arc_dir() -> Path:
    try:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        spec = {}
    return PROJECT_DIR / str(spec.get("arc_dir") or ".arc").strip("/")


def _runner_events_path() -> Path:
    try:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        spec = {}
    rel = str(spec.get("runner_events_path") or ".arc/runner-events.jsonl")
    return PROJECT_DIR / rel


def append_debug_log(message: str) -> None:
    """Append a timestamped line to <workspace>/execution.debug.log."""
    line = f"[{_now()}] {message}\n"
    with _DEBUG_LOCK:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)


def append_runner_event(stage: str, message: str, status: str = "info") -> None:
    """Append one JSONL event to the spec's runner-events path."""
    event = {"timestamp": _now(), "stage": stage, "message": message, "status": status}
    path = _runner_events_path()
    with _EVENTS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    append_debug_log(f"event[{stage}/{status}] {message}")


def start_background_process(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdout_file: Path | None = None,
    stderr_file: Path | None = None,
    label: str = "background",
    env: dict | None = None,
):
    """Spawn a process with both streams drained into files, non-blocking.

    Returns (process, stdout_thread, stderr_thread) — the exact shape the
    local deploy.sh wrapper threads through its patched run_web_template.
    """
    append_debug_log(f"start[{label}]: {' '.join(cmd)} (cwd={cwd})")
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def drain(pipe, target: Path | None, tag: str) -> None:
        if target is None and pipe is None:
            return
        try:
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("ab") as sink:
                    for chunk in iter(pipe.readline, b""):
                        sink.write(chunk)
                        sink.flush()
            else:
                for _ in iter(pipe.readline, b""):
                    pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(
        target=drain, args=(process.stdout, Path(stdout_file) if stdout_file else None, "out"),
        name=f"{label}-stdout", daemon=True)
    stderr_thread = threading.Thread(
        target=drain, args=(process.stderr, Path(stderr_file) if stderr_file else None, "err"),
        name=f"{label}-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    _APP_THREADS.extend([stdout_thread, stderr_thread])
    return process, stdout_thread, stderr_thread


def _run_foreground(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    stdout_file: Path | None = None,
    stderr_file: Path | None = None,
    label: str,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    append_debug_log(f"run[{label}]: {' '.join(cmd)} (cwd={cwd})")
    stdout_target = Path(stdout_file) if stdout_file else None
    stderr_target = Path(stderr_file) if stderr_file else None

    def open_sink(target: Path | None):
        if target is None:
            return open(os.devnull, "wb")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.open("ab")

    with open_sink(stdout_target) as out, open_sink(stderr_target) as err:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env if env is not None else os.environ.copy(),
            stdout=out,
            stderr=err,
            timeout=timeout,
            check=False,
        )
    append_debug_log(f"run[{label}]: exit={completed.returncode}")
    return completed


def _load_spec() -> dict:
    try:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        append_debug_log(f"spec loaded: {json.dumps(spec, ensure_ascii=False)}")
        return spec
    except (OSError, json.JSONDecodeError) as exc:
        append_debug_log(f"spec load failed ({exc}); continuing with defaults")
        return {}


def _pip_install_requirements() -> int:
    append_runner_event("install_agent", "Installing agent Python dependencies")
    req = SUBMISSION_DIR / "requirements.txt"
    if not req.is_file():
        append_runner_event("install_agent", f"missing {req}", status="error")
        return 1
    if not req.read_text(encoding="utf-8").strip():
        append_debug_log("requirements.txt is empty; skipping pip install")
        append_runner_event("install_agent", "requirements.txt empty; nothing to install", status="success")
        return 0
    cmd = [sys.executable, "-m", "pip", "install", "--break-system-packages",
           "--no-cache-dir", "-r", str(req)]
    index = os.environ.get("ARCBENCH_PIP_INDEX_URL")
    if index:
        cmd += ["--index-url", index]
    trusted = os.environ.get("ARCBENCH_PIP_TRUSTED_HOST")
    if trusted:
        cmd += ["--trusted-host", trusted]
    log = _spec_arc_dir() / "pip-install.log"
    try:
        completed = _run_foreground(cmd, cwd=SUBMISSION_DIR, stdout_file=log, stderr_file=log,
                                    label="pip-install", timeout=900)
    except subprocess.TimeoutExpired:
        append_runner_event("install_agent", "pip install timed out after 900s", status="error")
        return 1
    if completed.returncode != 0:
        append_runner_event("install_agent", f"pip install failed (exit={completed.returncode}); see {log}",
                            status="error")
        return 1
    append_runner_event("install_agent", "agent dependencies installed", status="success")
    return 0


def _snapshot_requirements() -> Path | None:
    source = PROJECT_DIR / "requirements"
    if not source.is_dir():
        append_runner_event("run_agent", f"missing requirements directory: {source}", status="error")
        return None
    REQUIREMENTS_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    if REQUIREMENTS_SNAPSHOT.exists():
        shutil.rmtree(REQUIREMENTS_SNAPSHOT)
    shutil.copytree(source, REQUIREMENTS_SNAPSHOT)
    append_debug_log(f"requirements snapshot: {source} -> {REQUIREMENTS_SNAPSHOT}")
    return REQUIREMENTS_SNAPSHOT


def _run_agent() -> int:
    arc_dir = _spec_arc_dir()
    arc_dir.mkdir(parents=True, exist_ok=True)
    trace = None
    try:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        trace = PROJECT_DIR / str(spec.get("traceability_dir") or ".arc/traceability").strip("/")
    except (OSError, json.JSONDecodeError):
        trace = PROJECT_DIR / ".arc" / "traceability"
    trace.mkdir(parents=True, exist_ok=True)

    snapshot = _snapshot_requirements()
    if snapshot is None:
        return 1

    main_py = SUBMISSION_DIR / "main.py"
    if not main_py.is_file():
        append_runner_event("run_agent", f"missing agent entrypoint: {main_py}", status="error")
        return 1

    stdout_log = arc_dir / "stdout.log"
    stderr_log = arc_dir / "stderr.log"
    append_runner_event("run_agent", "Running agent", status="started")
    started = time.monotonic()
    cmd = [sys.executable, str(main_py), str(snapshot), "--output-dir", str(PROJECT_DIR)]
    try:
        completed = _run_foreground(cmd, cwd=PROJECT_DIR, stdout_file=stdout_log,
                                    stderr_file=stderr_log, label="agent",
                                    timeout=AGENT_RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        duration = round(time.monotonic() - started, 3)
        (arc_dir / "agent-execution.json").write_text(json.dumps({
            "command": cmd, "duration_seconds": duration, "exit_code": None,
            "timeout_seconds": AGENT_RUN_TIMEOUT_SECONDS, "finished_at": _now(),
        }, indent=2) + "\n", encoding="utf-8")
        append_runner_event("run_agent", f"agent timed out after {AGENT_RUN_TIMEOUT_SECONDS}s",
                            status="error")
        return 1
    duration = round(time.monotonic() - started, 3)
    (arc_dir / "agent-execution.json").write_text(json.dumps({
        "command": cmd, "duration_seconds": duration, "exit_code": completed.returncode,
        "finished_at": _now(),
    }, indent=2) + "\n", encoding="utf-8")
    append_runner_event(
        "run_agent",
        f"agent finished in {duration:.1f}s (exit={completed.returncode})",
        status="success" if completed.returncode == 0 else "error",
    )
    return 0 if completed.returncode == 0 else 1


def _wait_for_app(process: subprocess.Popen, timeout_seconds: int = APP_READY_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    append_debug_log(f"waiting for app at {WEB_APP_BASE_URL} (timeout={timeout_seconds}s)")
    while time.time() < deadline:
        if process.poll() is not None:
            append_debug_log(f"app process exited early (code={process.returncode})")
            return False
        try:
            with urllib.request.urlopen(WEB_APP_BASE_URL, timeout=2) as response:
                if response.status < 500:
                    append_debug_log(f"app ready with status={response.status}")
                    return True
        except Exception:
            pass
        time.sleep(1)
    append_debug_log("app ready wait timed out")
    return False


def run_web_template(stdout_file, stderr_file) -> dict:
    """Default deploy: standard frontend/ + backend/ (path B of README §3.3).

    local_runner.py replaces this with a deploy.sh-aware wrapper when the
    output root carries deploy.sh; the signature matches that call site.
    """
    frontend = PROJECT_DIR / "frontend"
    backend = PROJECT_DIR / "backend"
    if not ((frontend / "package.json").is_file() and (backend / "package.json").is_file()):
        message = "web template is incomplete: expected frontend/ and backend/ directories"
        append_runner_event("run_tests", message, status="error")
        raise RuntimeError(message)

    npm_env = os.environ.copy()
    npm_env.setdefault("npm_config_registry", os.environ.get("NPM_CONFIG_REGISTRY", ""))
    for rel in ("frontend", "backend"):
        append_runner_event("run_tests", f"npm install in {rel}/", status="started")
        completed = _run_foreground(
            ["npm", "install", "--include=optional", "--no-audit", "--no-fund"],
            cwd=PROJECT_DIR / rel, env=npm_env,
            stdout_file=Path(stdout_file) if stdout_file else None,
            stderr_file=Path(stderr_file) if stderr_file else None,
            label=f"npm-install-{rel}", timeout=1800)
        if completed.returncode != 0:
            message = f"npm install failed in {rel}/ (exit={completed.returncode})"
            append_runner_event("run_tests", message, status="error")
            raise RuntimeError(message)

    append_runner_event("run_tests", "npm run build in frontend/", status="started")
    completed = _run_foreground(
        ["npm", "run", "build"], cwd=frontend, env=npm_env,
        stdout_file=Path(stdout_file) if stdout_file else None,
        stderr_file=Path(stderr_file) if stderr_file else None,
        label="npm-build-frontend", timeout=1800)
    if completed.returncode != 0:
        message = f"frontend build failed (exit={completed.returncode})"
        append_runner_event("run_tests", message, status="error")
        raise RuntimeError(message)

    append_runner_event("run_tests", "Starting application (npm run start in backend/)",
                        status="started")
    app_env = npm_env.copy()
    app_env["HOST"] = "0.0.0.0"
    app_env["PORT"] = str(WEB_APP_PORT)
    app_env["ARCBENCH_WEB_BASE_URL"] = WEB_APP_BASE_URL
    process, out_thread, err_thread = start_background_process(
        ["npm", "run", "start"], cwd=backend, stdout_file=stdout_file,
        stderr_file=stderr_file, label="template-app", env=app_env)
    global _APP_PROCESS
    _APP_PROCESS = process
    if not _wait_for_app(process):
        raise RuntimeError(
            f"application did not become reachable at {WEB_APP_BASE_URL} "
            f"within {APP_READY_TIMEOUT_SECONDS}s")
    append_runner_event("run_tests",
                        f"Template application is reachable on {WEB_APP_BASE_URL}",
                        status="success")
    return {
        "app_process": process,
        "app_stdout_thread": out_thread,
        "app_stderr_thread": err_thread,
        "base_url": WEB_APP_BASE_URL,
    }


def write_playwright_config(base_url: str) -> None:
    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _spec_arc_dir() / "playwright-report.json"
    config = f"""\
import {{ defineConfig }} from '@playwright/test';

export default defineConfig({{
  testDir: '.',
  timeout: 10000,
  expect: {{ timeout: 10000 }},
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['json', {{ outputFile: '{report_path}' }}]],
  use: {{
    baseURL: '{base_url}',
    headless: true,
    screenshot: 'off',
    trace: 'off',
  }},
  projects: [{{ name: 'chromium', use: {{ browserName: 'chromium' }} }}],
}});
"""
    (TESTS_DIR / "playwright.config.ts").write_text(config, encoding="utf-8")
    append_debug_log(f"playwright config written for {base_url}")


GLOBAL_PW_TEST = Path("/usr/local/node/lib/node_modules/@playwright/test")


def ensure_test_package(stdout_file, stderr_file) -> None:
    """Make `@playwright/test` resolvable from the tests dir (image ships it globally).

    直接符号链接全局包(npm link 会 reify 并 chmod 全局包文件,非 root 下
    EPERM 且徒增噪音);随后幂等 `playwright install chromium` 自愈浏览器
    缺口(已就位时秒级 no-op)。
    """
    marker = TESTS_DIR / "node_modules" / "@playwright" / "test"
    if not marker.exists():
        TESTS_DIR.mkdir(parents=True, exist_ok=True)
        manifest = TESTS_DIR / "package.json"
        if not manifest.exists():
            manifest.write_text('{\n  "name": "arcbench-tests",\n  "private": true\n}\n',
                                encoding="utf-8")
        scope = marker.parent
        scope.mkdir(parents=True, exist_ok=True)
        bin_dir = TESTS_DIR / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        try:
            marker.symlink_to(GLOBAL_PW_TEST)
            for name in ("playwright", "playwright-core"):
                bin_link = bin_dir / name
                if not bin_link.exists():
                    bin_link.symlink_to(Path("/usr/local/node/bin") / name)
            append_debug_log(f"linked {GLOBAL_PW_TEST} -> {marker}")
        except OSError as exc:
            append_debug_log(f"direct symlink failed ({exc}); falling back to npm link")
            _run_foreground(
                ["npm", "link", "@playwright/test"], cwd=TESTS_DIR,
                stdout_file=Path(stdout_file) if stdout_file else None,
                stderr_file=Path(stderr_file) if stderr_file else None,
                label="npm-link-playwright", timeout=300)
        if not marker.exists():
            raise RuntimeError("could not link @playwright/test into the tests directory")
    _run_foreground(
        ["npx", "playwright", "install", "chromium"], cwd=TESTS_DIR,
        stdout_file=Path(stdout_file) if stdout_file else None,
        stderr_file=Path(stderr_file) if stderr_file else None,
        label="playwright-install-chromium", timeout=900)


def run_playwright_tests_with_progress(stdout_file, stderr_file) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["E2E_BASE_URL"] = WEB_APP_BASE_URL
    env["PLAYWRIGHT_BASE_URL"] = WEB_APP_BASE_URL
    env["ARC_WEB_BASE_URL"] = WEB_APP_BASE_URL
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")
    append_debug_log(
        "playwright env:"
        f" PLAYWRIGHT_BROWSERS_PATH={env.get('PLAYWRIGHT_BROWSERS_PATH')}"
        f" pwd={TESTS_DIR}")

    list_result = _run_foreground(
        ["npx", "playwright", "test", "--list"], cwd=TESTS_DIR, env=env,
        stdout_file=Path(stdout_file) if stdout_file else None,
        stderr_file=Path(stderr_file) if stderr_file else None,
        label="playwright-list", timeout=300)
    if list_result.returncode != 0:
        append_runner_event("run_tests", "playwright test --list failed", status="error")
        return list_result

    append_runner_event("run_tests", "Running Playwright suite (workers=1)", status="started")
    return _run_foreground(
        ["npx", "playwright", "test", "--workers=1"], cwd=TESTS_DIR, env=env,
        stdout_file=Path(stdout_file) if stdout_file else None,
        stderr_file=Path(stderr_file) if stderr_file else None,
        label="playwright-run", timeout=3600)


def parse_playwright_results() -> dict:
    report_path = _spec_arc_dir() / "playwright-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        append_runner_event("run_tests", f"playwright report unreadable: {exc}", status="error")
        return {"passed": 0, "failed": 0, "score": 0.0, "duration_seconds": 0.0,
                "tests": [], "evaluation_status": "failed"}

    tests: list[dict] = []

    def visit(suite: dict, inherited_file: str | None = None) -> None:
        suite_file = str(suite.get("file") or inherited_file or "")
        for spec in suite.get("specs", []) or []:
            title = str(spec.get("title") or "Unnamed test")
            for test in spec.get("tests", []) or []:
                results = test.get("results", []) or []
                statuses = [str(item.get("status") or "") for item in results]
                if any(s in {"failed", "timedOut", "interrupted"} for s in statuses):
                    passed = False
                elif "passed" in statuses:
                    passed = True
                elif "skipped" in statuses:
                    passed = False
                else:
                    passed = str(test.get("status") or "") == "expected"
                tests.append({
                    "file": suite_file,
                    "title": title,
                    "passed": passed,
                    "statuses": statuses,
                    "duration_ms": sum(int(item.get("duration") or 0) for item in results),
                })
        for child in suite.get("suites", []) or []:
            visit(child, suite_file)

    for suite in report.get("suites", []) or []:
        visit(suite)

    passed = sum(1 for t in tests if t["passed"])
    failed = len(tests) - passed
    duration = round(sum(t["duration_ms"] for t in tests) / 1000, 3)
    summary = {
        "passed": passed,
        "failed": failed,
        "total": len(tests),
        "score": round((passed / len(tests)) * 100, 1) if tests else 0.0,
        "duration_seconds": duration,
        "tests": tests,
        "evaluation_status": "completed",
    }
    append_runner_event(
        "run_tests",
        f"Playwright finished: {passed}/{len(tests)} passed",
        status="success" if tests else "error",
    )
    return summary


def _terminate_app() -> None:
    global _APP_PROCESS
    if _APP_PROCESS is None:
        return
    if _APP_PROCESS.poll() is None:
        append_debug_log("terminating application process")
        _APP_PROCESS.terminate()
        try:
            _APP_PROCESS.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _APP_PROCESS.kill()
    for thread in _APP_THREADS:
        thread.join(timeout=5)
    _APP_PROCESS = None
    _APP_THREADS.clear()


def main() -> int:
    started = time.monotonic()
    # 容器以宿主 uid 运行且 HOME 指向 /tmp/arcbench-home:先保证它存在,
    # pip/npm/git 才有可写 HOME。
    Path(os.environ.get("HOME") or "/tmp/arcbench-home").mkdir(parents=True, exist_ok=True)
    Path(os.environ.get("npm_config_cache") or "/tmp/arcbench-npm-cache").mkdir(
        parents=True, exist_ok=True)
    append_debug_log(f"=== run_submission start ({_now()}) ===")
    spec = _load_spec()
    evaluation_enabled = bool(spec.get("evaluation_enabled", True))
    append_runner_event("run_submission", "Runner started")

    try:
        if not SUBMISSION_DIR.is_dir():
            append_runner_event("run_submission",
                                f"missing submission directory: {SUBMISSION_DIR}", status="error")
            return 1
        if _pip_install_requirements() != 0:
            return 1
        if _run_agent() != 0:
            append_debug_log("agent stage failed; stopping before deploy")
            return 1

        arc_dir = _spec_arc_dir()
        arc_dir.mkdir(parents=True, exist_ok=True)
        deploy_out = arc_dir / "deploy-stdout.log"
        deploy_err = arc_dir / "deploy-stderr.log"
        try:
            run_web_template(deploy_out, deploy_err)
        except RuntimeError as exc:
            append_debug_log(f"deploy failed: {exc}")
            return 1

        if evaluation_enabled:
            write_playwright_config(WEB_APP_BASE_URL)
            try:
                ensure_test_package(deploy_out, deploy_err)
            except RuntimeError as exc:
                append_debug_log(f"test package setup failed: {exc}")
                return 1
            completed = run_playwright_tests_with_progress(deploy_out, deploy_err)
            if not (_spec_arc_dir() / "playwright-report.json").is_file():
                append_debug_log(f"playwright run produced no report (exit={completed.returncode})")
                append_runner_event("run_tests", "no Playwright report was produced", status="error")
                return 1
            parse_playwright_results()
        else:
            append_runner_event("run_tests", "evaluation disabled by spec; skipping Playwright",
                                status="info")

        append_runner_event(
            "run_submission",
            f"Runner finished in {time.monotonic() - started:.1f}s",
            status="success",
        )
        return 0
    finally:
        _terminate_app()


if __name__ == "__main__":
    raise SystemExit(main())
