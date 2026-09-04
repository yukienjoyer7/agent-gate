"""Windows-safe launcher.

On Windows, whichever code creates the asyncio event loop FIRST decides
whether Playwright can spawn a browser subprocess later: SelectorEventLoop
cannot (NotImplementedError on subprocess_exec), ProactorEventLoop can.

Running via the `uvicorn` CLI (especially with --reload) can end up
creating that first loop before `app.main` is even imported, so setting
the policy inside `app/main.py` is sometimes too late. Setting it here,
as the very first thing this script does, guarantees it wins.

Usage (instead of `uvicorn app.main:app --reload`):
    python run.py
"""

import asyncio
import sys

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn  # noqa: E402  (must come after the policy is set)

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,  # keep off for now to rule out the reloader as a variable
    )
