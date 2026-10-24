import assert from "node:assert/strict";
import { afterEach, describe, mock, test } from "node:test";
import type { NextRequest } from "next/server";

// route.ts reads its settings when it loads, so set them before importing it.
process.env.REPOPILOT_API_URL = "http://api.test";
process.env.REPOPILOT_API_TOKEN = "server-token";
delete process.env.REPOPILOT_UI_HOSTS;
const route = await import("./route.ts");

type Init = { method?: string; headers?: Record<string, string>; search?: string; body?: string };

/** The parts of a NextRequest the route reads, plus the context Next.js passes with it. */
function call(handler: typeof route.GET, path: string[], init: Init = {}) {
  const req = {
    method: init.method ?? "GET",
    headers: new Headers({ host: "127.0.0.1:3000", ...init.headers }),
    nextUrl: { search: init.search ?? "" },
    signal: new AbortController().signal,
    text: async () => init.body ?? "",
  };
  return handler(req as unknown as NextRequest, { params: Promise.resolve({ path }) });
}

function stubApi(respond: () => Promise<Response>) {
  return mock.method(globalThis, "fetch", respond);
}

afterEach(() => mock.restoreAll());

describe("proxy route", () => {
  test("serves only GET and POST, so Next.js answers 405 to other methods", () => {
    const methods = Object.keys(route).filter((name) => /^[A-Z]+$/.test(name)).sort();
    assert.deepEqual(methods, ["GET", "POST"]);
    assert.equal(route.dynamic, "force-dynamic");
  });

  const refusals: Array<[string, string[], Init, number, string]> = [
    ["a foreign Host", ["runs"], { headers: { host: "attacker.example" } }, 403, "forbidden_host"],
    ["a cross-site POST", ["runs"], { method: "POST", headers: { origin: "https://attacker.example" }, body: "{}" }, 403, "forbidden_origin"],
    ["the null origin", ["runs"], { headers: { origin: "null" } }, 403, "forbidden_origin"],
    ["a dot-dot segment", ["runs", ".."], {}, 400, "bad_path"],
    ["an encoded slash", ["runs", "x/../.."], {}, 400, "bad_path"],
  ];
  for (const [name, path, init, status, code] of refusals) {
    test(`refuses ${name} without calling the API`, async () => {
      const api = stubApi(async () => new Response("{}"));
      const resp = await call(init.method === "POST" ? route.POST : route.GET, path, init);
      assert.equal(resp.status, status);
      assert.equal((await resp.json()).error.code, code);
      assert.equal(api.mock.callCount(), 0);
    });
  }

  test("GET goes to /v1 with the server's token, no body and only the allowed headers", async () => {
    const api = stubApi(async () => new Response("data: {}\n\n", { headers: { "content-type": "text/event-stream" } }));
    const resp = await call(route.GET, ["runs", "abc", "events"], {
      search: "?after=3",
      headers: { "last-event-id": "3", authorization: "Bearer from-browser", cookie: "session=1" },
    });
    assert.equal(api.mock.callCount(), 1);
    const [url, init] = api.mock.calls[0].arguments;
    assert.equal(url, "http://api.test/v1/runs/abc/events?after=3");
    assert.equal(init?.method, "GET");
    assert.equal(init?.body, undefined);
    assert.equal(init?.cache, "no-store");
    const sent = new Headers(init?.headers);
    assert.deepEqual([...sent.keys()].sort(), ["authorization", "last-event-id"]);
    assert.equal(sent.get("authorization"), "Bearer server-token");
    assert.equal(sent.get("last-event-id"), "3");

    assert.equal(resp.status, 200);
    assert.equal(resp.headers.get("content-type"), "text/event-stream");
    assert.equal(resp.headers.get("cache-control"), "no-store");
    assert.equal(await resp.text(), "data: {}\n\n");
  });

  test("HEAD, which Next.js serves with the GET handler, is forwarded without a body", async () => {
    const api = stubApi(async () => new Response(null, { status: 200 }));
    const resp = await call(route.GET, ["runs"], { method: "HEAD" });
    assert.equal(api.mock.callCount(), 1);
    const [, init] = api.mock.calls[0].arguments;
    assert.equal(init?.method, "HEAD");
    assert.equal(init?.body, undefined);
    assert.equal(resp.status, 200);
  });

  test("same-origin POST forwards its body and content type, and the API's status", async () => {
    const api = stubApi(async () => new Response(null, { status: 204 }));
    const body = JSON.stringify({ decision: "approve" });
    const resp = await call(route.POST, ["runs", "abc", "approval"], {
      method: "POST",
      headers: { host: "localhost:3000", origin: "http://localhost:3000", "content-type": "application/json" },
      body,
    });
    const [url, init] = api.mock.calls[0].arguments;
    assert.equal(url, "http://api.test/v1/runs/abc/approval");
    assert.equal(init?.method, "POST");
    assert.equal(init?.body, body);
    assert.equal(new Headers(init?.headers).get("content-type"), "application/json");
    assert.equal(resp.status, 204);
    assert.equal(resp.headers.get("content-type"), "application/json");
  });

  test("an unreachable API is a 502 that says where it looked", async () => {
    stubApi(async () => {
      throw new TypeError("fetch failed");
    });
    const resp = await call(route.GET, ["runs"]);
    assert.equal(resp.status, 502);
    const { error } = await resp.json();
    assert.equal(error.code, "api_unreachable");
    assert.match(error.message, /http:\/\/api\.test/);
  });
});
