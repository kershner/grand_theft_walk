"""Suite staging, retired components, and graphics capacity validation."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build import build_suite
from build.build_suite import validate_graphics_capacity


class StagingCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_build_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'build/setup/setup.py'
            source.parent.mkdir(parents=True)
            source.write_text('installer source')
            stage = root / 'build/staging/suite-test'
            stage.mkdir(parents=True)
            (stage / 'temporary.txt').write_text('temporary output')
            with patch.object(build_suite, 'ROOT', root):
                build_suite.cleanup_stage(stage)
                with self.assertRaisesRegex(ValueError, 'unexpected build path'):
                    build_suite.cleanup_stage(source.parent)
            self.assertEqual(source.read_text(), 'installer source')
            self.assertFalse(stage.exists())


class RetiredComponentTests(unittest.TestCase):
    def test_retired_component_has_no_payload_and_only_accepts_known_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'games/vice_city/installer'
            source.mkdir(parents=True)
            name = 'scripts/GTWLightOcclusion.asi'
            expected = '1' * 64
            base = {'files': [], 'disable': [name], 'disabled_hashes': {name: expected}}
            (source / 'manifest.json').write_text(json.dumps(base))
            (source / 'first-person.json').write_text('{"files": []}')
            (root / 'games/vice_city/profile.json').write_text(json.dumps({
                'title': 'Vice City', 'executable': 'gta-vc.exe',
                'install_folder': 'Grand Theft Auto Vice City', 'controller_ready': True}))
            with patch.object(build_suite, 'ROOT', root):
                definition = build_suite.add_component(root / 'stage', 'vice_city')
                self.assertEqual(definition['files'], [{
                    'path': name, 'payload': None, 'sha256': None, 'accepted_before': [None, expected]}])
                self.assertFalse((root / 'stage/games/vice_city/installer/payload').exists())
                base.pop('disabled_hashes')
                (source / 'manifest.json').write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, 'Missing known hash'):
                    build_suite.add_component(root / 'stage', 'vice_city')


class GraphicsCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.payload = Path(temporary.name)
        (self.payload / "scripts").mkdir()
        self.cfg = self.payload / "scripts/SALodLights.cfg"
        self.ini = self.payload / "scripts/III.VC.SA.LimitAdjuster.ini"
        self.cfg.write_text("[DISTANCE]\nViewDistance=3000\nDrawDistance=5\nDetailDistance=4\n")

    def test_each_native_scenery_limit_is_rejected_at_maximum_distance(self):
        capacities = {"Buildings": 32767, "Dummys": 32767, "PtrNodeSingle": 200000}
        for key in capacities:
            with self.subTest(key=key):
                limits = dict(capacities, **{key: 0})
                self.ini.write_text("[SALIMITS]\n" + "\n".join(f"{k}={v}" for k, v in limits.items()))
                with self.assertRaisesRegex(ValueError, key):
                    validate_graphics_capacity("san_andreas", self.payload)

    def test_verified_and_unlimited_scenery_capacity_are_accepted(self):
        for values in ((32767, 32767, 200000), ("unlimited",) * 3):
            self.ini.write_text("[SALIMITS]\n" + "\n".join(
                f"{key}={value}" for key, value in zip(("Buildings", "Dummys", "PtrNodeSingle"), values)))
            validate_graphics_capacity("san_andreas", self.payload)

    def test_missing_adjuster_configuration_is_rejected_for_maximum_distance(self):
        with self.assertRaisesRegex(ValueError, "limit adjuster configuration"):
            validate_graphics_capacity("san_andreas", self.payload)

    def test_modest_preset_and_other_games_do_not_require_sa_scenery_pools(self):
        validate_graphics_capacity("vice_city", self.payload)
        self.cfg.write_text("[DISTANCE]\nViewDistance=0\nDrawDistance=1.5\nDetailDistance=1.5\n")
        validate_graphics_capacity("san_andreas", self.payload)
