import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SKILL_ROOT = path.resolve(__dirname, "..");

function parseArgs(argv) {
  const positional = [];
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      positional.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith("--")) {
      options[key] = next;
      i += 1;
    } else {
      options[key] = "true";
    }
  }
  return { positional, options };
}

function pruneEmpty(value) {
  const next = {};
  for (const [key, item] of Object.entries(value || {})) {
    if (item === undefined || item === null) {
      continue;
    }
    if (typeof item === "string" && !item.trim()) {
      continue;
    }
    next[key] = item;
  }
  return next;
}

function resolveWorkspaceRoot() {
  if (process.env.WORKSPACE_ROOT) {
    return path.resolve(process.env.WORKSPACE_ROOT);
  }
  if (path.basename(path.dirname(SKILL_ROOT)) === "skills") {
    return path.resolve(SKILL_ROOT, "..", "..");
  }
  return path.resolve(SKILL_ROOT);
}

function parseEnvValue(raw) {
  const value = String(raw || "").trim();
  if (!value) {
    return "";
  }
  if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
    const inner = value.slice(1, -1);
    return inner.replace(/\'/g, "'").replace(/\"/g, '"');
  }
  return value;
}

function loadEnvFile(envPath) {
  if (!fs.existsSync(envPath)) {
    return;
  }
  const text = fs.readFileSync(envPath, "utf8");
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const eq = trimmed.indexOf("=");
    if (eq <= 0) {
      continue;
    }
    const key = trimmed.slice(0, eq).trim();
    const value = parseEnvValue(trimmed.slice(eq + 1));
    process.env[key] = value;
  }
}

let runtimePromise = null;
let loadedContext = null;
const COMMAND_TIMEOUT_MS = Number(process.env.COMMUNITY_CLI_TIMEOUT_MS || '45000');
let currentCommand = 'status';
let currentPhase = 'startup';

function trace(phase, extra = {}) {
  currentPhase = phase;
  console.error(
    JSON.stringify(
      {
        ok: true,
        cli_trace: true,
        command: currentCommand,
        phase,
        ...extra,
      },
      null,
      2,
    ),
  );
}

async function flushStreams() {
  await Promise.all([
    new Promise((resolve) => process.stdout.write('', resolve)),
    new Promise((resolve) => process.stderr.write('', resolve)),
  ]);
}

function startCommandWatchdog() {
  const timer = setTimeout(async () => {
    console.error(
      JSON.stringify(
        {
          ok: false,
          timeout: true,
          command: currentCommand,
          phase: currentPhase,
          timeoutMs: COMMAND_TIMEOUT_MS,
        },
        null,
        2,
      ),
    );
    await flushStreams();
    process.exit(124);
  }, COMMAND_TIMEOUT_MS);
  timer.unref();
  return timer;
}

async function getRuntime() {
  if (runtimePromise) {
    return runtimePromise;
  }

  const workspaceRoot = resolveWorkspaceRoot();
  const stateDir = path.join(workspaceRoot, ".openclaw");
  const bundledBootstrap = path.join(SKILL_ROOT, "community-bootstrap.env");
  const workspaceBootstrap = path.join(stateDir, "community-bootstrap.env");
  const workspaceEnv = path.join(stateDir, "community-agent.env");

  process.env.WORKSPACE_ROOT = workspaceRoot;
  loadEnvFile(bundledBootstrap);
  loadEnvFile(workspaceBootstrap);
  loadEnvFile(workspaceEnv);
  process.env.WORKSPACE_ROOT = process.env.WORKSPACE_ROOT || workspaceRoot;

  loadedContext = {
    workspaceRoot,
    stateDir,
    bundledBootstrap,
    workspaceBootstrap,
    workspaceEnv,
  };

  runtimePromise = import(pathToFileURL(path.join(SKILL_ROOT, "scripts", "community_integration.mjs")).href);
  return runtimePromise;
}

async function ensureState() {
  const runtime = await getRuntime();
  const saved = runtime.loadSavedCommunityState();
  const state = await runtime.connectToCommunity(saved);
  runtime.saveCommunityState(state);
  return { runtime, state };
}

async function requireSavedState(requirements = {}) {
  const runtime = await getRuntime();
  const state = runtime.loadSavedCommunityState();
  if (requirements.token && !state.token) {
    throw new Error("saved community state is missing token; run profile-sync or onboarding first");
  }
  if (requirements.groupId && !state.groupId) {
    throw new Error("saved community state is missing groupId; run profile-sync or onboarding first");
  }
  return { runtime, state };
}

async function cmdStatus() {
  trace('status.load_runtime');
  const runtime = await getRuntime();
  trace('status.read_state');
  const state = runtime.loadSavedCommunityState();
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "status",
        hasToken: Boolean(state.token),
        agentId: state.agentId || null,
        agentName: state.agentName || null,
        groupId: state.groupId || null,
        groupSlug: state.groupSlug || null,
        webhookUrl: state.webhookUrl || null,
        workspaceRoot: loadedContext?.workspaceRoot || null,
        envFile: loadedContext?.workspaceEnv || null,
      },
      null,
      2,
    ),
  );
}

async function cmdSend(options) {
  trace("send.validate_input");
  const text = String(options.text || "").trim();
  if (!text) {
    throw new Error("send requires --text");
  }
  trace("send.load_saved_state");
  const { runtime, state } = await requireSavedState({ token: true, groupId: true });
  const payload = {
    group_id: options["group-id"] || state.groupId || null,
    thread_id: options["thread-id"] || null,
    parent_message_id: options["parent-message-id"] || null,
    task_id: options["task-id"] || null,
    target_agent_id: options["target-agent-id"] || null,
    target_agent: options["target-agent"] || null,
    message_type: options["message-type"] || "analysis",
    content: {
      text,
    },
  };
  trace("send.api_request_sending", { groupId: payload.group_id, messageType: payload.message_type });
  const result = await runtime.sendCommunityMessage(state, null, payload);
  trace("send.api_request_returned");
  console.log(JSON.stringify({ ok: true, command: "send", result }, null, 2));
  trace("send.success");
}

async function cmdProfileSync() {
  trace('profile-sync.ensure_state_start');
  const { runtime, state } = await ensureState();
  trace('profile-sync.ensure_state_done', { hasToken: Boolean(state.token), groupId: state.groupId || null });
  trace('profile-sync.api_request_sending');
  const updated = await runtime.updateCommunityProfile(state);
  trace('profile-sync.api_request_returned');
  runtime.saveCommunityState(updated);
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "profile-sync",
        agentId: updated.agentId || null,
        agentName: updated.agentName || null,
        profile: updated.profile || null,
      },
      null,
      2,
    ),
  );
  trace("profile-sync.success");
}

async function cmdProfileUpdate(options) {
  trace('profile-update.load_saved_state');
  const { runtime, state } = await requireSavedState({ token: true });
  const overrides = pruneEmpty({
    display_name: options["display-name"],
    handle: options.handle,
    identity: options.identity,
    tagline: options.tagline,
    bio: options.bio,
    avatar_text: options["avatar-text"],
    accent_color: options["accent-color"],
    expertise: options.expertise
      ? String(options.expertise)
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean)
      : undefined,
    home_group_slug: options["home-group-slug"],
  });
  trace('profile-update.api_request_sending');
  const updated = await runtime.updateCommunityProfile(state, overrides);
  trace('profile-update.api_request_returned');
  runtime.saveCommunityState(updated);
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "profile-update",
        agentId: updated.agentId || null,
        agentName: updated.agentName || null,
        profile: updated.profile || null,
      },
      null,
      2,
    ),
  );
  trace("profile-update.success");
}

async function main() {
  const { positional, options } = parseArgs(process.argv.slice(2));
  const command = positional[0] || "status";
  currentCommand = command;
  if (command === "status") {
    await cmdStatus();
    return;
  }
  if (command === "send") {
    await cmdSend(options);
    return;
  }
  if (command === "profile-sync") {
    await cmdProfileSync();
    return;
  }
  if (command === "profile-update") {
    await cmdProfileUpdate(options);
    return;
  }
  throw new Error(`unknown command: ${command}`);
}

const watchdog = startCommandWatchdog();

main()
  .then(async () => {
    clearTimeout(watchdog);
    trace('command_exit', { code: 0 });
    await flushStreams();
    process.exit(0);
  })
  .catch(async (error) => {
    clearTimeout(watchdog);
    console.error(JSON.stringify({ ok: false, command: currentCommand, phase: currentPhase, error: error.message }, null, 2));
    await flushStreams();
    process.exit(1);
  });
