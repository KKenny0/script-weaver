import { expect, test } from "@playwright/test";
import {
  gateRespond,
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  release,
  seedOutline,
  seedProject,
  seedRun,
  sseClosed,
  sseForceClose,
  sseCount,
  sseDispatch,
  updateRunStatus,
  waitForHeld,
} from "./helpers";

/**
 * Generation as a server-owned run (ticket #14), driven through stubs.
 *
 * Submission is POST /projects/{id}/generate — always gated with an in-page
 * mock so no real request ever reaches the model-bound endpoint — and the
 * progress view is an EventSource on /runs/{run_id}/events, replaced by the
 * stand-in. The e2e backend is model-less by construction
 * (playwright.config.ts neutralizes every provider key).
 *
 * Disconnect semantics changed with #14: a dropped subscription is a lost
 * VIEW, not a failed run — generation keeps running server-side. Explicit
 * stop, refresh-restore and failed-run notices each get their own test.
 */

const RUN_EVENTS = "/runs/";
const SUBMIT_KEY = "POST /generate";

function mockRunSubmission() {
  return {
    run: { run_id: "run_stub_1", status: "running", completed_steps: [] },
    created: true,
  };
}

async function submitStubbedGeneration(page: import("@playwright/test").Page, idea: string) {
  // The gate rules live in the page: navigate first, then arm the mock so
  // the composer's POST /generate resolves with the stubbed run.
  await openApp(page, "/");
  await gateRespond(page, SUBMIT_KEY, 200, mockRunSubmission());
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill(idea);
  await page.locator("textarea ~ button").click();
  // The creation POST is real; the /generate POST is held by the gate —
  // release it only once it is actually held, then the run view opens.
  await waitForHeld(page, SUBMIT_KEY);
  await release(page, SUBMIT_KEY);
}

let egress: string[];

test.beforeEach(async ({ page }) => {
  egress = modelEgressWatcher(page);
  await installGate(page);
  await installSSEStub(page);
});

test.afterEach(async () => {
  expect(egress, "no real request may reach a model-bound endpoint").toEqual([]);
});

test("the e2e backend itself is model-less: a submitted run fails fast without keys", async ({ request }) => {
  // Structural guarantee: even a machine whose root .env holds real keys
  // cannot arm this backend — the run's engine initialization fails before
  // any provider call and settles the run failed.
  const seeded = await seedProject(request, "无模型探针项目P");
  // The read-only GET alias never starts anything: without a run it answers
  // a clear submit-first hint. (Checked first — the POST below leaves a
  // failed run row behind, and the alias would then subscribe to it
  // instead of answering no_run.)
  const alias = await request.get(
    `http://127.0.0.1:8310/api/projects/${seeded.project_id}/generate`,
    { timeout: 5_000 },
  );
  expect(alias.status()).toBe(404);
  expect((await alias.json()).detail.code).toBe("no_run");

  // Admission is durable (review R3: engine construction lives inside the
  // server-owned task); with no keys the run settles failed immediately.
  const res = await request.post(
    `http://127.0.0.1:8310/api/projects/${seeded.project_id}/generate`,
    { data: {}, timeout: 5_000 },
  );
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.created).toBe(true);
  await expect
    .poll(
      async () => {
        const latest = await request.get(
          `http://127.0.0.1:8310/api/projects/${seeded.project_id}/runs/latest`,
          { timeout: 5_000 },
        );
        return (await latest.json()).status;
      },
      { timeout: 5_000 },
    )
    .toBe("failed");
  const final = await (
    await request.get(
      `http://127.0.0.1:8310/api/projects/${seeded.project_id}/runs/latest`,
      { timeout: 5_000 },
    )
  ).json();
  expect(final.error).toContain("模型不可用");
});

test("stubbed run: progress + succeeded done complete the project without any model call", async ({ page }) => {
  await submitStubbedGeneration(page, "替身生成成功项目S");
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);
  await expect(page.getByText("正在生成...")).toBeVisible();

  expect(await sseDispatch(page, RUN_EVENTS, "progress", { message: "正在提炼大纲…" })).toBe(true);
  await expect(page.getByText("正在提炼大纲…")).toBeVisible();

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await expect(page.getByText(/🎉 全部生成完成/)).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("stubbed run: a failed run reports the error and frees the composer", async ({ page }) => {
  await submitStubbedGeneration(page, "替身失败项目F");
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  expect(
    await sseDispatch(page, RUN_EVENTS, "done", { status: "failed", error: "模拟生成失败" }),
  ).toBe(true);
  await expect(page.getByText(/❌ 生成未完成：模拟生成失败/)).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("a dropped connection is a lost view, not a failed run (updated #14 semantics)", async ({ page }) => {
  await submitStubbedGeneration(page, "替身断连项目X");
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  // Transient drop (auto-reconnect): the run keeps going server-side, the
  // composer stays locked, no failure is claimed.
  expect(await sseDispatch(page, RUN_EVENTS, "error", {})).toBe(true);
  await expect(page.getByText("正在重新连接生成进度…")).toBeVisible();
  await expect(page.getByText("正在生成...")).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toHaveCount(0);

  // The reconnected view then sees the run through to success.
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await expect(page.getByText(/🎉 全部生成完成/)).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("explicit stop: the stop button posts stop, cancelled done keeps saved stages", async ({ page }) => {
  await submitStubbedGeneration(page, "替身停止项目T");
  await expect(page).toHaveURL(/project=/);

  const stopButton = page.getByRole("button", { name: "停止生成" });
  await expect(stopButton).toBeVisible();

  await gateRespond(page, "POST /stop", 200, {
    run: { run_id: "run_stub_1", status: "stopping" },
    stopped: true,
  });
  await stopButton.click();
  await expect(page.getByText("正在停止…")).toBeVisible();
  // Let the mocked stop response resolve; the cancelled done closes the view.
  await waitForHeld(page, "POST /stop");
  await release(page, "POST /stop");
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "cancelled" })).toBe(true);
  await expect(page.getByText(/🛑 生成已停止/)).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
  await expect(page.getByText("正在停止…")).toHaveCount(0);
});

test("refresh/reopen finds the running generation and resubscribes to it", async ({ page, request }) => {
  const seeded = await seedProject(request, "找回进度项目R");
  const runId = await seedRun(seeded.project_id, "running");

  await openApp(page, `/?project=${seeded.project_id}`);
  await expect(page.getByText(/已重新连接进度/)).toBeVisible();
  await expect(page.getByText("正在生成...")).toBeVisible();
  // The resubscription targets the persisted run's event stream.
  expect(await sseCount(page)).toBe(1);
  const targets = await page.evaluate(
    (frag) => (window as any).__mockSSE.map((s: any) => s.url as string)
      .filter((u: string) => u.includes(frag)),
    RUN_EVENTS,
  );
  expect(targets).toHaveLength(1);
  expect(targets[0]).toContain(runId);

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await expect(page.getByText(/🎉 全部生成完成/)).toBeVisible();
});

test("reopening a project with a previously failed run shows a one-shot notice", async ({ page, request }) => {
  const seeded = await seedProject(request, "失败遗留项目L");
  await seedRun(seeded.project_id, "failed");

  await openApp(page, `/?project=${seeded.project_id}`);
  await expect(page.getByText(/上次生成未完成/)).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
  // No subscription is opened for a terminal run.
  expect(await sseCount(page)).toBe(0);
});

test("R5: re-clicking the running project keeps the observation; a lost view recovers", async ({ page, request }) => {
  const seeded = await seedProject(request, "找回与保留项目K");
  await seedRun(seeded.project_id, "running");
  await openApp(page, `/?project=${seeded.project_id}`);
  await expect(page.getByText(/已重新连接进度/)).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();

  // Re-click the CURRENT project: the live observation stays (connection
  // not closed, stop button intact), progress keeps flowing.
  await page.getByRole("button", { name: new RegExp(`^${seeded.title}`) }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  expect(await sseClosed(page, RUN_EVENTS)).toBe(false);
  expect(await sseDispatch(page, RUN_EVENTS, "progress", { message: "正在写剧本…" })).toBe(true);
  await expect(page.getByText("正在写剧本…")).toBeVisible();

  // A truly lost view (CLOSED, no auto-retry) reports the drop; re-clicking
  // the project re-queries and resubscribes without a page refresh. (The
  // first reconnect notice was legitimately replaced in-place by the live
  // progress message above, so count the new subscription, not messages.)
  expect(await sseForceClose(page, RUN_EVENTS)).toBe(true);
  await expect(page.getByText(/与生成进度的连接已断开/)).toBeVisible();
  await page.getByRole("button", { name: new RegExp(`^${seeded.title}`) }).click();
  await expect(page.getByText(/已重新连接进度/)).toBeVisible();
  expect(await sseCount(page)).toBe(2); // a fresh subscription opened
  expect(await sseDispatch(page, RUN_EVENTS, "progress", { message: "恢复后的进度" })).toBe(true);
  await expect(page.getByText("恢复后的进度")).toBeVisible();
});

test("R6: succeeded run whose content refresh fails is not a failed run", async ({ page }) => {
  await submitStubbedGeneration(page, "刷新失败成功故事V");
  await expect(page).toHaveURL(/project=/);
  const pid = new URL(page.url()).searchParams.get("project")!;

  // The outcome GET fails (backend hiccup) — the run itself succeeded.
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await expect(page.getByText(/生成已完成，但内容刷新失败/)).toBeVisible();
  await expect(page.getByText(/生成未完成/)).toHaveCount(0);
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "刷新内容" })).toBeVisible();

  // The refresh entry only re-reads: unblock the GET, click, content loads.
  await page.unroute(`**/api/projects/${pid}`);
  await page.getByRole("button", { name: "刷新内容" }).click();
  await expect(page.getByText(/项目内容已刷新/)).toBeVisible();
  await expect(page.getByRole("button", { name: "刷新内容" })).toHaveCount(0);
});

test("R6: a failed run with a failing refresh stays a failure with a retry entry", async ({ page }) => {
  await submitStubbedGeneration(page, "失败刷新失败故事W");
  await expect(page).toHaveURL(/project=/);
  const pid = new URL(page.url()).searchParams.get("project")!;
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "failed", error: "模拟失败" })).toBe(true);
  await expect(page.getByText(/❌ 生成未完成：模拟失败/)).toBeVisible();
  await expect(page.getByText(/内容刷新失败/)).toBeVisible();
  await expect(page.getByText(/生成已完成/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "刷新内容" })).toBeVisible();
});

test("R6: a cancelled run with a failing refresh reports the stop, not a failure", async ({ page }) => {
  await submitStubbedGeneration(page, "停止刷新失败故事U");
  await expect(page).toHaveURL(/project=/);
  const pid = new URL(page.url()).searchParams.get("project")!;
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "cancelled" })).toBe(true);
  await expect(page.getByText(/🛑 生成已停止/)).toBeVisible();
  await expect(page.getByText(/内容刷新失败/)).toBeVisible();
  await expect(page.getByText(/生成未完成/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "刷新内容" })).toBeVisible();
});

test("R7: the status snapshot shows current progress without waiting for new events", async ({ page, request }) => {
  const seeded = await seedProject(request, "快照进度项目Z");
  await seedRun(seeded.project_id, "running", {
    message: "正在撰写第三场剧本…",
    completedSteps: ["idea_refiner", "structurer"],
  });
  await openApp(page, `/?project=${seeded.project_id}`);
  await expect(page.getByText(/已重新连接进度/)).toBeVisible();

  // The server's first event on subscribe is the snapshot: display it
  // immediately — no progress event needed.
  expect(await sseDispatch(page, RUN_EVENTS, "status", {
    status: "running",
    last_progress: { stage: "scriptwriter", message: "正在撰写第三场剧本…" },
    completed_steps: ["idea_refiner", "structurer"],
  })).toBe(true);
  await expect(page.getByText("正在撰写第三场剧本…")).toBeVisible();

  // A repeated snapshot (reconnect) replaces the message — no stacking.
  expect(await sseDispatch(page, RUN_EVENTS, "status", {
    status: "running",
    last_progress: { stage: "scriptwriter", message: "正在撰写第三场剧本…" },
    completed_steps: ["idea_refiner", "structurer"],
  })).toBe(true);
  await expect(page.getByText("正在撰写第三场剧本…")).toHaveCount(1);

  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await expect(page.getByText(/🎉 全部生成完成/)).toBeVisible();
});

test("switching projects mid-generation closes the view; late events cannot touch B", async ({ page, request }) => {
  const b = await seedProject(request, "切换关闭目标项目B");
  await seedOutline(b.project_id, "B的大纲不动如山");

  await submitStubbedGeneration(page, "切换关闭项目C");
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  // Opening B must close C's view — the page's closeStreamRef must point at
  // the one live subscription owner. C's run keeps going server-side.
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  expect(await sseClosed(page, RUN_EVENTS)).toBe(true);

  // After close() nothing can be delivered any more (real EventSource
  // semantics) — and even a forced attempt leaves B intact.
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "failed", error: "迟到的失败" })).toBe(false);
  await expect(page.getByText("B的大纲不动如山")).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toHaveCount(0);
});

// ── Review round 3 (P2 #2/#3): terminal restore and stale-response guards ──

test("P2-2: a run that finished while the view was lost is restored on re-click (every terminal status)", async ({ page, request }) => {
  const cases = [
    { status: "succeeded" as const, expectMsg: /已恢复上次生成的结果/, pill: "已完成" },
    { status: "cancelled" as const, expectMsg: /上次生成已停止/, pill: "已完成" },
    { status: "failed" as const, expectMsg: /上次生成未完成/, pill: "出错了" },
    { status: "interrupted" as const, expectMsg: /上次生成未完成/, pill: "出错了" },
  ];
  for (const tc of cases) {
    const p = await seedProject(request, `终态恢复项目${tc.status}`);
    const runId = await seedRun(p.project_id, "running");
    await openApp(page, `/?project=${p.project_id}`);
    // The running run is resubscribed (stop button + one subscription). The
    // final open notice is the open flow's deterministic settle point —
    // only then are chat-message assertions safe from its session reset.
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    expect(await sseCount(page)).toBe(1);
    await expect(page.getByText(/已打开项目/)).toBeVisible();

    // The view is lost; the run keeps going server-side and finishes while
    // nobody is watching — saving a NEW outline along the way.
    expect(await sseForceClose(page, RUN_EVENTS)).toBe(true);
    await expect(page.getByText(/与生成进度的连接已断开/)).toBeVisible();
    await seedOutline(p.project_id, `断连后新大纲${tc.status}`);
    await updateRunStatus(runId, tc.status);

    // Re-clicking the CURRENT project re-queries latest: a terminal run
    // restores BOTH the outcome message and the fresh content.
    await page.getByRole("button", { name: new RegExp(`^${p.title}`) }).click();
    await expect(page.getByText(`断连后新大纲${tc.status}`, { exact: true })).toBeVisible();
    await expect(page.getByText(tc.expectMsg)).toBeVisible();
    await expect(page.getByText(tc.pill, { exact: true })).toBeVisible();
    // The restore is read-only: no new subscription for a terminal run.
    expect(await sseCount(page)).toBe(1);
  }
});

test("P2-2: the latest-run query distinguishes 404 no_run from failure, and a failure is retryable by re-clicking", async ({ page, request }) => {
  const p = await seedProject(request, "进度查询失败项目Q");
  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByText(/已打开项目/)).toBeVisible();
  // No generation history at all: 404 no_run is silent, not an error.
  await expect(page.getByText(/查询生成进度失败/)).toHaveCount(0);

  await page.route(`**/runs/latest`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );
  await page.getByRole("button", { name: new RegExp(`^${p.title}`) }).click();
  await expect(page.getByText(/查询生成进度失败/)).toBeVisible();
  await expect(page.getByText(/重新点击当前项目可重试/)).toBeVisible();

  // Unblocking and re-clicking retries the query; the silent no_run answer
  // adds no second message.
  await page.unroute(`**/runs/latest`);
  const retried = page.waitForResponse((r) => r.url().includes("/runs/latest"), { timeout: 5_000 });
  await page.getByRole("button", { name: new RegExp(`^${p.title}`) }).click();
  await retried;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
  await expect(page.getByText(/查询生成进度失败/)).toHaveCount(1);
});

test("P2-3: an old done refresh cannot overwrite a newly reopened session of the same project (A→B→A)", async ({ page, request }) => {
  const a = await seedProject(request, "复审往返项目甲");
  const b = await seedProject(request, "复审往返项目乙");
  await seedOutline(a.project_id, "往返前旧大纲");
  await seedRun(a.project_id, "running");
  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  const old = await (await request.get(`http://127.0.0.1:8310/api/projects/${a.project_id}`)).json();

  // Hold A's FIRST content GET — the done handler's refresh.
  let heldRoute: any;
  let markHeld!: () => void;
  const held = new Promise<void>((resolve) => { markHeld = resolve; });
  let first = true;
  await page.route(`**/api/projects/${a.project_id}`, async (route) => {
    if (first) { first = false; heldRoute = route; markHeld(); return; }
    await route.continue();
  });
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await held;

  // Switch away, update A's content, and reopen A: the page fetches the NEW
  // content; the still-pending old response belongs to a dead session.
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(b.project_id));
  await seedOutline(a.project_id, "往返后新大纲");
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  await expect(page.getByText("往返后新大纲", { exact: true })).toBeVisible();

  // Releasing the stale response must change nothing: no content overwrite,
  // no completion message from the dead session's listener.
  await heldRoute.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(old) });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
  await expect(page.getByText("往返后新大纲", { exact: true })).toBeVisible();
  await expect(page.getByText("往返前旧大纲", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/全部生成完成/)).toHaveCount(0);
});

test("P2-3: an old done refresh that FAILS late cannot write into the current session either", async ({ page, request }) => {
  const a = await seedProject(request, "迟到失败项目甲");
  const b = await seedProject(request, "迟到失败项目乙");
  await seedRun(a.project_id, "running");
  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();

  let heldRoute: any;
  let markHeld!: () => void;
  const held = new Promise<void>((resolve) => { markHeld = resolve; });
  let first = true;
  await page.route(`**/api/projects/${a.project_id}`, async (route) => {
    if (first) { first = false; heldRoute = route; markHeld(); return; }
    await route.continue();
  });
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await held;
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(b.project_id));
  // And back to A — the round trip is the point: a naive isProjectActive
  // guard would let the late failure write into the reopened session.
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(a.project_id));
  await heldRoute.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
  // The late failure belongs to the dead session: no error text, no retry
  // entry, no status damage leaks into the reopened A.
  await expect(page.getByText(/生成已完成，但内容刷新失败/)).toHaveCount(0);
  await expect(page.getByText(/❌ 生成未完成/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "刷新内容" })).toHaveCount(0);
  await expect(page.getByText("出错了", { exact: true })).toHaveCount(0);
});

test("P2-3: reconnecting the SAME project invalidates the old subscription's done continuation", async ({ page, request }) => {
  const a = await seedProject(request, "同项目重连项目N");
  await seedOutline(a.project_id, "重连时的大纲");
  await seedRun(a.project_id, "running");
  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  const old = await (await request.get(`http://127.0.0.1:8310/api/projects/${a.project_id}`)).json();

  let heldRoute: any;
  let markHeld!: () => void;
  const held = new Promise<void>((resolve) => { markHeld = resolve; });
  let first = true;
  await page.route(`**/api/projects/${a.project_id}`, async (route) => {
    if (first) { first = false; heldRoute = route; markHeld(); return; }
    await route.continue();
  });
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "succeeded" })).toBe(true);
  await held;

  // Re-click the SAME project: the lost view is recovered by a NEW
  // subscription (the backend run is still live) — the old done listener
  // must not write over it when its held response finally returns.
  // (Subscription facts only: the open-time "已重新连接进度" chat message is
  // subject to the open notice's session reset, so it is not asserted.)
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  expect(await sseCount(page)).toBe(2);
  await expect(page.getByText(/已打开项目/)).toBeVisible(); // settle before releasing

  await heldRoute.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(old) });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
  await expect(page.getByText(/全部生成完成/)).toHaveCount(0); // old done dropped its message
  await expect(page.getByText("正在生成...")).toBeVisible();   // the new observation stays live
  expect(await sseClosed(page, RUN_EVENTS)).toBe(false);

  // The CURRENT subscription's own events still work end to end.
  expect(await sseDispatch(page, RUN_EVENTS, "progress", { message: "重连后的进度" })).toBe(true);
  await expect(page.getByText("重连后的进度")).toBeVisible();
});

test("P2-3: two overlapping content refreshes returning out of order — only the newest applies", async ({ page, request }) => {
  const a = await seedProject(request, "倒序刷新项目O");
  await seedOutline(a.project_id, "最初的旧大纲");
  await seedRun(a.project_id, "succeeded");
  const real = await (await request.get(`http://127.0.0.1:8310/api/projects/${a.project_id}`)).json();
  const withLogline = (text: string) => {
    const body = JSON.parse(JSON.stringify(real));
    body.outline = { basic_info: { logline: text } };
    body.refined_idea = text;
    return body;
  };

  // #1 = the open-time project GET (real); #2 = the first outcome restore's
  // refresh (HELD); #3+ = later refreshes answered with the newer snapshot.
  let n = 0;
  let heldRoute: any;
  let markHeld!: () => void;
  const held = new Promise<void>((resolve) => { markHeld = resolve; });
  await page.route(`**/api/projects/${a.project_id}`, async (route) => {
    n += 1;
    if (n === 2) { heldRoute = route; markHeld(); return; }
    if (n >= 3) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(withLogline("第二次恢复的新快照")) });
      return;
    }
    await route.continue();
  });

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText("最初的旧大纲", { exact: true })).toBeVisible();
  // The succeeded run's restore opens the first refresh (held). Give it a
  // bounded window, then make a NEWER finished run and re-click: a second
  // restore runs and its refresh answers immediately.
  await Promise.race([held, new Promise((r) => setTimeout(r, 3_000))]);
  await seedRun(a.project_id, "succeeded");
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  await expect(page.getByText("第二次恢复的新快照", { exact: true })).toBeVisible();

  // The OLDER refresh returns last: it must not win.
  if (heldRoute) {
    await heldRoute.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(withLogline("第一次恢复的旧快照")) });
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
        ),
    );
  }
  await expect(page.getByText("第二次恢复的新快照", { exact: true })).toBeVisible();
  await expect(page.getByText("第一次恢复的旧快照", { exact: true })).toHaveCount(0);
});

test("P2-3: switching projects clears the previous session's pending refresh-retry entry", async ({ page, request }) => {
  const b = await seedProject(request, "清理重试目标项目M");
  await submitStubbedGeneration(page, "清理重试故事");
  await expect(page).toHaveURL(/project=/);
  const pid = new URL(page.url()).searchParams.get("project")!;
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );
  expect(await sseDispatch(page, RUN_EVENTS, "done", { status: "failed", error: "模拟失败" })).toBe(true);
  await expect(page.getByRole("button", { name: "刷新内容" })).toBeVisible();

  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(b.project_id));
  await expect(page.getByRole("button", { name: "刷新内容" })).toHaveCount(0);
});

test("P2-3: the page's own open-project response cannot overwrite a reopened project (A→B→A)", async ({ page, request }) => {
  const a = await seedProject(request, "页级往返项目甲");
  const b = await seedProject(request, "页级往返项目乙");
  await seedOutline(a.project_id, "页级旧大纲");
  const old = await (await request.get(`http://127.0.0.1:8310/api/projects/${a.project_id}`)).json();

  await openApp(page, `/?project=${b.project_id}`);
  await expect(page).toHaveURL(new RegExp(b.project_id));

  let heldRoute: any;
  let markHeld!: () => void;
  const held = new Promise<void>((resolve) => { markHeld = resolve; });
  let first = true;
  await page.route(`**/api/projects/${a.project_id}`, async (route) => {
    if (first) { first = false; heldRoute = route; markHeld(); return; }
    await route.continue();
  });

  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click(); // open A — content GET held
  await held;
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click(); // back to B
  await expect(page).toHaveURL(new RegExp(b.project_id));
  await seedOutline(a.project_id, "页级新大纲");
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click(); // reopen A — fresh GET wins
  await expect(page.getByText("页级新大纲", { exact: true })).toBeVisible();

  await heldRoute.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(old) });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(() => resolve()), 0)),
      ),
  );
  await expect(page.getByText("页级新大纲", { exact: true })).toBeVisible();
  await expect(page.getByText("页级旧大纲", { exact: true })).toHaveCount(0);
});
