import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";

const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));
const freePort = async () => { const server = createServer(); const port = await listen(server); await close(server); return port; };
const waitFor = async (url) => {
  let last;
  for (let index = 0; index < 80; index += 1) {
    try { return await fetch(url); } catch (error) { last = error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw last;
};

async function runCase(name, configure, expected) {
  const root = await mkdtemp(path.join(tmpdir(), `script-weaver-proxy-${name}-`));
  const expectedToken = "correct-creator-token";
  const daemon = createServer((request, response) => {
    if (request.headers.authorization !== `Bearer ${expectedToken}`) {
      response.writeHead(401, { "content-type": "application/json" });
      response.end(JSON.stringify({ detail: "invalid daemon session token" }));
      return;
    }
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ ok: true }));
  });
  const daemonPort = await listen(daemon);
  const nextPort = await freePort();
  const env = { ...process.env, HOME: path.join(root, "home"), SCRIPT_WEAVER_DAEMON_URL: `http://127.0.0.1:${daemonPort}` };
  delete env.SCRIPT_WEAVER_TOKEN_FILE;
  delete env.LOCALAPPDATA;
  delete env.XDG_DATA_HOME;
  await configure({ root, env, expectedToken });
  const child = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", String(nextPort)], {
    cwd: new URL("..", import.meta.url), env, stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stderr.on("data", (chunk) => { output += chunk; });
  try {
    await waitFor(`http://127.0.0.1:${nextPort}/`);
    const response = await fetch(`http://127.0.0.1:${nextPort}/api/projects`);
    const payload = await response.json();
    assert.equal(response.status, expected.status, `${name}: ${output}`);
    if (expected.code) assert.equal(payload.code, expected.code, `${name}: ${JSON.stringify(payload)}`);
    else assert.equal(payload.ok, true, `${name}: ${JSON.stringify(payload)}`);
  } finally {
    child.kill("SIGTERM");
    await new Promise((resolve) => child.once("exit", resolve));
    await close(daemon);
    await rm(root, { recursive: true, force: true });
  }
}

const writeToken = async (file, value) => { await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, value); };

await runCase("default", async ({ env, expectedToken }) => {
  await writeToken(path.join(env.HOME, ".local/share/script-weaver/runtime.token"), expectedToken);
}, { status: 200 });
await runCase("xdg", async ({ root, env, expectedToken }) => {
  env.XDG_DATA_HOME = path.join(root, "xdg");
  await writeToken(path.join(env.XDG_DATA_HOME, "script-weaver/runtime.token"), expectedToken);
  await writeToken(path.join(env.HOME, ".local/share/script-weaver/runtime.token"), "wrong-default");
}, { status: 200 });
await runCase("localappdata", async ({ root, env, expectedToken }) => {
  env.XDG_DATA_HOME = path.join(root, "xdg");
  env.LOCALAPPDATA = path.join(root, "local");
  await writeToken(path.join(env.LOCALAPPDATA, "script-weaver/runtime.token"), expectedToken);
  await writeToken(path.join(env.XDG_DATA_HOME, "script-weaver/runtime.token"), "wrong-xdg");
}, { status: 200 });
await runCase("explicit", async ({ root, env, expectedToken }) => {
  env.XDG_DATA_HOME = path.join(root, "xdg");
  env.LOCALAPPDATA = path.join(root, "local");
  env.SCRIPT_WEAVER_TOKEN_FILE = path.join(root, "explicit.token");
  await writeToken(path.join(env.XDG_DATA_HOME, "script-weaver/runtime.token"), "wrong-xdg");
  await writeToken(path.join(env.LOCALAPPDATA, "script-weaver/runtime.token"), "wrong-local");
  await writeToken(env.SCRIPT_WEAVER_TOKEN_FILE, expectedToken);
}, { status: 200 });
await runCase("missing", async () => {}, { status: 503, code: "creator_token_missing" });
await runCase("unreachable", async ({ root, env, expectedToken }) => {
  env.SCRIPT_WEAVER_TOKEN_FILE = path.join(root, "creator.token");
  await writeToken(env.SCRIPT_WEAVER_TOKEN_FILE, expectedToken);
  env.SCRIPT_WEAVER_DAEMON_URL = `http://127.0.0.1:${await freePort()}`;
}, { status: 503, code: "daemon_unreachable" });
await runCase("auth", async ({ root, env }) => {
  env.SCRIPT_WEAVER_TOKEN_FILE = path.join(root, "creator.token");
  await writeToken(env.SCRIPT_WEAVER_TOKEN_FILE, "wrong-token");
}, { status: 502, code: "daemon_auth_failed" });

console.log("production proxy token/error contract: ok");
