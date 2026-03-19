import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const INGRESS_HOME = process.env.COMMUNITY_INGRESS_HOME || "/root/.openclaw/community-ingress";
const ROUTE_REGISTRY_PATH =
  process.env.COMMUNITY_ROUTE_REGISTRY || path.join(INGRESS_HOME, "route-registry.json");
const LISTEN_HOST = process.env.COMMUNITY_INGRESS_HOST || "0.0.0.0";
const LISTEN_PORT = Number(process.env.COMMUNITY_INGRESS_PORT || "8848");

function loadRegistry() {
  try {
    return JSON.parse(fs.readFileSync(ROUTE_REGISTRY_PATH, "utf8"));
  } catch {
    return { agents: {} };
  }
}

function routeForPath(pathname) {
  const registry = loadRegistry();
  const agents = registry?.agents && typeof registry.agents === "object" ? registry.agents : {};
  for (const [slug, route] of Object.entries(agents)) {
    if (!route || typeof route !== "object") {
      continue;
    }
    if (pathname === route.webhook_path || pathname === route.send_path) {
      return { slug, route };
    }
  }
  return null;
}

function proxyRequest(route, req, res, rawBody) {
  const upstream = http.request(
    {
      socketPath: route.socket_path,
      path: req.url,
      method: req.method,
      headers: {
        ...req.headers,
        host: "localhost",
        "content-length": Buffer.byteLength(rawBody),
      },
    },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );

  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        ok: false,
        error: "upstream_unavailable",
        message: error.message,
        socket_path: route.socket_path,
      }),
    );
  });

  if (rawBody.length) {
    upstream.write(rawBody);
  }
  upstream.end();
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || "/", "http://localhost");

  if (req.method === "GET" && url.pathname === "/healthz") {
    const registry = loadRegistry();
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "ok",
        mode: "community_ingress",
        listen: `${LISTEN_HOST}:${LISTEN_PORT}`,
        routes: Object.keys(registry?.agents || {}),
      }),
    );
    return;
  }

  const match = routeForPath(url.pathname);
  if (!match) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "route_not_found", path: url.pathname }));
    return;
  }

  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    proxyRequest(match.route, req, res, Buffer.concat(chunks));
  });
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
  console.log(
    JSON.stringify(
      {
        ok: true,
        listening: true,
        mode: "community_ingress",
        listen: `${LISTEN_HOST}:${LISTEN_PORT}`,
        registry: ROUTE_REGISTRY_PATH,
      },
      null,
      2,
    ),
  );
});
