import type { APIRequestContext, Page } from "@playwright/test";

/**
 * In-page request gate: deterministic race control without fixed waits.
 *
 * `gate(page, "GET /versions")` holds matching fetches; `release(page, key)`
 * resolves the OLDEST held request (FIFO), `releaseAll` resolves everything.
 * `gateRespond` additionally replaces the response body when released (e.g.
 * to fake a 500). Requests started after `ungate` pass through untouched.
 *
 * Keys are "<METHOD> <url-fragment>", matched via url.includes(fragment).
 */
export async function installGate(page: Page) {
  await page.addInitScript(() => {
    const w = window as any;
    w.__gateRules = new Map<string, null | { status: number; body: unknown }>();
    w.__gatePending = new Map<string, Array<() => void>>();

    const orig = window.fetch.bind(window);
    window.fetch = (input: any, init: any) => {
      const url = typeof input === "string" ? input : input.url;
      const method = ((init && init.method) || "GET").toUpperCase();
      for (const key of w.__gateRules.keys()) {
        const [m, frag] = key.split(" ");
        if (method === m && url.includes(frag)) {
          if (!w.__gatePending.has(key)) w.__gatePending.set(key, []);
          const queue = w.__gatePending.get(key);
          return new Promise<void>((resolve) => queue.push(resolve)).then(() => {
            const mock = w.__gateRules.get(key);
            if (mock) {
              return new Response(JSON.stringify(mock.body), {
                status: mock.status,
                headers: { "Content-Type": "application/json" },
              });
            }
            return orig(input, init);
          });
        }
      }
      return orig(input, init);
    };
  });
}

export async function gate(page: Page, key: string) {
  await page.evaluate((key) => {
    (window as any).__gateRules.set(key, null);
  }, key);
}

export async function gateRespond(
  page: Page,
  key: string,
  status: number,
  body: unknown,
) {
  await page.evaluate(
    ({ key, status, body }) => {
      (window as any).__gateRules.set(key, { status, body });
    },
    { key, status, body },
  );
}

export async function ungate(page: Page, key: string) {
  await page.evaluate((key) => {
    (window as any).__gateRules.delete(key);
  }, key);
}

/** Resolve the oldest held request for this key (FIFO). */
export async function release(page: Page, key: string) {
  await page.evaluate((key) => {
    const queue = (window as any).__gatePending.get(key) || [];
    const resolve = queue.shift();
    if (resolve) resolve();
  }, key);
}

/** Wait until at least one request is held by the gate for this key.
 *
 * Releasing before the app has actually issued the request would let it
 * slip through to the real network; this makes the release deterministic.
 */
export async function waitForHeld(page: Page, key: string, timeout = 5_000) {
  await page.waitForFunction(
    (k) => {
      const queue = (window as any).__gatePending.get(k);
      return Array.isArray(queue) && queue.length > 0;
    },
    key,
    { timeout },
  );
}

export async function releaseAll(page: Page, key: string) {
  await page.evaluate((key) => {
    const queue = (window as any).__gatePending.get(key) || [];
    queue.splice(0).forEach((resolve: () => void) => resolve());
  }, key);
}

/** Seed a project directly against the isolated backend.
 *
 * The title gets a unique suffix: the e2e data dir persists across runs,
 * so identical titles would produce ambiguous list entries.
 */
export async function seedProject(
  request: APIRequestContext,
  title: string,
): Promise<{ project_id: string; revision: number; title: string }> {
  const unique = `${title}-${Math.random().toString(36).slice(2, 8)}`;
  const res = await request.post("http://127.0.0.1:8310/api/projects", {
    data: { user_input: `想法：${unique}`, title: unique },
  });
  const body = await res.json();
  return { ...body, title: unique };
}

/** Advance a seeded project to revision 2 (rename) so history has entries. */
export async function seedSecondRevision(
  request: APIRequestContext,
  projectId: string,
  newTitle: string,
): Promise<void> {
  const res = await request.patch(`http://127.0.0.1:8310/api/projects/${projectId}`, {
    data: { title: newTitle, expected_revision: 1 },
  });
  if (!res.ok()) throw new Error(`seedSecondRevision failed: ${res.status()}`);
}

/**
 * In-page EventSource stand-in for the generation SSE endpoint.
 *
 * The e2e backend runs model-less on purpose (see playwright.config.ts);
 * replacing window.EventSource before hydration means the suite can drive
 * a generation connection's full lifecycle — waiting, progress, done,
 * error, close — deterministically, and no test can ever open a real
 * /generate connection (which would start a real pipeline run).
 */
export async function installSSEStub(page: Page) {
  await page.addInitScript(() => {
    class MockEventSource {
      url: string;
      readyState = 1; // OPEN
      onopen: ((e?: unknown) => void) | null = null;
      onmessage: ((e?: unknown) => void) | null = null;
      onerror: ((e?: unknown) => void) | null = null;
      closed = false; // test-visible marker: close() was called
      private listeners = new Map<string, Set<(e: unknown) => void>>();

      constructor(url: string) {
        this.url = url;
        (window as any).__mockSSE.push(this);
      }

      addEventListener(type: string, cb: (e: unknown) => void) {
        if (!this.listeners.has(type)) this.listeners.set(type, new Set());
        this.listeners.get(type)!.add(cb);
      }

      removeEventListener(type: string, cb: (e: unknown) => void) {
        this.listeners.get(type)?.delete(cb);
      }

      close() {
        this.closed = true;
        this.readyState = 2; // CLOSED
      }

      // Test driver: deliver an event the way the browser would. Like a
      // real EventSource, nothing is delivered after close().
      __dispatch(type: string, payload: unknown): boolean {
        if (this.closed) return false;
        const e = { data: typeof payload === "string" ? payload : JSON.stringify(payload) };
        if (type === "error" && this.onerror) this.onerror(e);
        const set = this.listeners.get(type);
        if (set) for (const cb of set) cb(e);
        return true;
      }
    }
    const w = window as any;
    w.__mockSSE = [];
    w.EventSource = MockEventSource;
  });
}

/** Number of EventSource connections opened so far in this page. */
export async function sseCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as any).__mockSSE.length);
}

/** Index of the LATEST connection whose URL contains the fragment, or -1. */
export async function sseFind(page: Page, urlFragment: string): Promise<number> {
  return page.evaluate((frag) => {
    const list = (window as any).__mockSSE as { url: string }[];
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].url.includes(frag)) return i;
    }
    return -1;
  }, urlFragment);
}

/** Whether the latest matching connection has been closed. */
export async function sseClosed(page: Page, urlFragment: string): Promise<boolean | null> {
  return page.evaluate((frag) => {
    const list = (window as any).__mockSSE as { url: string; closed: boolean }[];
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].url.includes(frag)) return list[i].closed;
    }
    return null;
  }, urlFragment);
}

/** Dispatch an SSE event on the latest matching connection.
 *
 * Returns false when no connection exists or it is already closed (the
 * delivery was refused, exactly like a real EventSource).
 */
export async function sseDispatch(
  page: Page,
  urlFragment: string,
  type: string,
  payload: unknown,
): Promise<boolean> {
  return page.evaluate(
    ({ frag, type, payload }) => {
      const list = (window as any).__mockSSE as any[];
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].url.includes(frag)) return list[i].__dispatch(type, payload) as boolean;
      }
      return false;
    },
    { frag: urlFragment, type, payload },
  );
}

/**
 * Collect real page requests to model-bound endpoints. With the SSE stub
 * installed and refine calls gated, this must stay empty for every e2e
 * test — that is the proof the suite never triggers a real model call.
 */
export function modelEgressWatcher(page: Page): string[] {
  const hits: string[] = [];
  page.on("request", (req) => {
    if (/\/(generate|refine)(\?|$)/.test(req.url())) hits.push(req.url());
  });
  return hits;
}

/**
 * Navigate and wait until the app has hydrated: the mount-time skills
 * request only fires from the client bundle, so it is a reliable signal
 * (SSR-only pages never issue it and would silently drop interactions).
 */
export async function openApp(page: import("@playwright/test").Page, url: string) {
  const skills = page.waitForResponse((r) => r.url().includes("/api/skills"), { timeout: 30_000 });
  await page.goto(url);
  await skills;
}

/**
 * Release a gated request and wait until its outcome has been processed:
 * the real network response (when it goes to the backend) plus two frame
 * ticks so React has committed whatever the handlers decided. Event-driven
 * — no fixed sleeps.
 */
export async function releaseAndSettle(
  page: import("@playwright/test").Page,
  key: string,
  urlFragment: string,
) {
  const responded = page
    .waitForResponse((r) => r.url().includes(urlFragment), { timeout: 5_000 })
    .catch(() => null);
  await release(page, key);
  await responded;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
}

/** Write an outline into a seeded project via the store (no model needed). */
export async function seedOutline(
  projectId: string,
  logline: string,
): Promise<void> {
  const { execFile } = await import("node:child_process");
  const script = `
import sys
from pathlib import Path
from script_weaver.core.project_store import ProjectStore
from script_weaver.core.types import Outline, BasicInfo, ProjectStatus
pid, logline = sys.argv[1], sys.argv[2]
store = ProjectStore(Path("/tmp/script-weaver-e2e-data/main-web/projects.sqlite3"))
rec = store.get_required(pid)
rec.state.outline = Outline(basic_info=BasicInfo(logline=logline, genre="e2e"))
rec.state.refined_idea = logline
rec.state.meta.status = ProjectStatus.STRUCTURED
store.save_state(pid, rec.state, rec.revision, source="manual", summary="e2e seed")
store.close()
`;
  await new Promise<void>((resolve, reject) => {
    execFile(
      "../.venv/bin/python",
      ["-c", script, projectId, logline],
      { cwd: process.cwd() },
      (err) => (err ? reject(err) : resolve()),
    );
  });
}

/** Seed a generation-run row directly (no model needed) and return its id.
 *
 * Status "running" makes the page resubscribe on open (ticket #14 restore);
 * terminal statuses surface as a one-shot notice instead.
 */
export async function seedRun(
  projectId: string,
  status: "running" | "failed" | "succeeded",
): Promise<string> {
  const { execFile } = await import("node:child_process");
  const script = `
import json, sys
from pathlib import Path
from script_weaver.core.project_store import ProjectStore
pid, status = sys.argv[1], sys.argv[2]
store = ProjectStore(Path("/tmp/script-weaver-e2e-data/main-web/projects.sqlite3"))
rec = store.get_required(pid)
run = store.create_generation_run(
    pid, kind="generate", request_key=f"seed-{status}",
    request={"user_input": rec.state.user_input, "auto_approve": True},
    base_revision=rec.revision, base_state_json=rec.state_json,
    checkpoint_json=rec.state_json)
if status != "running":
    store.update_generation_run(run.run_id, status=status,
                                error="种子运行预先写好的失败原因" if status == "failed" else None)
run = store.get_generation_run(run.run_id)
print(json.dumps({"run_id": run.run_id, "status": run.status}))
store.close()
`;
  const { stdout } = await new Promise<{ stdout: string }>((resolve, reject) => {
    execFile(
      "../.venv/bin/python",
      ["-c", script, projectId, status],
      { cwd: process.cwd() },
      (err, stdout) => (err ? reject(new Error(`${err}\n${stdout}`)) : resolve({ stdout })),
    );
  });
  return (JSON.parse(stdout) as { run_id: string }).run_id;
}

/** Write script + storyboard (Chinese, one >200-char prompt) via the store.
 *
 * Mirrors the API unit tests' exportable_state: two shots, the first carrying
 * `longPrompt` as image/video prompt, dialogue "灯不能灭。". Export e2e tests
 * read the very same strings back out of the downloaded files.
 */
export async function seedExportableProject(
  projectId: string,
  longPrompt: string,
): Promise<void> {
  const { execFile } = await import("node:child_process");
  const script = `
import sys
from pathlib import Path
from script_weaver.core.project_store import ProjectStore
from script_weaver.core.types import (
    Outline, BasicInfo, ProjectStatus, Script, ScriptScene, ScriptSceneHeading,
    ScriptBlock, ScriptBlockType, Storyboard, Shot,
)
pid, long_prompt = sys.argv[1], sys.argv[2]
store = ProjectStore(Path("/tmp/script-weaver-e2e-data/main-web/projects.sqlite3"))
rec = store.get_required(pid)
s = rec.state
s.outline = Outline(basic_info=BasicInfo(logline="导出验收", genre="e2e"))
s.refined_idea = "导出验收"
s.meta.status = ProjectStatus.STRUCTURED
s.script = Script(
    title="夜行灯塔",
    scenes=[ScriptScene(
        heading=ScriptSceneHeading(location="灯塔顶层", time_of_day="夜"),
        blocks=[
            ScriptBlock(block_type=ScriptBlockType.ACTION,
                        content={"description": "阿芸推开锈蚀的铁门。"}),
            ScriptBlock(block_type=ScriptBlockType.DIALOGUE,
                        content={"character_name": "阿芸", "dialogue": "灯不能灭。"}),
        ],
        characters_involved=["阿芸"],
    )],
    total_estimated_duration=10,
)
s.storyboard = Storyboard(shots=[
    Shot(shot_id="shot_e2e_01", scene_id="sc_1",
         visual_description="灯塔外景", image_prompt=long_prompt,
         video_prompt=long_prompt, dialogue="灯不能灭。", duration_seconds=2),
    Shot(shot_id="shot_e2e_02", scene_id="sc_1",
         visual_description="阿芸特写", image_prompt="近景：阿芸",
         video_prompt="镜头缓推", duration_seconds=3),
])
s.storyboard.compute_totals()
store.save_state(pid, s, rec.revision, source="manual", summary="e2e export seed")
store.close()
`;
  await new Promise<void>((resolve, reject) => {
    execFile(
      "../.venv/bin/python",
      ["-c", script, projectId, longPrompt],
      { cwd: process.cwd() },
      (err, stdout) =>
        err ? reject(new Error(`${err}\n${stdout}`)) : resolve(),
    );
  });
}

/** Inspect a downloaded ZIP with Python's zipfile and return the evidence:
 * is_zipfile, testzip, member list, the parsed shots JSON, the CSV text and
 * one per-shot TXT. Assertions stay in the spec; this only reads the file.
 */
export async function inspectZip(zipPath: string): Promise<{
  isZip: boolean;
  badMember: string | null;
  members: string[];
  shotsJson: Array<Record<string, unknown>>;
  csvText: string;
  firstShotTxt: string;
}> {
  const { execFile } = await import("node:child_process");
  const script = `
import io, json, sys, zipfile
payload = open(sys.argv[1], "rb").read()
info = {"is_zip": zipfile.is_zipfile(io.BytesIO(payload))}
if info["is_zip"]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        info["bad_member"] = zf.testzip()
        info["members"] = sorted(zf.namelist())
        info["shots_json"] = json.loads(zf.read("video_gen_shots.json").decode("utf-8"))
        info["csv_text"] = zf.read("video_gen_shots.csv").decode("utf-8")
        info["first_shot_txt"] = zf.read("shots/shot_e2e_01.txt").decode("utf-8")
print(json.dumps(info, ensure_ascii=False))
`;
  const { stdout } = await new Promise<{ stdout: string }>((resolve, reject) => {
    execFile(
      "../.venv/bin/python",
      ["-c", script, zipPath],
      { cwd: process.cwd(), maxBuffer: 10 * 1024 * 1024 },
      (err, stdout) => (err ? reject(err) : resolve({ stdout })),
    );
  });
  const raw = JSON.parse(stdout);
  return {
    isZip: raw.is_zip,
    badMember: raw.bad_member ?? null,
    members: raw.members ?? [],
    shotsJson: raw.shots_json ?? [],
    csvText: raw.csv_text ?? "",
    firstShotTxt: raw.first_shot_txt ?? "",
  };
}
