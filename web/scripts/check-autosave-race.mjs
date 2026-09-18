import assert from "node:assert/strict";
import { createSaveCoordinator } from "../src/workbench/save-coordinator.mjs";

let releaseFirst;
const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
let releaseSecond;
const secondGate = new Promise((resolve) => { releaseSecond = resolve; });
let state = { documentId: "doc", content: "A", revision: 0, dirty: true };
const writes = [];
const coordinator = createSaveCoordinator({
  read: () => state,
  write: async (snapshot) => {
    writes.push(snapshot.content);
    if (writes.length === 1) await firstGate;
    if (writes.length === 2) await secondGate;
    return snapshot.revision + 1;
  },
  settled: (snapshot, revision, exact) => {
    if (state.documentId !== snapshot.documentId) return;
    state = { ...state, revision, dirty: exact ? false : state.dirty };
  },
  failed: () => {},
  busy: () => {},
});

const savingA = coordinator.save();
await Promise.resolve();
state = { ...state, content: "B", dirty: true };
const flushingB = coordinator.flush("doc");
releaseFirst();
await savingA;
assert.equal(state.dirty, true, "stale A response must not mark B saved");
releaseSecond();
assert.equal(await flushingB, 2);
assert.deepEqual(writes, ["A", "B"]);
assert.deepEqual(state, { documentId: "doc", content: "B", revision: 2, dirty: false });

let releaseOld;
const oldGate = new Promise((resolve) => { releaseOld = resolve; });
state = { documentId: "old", content: "old draft", revision: 0, dirty: true };
const switching = createSaveCoordinator({
  read: () => state,
  write: async () => { await oldGate; return 1; },
  settled: (snapshot, revision, exact) => {
    if (state.documentId === snapshot.documentId) state = { ...state, revision, dirty: exact ? false : state.dirty };
  },
  failed: () => {},
  busy: () => {},
});
const oldSave = switching.save();
await Promise.resolve();
state = { documentId: "new", content: "unsaved new draft", revision: 0, dirty: true };
releaseOld();
await oldSave;
assert.equal(state.dirty, true, "old document response must not clear the new document warning");
assert.equal(state.revision, 0);

console.log("autosave race check passed");
