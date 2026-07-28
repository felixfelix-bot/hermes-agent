/**
 * Kanban dashboard plugin — API client.
 *
 * Wraps all ``/api/plugins/kanban/*`` endpoints. Uses the SDK's fetchJSON
 * (auth in both loopback + gated modes) and authedFetch for uploads/downloads.
 */

import type {
  Board,
  BoardListResponse,
  CreateBoardBody,
  CreateBoardResponse,
  CreateTaskBody,
  CreateTaskResponse,
  DashboardConfig,
  DecomposeResponse,
  HomeChannelsResponse,
  OrchestrationSettings,
  ProfileRoster,
  Run,
  SpecifyResponse,
  TaskDetail,
  TaskPatch,
  BulkPatch,
  BulkResponse,
  WorkerLog,
} from "./types";
import { API } from "./constants";
import { getFetchJSON, withBoard } from "./sdk";

/**
 * Deferred SDK fetch — resolves ``window.__HERMES_PLUGIN_SDK__.fetchJSON``
 * on each call (the SDK isn't available at module-load time) and forwards
 * the arguments. This wrapper matches the ``FetchJSON`` signature so
 * callers use it identically to the SDK function.
 */
function fetchJSON<T = unknown>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  // Only pass `init` when defined so GET calls don't forward an extra
  // `undefined` argument — the SDK fetchJSON and test spies assert
  // single-arg calls for parameterless GETs.
  return init !== undefined
    ? (getFetchJSON()(url, init) as Promise<T>)
    : (getFetchJSON()(url) as Promise<T>);
}

// ── Board ─────────────────────────────────────────────────────────────────

export function getBoard(board: string | null, params?: Record<string, string>): Promise<Board> {
  const qs = new URLSearchParams(params || {});
  const url = qs.toString() ? `${API}/board?${qs}` : `${API}/board`;
  return fetchJSON<Board>(withBoard(url, board));
}

export function getBoards(board: string | null): Promise<BoardListResponse> {
  return fetchJSON<BoardListResponse>(withBoard(`${API}/boards`, board));
}

export function createBoard(payload: CreateBoardBody): Promise<CreateBoardResponse> {
  return fetchJSON<CreateBoardResponse>(`${API}/boards`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteBoard(slug: string): Promise<void> {
  return fetchJSON<void>(`${API}/boards/${encodeURIComponent(slug)}`, { method: "DELETE" });
}

// ── Tasks ──────────────────────────────────────────────────────────────────

export function getTask(taskId: string, board: string | null): Promise<TaskDetail> {
  return fetchJSON<TaskDetail>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, board));
}

export function createTask(body: CreateTaskBody, board: string | null): Promise<CreateTaskResponse> {
  return fetchJSON<CreateTaskResponse>(withBoard(`${API}/tasks`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function patchTask(
  taskId: string,
  patch: TaskPatch,
  board: string | null,
): Promise<TaskDetail> {
  return fetchJSON<TaskDetail>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, board), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteTask(taskId: string): Promise<void> {
  return fetchJSON<void>(`${API}/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export function bulkUpdate(body: BulkPatch, board: string | null): Promise<BulkResponse> {
  return fetchJSON<BulkResponse>(withBoard(`${API}/tasks/bulk`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function addComment(
  taskId: string,
  body: string,
  board: string | null,
): Promise<void> {
  return fetchJSON<void>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/comments`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
}

// ── Links ───────────────────────────────────────────────────────────────────

export function addLink(parentId: string, childId: string, board: string | null): Promise<void> {
  return fetchJSON<void>(withBoard(`${API}/links`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId, child_id: childId }),
  });
}

export function removeLink(parentId: string, childId: string, board: string | null): Promise<void> {
  const qs = new URLSearchParams({ parent_id: parentId, child_id: childId });
  return fetchJSON<void>(withBoard(`${API}/links?${qs}`, board), { method: "DELETE" });
}

// ── Attachments ────────────────────────────────────────────────────────────

export function listAttachments(taskId: string, board: string | null): Promise<unknown[]> {
  return fetchJSON<unknown[]>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/attachments`, board));
}

export function deleteAttachment(attachmentId: number, board: string | null): Promise<void> {
  return fetchJSON<void>(withBoard(`${API}/attachments/${attachmentId}`, board), { method: "DELETE" });
}

// ── Diagnostics ──────────────────────────────────────────────────────────────

export function getDiagnostics(): Promise<unknown> {
  return fetchJSON<unknown>(`${API}/diagnostics`);
}

export function getActiveWorkers(): Promise<unknown> {
  return fetchJSON<unknown>(`${API}/workers/active`);
}

// ── Runs ─────────────────────────────────────────────────────────────────────

export function getRun(runId: number): Promise<Run> {
  return fetchJSON<Run>(`${API}/runs/${runId}`);
}

export function inspectRun(runId: number): Promise<unknown> {
  return fetchJSON<unknown>(`${API}/runs/${runId}/inspect`);
}

export function terminateRun(runId: number): Promise<void> {
  return fetchJSON<void>(`${API}/runs/${runId}/terminate`, { method: "POST" });
}

// ── Recovery actions ─────────────────────────────────────────────────────────

export function reclaimTask(taskId: string, reason: string, board: string | null): Promise<void> {
  return fetchJSON<void>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/reclaim`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function specifyTask(taskId: string, board: string | null): Promise<SpecifyResponse> {
  return fetchJSON<SpecifyResponse>(
    withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/specify`, board),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) },
  );
}

export function decomposeTask(taskId: string, board: string | null): Promise<DecomposeResponse> {
  return fetchJSON<DecomposeResponse>(
    withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/decompose`, board),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) },
  );
}

export function reassignTask(
  taskId: string,
  profile: string | null,
  reclaimFirst: boolean,
  reason: string,
  board: string | null,
): Promise<void> {
  return fetchJSON<void>(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/reassign`, board), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile, reclaim_first: reclaimFirst, reason }),
  });
}

// ── Config ───────────────────────────────────────────────────────────────────

export function getConfig(board: string | null): Promise<DashboardConfig> {
  return fetchJSON<DashboardConfig>(withBoard(`${API}/config`, board));
}

export function getHomeChannels(taskId: string, board: string | null): Promise<HomeChannelsResponse> {
  const qs = new URLSearchParams({ task_id: taskId });
  return fetchJSON<HomeChannelsResponse>(withBoard(`${API}/home-channels?${qs}`, board));
}

export function toggleHomeSubscription(
  taskId: string,
  platform: string,
  subscribe: boolean,
  board: string | null,
): Promise<void> {
  const url = withBoard(
    `${API}/tasks/${encodeURIComponent(taskId)}/home-subscribe/${encodeURIComponent(platform)}`,
    board,
  );
  return fetchJSON<void>(url, { method: subscribe ? "POST" : "DELETE" });
}

// ── Stats ────────────────────────────────────────────────────────────────────

export function getStats(board: string | null): Promise<unknown> {
  return fetchJSON<unknown>(withBoard(`${API}/stats`, board));
}

export function getAssignees(board: string | null): Promise<unknown> {
  return fetchJSON<unknown>(withBoard(`${API}/assignees`, board));
}

// ── Worker log ───────────────────────────────────────────────────────────────

export function getWorkerLog(taskId: string, board: string | null, tail = 100000): Promise<WorkerLog> {
  return fetchJSON<WorkerLog>(
    withBoard(`${API}/tasks/${encodeURIComponent(taskId)}/log?tail=${tail}`, board),
  );
}

// ── Dispatch ─────────────────────────────────────────────────────────────────

export function dispatchNudge(board: string | null, max = 8): Promise<unknown> {
  return fetchJSON<unknown>(withBoard(`${API}/dispatch?max=${max}`, board), { method: "POST" });
}

// ── Profiles ─────────────────────────────────────────────────────────────────

export function getProfiles(): Promise<ProfileRoster> {
  return fetchJSON<ProfileRoster>(`${API}/profiles`);
}

export function updateProfileDescription(name: string, description: string): Promise<void> {
  return fetchJSON<void>(`${API}/profiles/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
}

export function autoDescribeProfile(name: string, overwrite: boolean): Promise<{ ok: boolean; reason?: string }> {
  return fetchJSON<{ ok: boolean; reason?: string }>(
    `${API}/profiles/${encodeURIComponent(name)}/describe-auto`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overwrite }),
    },
  );
}

// ── Orchestration ────────────────────────────────────────────────────────────

export function getOrchestration(): Promise<OrchestrationSettings> {
  return fetchJSON<OrchestrationSettings>(`${API}/orchestration`);
}

export function putOrchestration(patch: Partial<OrchestrationSettings>): Promise<OrchestrationSettings> {
  return fetchJSON<OrchestrationSettings>(`${API}/orchestration`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}