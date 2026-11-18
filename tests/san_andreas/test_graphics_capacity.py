"""Prevent the maximum-distance SA scenery-pool exhaustion regression."""
import tempfile
import unittest
from pathlib import Path

from build.build_suite import validate_graphics_capacity


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
