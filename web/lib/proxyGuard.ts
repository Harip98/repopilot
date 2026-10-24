// The checks the /api/rp proxy makes before it adds the API token to a request. Kept free of
// Next.js imports so Node's test runner can load it directly (see proxyGuard.test.ts).

export interface Refusal {
  status: number;
  code: string;
  message: string;
}

/** Hosts the console is served under, from REPOPILOT_UI_HOSTS (comma-separated). */
export function allowedHosts(setting: string | undefined): string[] {
  return (setting ?? "127.0.0.1:3000,localhost:3000")
    .split(",").map((h) => h.trim()).filter(Boolean);
}

/** Why this request may not use the proxy, or null. */
export function problem(
  hosts: readonly string[],
  hostHeader: string | null,
  origin: string | null,
  path: readonly string[],
): Refusal | null {
  const host = hostHeader ?? "";
  if (!hosts.includes(host)) return { status: 403, code: "forbidden_host", message: `host ${host} is not allowed` };
  if (origin) {
    let originHost = "";
    try {
      originHost = new URL(origin).host;
    } catch {}
    if (originHost !== host) return { status: 403, code: "forbidden_origin", message: "cross-origin request refused" };
  }
  // Segments arrive decoded: "%2e%2e" is "..", which fetch would resolve out of /v1.
  if (path.some((seg) => seg === "" || seg === "." || seg === ".." || seg.includes("/"))) {
    return { status: 400, code: "bad_path", message: "invalid path" };
  }
  return null;
}

/** The API URL a proxied request goes to. Each segment is re-encoded, so none can add a query. */
export function upstreamUrl(api: string, path: readonly string[], search: string): string {
  return `${api}/v1/${path.map(encodeURIComponent).join("/")}${search}`;
}
