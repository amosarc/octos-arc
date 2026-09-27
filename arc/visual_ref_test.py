"""Tests for arc/visual_ref.py (stdlib unittest only)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import visual_ref as vr  # noqa: E402


# 最小合法 1x1 PNG(视觉路径不解析像素,只读字节做 base64)。
PNG_BYTES = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
    0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
    0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
    0x00, 0x00, 0x02, 0x00, 0x01, 0xE2, 0x21, 0xBC,
    0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
    0x44, 0xAE, 0x42, 0x60, 0x82,
])


def sheet_tree() -> dict:
    """带 description + 场景 step 图片引用的最小需求树。"""
    return {
        "id": "ROOT", "type": "FOLDER",
        "children": [
            {
                "id": "REQ-1", "type": "ATOMIC",
                "description": "Build the home page.\n\nPage reference:\n"
                               "![image](reference/workbook-home.png)",
                "scenarios": [{"name": "s1", "steps": [
                    {"keyword": "GIVEN",
                     "content": "Home looks like ![shot](reference/create-workbook.png)"},
                ]}],
            },
            {
                "id": "REQ-2", "type": "ATOMIC",
                "description": "Toolbar per ![img](reference/workbook-home.png)",
            },
        ],
    }


class CollectImageRefsTests(unittest.TestCase):
    def test_finds_refs_in_descriptions_and_steps_deduped_in_order(self):
        refs = vr.collect_image_refs(sheet_tree())
        self.assertEqual(refs, ["reference/workbook-home.png",
                                "reference/create-workbook.png"])

    def test_ignores_remote_and_data_urls(self):
        tree = {"id": "R", "type": "ATOMIC",
                "description": "![a](https://x/y.png) ![b](data:image/png;base64,AA) "
                               "![c](reference/ok.png)"}
        self.assertEqual(vr.collect_image_refs(tree), ["reference/ok.png"])

    def test_no_refs_leaves_empty(self):
        self.assertEqual(vr.collect_image_refs(sheet_tree()["children"][1]
                                               | {"description": "plain"}), [])


class RequestPayloadTests(unittest.TestCase):
    def test_payload_carries_vision_and_thinking_fields(self):
        payload = vr._request_payload("glm-5.3-flash", "describe", "image/png",
                                      "QUJD")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe"})
        self.assertEqual(
            content[1]["image_url"]["url"], "data:image/png;base64,QUJD")
        # GLM-5.2+ 思考开启:首轮策略 max;max_tokens 留足思考+正文预算。
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertGreaterEqual(payload["max_tokens"], 4096)


class VisualLaneTests(unittest.TestCase):
    ENV_KEYS = ("VISUAL_MODEL", "VISUAL_API_KEY", "VISUAL_BASE_URL",
                "MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_direct_values_win(self):
        os.environ.update(VISUAL_MODEL="glm-5.3-flash",
                          VISUAL_API_KEY="vk", VISUAL_BASE_URL="https://v/")
        self.assertEqual(vr.visual_lane(),
                         ("glm-5.3-flash", "https://v", "vk"))

    def test_fallback_to_main_lane_then_default_model(self):
        os.environ.update(MODEL="coder-x", OPENAI_API_KEY="mk",
                          OPENAI_BASE_URL="https://m/v1/")
        self.assertEqual(vr.visual_lane(), ("coder-x", "https://m/v1", "mk"))
        os.environ.pop("MODEL")
        self.assertEqual(vr.visual_lane()[0], vr.DEFAULT_VISUAL_MODEL)

    def test_unconfigured_returns_none(self):
        os.environ["OPENAI_BASE_URL"] = "https://m/v1"
        self.assertIsNone(vr.visual_lane())


class InjectTests(unittest.TestCase):
    def test_rewrites_tokens_with_description(self):
        tree = sheet_tree()
        n = vr.inject_visual_descriptions(
            tree, {"reference/workbook-home.png": "A grid with toolbar."},
            model_label="glm-5.3-flash")
        # workbook-home 出现两次(REQ-1 描述 + REQ-2 描述)都注入
        self.assertEqual(n, 2)
        self.assertIn(
            "Page reference image reference/workbook-home.png "
            "(style/layout reference only) — "
            "glm-5.3-flash visual description: A grid with toolbar.",
            tree["children"][0]["description"])
        self.assertNotIn("![image]", tree["children"][0]["description"])
        self.assertIn(
            "visual description: A grid with toolbar.",
            tree["children"][1]["description"])

    def test_rewrites_scenario_step_content(self):
        tree = sheet_tree()
        vr.inject_visual_descriptions(
            tree, {"reference/create-workbook.png": "Create dialog."},
            model_label="m")
        step = tree["children"][0]["scenarios"][0]["steps"][0]
        self.assertIn("visual description: Create dialog.", step["content"])
        self.assertNotIn("![shot]", step["content"])

    def test_missing_description_gets_placeholder(self):
        tree = {"id": "R", "type": "ATOMIC",
                "description": "x ![i](reference/gone.png)"}
        n = vr.inject_visual_descriptions(tree, {}, model_label="m")
        self.assertEqual(n, 0)
        self.assertIn("Page reference image reference/gone.png "
                      "(no visual description available)",
                      tree["description"])


class DescribeImagesTests(unittest.TestCase):
    def setUp(self):
        self._real_describe = vr._describe_image
        self.calls: list[Path] = []
        vr._describe_image = self._fake_describe

    def tearDown(self):
        vr._describe_image = self._real_describe

    def _fake_describe(self, model, base_url, api_key, image_path, *,
                       timeout=90, log=print):
        self.calls.append(image_path)
        return f"desc of {image_path.name}"

    def test_describes_each_unique_image_and_caches(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "reference").mkdir()
            (root / "reference/a.png").write_bytes(PNG_BYTES)
            (root / "reference/b.png").write_bytes(PNG_BYTES * 2)
            cache = root / ".arc" / "visual-ref-cache.json"
            lane = ("m", "https://x", "k")
            d1 = vr.describe_images(root, ["reference/a.png", "reference/b.png",
                                           "reference/missing.png"],
                                    lane=lane, cache_path=cache)
            self.assertEqual(d1, {"reference/a.png": "desc of a.png",
                                 "reference/b.png": "desc of b.png"})
            self.assertEqual(len(self.calls), 2)
            # 第二轮:同字节同模型 → 全走缓存,零调用
            self.calls.clear()
            d2 = vr.describe_images(root, ["reference/a.png"],
                                    lane=lane, cache_path=cache)
            self.assertEqual(d2, {"reference/a.png": "desc of a.png"})
            self.assertEqual(self.calls, [])
            # 换模型 → 缓存失效重述
            self.calls.clear()
            vr.describe_images(root, ["reference/a.png"],
                               lane=("other", "https://x", "k"),
                               cache_path=cache)
            self.assertEqual(len(self.calls), 1)

    def test_phase_deadline_skips_remote_calls(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "reference").mkdir()
            (root / "reference/a.png").write_bytes(PNG_BYTES)
            d = vr.describe_images(root, ["reference/a.png"],
                                   lane=("m", "https://x", "k"),
                                   phase_deadline=0.0)  # 已过期
            self.assertEqual(d, {})
            self.assertEqual(self.calls, [])


class ApplyVisualReferencesTests(unittest.TestCase):
    ENV_KEYS = VisualLaneTests.ENV_KEYS

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(VISUAL_MODEL="glm-5.3-flash",
                          VISUAL_API_KEY="vk", VISUAL_BASE_URL="https://v")
        self._real_describe = vr._describe_image
        vr._describe_image = lambda model, base, key, path, **kw: \
            f"vision of {path.name}"

    def tearDown(self):
        vr._describe_image = self._real_describe
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_end_to_end_injects_tree_and_writes_artifacts(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            req = root / "req"
            (req / "reference").mkdir(parents=True)
            (req / "reference/workbook-home.png").write_bytes(PNG_BYTES)
            work = root / "workspace"
            work.mkdir()
            tree = sheet_tree()
            n = vr.apply_visual_references(tree, req, work_dir=work)
            self.assertEqual(n, 2)  # workbook-home 两个记号;create- 图缺失→占位
            self.assertIn("visual description: vision of workbook-home.png",
                          tree["children"][0]["description"])
            self.assertIn("(no visual description available)",
                          tree["children"][0]["scenarios"][0]["steps"][0]
                          ["content"])
            sidecar = work / ".arc" / "visual-refs.md"
            cache = work / ".arc" / "visual-ref-cache.json"
            self.assertTrue(sidecar.is_file())
            self.assertIn("vision of workbook-home.png",
                          sidecar.read_text(encoding="utf-8"))
            self.assertIn("reference/workbook-home.png",
                          json.loads(cache.read_text(encoding="utf-8")))

    def test_no_refs_is_noop(self):
        with TemporaryDirectory() as td:
            tree = {"id": "R", "type": "ATOMIC", "description": "plain"}
            self.assertEqual(
                vr.apply_visual_references(tree, Path(td), work_dir=Path(td)),
                0)

    def test_unconfigured_lane_leaves_tree_untouched(self):
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        with TemporaryDirectory() as td:
            tree = sheet_tree()
            before = tree["children"][0]["description"]
            self.assertEqual(
                vr.apply_visual_references(tree, Path(td)), 0)
            self.assertEqual(tree["children"][0]["description"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
