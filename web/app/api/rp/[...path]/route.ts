import type { NextRequest } from "next/server";

// Proxy to the RepoPilot API. The bearer token is added here, on the server, so it never
// reaches the browser. Streaming responses (the SSE timeline) are passed through as-is.
const API = process.env.REPOPILOT_API_URL ?? "http://127.0.0.1:8000";
const TOKEN = process.env.REPOPILOT_API_TOKEN ?? "dev-api-token-change-me";
// Hosts the console is served under. Anything else is refused, so a DNS-rebinding page (which
// reaches this server under its own host name) cannot use the proxy's token.
const HOSTS = (process.env.REPOPILOT_UI_HOSTS ?? "127.0.0.1:3000,localhost:3000")
  .split(",").map((h) => h.trim()).filter(Boolean);

export const dynamic = "force-dynamic";

function refuse(status: number, code: string, message: string) {
  return Response.json({ error: { code, message } }, { status });
}

/** Why this request may not use the proxy, or null. */
function problem(req: NextRequest, path: string[]): Response | null {
  const host = req.headers.get("host") ?? "";
  if (!HOSTS.includes(host)) return refuse(403, "forbidden_host", `host ${host} is not allowed`);
  const origin = req.headers.get("origin");
  if (origin) {
    let originHost = "";
    try {
      originHost = new URL(origin).host;
    } catch {}
    if (originHost !== host) return refuse(403, "forbidden_origin", "cross-origin request refused");
  }
  // Segments arrive decoded: "%2e%2e" is "..", which fetch would resolve out of /v1.
  if (path.some((seg) => seg === "" || seg === "." || seg === ".." || seg.includes("/"))) {
    return refuse(400, "bad_path", "invalid path");
  }
  return null;
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const refused = problem(req, path);
  if (refused) return refused;
  const url = `${API}/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${TOKEN}` });
  for (const name of ["content-type", "last-event-id"]) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body: req.method === "GET" ? undefined : await req.text(),
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return refuse(502, "api_unreachable",
      `The RepoPilot API at ${API} is not responding. Start it with "repopilot serve".`);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}

export { forward as GET, forward as POST };
