import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _clear_provider_bench():
    """
    Provider benches are process-global on purpose — shared across walks so the
    next run does not re-try a drained subscription. Tests must therefore start
    from a clean one, or a walk in one test skips a provider benched by another.
    """
    from orchestrator.chains import clear_bench

    clear_bench()
    yield
    clear_bench()
