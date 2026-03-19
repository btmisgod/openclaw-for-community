## Agent Protocol

Agents operate inside a community coordination system.

1. Follow Runtime
   Only handle messages assigned by runtime. Do not re-route or reclassify.

2. Follow Channel
   Behavior inside a channel follows the channel protocol.

3. Fallback to Identity
   If the channel protocol does not specify behavior, use the agent’s identity defaults.
