#!/usr/bin/env python3
"""Offline artwork boundary and provenance regressions; no remote requests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import presentation_artwork as art


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        repo = patch.object(art, 'REPO', self.root)
        repo.start()
        self.addCleanup(repo.stop)
        self.brief = {'schema': 'presentation-artwork-brief-v1', 'canvas': [526, 720],
                      'sensitivity': 'public', 'brief': '有来源的关系图', 'sources': ['academic/raw/source.md']}
        self.path = self.root / 'brief.json'
        art.write_json(self.path, self.brief)
        self.candidate = {'schema': art.SCHEMA, 'canvas': [526, 720], 'background': '#10283E',
                          'rationale': 'API chooses emphasis', 'objects': [
                              {'type': 'text', 'x': 30, 'y': 40, 'w': 460, 'h': 80,
                               'text': '<script>not executable</script>', 'size': 20,
                               'color': '#FFFFFF', 'align': 'center', 'bold': True}]}

    def test_agent_default_has_task_and_never_calls_api(self):
        with patch.object(art.qa, 'call_json_vision') as remote:
            result = art.generate(self.path)
            remote.assert_not_called()
        self.assertEqual(result['status'], 'prepared')
        run = Path(result['run'])
        art.write_json(run / 'candidate.json', self.candidate)
        receipt = art.render(run)['receipt']
        self.assertEqual(receipt['creator'], 'agent')
        self.assertIsNone(receipt['model'])
        self.assertEqual(receipt['visual_review'], 'not_checked')
        self.assertIn('&lt;script&gt;', (run / 'artwork.fragment.html').read_text())
        self.assertNotIn('<script>', (run / 'artwork.svg').read_text())

    def test_api_requires_remote_consent_and_public_input(self):
        with patch.object(art.qa, 'call_json_vision') as remote:
            with self.assertRaises(ValueError):
                art.generate(self.path, 'api')
            for sensitivity in ('local_only', 'private'):
                art.write_json(self.path, {**self.brief, 'sensitivity': sensitivity})
                with self.assertRaises(ValueError):
                    art.generate(self.path, 'api', True)
            private = self.root / 'private' / 'brief.json'
            private.parent.mkdir()
            art.write_json(private, self.brief)
            with self.assertRaises(ValueError):
                art.generate(private, 'api', True)
            remote.assert_not_called()

    def api_run(self):
        config = {'LLM_API_BASE': 'https://example.invalid/v1', 'LLM_API_KEY': 'test-only'}
        with patch.object(art.env_config, 'load_env', return_value=config), \
             patch.object(art.qa, 'call_json_vision', return_value={'result': self.candidate, 'usage': {}}) as remote:
            result = art.generate(self.path, 'api', True)
        self.assertEqual(remote.call_args.args[0], 'GLM-5.3-FlashX')
        return Path(result['run'])

    def test_api_provenance_rejects_host_redrawing(self):
        run = self.api_run()
        receipt = json.loads((run / 'receipt.json').read_text())
        self.assertEqual(receipt['model'], 'GLM-5.3-FlashX')
        self.assertEqual(receipt['creator'], 'api')
        obj = copy.deepcopy(self.candidate)
        obj['objects'][0]['x'] = 31
        art.write_json(run / 'candidate.json', obj)
        with self.assertRaisesRegex(ValueError, 'API candidate changed'):
            art.render(run)

    def test_remote_failure_never_falls_back_to_host_or_claims_rendered(self):
        config = {'LLM_API_BASE': 'https://example.invalid/v1', 'LLM_API_KEY': 'test-only'}
        with patch.object(art.env_config, 'load_env', return_value=config), \
             patch.object(art.qa, 'call_json_vision', side_effect=TimeoutError):
            with self.assertRaisesRegex(ValueError, 'no host fallback'):
                art.generate(self.path, 'api', True)
        runs = list((self.root / 'temp/presentation-artwork').iterdir())
        self.assertEqual(len(runs), 1)
        self.assertTrue((runs[0] / 'failure.json').exists())
        self.assertFalse((runs[0] / 'receipt.json').exists())

    def test_schema_rejects_code_bad_geometry_and_invalid_brief(self):
        for update in ({'x': -1}, {'w': 1000}, {'x': float('nan')}, {'size': 0},
                       {'onclick': 'evil()'}, {'h': 1}, {'type': 'script'}, {'bold': 1}):
            obj = copy.deepcopy(self.candidate)
            obj['objects'][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                art.validate_artwork(obj, self.brief)
        for invalid in (None, [], {}, {'schema': 'wrong'}):
            with self.assertRaises(ValueError):
                art.validate_brief(invalid)

    def test_changed_brief_and_symlink_outputs_are_rejected(self):
        run = self.api_run()
        (run / 'artwork.svg').unlink()
        victim = self.root / 'do-not-overwrite'
        victim.write_text('keep')
        (run / 'artwork.svg').symlink_to(victim)
        with self.assertRaises(ValueError):
            art.render(run)
        self.assertEqual(victim.read_text(), 'keep')
        (run / 'artwork.svg').unlink()
        art.write_json(run / 'brief.json', {**self.brief, 'brief': 'changed'})
        with self.assertRaisesRegex(ValueError, 'Brief changed'):
            art.render(run)
        with self.assertRaises(ValueError):
            art.guarded(self.root / 'raw' / 'artwork')


if __name__ == '__main__':
    unittest.main()
