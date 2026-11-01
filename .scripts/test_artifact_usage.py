"""Use guidance reaches consumers without promoting candidates or failed reads."""
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import artifact_usage as usage
import query_actions as qa
import wg
from dsh import tools as dsh_tools


class ArtifactUsageTests(unittest.TestCase):
    def test_search_and_read_have_different_use_without_quality_claim(self):
        for name in qa.DISPATCH:
            self.assertIsNotNone(usage.for_action(name), name)
        self.assertEqual(usage.for_action("knowledge_search")["role"], "navigation")
        self.assertEqual(usage.for_action("knowledge_read")["role"], "knowledge")
        self.assertEqual(usage.for_action("source_search")["role"], "navigation")
        self.assertEqual(usage.for_action("read_raw")["role"], "source_material")
        self.assertEqual(usage.for_action("calculate")["role"], "result")

    def test_failure_or_unknown_action_cannot_be_a_source(self):
        self.assertIsNone(usage.for_action("read_raw", ok=False))
        self.assertIsNone(usage.for_action("new_unclassified_tool"))
        self.assertEqual(usage.render("read_raw", "failure", ok=False), "failure")

    def test_render_keeps_content_and_overrides_claimed_role(self):
        payload = {"candidates": [{"id": "source", "excerpt": "Possibly under condition A."}],
                   "usage": {"role": "source_material"}}
        rendered = json.loads(usage.render("source_search", json.dumps(payload)))
        self.assertEqual(rendered["candidates"], payload["candidates"])
        self.assertEqual(rendered["usage"]["role"], "navigation")
        text = "[raw source=x; evidence=x#L1-L2]\nAn uncertain source claim."
        self.assertTrue(usage.render("read_raw", text).endswith(text))

    def test_shared_action_envelope_preserves_body_and_failure_boundary(self):
        with patch.dict(qa.DISPATCH, source_search=lambda inp: ('{"candidates":[]}', 5)):
            result = qa.execute("source_search", {})
        self.assertEqual(result["text"], '{"candidates":[]}')
        self.assertEqual(result["tokens"], 5)
        self.assertEqual(result["usage"]["role"], "navigation")
        with patch.dict(qa.DISPATCH, read_raw=lambda inp: ("[ERROR binding failed]", 0)):
            result = qa.execute("read_raw", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("usage", result)

    def test_dsh_uses_trusted_action_and_keeps_failed_reads_unmarked(self):
        with patch.object(qa, "execute", return_value={"ok": True, "text": '{"statement":"May hold."}',
                                                       "usage": {"role": "source_material"}}):
            result = json.loads(dsh_tools._qa_call("knowledge_read", {}))
        self.assertEqual(result["usage"]["role"], "knowledge")
        self.assertEqual(result["statement"], "May hold.")
        with patch.object(qa, "execute", return_value={"ok": False, "error": "binding failed"}):
            self.assertEqual(dsh_tools._qa_call("read_raw", {}), "binding failed")

    def test_wg_output_marks_usage_without_replacing_material(self):
        for ok in [True, False]:
            output = io.StringIO()
            with redirect_stdout(output):
                wg.envelope("ku-read", {"statement": "Under A, possibly B."}, ok=ok)
            result = json.loads(output.getvalue())
            self.assertEqual(result["result"]["statement"], "Under A, possibly B.")
            self.assertEqual("usage" in result, ok)


if __name__ == "__main__":
    unittest.main()
