from __future__ import annotations

import multiprocessing
import os

import uvicorn

from config import initialize_environment


def main() -> None:
    multiprocessing.freeze_support()
    initialize_environment()
    from app import app

    uvicorn.run(
        app,
        host=os.environ.get("ARTICLE_AGENT_BACKEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("ARTICLE_AGENT_BACKEND_PORT", "8000")),
        loop="asyncio",
        http="h11",
        workers=1,
    )


if __name__ == "__main__":
    main()
