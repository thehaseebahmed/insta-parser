"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  CliError,
  parseArgs,
  baseUrlFrom,
  positiveNumber,
  request,
  jobStart,
  jobWait,
} = require("../bin/insta-parser.js");

function withEnv(name, value, fn) {
  const prev = process.env[name];
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env[name];
    else process.env[name] = prev;
  }
}

function withFetch(impl, fn) {
  const original = global.fetch;
  global.fetch = impl;
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      global.fetch = original;
    });
}

test("parseArgs separates positionals from known flags", () => {
  const { positional, flags } = parseArgs([
    "job",
    "start",
    "--base-url",
    "http://x",
    "https://www.instagram.com/reel/ABC/",
  ]);
  assert.deepEqual(positional, ["job", "start", "https://www.instagram.com/reel/ABC/"]);
  assert.equal(flags.baseUrl, "http://x");
});

test("parseArgs recognizes -h and --help", () => {
  assert.equal(parseArgs(["-h"]).flags.help, true);
  assert.equal(parseArgs(["--help"]).flags.help, true);
});

test("parseArgs rejects unknown long options", () => {
  assert.throws(() => parseArgs(["--bogus"]), CliError);
});

test("baseUrlFrom prefers the --base-url flag over the env var", () => {
  withEnv("INSTA_PARSER_URL", "http://env-host", () => {
    assert.equal(baseUrlFrom({ baseUrl: "http://flag-host/" }), "http://flag-host");
  });
});

test("baseUrlFrom falls back to the env var, then the default, stripping trailing slashes", () => {
  withEnv("INSTA_PARSER_URL", undefined, () => {
    assert.equal(baseUrlFrom({}), "http://localhost:8420");
  });
  withEnv("INSTA_PARSER_URL", "http://homelab:9000/", () => {
    assert.equal(baseUrlFrom({}), "http://homelab:9000");
  });
});

test("positiveNumber returns the fallback when the flag is unset", () => {
  assert.equal(positiveNumber(undefined, 7, "--interval"), 7);
});

test("positiveNumber parses a valid value", () => {
  assert.equal(positiveNumber("12", 7, "--interval"), 12);
});

test("positiveNumber rejects zero, negative, and non-numeric values", () => {
  for (const bad of ["0", "-3", "abc"]) {
    assert.throws(() => positiveNumber(bad, 7, "--interval"), CliError);
  }
});

test("jobStart requires a url", async () => {
  await assert.rejects(() => jobStart("http://x", undefined), CliError);
});

test("request throws a CliError with a helpful message when the fetch itself fails", async () => {
  await withFetch(
    async () => {
      throw new Error("connect ECONNREFUSED");
    },
    async () => {
      await assert.rejects(
        () => request("GET", "http://nowhere:1", "/health"),
        (err) => err instanceof CliError && /Could not reach insta-parser/.test(err.message)
      );
    }
  );
});

test("request surfaces the server's error detail on a non-2xx response", async () => {
  await withFetch(
    async () => new Response(JSON.stringify({ detail: "Unknown job_id" }), { status: 404 }),
    async () => {
      await assert.rejects(
        () => request("GET", "http://x", "/jobs/whatever"),
        (err) => err instanceof CliError && /insta-parser returned 404: Unknown job_id/.test(err.message)
      );
    }
  );
});

test("jobWait polls until a terminal status and returns the job", async () => {
  let calls = 0;
  await withFetch(
    async () => {
      calls += 1;
      const status = calls < 3 ? "running" : "done";
      return new Response(
        JSON.stringify({ job_id: "abc", status, step: status === "done" ? null : "transcribe" }),
        { status: 200 }
      );
    },
    async () => {
      const job = await jobWait("http://x", "abc", { interval: "0.01" });
      assert.equal(job.status, "done");
      assert.equal(calls, 3);
    }
  );
});

test("jobWait gives up and throws once the timeout elapses", async () => {
  await withFetch(
    async () =>
      new Response(JSON.stringify({ job_id: "abc", status: "running", step: "transcribe" }), {
        status: 200,
      }),
    async () => {
      await assert.rejects(
        () => jobWait("http://x", "abc", { interval: "0.01", timeout: "0.03" }),
        (err) => err instanceof CliError && /Gave up waiting/.test(err.message)
      );
    }
  );
});
