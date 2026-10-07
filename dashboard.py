#!/usr/bin/env python3
"""GhostCode Streamlit dashboard entry point.

Run the dashboard with::

    streamlit run dashboard.py

Optionally preselect a repository path via the ``GHOSTCODE_REPOSITORY``
environment variable::

    GHOSTCODE_REPOSITORY=~/Projects/PayGuard streamlit run dashboard.py

Executing this file *without* Streamlit (``python3 dashboard.py``) prints
the usage hint above instead of failing.

All dashboard logic lives in ``src/ghostcode/dashboard.py``: a pure
pipeline builder (``build_dashboard_model``) plus the Streamlit view.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the dashboard directly from the repository root without
# installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ghostcode.dashboard import main  # noqa: E402

# Streamlit executes the script top-to-bottom (with a script run context
# active); running `python3 dashboard.py` has no context, and main()
# prints a usage hint in that case.
main()
