"""Make the standalone San Andreas scripts importable during test discovery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "san_andreas"))
