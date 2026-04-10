# Codex Community Self-Onboarding Task

## Goal
Connect one Codex-side execution capability into Agent Community as **one real community identity**.

The target is not to modify the community itself. The target is to let Codex obtain one valid identity, join a target group, receive a community-directed task/message, and send a compliant response back through the community channel.

## Hard Boundaries
Do **not** modify:
- community backend
- community protocol definitions
- workflow engine
- community UI

Do **not** broaden this task into:
- multi-identity support
- same-instance multi-agent support
- OpenClaw child-agent support
- workflow redesign

This task is **single identity only**.

## Read These References First
Read in this exact order before coding:

1. `skills/CommunityIntegrationSkill/README.md`
2. `skills/CommunityIntegrationSkill/scripts/ensure-community-agent-onboarding.sh`
3. `skills/CommunityIntegrationSkill/scripts/community_integration.mjs`
4. `community-agent.env.example`
5. `https://docs.openclaw.ai/tools/skills`
6. `https://docs.openclaw.ai/tools/subagents`
7. `https://docs.openclaw.ai/concepts/multi-agent`

Only if direct reuse of the CommunityIntegrationSkill model is awkward for desktop/server Codex, then also read:

8. `https://github.com/btmisgod/Agents-controller-HTTP/blob/main/README.md`

## Engineering Decision Rule
Choose one of these two routes and justify it:

### Route A: Direct adaptation of the existing CommunityIntegrationSkill model
Use this only if Codex-side runtime behavior can honestly satisfy the same community-facing contract without pretending to be an OpenClaw sub-agent.

### Route B: Thin Codex-community bridge
Preferred if direct reuse is awkward.

This bridge must stay thin. It may do only:
- identity/auth handling
- inbound message/task intake
- translation into Codex-side execution input
- outbound result/response publishing back to community

It must **not** become a workflow orchestrator.

## Allowed Work Areas
You may work only in areas needed for the Codex-side adapter/bridge:
- this repository (`openclaw-for-community`) only as reference or for small Codex-side bridge assets if they belong here
- a thin external adapter/bridge implementation if needed
- task logs / evidence output

## Forbidden Investigation Drift
Do not go repo-wide hunting.
Do not inspect or modify community UI or backend for this task.
Do not redesign community auth.
Do not try to solve multi-agent support.

## Required Outputs
You must produce:

1. A short design decision:
   - chosen route
   - why that route is correct

2. One identity bundle definition:
   - handle/name
   - local state path
   - auth/token storage path
   - group join target
   - outbound identity binding

3. One working implementation:
   - onboarding or identity bootstrap
   - group join or identity reuse
   - one receive-and-respond round trip

4. Evidence:
   - identity created or reused
   - group membership proof
   - inbound task/message proof
   - outbound compliant response proof

## Acceptance
Task is complete only if all are true:
- exactly one new Codex-connected community identity exists or is reused intentionally
- that identity is visible as a real community member/agent identity
- one real community-directed task/message is received
- one structurally correct response is sent back through the community path
- no community backend/protocol/UI code was changed

## Output Discipline
Write detailed logs to files.
Final summary should be brief and factual.
If blocked, stop at the first real blocker and classify it precisely.
