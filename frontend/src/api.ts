import type { ConfigPayload, StatePayload, Task } from "./types";

export const requestToken =
  document.querySelector<HTMLMetaElement>('meta[name="grok-manager-token"]')?.content ?? "";

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Grok-Manager-Token": requestToken,
  };
  const init: RequestInit = { method: options.method ?? "GET", headers };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(String(payload.error ?? `请求失败 HTTP ${response.status}`));
  }
  return payload as T;
}

export function getState(params: {
  search: string;
  status: string;
  enabled: string;
  page: number;
  pageSize: number;
}): Promise<StatePayload> {
  const query = new URLSearchParams({
    search: params.search,
    status: params.status,
    enabled: params.enabled,
    page: String(params.page),
    page_size: String(params.pageSize),
  });
  return api<StatePayload>(`/api/state?${query.toString()}`);
}

export const getConfig = () => api<ConfigPayload>("/api/config");
export const getTask = (id: string) => api<Task>(`/api/tasks/${encodeURIComponent(id)}`);
export function getSelection(params: {
  search: string;
  status: string;
  enabled: string;
}): Promise<{ ids: number[]; total: number }> {
  const query = new URLSearchParams({
    search: params.search,
    status: params.status,
    enabled: params.enabled,
  });
  return api<{ ids: number[]; total: number }>("/api/accounts/selection?" + query.toString());
}
export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body });
export async function downloadExport(body: Record<string, unknown>): Promise<void> {
  const response = await fetch("/api/accounts/export", {
    method: "POST",
    headers: {
      Accept: "application/octet-stream",
      "Content-Type": "application/json",
      "X-Grok-Manager-Token": requestToken,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(String(payload.error ?? `导出失败 HTTP ${response.status}`));
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? "grok-export";
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}
