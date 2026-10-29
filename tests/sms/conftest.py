from collections.abc import AsyncIterator

import pytest

from .fake_goto import FakeGoTo, start_fake_goto


@pytest.fixture
async def fake_goto() -> AsyncIterator[FakeGoTo]:
    """A fake GoTo on an ephemeral 127.0.0.1 port; stopped after the test."""
    fake = await start_fake_goto()
    try:
        yield fake
    finally:
        await fake.close()
