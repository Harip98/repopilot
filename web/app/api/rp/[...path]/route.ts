import type { NextRequest } from "next/server";
// Relative and with its extension, so Node's test runner can load this module (route.test.ts).
import { allowedHosts, problem, upstreamUrl } from "../../../../lib/proxyGuard.ts";

// Proxy to the RepoPilot API. The bearer token is added here, on the server, so it never
// reaches the browser. Streaming responses (the SSE timeline) are passed through as-is.
const API = process.env.REPOPILOT_API_URL ?? "http://127.0.0.1:8000";
const TOKEN = process.env.REPOPILOT_API_TOKEN ?? "dev-api-token-change-me";
// Hosts the console is served under. Anything else is refused, so a DNS-rebinding page (which
// reaches this server under its own host name) cannot use the proxy's token.
const HOSTS = allowedHosts(process.env.REPOPILOT_UI_HOSTS);

export const dynamic = "force-dynamic";

function refuse(status: number, code: string, message: string) {
  return Response.json({ error: { code, message } }, { status });
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const refused = problem(HOSTS, req.headers.get("host"), req.headers.get("origin"), path);
  if (refused) return refuse(refused.status, refused.code, refused.message);
  const url = upstreamUrl(API, path, req.nextUrl.search);
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
      // HEAD reaches here through the GET handler, and fetch refuses a body on either.
      body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.text(),
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
