"""Root-level conftest so `pytest` (run from the repo root) can import the
`app` package regardless of the pythonpath ini option honoring the runner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
