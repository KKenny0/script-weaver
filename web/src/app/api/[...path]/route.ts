import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 2 * 1024 * 1024;
const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

function isLoopbackHost(host: string | null | undefined): boolean {
  if (!host) return false;
  try {
    return LOOPBACK_HOSTNAMES.has(new URL(`http://${host}`).hostname);
  } catch {
    return false;
  }
}

function daemonBaseUrl(): URL {
  const raw = process.env.SCRIPT_WEAVER_DAEMON_URL || "http://127.0.0.1:8000";
  const url = new URL(raw);
  // new URL() keeps IPv6 brackets, so "[::1]" is the canonical hostname form here.
  if (!LOOPBACK_HOSTNAMES.has(url.hostname)) {
    throw new Error("SCRIPT_WEAVER_DAEMON_URL 必须指向本机 loopback 地址");
  }
  return url;
}

function creatorTokenPath(): string {
  if (process.env.SCRIPT_WEAVER_TOKEN_FILE) return process.env.SCRIPT_WEAVER_TOKEN_FILE;
  const applicationRoot = process.env.LOCALAPPDATA || process.env.XDG_DATA_HOME;
  if (applicationRoot) return path.join(applicationRoot, "script-weaver", "runtime.token");
  return path.join(process.env.HOME || homedir(), ".local", "share", "script-weaver", "runtime.token");
}

async function readBoundedBody(request: NextRequest, limit: number): Promise<ArrayBuffer | null> {
  if (!request.body) return new ArrayBuffer(0);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (!value) continue;
    total += value.byteLength;
    if (total > limit) {
      reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const body = new ArrayBuffer(total);
  const view = new Uint8Array(body);
  let offset = 0;
  for (const chunk of chunks) {
    view.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

async function proxy(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  if (!isLoopbackHost(request.headers.get("host"))) {
    return Response.json({ detail: "script-weaver workbench 只接受本机访问" }, { status: 403 });
  }
  const method = request.method.toUpperCase();
  const origin = request.headers.get("origin");
  if (!["GET", "HEAD"].includes(method) && origin) {
    // "Origin: null" and other malformed values must be rejected, never crash the handler.
    let originLoopback = false;
    try {
      originLoopback = isLoopbackHost(new URL(origin).host);
    } catch {
      originLoopback = false;
    }
    if (!originLoopback) {
      return Response.json({ detail: "script-weaver workbench 只接受本机调用" }, { status: 403 });
    }
  }
  let token: string;
  const tokenFile = creatorTokenPath();
  try {
    token = (await readFile(tokenFile, "utf8")).trim();
  } catch {
    return Response.json({
      code: "creator_token_missing",
      detail: `未找到 creator token：${tokenFile}。请先运行 uv run script-weaverd。`,
    }, { status: 503 });
  }
  let daemon: URL;
  try {
    daemon = daemonBaseUrl();
  } catch (reason) {
    return Response.json({ detail: reason instanceof Error ? reason.message : "daemon 地址不合法" }, { status: 503 });
  }
  let body: ArrayBuffer | null = null;
  if (!["GET", "HEAD"].includes(method)) {
    body = await readBoundedBody(request, MAX_BODY_BYTES);
    if (body === null) return Response.json({ detail: "request body too large" }, { status: 413 });
  }
  const parts = (await params).path.map(encodeURIComponent).join("/");
  const target = new URL(`/api/${parts}${request.nextUrl.search}`, daemon);
  const headers = new Headers(request.headers);
  headers.delete("authorization");
  headers.delete("cookie");
  headers.delete("host");
  headers.delete("content-length");
  headers.set("authorization", `Bearer ${token}`);
  if (body !== null) headers.set("content-length", String(body.byteLength));
  let response: Response;
  try {
    response = await fetch(target, { method, headers, body, cache: "no-store", redirect: "error" });
  } catch {
    return Response.json({
      code: "daemon_unreachable",
      detail: `无法连接 script-weaverd（${daemon.origin}）。请确认 uv run script-weaverd 正在运行。`,
    }, { status: 503 });
  }
  if (response.status === 401 || response.status === 403) {
    return Response.json({
      code: "daemon_auth_failed",
      detail: "Workbench 与 script-weaverd 的 creator token 不一致。请重启 daemon 和 Web 工作台后重试。",
    }, { status: 502 });
  }
  const outgoing = new Headers(response.headers);
  outgoing.delete("content-length");
  outgoing.delete("content-encoding");
  return new Response(response.body, { status: response.status, headers: outgoing });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
