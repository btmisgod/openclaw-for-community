import { startCommunityIntegration } from "./community_integration.mjs";

startCommunityIntegration().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exit(1);
});
