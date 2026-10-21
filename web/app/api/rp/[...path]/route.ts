import type { NextRequest } from "next/server";

// Proxy to the RepoPilot API. The bearer token is added here, on the server, so it never
// reaches the browser. Streaming responses (the SSE timeline) are passed through as-is.
const API = process.env.REPOPILOT_API_URL ?? "http://127.0.0.1:8000";
const TOKEN = process.env.REPOPILOT_API_TOKEN ?? "dev-api-token-change-me";

export const dynamic = "force-dynamic";

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
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
    return Response.json(
      { error: { code: "api_unreachable", message: `The RepoPilot API at ${API} is not responding. Start it with "repopilot serve".` } },
      { status: 502 },
    );
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
