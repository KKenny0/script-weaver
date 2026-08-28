export class ApiError extends Error {
  status: number;
  detail: string;
  currentRevisions: Record<string, number> | null;
  raw: unknown;

  constructor(status: number, payload: unknown, fallback: string) {
    const body = (payload && typeof payload === "object" ? payload : {}) as { detail?: unknown; current_revisions?: unknown };
    const detail = typeof body.detail === "string" ? body.detail : fallback;
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.currentRevisions = body.current_revisions && typeof body.current_revisions === "object" ? body.current_revisions as Record<string, number> : null;
    this.raw = payload;
  }
}

export function revisionHint(error: unknown): string {
  if (error instanceof ApiError && error.currentRevisions) {
    const parts = Object.entries(error.currentRevisions).map(([id, revision]) => `${id.slice(0, 8)}→r${revision}`);
    return parts.length ? `（当前 revision：${parts.join("，")}）` : "";
  }
  return "";
}

const json = { "Content-Type": "application/json" };

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...init, headers: { ...json, ...init?.headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(response.status, payload, `HTTP ${response.status}`);
  }
  return response.json();
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
