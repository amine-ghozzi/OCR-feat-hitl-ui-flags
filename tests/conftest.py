"""``FINALZE_SECRET_KEY`` has no default (SEC-8) and ``get_settings()`` runs at
import time in ten modules, so the variable must exist before any test imports them.
"""

import os

os.environ.setdefault("FINALZE_SECRET_KEY", "pytest-only-secret-not-for-deploy-0000")
