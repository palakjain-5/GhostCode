"""Test configuration for GhostCode.

The project uses a ``src/`` layout without being installed, so make the
package importable no matter where pytest is invoked from.
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
