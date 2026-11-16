"""Retired native components must be removed by normal upgrades."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_suite


class RetiredComponentTests(unittest.TestCase):
    def test_retired_component_has_no_payload_and_only_accepts_known_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'vice_city/installer'
            source.mkdir(parents=True)
            name = 'scripts/GTWLightOcclusion.asi'
            expected = '1' * 64
            base = {'files': [], 'disable': [name], 'disabled_hashes': {name: expected}}
            (source / 'manifest.json').write_text(json.dumps(base))
            (source / 'first-person.json').write_text('{"files": []}')
            (root / 'vice_city/profile.json').write_text(json.dumps({
                'title': 'Vice City', 'executable': 'gta-vc.exe',
                'install_folder': 'Grand Theft Auto Vice City', 'controller_ready': True}))
            with patch.object(build_suite, 'ROOT', root):
                definition = build_suite.add_component(root / 'stage', 'vice_city')
                self.assertEqual(definition['files'], [{
                    'path': name, 'payload': None, 'sha256': None, 'accepted_before': [None, expected]}])
                self.assertFalse((root / 'stage/vice_city/installer/payload').exists())
                base.pop('disabled_hashes')
                (source / 'manifest.json').write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, 'Missing known hash'):
                    build_suite.add_component(root / 'stage', 'vice_city')
