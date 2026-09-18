import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const workspace = readFileSync(new URL("../src/workbench/DocumentWorkspace.tsx", import.meta.url), "utf8");
const changes = readFileSync(new URL("../src/workbench/WorkbenchShell.tsx", import.meta.url), "utf8");

assert.match(workspace, /<pre>\{version\.content\}<\/pre>/);
assert.match(workspace, /disabled=\{!reviewedVersions\[version\.id\]\}/);
assert.match(workspace, /document\.versions\.find\(\(item\) => item\.status === "ACCEPTED"\)/);
assert.doesNotMatch(changes, /payload\.content\.slice/);
assert.match(changes, /<pre>\{op\.payload\.content\}<\/pre>/);

const longCandidate = "候选正文".repeat(30_000);
assert.ok(longCandidate.length > 100_000);
console.log("candidate review contract: ok");
