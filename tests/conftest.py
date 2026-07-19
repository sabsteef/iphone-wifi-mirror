"""Shared pytest fixtures."""
import asyncio
import sys
from pathlib import Path

import pytest

# Make ``src`` importable when running from repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
