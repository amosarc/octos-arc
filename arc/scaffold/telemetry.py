"""评测期遥测的交付前兜底落位。

平台 func/e2e 失败明细不下发;应用自身把评测流量与前端异常写
octos-logs/eval-traffic.jsonl 随工作区 zip 带回(2026-09-26 github 6 分
run:8 会话后零写入的死因只能靠这层还原)。常规路径由 scaffold 开工时
落盘三件(lib/eval-telemetry.js、app.js 的 require 行、index.html 错误
钩子);本模块在交付前兜底——模型重写过 app.js/index.html 丢了钩子则
原则注入回补,并写 marker 行切分 agent/评测两阶段流量。注入失败只告警:
遥测永远不破坏交付。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# index.html 回补钩子的锚点标记(scaffold 常量的内联脚本也含此 data 属性,
# 存在即免注)。字符串形态与 scaffold/frontend.py 保持一致。
_HOOK_MARKER = "__arcTelemetryHook"
_HOOK_HTML = (
    '<script data-host="__arcTelemetryHook">(function(){var s=function(k,m)'
    "{try{navigator.sendBeacon('/api/__telemetry',new Blob([JSON.stringify("
    "{kind:k,msg:String(m).slice(0,300),href:location.href})],"
    "{type:'application/json'}))}catch(e){}};"
    "window.addEventListener('error',function(e){s('pageerror',"
    "(e&&e.message?e.message:'error')+' @'+((e&&e.filename)||'')+':'"
    "+((e&&e.lineno)||''))});window.addEventListener("
    "'unhandledrejection',function(e){s('unhandledrejection',e&&e.reason"
    "!==undefined?e.reason:'rejection')});})();</script>")
_REQUIRE_LINE = (
    "try { require('./lib/eval-telemetry').mount(app); } "
    "catch { /* telemetry best-effort */ }")
_APP_ANCHOR = re.compile(r"(const\s+app\s*=\s*express\(\)\s*;)")


def ensure(output_dir, log=print) -> bool:
    """三件兜底 + 交付 marker。返回 True=遥测链路完整可用。"""
    output_dir = Path(output_dir)
    from . import backend as scaffold_backend
    ok = True
    # 1) telemetry 模块本体(无条件覆写:宿主独占文件,内容即清单源)
    tel = output_dir / "backend" / "src" / "lib" / "eval-telemetry.js"
    try:
        tel.parent.mkdir(parents=True, exist_ok=True)
        tel.write_text(scaffold_backend.EVAL_TELEMETRY_JS, encoding="utf-8")
    except OSError as exc:
        log(f"[postflight] eval telemetry module not written: {exc}")
        return False
    # 2) app.js require 行(模型重写过则锚点注入)
    app_js = output_dir / "backend" / "src" / "app.js"
    try:
        src = app_js.read_text(encoding="utf-8")
        if "eval-telemetry" not in src:
            patched = _APP_ANCHOR.sub(
                r"\1\n" + _REQUIRE_LINE.replace("\\", "\\\\"),
                src, count=1)
            if patched != src:
                app_js.write_text(patched, encoding="utf-8")
                log("[postflight] eval telemetry: re-injected require "
                    "into app.js (model had rewritten it)")
            else:
                ok = False
                log("[postflight] eval telemetry WARNING: app.js carries "
                    "no `const app = express()` anchor; traffic log off")
    except OSError as exc:
        ok = False
        log(f"[postflight] eval telemetry app.js check failed: {exc}")
    # 3) index.html 前端错误钩子(缺失则 </head> 前注入)
    index_html = output_dir / "frontend" / "index.html"
    try:
        html = index_html.read_text(encoding="utf-8")
        if _HOOK_MARKER not in html and "__arcErr" not in html:
            anchor = "</head>" if "</head>" in html else "</body>"
            if anchor in html:
                index_html.write_text(
                    html.replace(anchor, _HOOK_HTML + anchor, 1),
                    encoding="utf-8")
                log("[postflight] eval telemetry: injected frontend error "
                    "hook into index.html")
            else:
                ok = False
                log("[postflight] eval telemetry WARNING: index.html has "
                    "no </head> or </body>; frontend error hook off")
    except OSError as exc:
        ok = False
        log(f"[postflight] eval telemetry index.html check failed: {exc}")
    # 4) 交付 marker:评测窗口 = marker 之后的行
    try:
        logs_dir = output_dir / "octos-logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(logs_dir / "eval-traffic.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": datetime.now(timezone.utc).isoformat(),
                "kind": "marker",
                "note": "agent finished; platform eval follows"}) + "\n")
    except OSError as exc:
        ok = False
        log(f"[postflight] eval telemetry marker not written: {exc}")
    return ok
