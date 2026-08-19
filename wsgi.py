"""WSGI entry point for production servers.

    gunicorn -b 0.0.0.0:8080 wsgi:app

mock_site/app.py stays runnable on its own for local work; this module only
gives gunicorn an importable target from the repository root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "mock_site"))

from app import _db, app  # noqa: E402

# Seed and preload here, in the master process. Run gunicorn with --preload so
# this module is imported once before forking; otherwise each worker would
# reach _db() concurrently on its first request and race to seed the same
# SQLite file, inserting the calendar several times over.
_db().close()
