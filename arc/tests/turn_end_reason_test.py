"""Tests for kernel-authored terminal detection and honest batch verdicts.

A kernel governance kill (doom loop) reaches the host as ok=True + a
synthesized final assistant row persisted in the session — invisible in the
message/delta stream the host's `text` is built from. These tests pin:
marker matching, turn-end attribution, the done/blocked batch verdict, and
the stdio hydrate fetch against a fake `octos serve --stdio` process.

Run: python3 arc/tests/turn_end_reason_test.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from octos_stdio import OctosStdioSession  # noqa: E402

# Real-world sample: loop_runner.rs doom_loop_terminal_message, as fired in
# the 2026-10-01 github run (streak=4, `bash` sed fixed point).
DOOM_ROW = ("[DOOM LOOP DETECTED] The `bash` tool was called 4 times in a "
            "row with identical arguments, so this turn was stopped before "
            "issuing another model call. Repeating the exact same call "
            "cannot produce a different result. Try a different approach — "
            "vary the arguments, use a different tool, or rephrase the "
            "request.")
MODEL_TEXT = ("I have a complete understanding. Let me start implementing. "
              "First, let me create the seed data and seed-accounts.json.")


class KernelTerminalMarkerTests(unittest.TestCase):
    def test_doom_row_matches(self):
        self.assertEqual(main.kernel_terminal_marker(DOOM_ROW), "doom loop")

    def test_leading_whitespace_tolerated(self):
        self.assertEqual(main.kernel_terminal_marker("\n  " + DOOM_ROW),
                         "doom loop")

    def test_none_and_empty_are_no_marker(self):
        self.assertIsNone(main.kernel_terminal_marker(None))
        self.assertIsNone(main.kernel_terminal_marker(""))

    def test_model_text_is_no_marker(self):
        self.assertIsNone(main.kernel_terminal_marker(MODEL_TEXT))

    def test_loop_thrash_row_matches(self):
        # 10-03 正式赛 REQ-5 真凶:警告注入后复发杀(loop_runner.rs
        # loop_detected_terminal_message),此前词表漏判 → 假 done →
        # 重入+修复轮全没触发。前缀长于 "[LOOP DETECTED]" 以避开同词头
        # 的中途警告行。
        row = ("[LOOP DETECTED] The agent kept calling the same tool "
               "with the same arguments even after a warning was "
               "injected. Stopping the turn to avoid a thrash. Please "
               "rephrase your request or try a different angle.")
        self.assertEqual(main.kernel_terminal_marker(row), "loop thrash")
        self.assertEqual(main.kernel_terminal_marker("\n " + row),
                         "loop thrash")

    def test_cycle_warning_rows_are_not_markers(self):
        # 同 "[LOOP DETECTED]" 词头的中途警告行(工具结果,非终局):
        # 短前缀会把它们误当切断——钉住长前缀纪律。
        for row in ("[LOOP DETECTED] The last 6 tool calls follow a "
                    "repeating pattern (cycle length 2). Try a different "
                    "approach or break the cycle.",
                    "[LOOP DETECTED] (companion call in the same batch; "
                    "see paired result for the warning)."):
            self.assertIsNone(main.kernel_terminal_marker(row), row)

    def test_budget_kills_are_tabled_without_reentry(self):
        # 预算/终止类切断(budget.rs / retry 耗尽 / 审查):记账面全覆盖
        # (不再假 done),重入面一律 None(与墙钟/空闲同则)。
        cases = (
            ("Token budget exceeded (120000 of 131072).", "token budget"),
            ("Activity timeout (900s limit).", "activity timeout"),
            ("Idle progress timeout (300s without progress).",
             "idle progress timeout"),
            ("The agent did not complete within 200 iterations. "
             "The task may be too broad for a single turn.",
             "max iterations"),
            ("The requested operation already exhausted its internal "
             "retries. I stopped a repeated 'bash' call in this turn so "
             "you do not have to wait for the same work again.",
             "tool retry exhausted"),
            ("[Content was blocked by the model's safety filter. "
             "Please rephrase your request.]", "content filter"),
            ("[HOOK DENIED] Prompt was blocked by a user_prompt_submit "
             "hook.", "hook denied"),
        )
        for row, label in cases:
            self.assertEqual(main.kernel_terminal_marker(row), label, row)
            self.assertIsNone(main.reentry_reason(MODEL_TEXT, row), row)
            self.assertEqual(main._batch_state(True, label), "blocked")
            self.assertEqual(main._turn_end_reason(True, MODEL_TEXT, row),
                             f"kernel cut: {label}")

    def test_green_stop_stays_benign(self):
        # [green stop] 是设计内良性收尾(内核明说宿主按正常完成处理,
        # 自测门兜底):永不入切断词表,恒按模型自选收束。
        row = ("[green stop] The staged acceptance spec passed identically "
               "twice with no source changes between the runs; the turn "
               "ended after the wrap-up window to bank the verified "
               "result. The requirement under work is considered complete.")
        self.assertIsNone(main.kernel_terminal_marker(row))
        self.assertEqual(main._turn_end_reason(True, row, row),
                         "model end_turn")

    def test_shell_spiral_marker_matches(self):
        # 2026-10-02 sheet 85s 误杀:spiral 终局以模型文本返回无标记,
        # 内核 ④ 在终局内容前加标记行,宿主词表对表。
        row = ("[SHELL SPIRAL DETECTED] This turn was stopped because "
               "repeated shell approaches did not converge.\n\n"
               "I tried multiple shell approaches but couldn't converge "
               "on an answer. Latest output:\n\n...")
        self.assertEqual(main.kernel_terminal_marker(row), "shell spiral")
        self.assertEqual(main.reentry_reason("...", row),
                         "aborted by the kernel shell spiral guard")

    def test_marker_only_matches_as_prefix(self):
        self.assertIsNone(main.kernel_terminal_marker(
            "quoted: [DOOM LOOP DETECTED] is what the kernel says"))


class TurnEndReasonTests(unittest.TestCase):
    def test_plain_ok_is_model_end_turn(self):
        self.assertEqual(main._turn_end_reason(True, MODEL_TEXT),
                         "model end_turn")

    def test_ok_with_doom_row_is_kernel_cut(self):
        self.assertEqual(main._turn_end_reason(True, MODEL_TEXT, DOOM_ROW),
                         "kernel cut: doom loop")

    def test_ok_without_terminal_is_model_end_turn(self):
        self.assertEqual(main._turn_end_reason(True, MODEL_TEXT, None),
                         "model end_turn")

    def test_failure_categories_unchanged(self):
        self.assertEqual(
            main._turn_end_reason(False, "hard wall clock exceeded"),
            "host cut: wall clock exceeded")
        self.assertEqual(
            main._turn_end_reason(False, "idle timeout (no protocol events)"),
            "host cut: idle timeout (no protocol events)")
        self.assertEqual(
            main._turn_end_reason(False, "octos process exited 1; stderr..."),
            "kernel process exited")
        self.assertEqual(main._turn_end_reason(False, "weird failure"),
                         "error: weird failure")


class BatchStateTests(unittest.TestCase):
    def test_done_only_for_model_chosen_end(self):
        self.assertEqual(main._batch_state(True, None), "done")

    def test_kernel_cut_is_blocked_even_when_ok(self):
        self.assertEqual(main._batch_state(True, "doom loop"), "blocked")

    def test_failed_turn_is_blocked(self):
        self.assertEqual(main._batch_state(False, None), "blocked")
        self.assertEqual(main._batch_state(False, "doom loop"), "blocked")


DOOM_REASON = "aborted by the kernel doom loop guard"
TRUNCATED_TEXT = ("output_truncated: Model output was truncated "
                  "(max_tokens); the response is incomplete")
TRUNCATED_REASON = "cut off by the output length limit mid-response"


class ReentryReasonTests(unittest.TestCase):
    def test_doom_terminal_reentry(self):
        self.assertEqual(main.reentry_reason(MODEL_TEXT, DOOM_ROW),
                         DOOM_REASON)

    def test_loop_thrash_terminal_reentry(self):
        # 治理切断新成员:可重入(A 级),再切触发 C 级修复轮。
        row = ("[LOOP DETECTED] The agent kept calling the same tool "
               "with the same arguments even after a warning was "
               "injected. Stopping the turn to avoid a thrash. Please "
               "rephrase your request or try a different angle.")
        self.assertEqual(main.reentry_reason(MODEL_TEXT, row),
                         "aborted by the kernel loop thrash guard")

    def test_output_truncated_reentry(self):
        self.assertEqual(main.reentry_reason(TRUNCATED_TEXT, None),
                         TRUNCATED_REASON)

    def test_environment_failures_do_not_reenter(self):
        for text in ("octos turn hard wall clock exceeded",
                     "octos turn idle timeout (no protocol events)",
                     "octos process exited 1; stderr tail: ...",
                     "some other error"):
            self.assertIsNone(main.reentry_reason(text, None), text)

    def test_clean_turn_does_not_reenter(self):
        self.assertIsNone(main.reentry_reason(MODEL_TEXT, None))

    def test_doom_reason_wins_over_truncated_text(self):
        self.assertEqual(main.reentry_reason(TRUNCATED_TEXT, DOOM_ROW),
                         DOOM_REASON)


def _bash_call(n: int) -> dict:
    return {"name": "bash", "arguments": {"command": f"cmd-{n}"}}


class DoomCallSignatureTests(unittest.TestCase):
    """doom 杀轮取证:会话尾部 tool 调用 → 重复签名(工具+命令原文)。"""

    def test_tail_streak_extracts_command_and_count(self):
        calls = [_bash_call(1), {"name": "read", "arguments": {"p": "x"}}]
        calls += [_bash_call(2) for _ in range(7)]
        sig = main.doom_call_signature(calls)
        self.assertEqual(sig, {"tool": "bash", "count": 7,
                               "command": "cmd-2"})

    def test_string_arguments_supported(self):
        calls = [{"name": "bash",
                  "arguments": json.dumps({"command": "npm test"})}
                 for _ in range(3)]
        self.assertEqual(main.doom_call_signature(calls)["command"],
                         "npm test")

    def test_no_streak_returns_none(self):
        self.assertIsNone(main.doom_call_signature([]))
        self.assertIsNone(main.doom_call_signature(
            [_bash_call(1), _bash_call(2)]))

    def test_command_truncated_to_400(self):
        calls = [{"name": "bash", "arguments": {"command": "x" * 500}}
                 for _ in range(2)]
        self.assertEqual(len(main.doom_call_signature(calls)["command"]),
                         400)

    def test_non_shell_arguments_rendered_empty(self):
        calls = [{"name": "read", "arguments": {"p": "x"}}
                 for _ in range(5)]
        sig = main.doom_call_signature(calls)
        self.assertEqual(sig["tool"], "read")
        self.assertEqual(sig["count"], 5)
        self.assertEqual(sig["command"], "")

    def test_context_log_cmd_key_extracted(self):
        # 内核 context-log tools.jsonl 的 bash 行用 cmd 键(hydrate 用
        # command)——两条数据源都要能点名命令原文。
        calls = [{"name": "bash", "arguments": {"cmd": "grep -n x f.ts"}}
                 for _ in range(3)]
        self.assertEqual(main.doom_call_signature(calls)["command"],
                         "grep -n x f.ts")

    def test_path_fallback_names_read_loops(self):
        # read_file 死循环(10-03 REQ-3 双杀形态)的取证点名文件路径。
        calls = [{"name": "read_file", "arguments": {"path": "a.jsx"}}
                 for _ in range(4)]
        sig = main.doom_call_signature(calls)
        self.assertEqual(sig["tool"], "read_file")
        self.assertEqual(sig["command"], "a.jsx")


class DoomEvidenceCaptureTests(unittest.TestCase):
    """_capture_doom_evidence:取证落 octos/doom-events.jsonl + stderr
    面板一行;绝不在取证路径上抛,无 recent_tool_calls 的驱动降级。"""

    def _run(self, tmp, tool_calls, kernel_terminal=DOOM_ROW,
             with_tool_method=True):
        logs = []
        driver = type("D", (), {"kernel_terminal": kernel_terminal})()
        if with_tool_method:
            driver.recent_tool_calls = lambda: tool_calls
        sig = main._capture_doom_evidence(Path(tmp), "REQ-6-2-1", driver,
                                          logs.append)
        events = Path(tmp) / "octos" / "doom-events.jsonl"
        lines = (events.read_text().splitlines()
                 if events.exists() else [])
        return logs, [json.loads(x) for x in lines], sig

    def test_capture_writes_event_and_logs_summary(self):
        calls = [_bash_call(1)] + [_bash_call(2) for _ in range(7)]
        with tempfile.TemporaryDirectory() as tmp:
            logs, records, sig = self._run(tmp, calls)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["node"], "REQ-6-2-1")
        self.assertEqual(records[0]["tool"], "bash")
        self.assertEqual(records[0]["count"], 7)
        self.assertEqual(records[0]["command"], "cmd-2")
        self.assertEqual(records[0]["reason"], DOOM_ROW)
        self.assertTrue(any("[doom] REQ-6-2-1: bash x7" in x
                            for x in logs), logs)
        # 返回签名供 C 级修复工单点名禁令。
        self.assertEqual(sig, {"tool": "bash", "count": 7,
                               "command": "cmd-2"})

    def test_no_terminal_no_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, records, sig = self._run(tmp, [_bash_call(1)],
                                           kernel_terminal=None)
        self.assertEqual(records, [])
        self.assertEqual(logs, [])
        self.assertIsNone(sig)

    def test_degrades_without_recent_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, records, sig = self._run(tmp, [], with_tool_method=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].get("tool"), None)
        self.assertTrue(any("no repeated-call signature" in x
                            for x in logs), logs)
        self.assertIsNone(sig)

    def test_reason_override_captures_unmarked_spiral(self):
        # 10-08 兜底:无标记 ok 截断(shell-spiral 收尾文本无标记,
        # kernel_terminal=None 词表够不到)由懒门尾部连扫触发,同一
        # 取证通道走 reason_override——记账可区分"确认内核杀"与
        # "疑似未标记 spiral"。
        calls = [_bash_call(1)] + [_bash_call(2) for _ in range(6)]
        logs = []
        driver = type("D", (), {"kernel_terminal": None})()
        driver.recent_tool_calls = lambda: calls
        with tempfile.TemporaryDirectory() as tmp:
            sig = main._capture_doom_evidence(
                Path(tmp), "REQ-5-4", driver, logs.append,
                reason_override="suspected unmarked spiral")
            events = Path(tmp) / "octos" / "doom-events.jsonl"
            records = [json.loads(x)
                       for x in events.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason"], "suspected unmarked spiral")
        self.assertEqual(records[0]["count"], 6)
        self.assertEqual(sig["command"], "cmd-2")
        self.assertTrue(any("[doom] REQ-5-4: bash x6" in x for x in logs),
                        logs)


class RecentToolCallsTests(unittest.TestCase):
    """OctosDriver.recent_tool_calls:从内核 context-log
    (<cwd>/.octos/context-logs/*/tools.jsonl)尾部取回调用签名——
    hydrate 消息投影不带 tool_calls 字段(10-03 平台跑 8/8 取证落空
    的根因),context-log 是逐调用落盘的权威面。"""

    def _driver(self, cwd: Path) -> main.OctosDriver:
        return main.OctosDriver(
            "octos-bin-stub", cwd, {}, cwd / "data", 100,
            cwd / "events" / "log.jsonl")

    def _write_session(self, cwd: Path, name: str, rows: list,
                       mtime: float | None = None) -> None:
        d = cwd / ".octos" / "context-logs" / name
        d.mkdir(parents=True, exist_ok=True)
        # str 行原样写入(坏行夹具),dict 行序列化写入。
        text = "".join(
            (r if isinstance(r, str)
             else json.dumps(r, ensure_ascii=False)) + "\n"
            for r in rows)
        f = d / "tools.jsonl"
        f.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(f, (mtime, mtime))

    def test_maps_tail_rows_and_newest_session_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._write_session(cwd, "20261002-1346", [
                {"kind": "tool_call", "tool": "bash",
                 "args": {"cmd": "old-session-call"}},
            ], mtime=1_000_000)
            self._write_session(cwd, "20261002-1404", [
                {"kind": "tool_call", "tool": "bash",
                 "args": {"cmd": "grep -n x e2e/req2.spec.ts"}},
                {"kind": "tool_call", "tool": "read_file",
                 "args": {"path": "a.jsx"}},
                {"kind": "other", "tool": "noise", "args": {}},
                "{broken json",
                {"kind": "tool_call", "tool": "bash",
                 "args": {"cmd": "npm run build"}},
            ], mtime=2_000_000)
            calls = self._driver(cwd).recent_tool_calls()
        self.assertEqual(calls, [
            {"name": "bash",
             "arguments": {"cmd": "grep -n x e2e/req2.spec.ts"}},
            {"name": "read_file", "arguments": {"path": "a.jsx"}},
            {"name": "bash", "arguments": {"cmd": "npm run build"}},
        ])

    def test_limit_returns_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._write_session(cwd, "s1", [
                {"kind": "tool_call", "tool": "bash",
                 "args": {"cmd": f"c{i}"}} for i in range(5)
            ])
            calls = self._driver(cwd).recent_tool_calls(limit=2)
        self.assertEqual([c["arguments"]["cmd"] for c in calls],
                         ["c3", "c4"])

    def test_missing_context_logs_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._driver(Path(tmp)).recent_tool_calls(),
                             [])


class FreshSessionRepairPromptTests(unittest.TestCase):
    """C 级修复工单:新会话先盘点→禁重写→点名被杀命令禁令,后接
    原批工单正文(完成锚保持最近位置)。"""

    CORE = ("TASK: implement the requirements below (REQ-3-1-1).\n\n"
            "End the turn only when this batch's scenarios walk green.")

    def test_prompt_surveys_bans_command_and_keeps_core(self):
        prompt = main.fresh_session_repair_prompt(
            self.CORE, DOOM_REASON,
            "grep -n switchToSheet e2e/req2.spec.ts")
        self.assertTrue(prompt.startswith("FRESH RESTART:"))
        self.assertIn(DOOM_REASON, prompt)
        self.assertIn("Survey the current state first", prompt)
        self.assertIn("do NOT re-implement or rewrite", prompt)
        self.assertIn("repeating this exact call with identical "
                      "arguments: `grep -n switchToSheet e2e/req2.spec.ts`",
                      prompt)
        self.assertIn("Never run it again unchanged", prompt)
        self.assertIn("smaller edits", prompt)
        self.assertTrue(prompt.endswith(self.CORE))

    def test_no_command_omits_ban_line(self):
        prompt = main.fresh_session_repair_prompt(self.CORE,
                                                  TRUNCATED_REASON, None)
        self.assertIn(TRUNCATED_REASON, prompt)
        self.assertNotIn("Never run it again unchanged", prompt)
        self.assertTrue(prompt.endswith(self.CORE))


class TurnReentryPromptTests(unittest.TestCase):
    def test_prompt_embeds_reason_and_keeps_core_verbatim(self):
        core = ("TASK: implement the requirements below (REQ-1-1-1).\n\n"
                "SCENARIOS (verbatim):\n...\n\n"
                "End the turn only when this batch's scenarios walk green.")
        prompt = main.turn_reentry_prompt(core, TRUNCATED_REASON)
        self.assertTrue(prompt.startswith(
            "CONTINUATION: your previous turn was " + TRUNCATED_REASON))
        self.assertIn("smaller edits", prompt)
        self.assertTrue(prompt.endswith(core))

    def test_prompt_carries_doom_reason(self):
        prompt = main.turn_reentry_prompt("TASK: x", DOOM_REASON)
        self.assertIn(DOOM_REASON, prompt)
        self.assertTrue(prompt.endswith("TASK: x"))


class _FakeKernel:
    """NDJSON `octos serve --stdio` stub: answers session/open, turn/start
    and session/hydrate; streams two deltas then turn/completed."""

    def __init__(self, root: Path, answer_hydrate: bool = True):
        self.script = root / "fake-octos"
        self.answer_hydrate = answer_hydrate
        body = r'''
import json, os, sys

answer_hydrate = os.environ.get("FAKE_ANSWER_HYDRATE", "1") == "1"
turn_id = None
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    frame = json.loads(raw)
    rid = frame.get("id")
    method = frame.get("method", "")
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    if rid is not None:
        if method == "turn/start":
            turn_id = (frame.get("params") or {}).get("turn_id")
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
            for piece in ("hello ", "world"):
                send({"jsonrpc": "2.0", "method": "message/delta",
                      "params": {"turn_id": turn_id, "text": piece}})
            send({"jsonrpc": "2.0", "method": "turn/completed",
                  "params": {"turn_id": turn_id,
                             "session_result": {"message_id": "m3"}}})
        elif method == "session/hydrate":
            if answer_hydrate:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "session_id": "s", "cursor": "c",
                    "messages": [
                        {"seq": 1, "role": "user", "content": "work order"},
                        {"seq": 2, "role": "assistant",
                         "content": os.environ.get("FAKE_MODEL_TEXT", "")},
                        {"seq": 3, "role": "assistant",
                         "content": os.environ.get("FAKE_DOOM_ROW", "")},
                    ]}})
            # answer_hydrate=False: never respond (degradation path).
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''
        self.script.write_text("#!/usr/bin/env python3\n" + body,
                               encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC
                          | stat.S_IXGRP | stat.S_IXOTH)


class StdioHydrateFetchTests(unittest.TestCase):
    def _session(self, root: Path, answer_hydrate: bool,
                 hydrate_timeout: float) -> OctosStdioSession:
        fake = _FakeKernel(root, answer_hydrate)
        env = os.environ.copy()
        env.pop("OCTOS_DANGER_FULL_ACCESS", None)
        env["FAKE_MODEL_TEXT"] = MODEL_TEXT
        env["FAKE_DOOM_ROW"] = DOOM_ROW
        if not answer_hydrate:
            env["FAKE_ANSWER_HYDRATE"] = "0"
        return OctosStdioSession(str(fake.script), root, env, root,
                                 hydrate_timeout=hydrate_timeout)

    def test_final_assistant_row_fetched_after_completed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = self._session(root, answer_hydrate=True,
                                    hydrate_timeout=10.0)
            try:
                session.open()
                ok, text = session.run_turn("work order", timeout=30.0)
            finally:
                session.close()
        self.assertTrue(ok)
        self.assertEqual(text, "hello world")
        self.assertEqual(session.last_final_assistant, DOOM_ROW)
        self.assertEqual(main.kernel_terminal_marker(
            session.last_final_assistant), "doom loop")
        self.assertEqual(
            main._turn_end_reason(True, text, session.last_final_assistant),
            "kernel cut: doom loop")

    def test_hydrate_failure_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = self._session(root, answer_hydrate=False,
                                    hydrate_timeout=0.5)
            try:
                session.open()
                ok, text = session.run_turn("work order", timeout=30.0)
            finally:
                session.close()
        self.assertTrue(ok)
        self.assertEqual(text, "hello world")
        self.assertIsNone(session.last_final_assistant)
        self.assertEqual(main._turn_end_reason(True, text, None),
                         "model end_turn")


class TurnLimitInjectionTests(unittest.TestCase):
    """_inject_turn_limits: registry JSON 双旋钮(iterations+max_output_tokens)。"""

    def _session(self, root: Path):
        session = OctosStdioSession.__new__(OctosStdioSession)
        session.data_dir = root
        session.profile_id = "p1"
        session._stderr_lines = []
        return session

    def _registry(self, root: Path) -> Path:
        return root / "profiles" / "p1.json"

    def _write_registry(self, root: Path, config: dict) -> None:
        reg = self._registry(root)
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"config": config}), encoding="utf-8")

    def _gateway(self, root: Path) -> dict:
        return json.loads(self._registry(root).read_text())["config"]["gateway"]

    def test_defaults_inject_both_caps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, {})
            self._session(root)._inject_turn_limits()
            gw = self._gateway(root)
        self.assertEqual(gw["max_iterations"], 100)
        self.assertEqual(gw["max_output_tokens"], 131072)

    def test_env_overrides_output_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, {})
            old = os.environ.get("OCTOS_MAX_OUTPUT_TOKENS")
            os.environ["OCTOS_MAX_OUTPUT_TOKENS"] = "65536"
            try:
                self._session(root)._inject_turn_limits()
            finally:
                if old is None:
                    os.environ.pop("OCTOS_MAX_OUTPUT_TOKENS", None)
                else:
                    os.environ["OCTOS_MAX_OUTPUT_TOKENS"] = old
            self.assertEqual(self._gateway(root)["max_output_tokens"], 65536)

    def test_zero_leaves_kernel_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, {})
            old = os.environ.get("OCTOS_MAX_OUTPUT_TOKENS")
            os.environ["OCTOS_MAX_OUTPUT_TOKENS"] = "0"
            try:
                self._session(root)._inject_turn_limits()
            finally:
                if old is None:
                    os.environ.pop("OCTOS_MAX_OUTPUT_TOKENS", None)
                else:
                    os.environ["OCTOS_MAX_OUTPUT_TOKENS"] = old
            self.assertNotIn("max_output_tokens", self._gateway(root))

    def test_injection_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, {})
            session = self._session(root)
            session._inject_turn_limits()
            before = self._registry(root).read_text()
            session._inject_turn_limits()
            self.assertEqual(self._registry(root).read_text(), before)

    def test_operator_set_value_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_registry(root, {"gateway": {"max_output_tokens": 4096}})
            self._session(root)._inject_turn_limits()
            self.assertEqual(self._gateway(root)["max_output_tokens"], 4096)


class ZeroOutputReentryPromptTests(unittest.TestCase):
    def test_prefix_demands_immediate_writing_and_keeps_core(self):
        core = ("TASK: implement the requirements below (REQ-1-1-1).\n\n"
                "End the turn only when this batch's scenarios walk green.")
        prompt = main.zero_output_reentry_prompt(core)
        self.assertTrue(prompt.startswith(
            "CONTINUATION: your previous turn ended without changing any "
            "project file"))
        self.assertIn("must create or edit a project file", prompt)
        self.assertTrue(prompt.endswith(core))


class ProjectFilesChangedTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        (root / "backend" / "src").mkdir(parents=True)
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "e2e").mkdir()
        return root

    def test_real_source_write_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._workspace(Path(td))
            src = root / "backend" / "src" / "routes.js"
            src.write_text("x")
            os.utime(src, (time.time() + 10, time.time() + 10))
            self.assertTrue(main.project_files_changed(root, time.time()))

    def test_package_lock_whitewash_excluded(self):
        # 02:42 跑实证:prewarm npm install 落在 1s slack 窗口改写
        # lockfile,把零工作轮洗白成"有变化"。
        with tempfile.TemporaryDirectory() as td:
            root = self._workspace(Path(td))
            lock = root / "frontend" / "package-lock.json"
            lock.write_text("{}")
            os.utime(lock, (time.time() + 10, time.time() + 10))
            self.assertFalse(main.project_files_changed(root, time.time()))

    def test_stale_source_only_does_not_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._workspace(Path(td))
            src = root / "backend" / "src" / "app.js"
            src.write_text("x")
            os.utime(src, (time.time() - 3600, time.time() - 3600))
            self.assertFalse(main.project_files_changed(root, time.time()))

    def test_host_dirs_never_scanned(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._workspace(Path(td))
            for rel in ("octos/logs/host.log", ".arc/debug.log"):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("noise")
                os.utime(p, (time.time() + 10, time.time() + 10))
            self.assertFalse(main.project_files_changed(root, time.time()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
