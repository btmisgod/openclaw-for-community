from __future__ import annotations

import os

from .workflow import create_db_if_needed, run_worker


def main():
    create_db_if_needed()
    agent_id = os.environ["NEWSFLOW_AGENT_ID"]
    run_worker(agent_id)


if __name__ == "__main__":
    main()
