import asyncio

import pytest


@pytest.fixture(autouse=True)
def ensure_event_loop():
    """Provide a default event loop for sync tests using asyncio.get_event_loop()."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)
