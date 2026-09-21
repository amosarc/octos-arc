#!/usr/bin/env python3
"""Base-image smoke test run by build scripts (`docker run ... /opt/arcbench/smoke_test.py`).

Verifies the pieces the ARC-Bench environment table promises: Python, Node,
Git, Playwright (JS + Python) and a working Chromium.
"""

from __future__ import annotations

import subprocess
import sys


def check(label: str, cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    detail = (proc.stdout + proc.stderr).strip().splitlines()
    summary = detail[0] if detail else ""
    if proc.returncode != 0:
        print(f"[smoke] FAIL {label}: exit={proc.returncode} {summary}")
        raise SystemExit(1)
    print(f"[smoke] ok   {label}: {summary}")


def main() -> int:
    check("python3", ["python3", "--version"])
    check("node", ["node", "--version"])
    check("npm", ["npm", "--version"])
    check("git", ["git", "--version"])
    check("lsof", ["lsof", "-v"])
    check("curl", ["curl", "--version"])
    check("playwright-js", ["npx", "--no-install", "playwright", "--version"])
    check("playwright-py", ["python3", "-m", "playwright", "--version"])

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h1>arcbench-smoke</h1>")
        if "arcbench-smoke" not in page.content():
            print("[smoke] FAIL chromium: page content mismatch")
            return 1
        browser.close()
    print("[smoke] ok   chromium: launched and rendered")
    print("[smoke] all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
