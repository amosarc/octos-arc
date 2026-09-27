"""schema.json → database/schema.js 编译器(纯标准库)。

信号驱动:backend/data/schema.json 落盘/变更即重编译(轮中 watch 线程
+ 轮末兜底 sync,共用一把锁)。产物是纯数据模块,含每实体 /api/<entity>
路由字面量——tree-sitter repomap 与内核字面量层可见;.json 不在内核扫描
范围,故必须编译成 .js。任何 schema 违规静默放弃(永不阻塞做题)。

09-27 C1:编译时读 .arc/host/req-messages.json(main.py 从需求原文提取
的引号消息清单)做确定性桥接——"<X> is required"/"<X> already exists"
按实体/字段名归一匹配灌进 field.messages(模型已写的 message/messages
永不覆盖);credential 族串进顶层 messages.loginFailure。宿主生成 API
的错误面由此以需求逐字串优先、英文契约族兜底。

路径一律取自 scaffold.manifest(单一事实源)。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .manifest import CODEGEN_STATE_REL as STATE_REL
from .manifest import REQ_MESSAGES_REL
from .manifest import SCHEMA_JSON_REL as SCHEMA_JSON
from .manifest import SCHEMA_JS_REL as SCHEMA_JS

TYPES = {"string", "number", "boolean", "password"}
FORMATS = {"email", "username", "password"}
MSG_KINDS = {"required", "invalid", "duplicate", "match"}
_MSG_SUFFIXES = ((" is required", "required"), (" already exists", "duplicate"))
_LOGIN_FAIL_RE = re.compile(r"^(invalid|incorrect|wrong)\b.*"
                            r"(credential|username|password|sign[- ]?in|log[- ]?in)",
                            re.I | re.S)


def schema_path(output_dir: Path) -> Path:
    return output_dir / SCHEMA_JSON


def load_schema(output_dir: Path) -> tuple[dict | None, str]:
    """读 + 严格校验 schema.json;(None, 原因) 表示放弃编译。"""
    p = schema_path(output_dir)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        s = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid json: {exc}"
    err = _validate(s)
    return (s, None) if not err else (None, err)


def _validate(s: object) -> str | None:
    if not isinstance(s, dict) or not isinstance(s.get("entities"), list):
        return "top-level entities[] missing"
    seen: set[str] = set()
    for e in s["entities"]:
        if not isinstance(e, dict) or not str(e.get("name", "")).isidentifier():
            return "entity name must be a JS identifier"
        name = e["name"]
        if name in seen:
            return f"duplicate entity {name}"
        seen.add(name)
        if not isinstance(e.get("fields"), list):
            return f"{name}.fields[] missing"
        names = [str(f.get("name")) for f in e["fields"] if isinstance(f, dict)]
        for f in e["fields"]:
            if not isinstance(f, dict) or not str(f.get("name", "")).isidentifier():
                return f"{name}: field name must be a JS identifier"
            fn = f["name"]
            if names.count(fn) > 1:
                return f"{name}.{fn}: duplicate field"
            # 容错归一:模型常写 type:"email"/"username" — 转成 format。
            if f.get("type") in ("email", "username"):
                if f.get("format") is None:
                    f["format"] = f["type"]
                f["type"] = "string"
            if f.get("type") is not None and f["type"] not in TYPES:
                return f"{name}.{fn}: unknown type {f['type']!r}"
            if f.get("format") is not None and f["format"] not in FORMATS:
                return f"{name}.{fn}: unknown format {f['format']!r}"
            for k in ("min", "max"):
                if f.get(k) is not None and not isinstance(f[k], int):
                    return f"{name}.{fn}: {k} must be int"
            if f.get("pattern") is not None and not isinstance(f["pattern"], str):
                return f"{name}.{fn}: pattern must be a JS regex string"
            if f.get("enum") is not None and not isinstance(f["enum"], list):
                return f"{name}.{fn}: enum must be a list"
            if f.get("match") is not None and f["match"] not in names:
                return f"{name}.{fn}: match points to unknown field"
            if f.get("message") is not None and not isinstance(f["message"], str):
                return f"{name}.{fn}: message must be str"
            if f.get("messages") is not None:
                m = f["messages"]
                if not isinstance(m, dict):
                    return f"{name}.{fn}: messages must be an object"
                for k, v in m.items():
                    if k not in MSG_KINDS:
                        return f"{name}.{fn}: messages key {k!r} not in {sorted(MSG_KINDS)}"
                    if not isinstance(v, str):
                        return f"{name}.{fn}: messages.{k} must be str"
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _singular(s: str) -> str:
    s = str(s)
    return s[:-1] if len(s) > 3 and s.endswith("s") else s


def _secret(field: dict) -> bool:
    return field.get("type") == "password" or field.get("format") == "password"


def _bridge(schema: dict, output_dir: Path) -> None:
    """需求引号消息 → 字段三态消息 + loginFailure(确定性,best-effort)。

    匹配键:字段 label/名、实体 label/单数名 × 字段 label/名的组合,
    全部归一(小写去非字母数字)。模型显式写的 message/messages 永不覆盖。"""
    try:
        msgs = json.loads(
            (output_dir / REQ_MESSAGES_REL).read_text(encoding="utf-8"))
        if not isinstance(msgs, list):
            return
    except Exception:  # noqa: BLE001 — sidecar 缺席/坏档静默跳过
        return
    for m in msgs:
        if not isinstance(m, str):
            continue
        low = m.lower()
        for suffix, kind in _MSG_SUFFIXES:
            if not low.endswith(suffix):
                continue
            xn = _norm(m[: len(m) - len(suffix)])
            if not xn:
                continue
            for e in schema["entities"]:
                ename = str(e.get("name") or "")
                hit = False
                for f in e.get("fields") or []:
                    fname = str(f.get("name") or "")
                    flabel = str(f.get("label") or "")
                    keys = {_norm(flabel), _norm(fname),
                            _norm(f"{e.get('label') or ''} {flabel or fname}"),
                            _norm(f"{_singular(ename)} {fname}"),
                            _norm(f"{_singular(ename)} {flabel}")}
                    keys.discard("")
                    if xn not in keys:
                        continue
                    if not (f.get("messages") or {}).get(kind) and \
                            f.get("message") is None:
                        f.setdefault("messages", {})[kind] = m
                    hit = True
                if not hit and kind == "duplicate" and \
                        xn in {_norm(ename), _norm(_singular(ename)),
                               _norm(str(e.get("label") or ""))}:
                    target = next((f for f in e.get("fields") or []
                                   if f.get("unique") and not _secret(f)), None)
                    if target is not None and \
                            not (target.get("messages") or {}).get(kind) and \
                            target.get("message") is None:
                        target.setdefault("messages", {})[kind] = m
    for m in msgs:
        if isinstance(m, str) and _LOGIN_FAIL_RE.search(m):
            schema.setdefault("messages", {})["loginFailure"] = m
            break


def compile_schema(output_dir: Path) -> list[str]:
    """schema.json → schema.js;内容未变或 schema 缺失/违规时跳过。返回写入文件。"""
    s, _err = load_schema(output_dir)
    if s is None:
        return []
    _bridge(s, output_dir)
    payload = {"entities": [dict(e, route=f"/api/{e['name']}") for e in s["entities"]]}
    if s.get("messages"):
        payload["messages"] = s["messages"]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    if _read_state(output_dir) == digest and (output_dir / SCHEMA_JS).is_file():
        return []
    out = output_dir / SCHEMA_JS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "// schema.js — host-compiled from backend/data/schema.json; do not edit.\n"
        "module.exports = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    _write_state(output_dir, digest)
    return [str(SCHEMA_JS)]


def _read_state(output_dir: Path) -> str | None:
    try:
        return json.loads((output_dir / STATE_REL).read_text(encoding="utf-8")).get("hash")
    except Exception:  # noqa: BLE001
        return None


def _write_state(output_dir: Path, digest: str) -> None:
    f = output_dir / STATE_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"hash": digest}) + "\n", encoding="utf-8")


class Watcher:
    """轮中 watch:每 2s 探一次 schema.json,mtime/size 稳定 3s 才编译。

    只写 schema.js(模型不碰的文件);sync() 是轮末兜底,与 watch 共锁。
    """

    def __init__(self, output_dir: Path, log=print):
        self.dir = Path(output_dir)
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sync(self) -> list[str]:
        """轮末兜底编译;返回本次写入的文件列表。"""
        with self._lock:
            return compile_schema(self.dir)

    def _loop(self) -> None:
        last: tuple[int, int] | None = None
        since: float | None = None
        while not self._stop.wait(2.0):
            try:
                st = schema_path(self.dir).stat()
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                last, since = None, None
                continue
            if sig != last:
                last, since = sig, time.time()
                continue
            if since is not None and time.time() - since >= 3.0:
                with self._lock:
                    written = compile_schema(self.dir)
                if written:
                    self.log(f"[codegen] mid-turn compiled {written}")
                since = None
