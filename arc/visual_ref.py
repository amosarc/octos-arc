"""Host-side visual reference pre-description (宿主参考图预描述注入).

requirements.yaml 里的页面参考图(`![image](reference/x.png)`)对编码通道
是死文本:主模型收到的只是 markdown 记号,看不见像素。本模块在启动期把
每张参考图按视觉模型格式(base64 data-URL 的 image_url 内容块)提交给
VISUAL_MODEL,把返回的描述原地写回需求树——describe_node 渲染的
node_spec、plan envelope 的 CONTRACT/FOCUS 等所有下游提示词随后自动
携带视觉描述,内核零改动。

图片定位(用户决策 2026-09-27):仅供样式布局参考。描述承载布局/结构/
样式线索;mockup 里的字符串、语言、数据来自原产品,不构成需求契约。

通道解析:VISUAL_MODEL / VISUAL_API_KEY / VISUAL_BASE_URL,缺省逐级
回落 MODEL / OPENAI_API_KEY / OPENAI_BASE_URL,模型最后默认
glm-5.3-flash(宿主决策 2026-09-27)。

失败纪律(best-effort,绝不阻塞生成流):文件缺失、端点死亡、模型无
视觉能力 → 该图降级为一行占位说明(保留文件名信息);通道整体未配置
则原样保留 markdown 记号(与历史行为一致)。缓存按 (图片 sha256,
模型名) 落在 <workspace>/.arc/visual-ref-cache.json,重跑不重复付费。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

# markdown 图片记号:![alt](target)。alt 任意(含空),target 排除空白。
IMAGE_REF_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)")

DEFAULT_VISUAL_MODEL = "glm-5.3-flash"

# 单图描述注入上限:参考图是 Google-Sheets 级整页截图,180 词量级的描述
# 截到 1500 字符足够指路;再长只会稀释节点提示词。
MAX_DESC_CHARS = 1500

# 视觉提问词:面向"要复刻这个 UI 的工程师"。图片定位=样式布局参考
# (用户决策 2026-09-27):描述只承载区域排布/组件形态/网格结构与视觉
# 层级;mockup 里的标签/语言/数据来自原产品,不构成需求契约,提及仅为
# 识别组件是什么。
_DESCRIBE_PROMPT = (
    "You are looking at a UI mockup screenshot attached to a software "
    "requirement. The image is a STYLE AND LAYOUT REFERENCE ONLY — the "
    "requirement text, not the mockup, defines every label, wording, "
    "and value. Describe for an engineer who must reproduce this UI as "
    "a web application, in plain-text bullets: the overall layout "
    "regions and their arrangement; the component types and placement "
    "(toolbars, sidebars, tabs, grids or tables with their row/column "
    "structure, dialogs, status areas); visual hierarchy and styling "
    "cues (grouping, alignment, density, which elements dominate). "
    "Mention a visible label only when needed to identify what a "
    "component is; never treat the mockup's wording, language, or data "
    "as part of the requirements. At most 180 words. Output the "
    "description text only."
)


def _walk_nodes(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk_nodes(child)


def _iter_step_contents(node: dict):
    for scenario in node.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for step in scenario.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("content"), str):
                yield step


def collect_image_refs(tree: dict,
                       only_nodes: set[str] | frozenset[str] | None = None
                       ) -> list[str]:
    """需求树里引用的本地图片路径(按首次出现顺序去重,忽略远程 URL)。

    only_nodes=节点 id 白名单(分层装配的范围 run)时只收集这些节点
    引用的图——范围外家族的图不进视觉通道(省墙钟与 token)。"""
    refs: list[str] = []
    seen: set[str] = set()

    def scan(text: str | None) -> None:
        for match in IMAGE_REF_RE.finditer(text or ""):
            target = match.group(2)
            if target.startswith(("http://", "https://", "data:")):
                continue
            if target not in seen:
                seen.add(target)
                refs.append(target)

    for node in _walk_nodes(tree):
        if only_nodes is not None:
            nid = str(node.get("id") or "")
            if nid not in only_nodes:
                continue
        if isinstance(node.get("description"), str):
            scan(node["description"])
        for step in _iter_step_contents(node):
            scan(step["content"])
    return refs


def visual_lane() -> tuple[str, str, str] | None:
    """(model, base_url, api_key);通道未配置返回 None。"""
    model = (os.environ.get("VISUAL_MODEL")
             or os.environ.get("MODEL")
             or DEFAULT_VISUAL_MODEL)
    base = (os.environ.get("VISUAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    key = (os.environ.get("VISUAL_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    if not base or not key:
        return None
    return model, base, key


def _mime_for(path: Path) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")


def _request_payload(model: str, prompt_text: str, mime: str,
                     encoded: str) -> dict:
    """视觉请求体:GLM-5.2+ 思考开启(用户规格 2026-09-27)。

    视觉调用单发单收、每次都是首轮对话 → reasoning_effort 取首轮策略
    max。max_tokens 必须给足:思考输出与正文共享完成预算,1024 会被
    reasoning 吃光导致 content 为空(deepseek 4096 默认的同款教训)。"""
    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }],
        "max_tokens": 8192,
        "temperature": 0,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


def _describe_image(model: str, base_url: str, api_key: str,
                    image_path: Path, *, timeout: int = 180,
                    log=print) -> str | None:
    """单图视觉描述:OpenAI 兼容 chat/completions + image_url 内容块。

    一次重试;任何失败返回 None(调用方降级为占位说明)。"""
    try:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as exc:
        log(f"[visual] cannot read {image_path}: {exc}")
        return None
    body = json.dumps(
        _request_payload(model, _DESCRIBE_PROMPT, _mime_for(image_path),
                         encoded)).encode()
    last_error: object = None
    for attempt in (1, 2):
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            text = ((payload.get("choices") or [{}])[0]
                    .get("message", {}).get("content") or "")
            text = " ".join(str(text).split()).strip()
            if text:
                return text[:MAX_DESC_CHARS]
            last_error = "empty content"
        except Exception as exc:  # noqa: BLE001 — 视觉通道降级不致命
            last_error = exc
        if attempt == 1:
            time.sleep(3)
    log(f"[visual] describe failed for {image_path.name}: {last_error}")
    return None


def describe_images(req_dir: Path, refs: list[str], *, lane: tuple[str, str, str],
                    log=print, cache_path: Path | None = None,
                    phase_deadline: float | None = None) -> dict[str, str]:
    """逐图取描述,带 (sha256, model) 缓存。返回 {相对路径: 描述文本}。"""
    model, base_url, api_key = lane
    cache: dict = {}
    if cache_path is not None and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
    descriptions: dict[str, str] = {}
    dirty = False
    for ref in refs:
        image_path = req_dir / ref
        if not image_path.is_file():
            log(f"[visual] reference image missing: {ref}")
            continue
        try:
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        except OSError:
            continue
        hit = cache.get(ref) if isinstance(cache.get(ref), dict) else None
        if hit and hit.get("sha256") == digest and hit.get("model") == model:
            descriptions[ref] = str(hit.get("text") or "")
            continue
        if phase_deadline is not None and time.time() >= phase_deadline:
            log(f"[visual] phase budget exhausted before {ref}")
            continue
        text = _describe_image(model, base_url, api_key, image_path, log=log)
        if text:
            descriptions[ref] = text
            cache[ref] = {"sha256": digest, "model": model, "text": text}
            dirty = True
    if dirty and cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True),
                                  encoding="utf-8")
        except OSError as exc:
            log(f"[visual] cache write failed (non-fatal): {exc}")
    return descriptions


def inject_visual_descriptions(tree: dict, descriptions: dict[str, str],
                               *, model_label: str) -> int:
    """把树里的 ![alt](path) 记号原地替换为视觉描述块。

    有描述 → "Page reference image <path> (style/layout reference only)
              — <model> visual description: …"
    无描述 → 占位说明(仍优于裸记号:模型知道此处有一张图但内容缺失)。
    前缀自带"仅供样式布局参考"定性,与内核提示词守则互为印证。
    返回成功注入描述的记号数。"""
    injected = 0

    def rewrite(text: str) -> str:
        def substitute(match: re.Match) -> str:
            nonlocal injected
            target = match.group(2)
            description = descriptions.get(target)
            if not description:
                return (f"Page reference image {target} "
                        "(no visual description available)")
            injected += 1
            return (f"Page reference image {target} "
                    f"(style/layout reference only) — {model_label} "
                    f"visual description: {description}")

        return IMAGE_REF_RE.sub(substitute, text)

    for node in _walk_nodes(tree):
        if isinstance(node.get("description"), str):
            node["description"] = rewrite(node["description"])
        for step in _iter_step_contents(node):
            step["content"] = rewrite(step["content"])
    return injected


def _write_sidecar(work_dir: Path, descriptions: dict[str, str],
                   refs: list[str], model_label: str, log) -> None:
    """.arc/visual-refs.md 人类可读存档:事后复盘看宿主到底给模型看了什么。"""
    lines = [f"# Reference image visual descriptions ({model_label})",
             f"{len(descriptions)}/{len(refs)} described", ""]
    for ref in refs:
        lines.append(f"## {ref}")
        lines.append(descriptions.get(ref, "(no visual description available)"))
        lines.append("")
    try:
        target = work_dir / ".arc" / "visual-refs.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log(f"[visual] sidecar write failed (non-fatal): {exc}")


def apply_visual_references(tree: dict, req_dir: Path, *, log=print,
                            work_dir: Path | None = None,
                            only_nodes: set[str] | frozenset[str] | None = None
                            ) -> int:
    """入口:收集参考图 → 视觉模型描述 → 原地注入需求树。

    必须在 fixture_manifest.extract 之后调用(夹具提取读需求原文,视觉
    描述不应混入其启发式);traceability 存档发生在更早处,保留原树。
    only_nodes(范围 run,OCTOS_ARC_FAMILIES)只描述范围内节点引用的图,
    范围外引用保持原样(它们没有工单,不需要视觉描述)。
    返回成功注入的描述数(0 = 无参考图或通道不可用,流程原样继续)。"""
    refs = collect_image_refs(tree, only_nodes=only_nodes)
    if not refs:
        return 0
    lane = visual_lane()
    if lane is None:
        log("[visual] visual lane unconfigured (VISUAL_BASE_URL/"
            "VISUAL_API_KEY unset); image refs stay as plain text")
        return 0
    model = lane[0]
    log(f"[visual] {len(refs)} reference image(s) found; "
        f"describing with visual model {model}")
    cache_path = (work_dir / ".arc" / "visual-ref-cache.json") \
        if work_dir is not None else None
    budget = int(os.environ.get("OCTOS_VISUAL_BUDGET", "900"))
    descriptions = describe_images(
        req_dir, refs, lane=lane, log=log, cache_path=cache_path,
        phase_deadline=time.time() + budget)
    injected = inject_visual_descriptions(
        tree, descriptions, model_label=model)
    if work_dir is not None:
        _write_sidecar(work_dir, descriptions, refs, model, log)
    log(f"[visual] injected {injected}/{len(refs)} visual description(s) "
        f"into the requirement tree")
    return injected
