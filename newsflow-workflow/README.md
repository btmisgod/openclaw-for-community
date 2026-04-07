## Newsflow Workflow Code Snapshot

This directory contains the current server-side workflow code snapshot from the active `newsflow-mvp` deployment on the Linux server.

Included:

- `app/`: current Python workflow/orchestrator implementation
- `grafana/newsflow-dashboard.json`: current Grafana dashboard definition
- `systemd/`: current systemd unit files for orchestrator and three agents

Snapshot date:

- `2026-04-07`

Source paths on server:

- `/opt/newsflow-mvp/app`
- `/opt/newsflow-mvp/grafana/newsflow-dashboard.json`
- `/etc/systemd/system/newsflow-orchestrator.service`
- `/etc/systemd/system/newsflow-agent-neko.service`
- `/etc/systemd/system/newsflow-agent-33.service`
- `/etc/systemd/system/newsflow-agent-xhs.service`
