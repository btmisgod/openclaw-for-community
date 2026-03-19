# Server Verification Workflow

## Role Split

Local Windows Codex responsibilities:
- modify code
- organize scripts
- update documentation
- prepare commits
- push to GitHub only when explicitly approved by the user

Server Codex responsibilities:
- pull or checkout the target Git commit from GitHub
- run Linux/systemd validation only on the server
- report PASS or FAIL
- return the new real blocker instead of editing code on the server

This project no longer treats the local Windows machine as a runtime validation environment for ingress, systemd, Unix socket, or service-level checks. Those checks belong to the Linux server only.

## Architecture Guardrails

The verification flow assumes the current shared ingress baseline remains unchanged:
- only `openclaw-community-ingress.service` listens on `8848`
- each agent service uses its own Unix socket
- ingress routes `/webhook/{agent_slug}` and `/send/{agent_slug}` through `.openclaw/community-ingress/route-registry.json`
- zero-config onboarding remains the default path

Do not revert to:
- one agent per TCP port
- per-agent ownership of `8848`
- manual port wiring as the primary model
- bootstrap flows that depend on manually sourcing env before install

## Verification Script

Primary server-side verification entrypoint:
- [scripts/server-verify-agent-onboarding.sh](G:\community agnts\openclaw-for-community\scripts\server-verify-agent-onboarding.sh)

Purpose:
- prepare a fresh test workspace
- bootstrap a test agent workspace
- install the agent service
- ensure ingress exists and is active
- ensure the agent service is active
- verify ingress is listening on `8848`
- verify route registry contains the generated `agent_slug`
- verify the agent socket exists
- verify `GET /healthz`
- verify `POST /send/{agent_slug}` returns `202`
- verify `POST /webhook/{agent_slug}` with an invalid signature returns `401`

## Server Usage

Example flow on the Linux validation server:

```bash
git fetch origin

git checkout <commit>

bash scripts/server-verify-agent-onboarding.sh
```

Optional custom test root:

```bash
bash scripts/server-verify-agent-onboarding.sh /root/openclaw-server-verify-agent-onboarding
```

The script is designed to be rerun safely:
- it removes the old test workspace
- it removes the old generated agent service unit for the same test slug
- it removes the old route-registry entry for the same test slug
- it recreates the workspace from scratch

## PASS Criteria

A successful run should end with:
- `PASS ingress service`
- `PASS agent service`
- `PASS ingress listening on 8848`
- `PASS route registry`
- `PASS socket ready`
- `PASS ingress healthz`
- `PASS send route`
- `PASS webhook invalid signature`
- `RESULT PASS`

## Failure Return Package

If verification fails, the server Codex should return all of the following back to the local development side:
- the exact Git commit SHA under test
- the full PASS and FAIL lines from the script output
- `systemctl status openclaw-community-ingress.service --no-pager`
- `systemctl status <agent service> --no-pager`
- `journalctl -u openclaw-community-ingress.service -n 200 --no-pager`
- `journalctl -u <agent service> -n 200 --no-pager`
- the contents of `.openclaw/community-ingress/route-registry.json`
- the `curl` response body and HTTP code for `GET /healthz`
- the `curl` response body and HTTP code for `POST /send/{agent_slug}`
- the `curl` response body and HTTP code for invalid-signature `POST /webhook/{agent_slug}`
- any unexpected socket path, missing file path, or service name observed during the run

## Operational Rule

When server verification fails, fix the code locally, commit locally, and push a new GitHub revision only after user approval. The server should then pull the new revision and rerun the same verification script. The server is a verification environment, not the main development environment.
