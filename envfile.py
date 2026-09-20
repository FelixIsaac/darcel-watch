"""Load .env into os.environ. Stdlib only, no python-dotenv dependency.

Called at the top of run.py, freshness.py and verify.py so every entry point
behaves the same whether you run it by hand, through npm, or spawned by the
app server. Real environment variables always win - .env is a convenience for
local runs, not an override.
"""

import os
import pathlib


def load(path=None):
    p = pathlib.Path(path or pathlib.Path(__file__).parent / ".env")
    if not p.exists():
        return {}
    loaded = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:  # a real env var beats the file
            os.environ[key] = value
            loaded[key] = value
    return loaded
