#!/usr/bin/env node
/**
 * Minimal repeatable regression for e2e service isolation (PR #20 review).
 *
 * Verifies that the Playwright entry refuses to run when its service ports
 * (backend 8310, frontend 3100) are already occupied: with
 * reuseExistingServer:false the runner pre-checks each webServer URL before
 * spawning anything, so the run must fail BEFORE any test executes — and
 * the occupying stand-in must never receive a project-creation, /generate
 * or /refine request (readiness probes are the only allowed traffic).
 *
 * Uses only a pure-local HTTP stand-in with fake responses; no real
 * services, no keys, no .env. Run from web/:
 *
 *   node tests/e2e/verify-port-isolation.mjs     (or: npm run check:e2e-ports)
 */
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";

const PORTS = [8310, 3100];
const RUN_TIMEOUT_MS = 180_000;
// What must NEVER reach the stand-in: project creation and the two
// model-bound endpoints. A single leaked request means tests ran against
// an unverified server — exactly the defect this script guards against.
const FORBIDDEN = [
  (line) => line.startsWith("POST /api/projects"),
  (line) => line.includes("/generate"),
  (line) => line.includes("/refine"),
];

function startStandIn(port) {
  const requests = [];
  const server = createServer((req, res) => {
    requests.push(`${req.method} ${req.url}`);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ standIn: true, port }));
  });
  return new Promise((resolve) =>
    server.listen(port, "127.0.0.1", () => resolve({ server, requests, port })),
  );
}

/** Wait until the port is bindable again (playwright cleaned up its child). */
function waitPortFree(port, timeoutMs = 20_000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const tick = () => {
      const probe = createServer();
      probe.once("error", () => {
        if (Date.now() < deadline) setTimeout(tick, 300);
        else resolve(false);
      });
      probe.listen(port, "127.0.0.1", () => probe.close(() => resolve(true)));
    };
    tick();
  });
}

async function occupiedScenario(port) {
  console.log(`\n=== scenario: port ${port} occupied by a local stand-in ===`);
  const standIn = await startStandIn(port);

  const child = spawn("npx", ["playwright", "test"], {
    cwd: process.cwd(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  const killTimer = setTimeout(() => child.kill("SIGKILL"), RUN_TIMEOUT_MS);
  killTimer.unref();
  let output = "";
  child.stdout.on("data", (d) => (output += d));
  child.stderr.on("data", (d) => (output += d));
  const [exitCode] = await once(child, "exit");
  clearTimeout(killTimer);

  // Shut down ONLY the stand-in this verification created.
  await new Promise((resolve) => standIn.server.close(resolve));

  const forbidden = standIn.requests.filter((line) => FORBIDDEN.some((f) => f(line)));
  const failedBeforeTests = exitCode !== 0 && output.includes("is already used") && !/\d+ passed/.test(output);
  const checks = [
    ["playwright exited non-zero", exitCode !== 0],
    ["failure is the occupied-port error (before any test ran)", failedBeforeTests],
    [
      "stand-in received no creation/generate/refine request",
      forbidden.length === 0,
    ],
  ];

  console.log(`playwright exit code: ${exitCode}`);
  console.log(`stand-in traffic (${standIn.requests.length} request(s), readiness probes allowed):`);
  for (const line of standIn.requests) console.log(`  ${line}`);
  if (forbidden.length) console.log("FORBIDDEN requests observed:", forbidden);

  // The other port must be free afterwards (playwright must not leak the
  // service it spawned before the pre-check failed).
  const other = PORTS.find((p) => p !== port);
  const otherFree = await waitPortFree(other, 1_000);
  checks.push([`port ${other} left free after the failed run`, otherFree]);
  const ownFree = await waitPortFree(port, 5_000);
  checks.push([`port ${port} released after closing the stand-in`, ownFree]);

  let ok = true;
  for (const [name, passed] of checks) {
    console.log(`${passed ? "  ✓" : "  ✗"} ${name}`);
    ok &&= passed;
  }
  return ok;
}

let ok = true;
for (const port of PORTS) {
  ok &&= await occupiedScenario(port);
}
console.log(
  ok
    ? "\nport-isolation verification passed: occupied ports fail the run before any test, with zero test traffic to the occupant"
    : "\nport-isolation verification FAILED",
);
process.exit(ok ? 0 : 1);
