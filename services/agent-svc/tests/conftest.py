import asyncio
import sys

import pytest


@pytest.fixture
def anyio_backend():
    # psycopg's async mode (used by the Postgres recorder and LangGraph's checkpointer) cannot
    # run on Windows' default Proactor loop.
    if sys.platform == "win32":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"
