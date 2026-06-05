"""Test setup. Runs before any test module is imported, so it can point database.py
at a throwaway SQLite file BEFORE it's first imported (database.py reads DATABASE_PATH
and runs init_db() at import time). Without this, import order could let a test write
to the real whats_on.db."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("DATABASE_URL", None)
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
