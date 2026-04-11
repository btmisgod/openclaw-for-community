from __future__ import annotations

import logging
import os

from .workflow import create_db_if_needed, run_worker


def main():
    logging.basicConfig(
        level=getattr(logging, os.getenv("NEWSFLOW_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    create_db_if_needed()
    agent_id = os.environ["NEWSFLOW_AGENT_ID"]
    logging.getLogger("newsflow.worker_entry").info("worker.start agent_id=%s", agent_id)
    run_worker(agent_id)


if __name__ == "__main__":
    main()
