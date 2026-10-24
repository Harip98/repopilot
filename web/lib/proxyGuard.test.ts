import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { allowedHosts, problem, upstreamUrl } from "./proxyGuard.ts";

const HOSTS = allowedHosts(undefined);
const RUNS = ["runs"];

const code = (host: string | null, origin: string | null, path: readonly string[] = RUNS) =>
  problem(HOSTS, host, origin, path)?.code ?? null;

describe("allowedHosts", () => {
  test("defaults to the local console addresses", () => {
    assert.deepEqual(allowedHosts(undefined), ["127.0.0.1:3000", "localhost:3000"]);
  });

  test("reads a comma-separated list and drops blanks", () => {
    assert.deepEqual(allowedHosts(" console.example.com , ,127.0.0.1:3000,"), ["console.example.com", "127.0.0.1:3000"]);
  });
});

describe("problem: allowed requests", () => {
  for (const host of ["127.0.0.1:3000", "localhost:3000"]) {
    test(`${host} without an Origin header`, () => {
      assert.equal(problem(HOSTS, host, null, RUNS), null);
    });

    test(`${host} with its own origin`, () => {
      assert.equal(problem(HOSTS, host, `http://${host}`, RUNS), null);
    });
  }

  test("a host from REPOPILOT_UI_HOSTS, same-origin over https", () => {
    const hosts = allowedHosts("console.example.com");
    assert.equal(problem(hosts, "console.example.com", "https://console.example.com", RUNS), null);
  });

  test("ordinary path segments", () => {
    for (const path of [["runs", "3f2a9c", "tool-calls"], ["evals", "dev v2"], ["runs", "a..b"], ["evals", "..."]]) {
      assert.equal(code("127.0.0.1:3000", null, path), null, path.join("/"));
    }
  });
});

describe("problem: forbidden host", () => {
  test("a host that is not in the list (DNS rebinding)", () => {
    assert.deepEqual(problem(HOSTS, "attacker.example:3000", null, RUNS), {
      status: 403,
      code: "forbidden_host",
      message: "host attacker.example:3000 is not allowed",
    });
  });

  test("no Host header", () => {
    assert.equal(code(null, null), "forbidden_host");
  });

  test("an allowed name on another port, or without the port", () => {
    assert.equal(code("127.0.0.1:3001", null), "forbidden_host");
    assert.equal(code("127.0.0.1", null), "forbidden_host");
    assert.equal(code("localhost", null), "forbidden_host");
  });

  test("is checked before the origin and the path", () => {
    assert.equal(code("attacker.example", "http://attacker.example", [".."]), "forbidden_host");
  });
});

describe("problem: forbidden origin", () => {
  const refused = { status: 403, code: "forbidden_origin", message: "cross-origin request refused" };

  test("another site", () => {
    assert.deepEqual(problem(HOSTS, "127.0.0.1:3000", "https://attacker.example", RUNS), refused);
  });

  test('the opaque "null" origin (sandboxed frame, file:// page)', () => {
    assert.deepEqual(problem(HOSTS, "127.0.0.1:3000", "null", RUNS), refused);
  });

  test("an origin that is not a URL", () => {
    assert.equal(code("127.0.0.1:3000", "not a url"), "forbidden_origin");
  });

  test("the same machine under another port or name", () => {
    assert.equal(code("127.0.0.1:3000", "http://127.0.0.1:3001"), "forbidden_origin");
    assert.equal(code("127.0.0.1:3000", "http://localhost:3000"), "forbidden_origin");
    assert.equal(code("localhost:3000", "http://127.0.0.1:3000"), "forbidden_origin");
  });

  test("is checked before the path", () => {
    assert.equal(code("127.0.0.1:3000", "https://attacker.example", [".."]), "forbidden_origin");
  });
});

describe("problem: bad path", () => {
  // Next.js hands the route decoded segments, so "%2e%2e" arrives as ".." and "%2f" as "/".
  const cases: Array<[string, string[]]> = [
    ["..", ["runs", ".."]],
    ["%2e%2e at the start", ["..", "metrics"]],
    [".", ["runs", "."]],
    ["empty segment", ["runs", ""]],
    ["%2f inside a segment", ["runs", "a/b"]],
    ["%2f..", ["runs", "/.."]],
    ["..%2f", ["../health"]],
  ];
  for (const [name, path] of cases) {
    test(name, () => {
      assert.deepEqual(problem(HOSTS, "127.0.0.1:3000", null, path), { status: 400, code: "bad_path", message: "invalid path" });
    });
  }
});

describe("upstreamUrl", () => {
  const API = "http://127.0.0.1:8000";

  test("joins the segments under /v1 and keeps the query", () => {
    assert.equal(upstreamUrl(API, ["runs", "abc", "events"], "?after=3"), "http://127.0.0.1:8000/v1/runs/abc/events?after=3");
  });

  test("re-encodes each segment, so it cannot add a query, fragment or path", () => {
    assert.equal(upstreamUrl(API, ["evals", "dev v2?x=1#y"], ""), "http://127.0.0.1:8000/v1/evals/dev%20v2%3Fx%3D1%23y");
  });

  test("a double-encoded dot segment stays inside /v1", () => {
    const url = new URL(upstreamUrl(API, ["%2e%2e", "%2e%2e", "health"], ""));
    assert.equal(url.pathname, "/v1/%252e%252e/%252e%252e/health");
  });
});
