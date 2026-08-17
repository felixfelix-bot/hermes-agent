/**
 * Kanban dashboard plugin — API client + helpers.
 *
 * Wraps the host SDK's ``fetchJSON``, ``authedFetch``, and ``buildWsUrl``
 * so every call carries the correct board slug and auth. Also provides
 * utility functions used across all components.
 */

import type {
  API_BASE as _,
  BoardData,
  BoardMeta,
  BoardsListResponse,
  BulkResponse,
  CreateBoardBody,
  CreateTaskBody,
  DiagnosticsResponse,
  KanbanConfig,
  OrchestrationSettings,
  ProfileRoster,
  Task,
  TaskDetail,
  UpdateTaskBody,
  HomeChannel,
  ActiveWorkersResponse,
  BoardStats,
} from "./types";

import { API_BASE, LS_BOARD_KEY } from "./types";

// ── SDK singleton ──

interface HermesSDK {
  fetchJSON<T = unknown>(url: string, init?: RequestInit, options?: { allowUnauthorized?: boolean }): Promise<T>;
  authedFetch(url: string, init?: RequestInit): Promise<Response>;
  buildWsUrl(path: string, params?: Record<string, string>): Promise<string>;
  useI18n(): { t: Record<string, unknown>; locale: string };
  utils: { cn: (...classes: Array<string | false | null | undefined>) => string; timeAgo: (ts: number) => string; isoTimeAgo: (iso: string) => string };
  components: Record<string, React.ComponentType<never>>;
  React: typeof import("react");
  hooks: {
    useState: typeof import("react").useState;
    useEffect: typeof import("react").useEffect;
    useCallback: typeof import("react").useCallback;
    useMemo: typeof import("react").useMemo;
    useRef: typeof import("react").useRef;
  };
  api: Record<string, (...args: never[]) => unknown>;
}

function getSDK(): HermesSDK {
  const sdk = (window as unknown as { __HERMES_PLUGIN_SDK__?: HermesSDK }).__HERMES_PLUGIN_SDK__;
  if (!sdk) throw new Error("Plugin SDK not available");
  return sdk;
}

// ── Board selection helpers ──

export function readSelectedBoard(): string | null {
  try {
    const v = window.localStorage.getItem(LS_BOARD_KEY);
    return (v || "").trim() || null;
  } catch {
    return null;
  }
}

export function writeSelectedBoard(slug: string | null): void {
  try {
    if (slug) window.localStorage.setItem(LS_BOARD_KEY, slug);
    else window.localStorage.removeItem(LS_BOARD_KEY);
  } catch {
    /* ignore quota / private mode */
  }
}

export function withBoard(url: string, board: string | null): string {
  if (!board) return url;
  const sep = url.indexOf("?") >= 0 ? "&" : "?";
  return `${url}${sep}board=${encodeURIComponent(board)}`;
}

// ── i18n helper ──

export function tx(
  t: Record<string, unknown> | null | undefined,
  path: string,
  fallback: string,
  vars?: Record<string, string | number>,
): string {
  let node: unknown = t && (t as Record<string, unknown>).kanban;
  if (node && typeof node === "object") {
    const parts = path.split(".");
    for (let i = 0; i < parts.length; i++) {
      const obj = node as Record<string, unknown>;
      if (obj && typeof obj === "object" && parts[i] in obj) {
        node = obj[parts[i]];
      } else {
        node = null;
        break;
      }
    }
  }
  let str = typeof node === "string" ? node : fallback;
  if (vars) {
    for (const k in vars) {
      str = str.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
    }
  }
  return str;
}

export function getColumnLabel(t: Record<string, unknown> | null | undefined, status: string): string {
  return tx(t, "columnLabels." + status, FALLBACK_COLUMN_LABEL_LOOKUP[status] || status);
}

export function getColumnHelp(t: Record<string, unknown> | null | undefined, status: string): string {
  return tx(t, "columnHelp." + status, FALLBACK_COLUMN_HELP_LOOKUP[status] || "");
}

// Avoid circular import: duplicate the fallback dicts here for the helper functions.
const FALLBACK_COLUMN_LABEL_LOOKUP: Record<string, string> = {
  triage: "Triage",
  todo: "Todo",
  ready: "Ready",
  running: "In Progress",
  blocked: "Blocked",
  done: "Done",
  archived: "Archived",
};

const FALLBACK_COLUMN_HELP_LOOKUP: Record<string, string> = {
  triage: "Raw ideas — a specifier will flesh out the spec",
  todo: "Waiting on dependencies or unassigned",
  ready: "Dependencies satisfied; assign a profile to dispatch",
  running: "Claimed by a worker — in-flight",
  blocked: "Worker asked for human input",
  done: "Completed",
  archived: "Archived",
};

// ── Error parsing ──

export function parseApiErrorMessage(err: unknown): string {
  const raw = err && err instanceof Error ? String(err.message) : String(err || "");
  const m = raw.match(/^(\d{3}):\s*(.*)$/s);
  const body = m ? m[2] : raw;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
    if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
      return parsed.detail.message;
    }
  } catch {
    /* not JSON */
  }
  return body || raw;
}

// ── Select change handler ──

export function selectChangeHandler(setter: (v: string) => void): {
  onValueChange: (v: string | null) => void;
  onChange: (e: { target?: { value: string } } | string) => void;
} {
  return {
    onValueChange: (v: string | null) => setter(v == null ? "" : v),
    onChange: (e: { target?: { value: string } } | string) => {
      const v = e && typeof e === "object" && e.target ? e.target.value : String(e);
      setter(v == null ? "" : v);
    },
  };
}

// ── Completion summary prompt ──

export function withCompletionSummary(
  patch: Record<string, unknown>,
  count: number,
  t?: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (!patch || patch.status !== "done") return patch;
  const label = count > 1 ? `${count} selected task(s)` : "this task";
  const value = window.prompt(
    tx(t, "completionSummary", "Completion summary for {label}. This is stored as the task result.", { label }),
    "",
  );
  if (value === null) return null;
  const summary = value.trim();
  if (!summary) {
    window.alert(tx(t, "completionSummaryRequired", "Completion summary is required before marking a task done."));
    return null;
  }
  return { ...patch, result: summary, summary };
}

// ── Staleness tiers ──

const STALENESS: Record<string, { amber: number; red: number }> = {
  ready: { amber: 1 * 60 * 60, red: 24 * 60 * 60 },
  running: { amber: 10 * 60, red: 60 * 60 },
  blocked: { amber: 1 * 60 * 60, red: 24 * 60 * 60 },
  todo: { amber: 7 * 24 * 60 * 60, red: 30 * 24 * 60 * 60 },
};

export function stalenessClass(task: { status: string; age?: { started_age_seconds: number | null; created_age_seconds: number | null } | null }): string {
  if (!task || !task.age) return "";
  const age = task.status === "running" ? task.age.started_age_seconds : task.age.created_age_seconds;
  const tier = STALENESS[task.status];
  if (!tier || age == null) return "";
  if (age >= tier.red) return "hermes-kanban-card--stale-red";
  if (age >= tier.amber) return "hermes-kanban-card--stale-amber";
  return "";
}

// ── Minimal safe markdown renderer ──

function escapeHtml(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderInline(esc: string): string {
  return esc
    .replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(
      /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      (_m, text, href) =>
        `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`,
    );
}

export function renderMarkdown(src: string): string {
  if (!src) return "";
  const blocks: string[] = [];
  let working = String(src).replace(/```([\s\S]*?)```/g, (_m, code) => {
    blocks.push(code);
    return `\u0000CODE${blocks.length - 1}\u0000`;
  });
  const escaped = escapeHtml(working);
  const lines = escaped.split(/\r?\n/);
  const out: string[] = [];
  let inList = false;
  for (const line of lines) {
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
    } else if (line.trim() === "") {
      out.push("");
    } else {
      out.push(`<p>${renderInline(line)}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  let html = out.join("\n");
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) =>
    `<pre class="hermes-kanban-md-code"><code>${escapeHtml(blocks[Number(i)])}</code></pre>`,
  );
  return html;
}

// ── Format bytes ──

export function fmtBytes(n: number): string {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// ── API functions ──

export const kanbanApi = {
  // Board
  getBoard: (board: string | null, params?: URLSearchParams): Promise<BoardData> => {
    const qs = params ? params.toString() : "";
    const url = qs ? `${API_BASE}/board?${qs}` : `${API_BASE}/board`;
    return getSDK().fetchJSON<BoardData>(withBoard(url, board));
  },

  // Task detail
  getTask: (taskId: string, board: string | null): Promise<TaskDetail> => {
    return getSDK().fetchJSON<TaskDetail>(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, board));
  },

  // Create task
  createTask: (body: CreateTaskBody, board: string | null): Promise<{ task: Task | null; warning?: string }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // Update task
  updateTask: (taskId: string, body: UpdateTaskBody, board: string | null): Promise<{ task: Task | null }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, board), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // Delete task
  deleteTask: (taskId: string, _board: string | null): Promise<{ deleted: boolean; task_id: string }> => {
    return getSDK().fetchJSON(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
    });
  },

  // Bulk update
  bulkUpdate: (body: Record<string, unknown>, board: string | null): Promise<BulkResponse> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/bulk`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // Comments
  addComment: (taskId: string, body: string, board: string | null): Promise<{ ok: boolean }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/comments`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    });
  },

  // Links
  addLink: (parentId: string, childId: string, board: string | null): Promise<{ ok: boolean }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/links`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parentId, child_id: childId }),
    });
  },

  deleteLink: (parentId: string, childId: string, board: string | null): Promise<{ ok: boolean }> => {
    const qs = new URLSearchParams({ parent_id: parentId, child_id: childId });
    return getSDK().fetchJSON(withBoard(`${API_BASE}/links?${qs}`, board), { method: "DELETE" });
  },

  // Config
  getConfig: (): Promise<KanbanConfig> => {
    return getSDK().fetchJSON(`${API_BASE}/config`);
  },

  // Boards
  listBoards: (): Promise<BoardsListResponse> => {
    return getSDK().fetchJSON(`${API_BASE}/boards`);
  },

  createBoard: (body: CreateBoardBody): Promise<{ board: BoardMeta; current: string }> => {
    return getSDK().fetchJSON(`${API_BASE}/boards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  deleteBoard: (slug: string): Promise<{ result: string; current: string }> => {
    return getSDK().fetchJSON(`${API_BASE}/boards/${encodeURIComponent(slug)}`, { method: "DELETE" });
  },

  // Diagnostics
  listDiagnostics: (board: string | null, severity?: string): Promise<DiagnosticsResponse> => {
    const qs = severity ? `?severity=${encodeURIComponent(severity)}` : "";
    return getSDK().fetchJSON(withBoard(`${API_BASE}/diagnostics${qs}`, board));
  },

  // Active workers
  listActiveWorkers: (board: string | null): Promise<ActiveWorkersResponse> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/workers/active`, board));
  },

  // Stats
  getStats: (board: string | null): Promise<BoardStats> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/stats`, board));
  },

  // Orchestration
  getOrchestration: (): Promise<OrchestrationSettings> => {
    return getSDK().fetchJSON(`${API_BASE}/orchestration`);
  },

  setOrchestration: (body: Partial<OrchestrationSettings>): Promise<OrchestrationSettings> => {
    return getSDK().fetchJSON(`${API_BASE}/orchestration`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // Profiles
  listProfiles: (): Promise<{ profiles: ProfileRoster[] }> => {
    return getSDK().fetchJSON(`${API_BASE}/profiles`);
  },

  updateProfileDescription: (name: string, description: string): Promise<{ ok: boolean; profile: string; description: string }> => {
    return getSDK().fetchJSON(`${API_BASE}/profiles/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description }),
    });
  },

  autoDescribeProfile: (name: string, overwrite: boolean): Promise<{ ok: boolean; profile: string; reason: string; description: string }> => {
    return getSDK().fetchJSON(`${API_BASE}/profiles/${encodeURIComponent(name)}/describe-auto`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overwrite }),
    });
  },

  // Home channels
  getHomeChannels: (taskId: string, board: string | null): Promise<{ home_channels: HomeChannel[] }> => {
    const qs = new URLSearchParams({ task_id: taskId });
    return getSDK().fetchJSON(withBoard(`${API_BASE}/home-channels?${qs}`, board));
  },

  toggleHomeSubscription: (taskId: string, platform: string, subscribe: boolean, board: string | null): Promise<{ ok: boolean }> => {
    const url = withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/home-subscribe/${encodeURIComponent(platform)}`, board);
    return getSDK().fetchJSON(url, { method: subscribe ? "POST" : "DELETE" });
  },

  // Reclaim
  reclaimTask: (taskId: string, reason: string, board: string | null): Promise<{ ok: boolean; task_id: string }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/reclaim`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
  },

  // Reassign
  reassignTask: (taskId: string, profile: string | null, reclaimFirst: boolean, reason: string, board: string | null): Promise<{ ok: boolean; task_id: string; assignee: string | null }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/reassign`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, reclaim_first: reclaimFirst, reason }),
    });
  },

  // Specify
  specifyTask: (taskId: string, board: string | null): Promise<{ ok: boolean; task_id: string; reason: string; new_title: string }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/specify`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  },

  // Decompose
  decomposeTask: (taskId: string, board: string | null): Promise<{ ok: boolean; task_id: string; reason: string; fanout: boolean; child_ids: string[]; new_title: string }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/decompose`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  },

  // Dispatch
  dispatch: (board: string | null, max: number = 8): Promise<Record<string, unknown>> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/dispatch?max=${max}`, board), { method: "POST" });
  },

  // Attachments
  uploadAttachment: (taskId: string, file: File, board: string | null): Promise<Response> => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return getSDK().authedFetch(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/attachments`, board), {
      method: "POST",
      body: fd,
    });
  },

  downloadAttachment: (attachmentId: number, board: string | null): Promise<Response> => {
    return getSDK().authedFetch(withBoard(`${API_BASE}/attachments/${attachmentId}`, board));
  },

  deleteAttachment: (attachmentId: number, board: string | null): Promise<{ ok: boolean; id: number }> => {
    return getSDK().fetchJSON(withBoard(`${API_BASE}/attachments/${attachmentId}`, board), { method: "DELETE" });
  },

  // Worker log
  getTaskLog: (taskId: string, board: string | null, tail?: number): Promise<{ task_id: string; path: string; exists: boolean; size_bytes: number; content: string; truncated: boolean }> => {
    const qs = tail ? `?tail=${tail}` : "";
    return getSDK().fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/log${qs}`, board));
  },
};

// ── Touch drag-drop helper ──

export function attachTouchDrag(el: HTMLElement | null, taskId: string): () => void {
  if (!el) return () => {};
  function onDown(e: PointerEvent) {
    if (e.pointerType !== "touch") return;
    if (!el) return;
    e.preventDefault();
    const proxy = el.cloneNode(true) as HTMLElement;
    proxy.classList.add("hermes-kanban-touch-proxy");
    document.body.appendChild(proxy);
    let lastTarget: HTMLElement | null = null;

    function move(ev: PointerEvent) {
      proxy.style.left = `${ev.clientX - proxy.offsetWidth / 2}px`;
      proxy.style.top = `${ev.clientY - 24}px`;
      proxy.style.display = "none";
      const under = document.elementFromPoint(ev.clientX, ev.clientY);
      proxy.style.display = "";
      const col = under && under.closest ? under.closest("[data-kanban-column]") : null;
      const trash = under && under.closest ? under.closest("[data-kanban-trash]") : null;
      const target = (col || trash) as HTMLElement | null;
      if (target !== lastTarget) {
        if (lastTarget) lastTarget.classList.remove("hermes-kanban-column--drop");
        if (target) target.classList.add("hermes-kanban-column--drop");
        lastTarget = target;
      }
    }
    function up() {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      if (lastTarget) {
        lastTarget.classList.remove("hermes-kanban-column--drop");
        const status = lastTarget.getAttribute("data-kanban-column");
        const isTrash = lastTarget.hasAttribute("data-kanban-trash");
        if (isTrash) {
          lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:delete", { detail: { taskId }, bubbles: true }));
        } else if (status) {
          lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:drop", { detail: { taskId, status }, bubbles: true }));
        }
      }
      proxy.remove();
    }
    proxy.style.position = "fixed";
    proxy.style.pointerEvents = "none";
    proxy.style.opacity = "0.85";
    proxy.style.zIndex = "9999";
    proxy.style.width = `${el.offsetWidth}px`;
    proxy.style.left = `${e.clientX - el.offsetWidth / 2}px`;
    proxy.style.top = `${e.clientY - 24}px`;
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  }
  el.addEventListener("pointerdown", onDown);
  return () => el.removeEventListener("pointerdown", onDown);
}