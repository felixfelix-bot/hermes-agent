/**
 * Kanban dashboard plugin — shared types.
 *
 * These mirror the JSON shapes returned by the 35 API endpoints in
 * ``plugin_api.py``. Every type is derived from the backend's
 * serialization helpers (``_task_dict``, ``_event_dict``, etc.) and the
 * Pydantic request bodies.
 */

// ── Task ──

export interface TaskAge {
  created_age_seconds: number | null;
  started_age_seconds: number | null;
  time_to_complete_seconds: number | null;
}

export interface LinkCounts {
  parents: number;
  children: number;
}

export interface Progress {
  done: number;
  total: number;
}

export interface DiagnosticWarning {
  count: number;
  kinds: Record<string, number>;
  latest_at: number;
  highest_severity: string | null;
}

export interface DiagnosticAction {
  kind: "reclaim" | "reassign" | "unblock" | "comment" | "cli_hint" | "open_docs";
  label: string;
  suggested?: boolean;
  payload?: Record<string, unknown>;
}

export interface Diagnostic {
  kind: string;
  severity: "warning" | "error" | "critical" | string;
  title: string;
  detail: string;
  data?: Record<string, unknown>;
  actions?: DiagnosticAction[];
  last_seen_at?: number;
  count?: number;
}

export interface Task {
  id: string;
  title: string;
  body: string | null;
  status: string;
  assignee: string | null;
  tenant: string | null;
  priority: number;
  result: string | null;
  created_by: string | null;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  completed_at: number | null;
  workspace_kind: string;
  workspace_path: string | null;
  current_run_id: number | null;
  claim_lock: string | null;
  claim_expires: number | null;
  block_reason: string | null;
  skills: string[] | null;
  goal_mode: boolean;
  goal_max_turns: number | null;
  age: TaskAge;
  latest_summary: string | null;
  // Board-level extras (only on /board list responses)
  link_counts?: LinkCounts;
  comment_count?: number;
  progress?: Progress | null;
  diagnostics?: Diagnostic[];
  warnings?: DiagnosticWarning | null;
}

// ── Board ──

export interface Column {
  name: string;
  tasks: Task[];
}

export interface BoardData {
  columns: Column[];
  tenants: string[];
  assignees: string[];
  latest_event_id: number;
  now: number;
}

// ── Task detail (GET /tasks/:id) ──

export interface Comment {
  id: number;
  task_id: string;
  author: string;
  body: string;
  created_at: number;
}

export interface TaskEvent {
  id: number;
  task_id: string;
  kind: string;
  payload: Record<string, unknown> | null;
  created_at: number;
  run_id: number | null;
}

export interface Attachment {
  id: number;
  task_id: string;
  filename: string;
  content_type: string | null;
  size: number;
  uploaded_by: string;
  stored_path: string;
  created_at: number;
}

export interface TaskRun {
  id: number;
  task_id: string;
  profile: string;
  step_key: string | null;
  status: string;
  claim_lock: string | null;
  claim_expires: number | null;
  worker_pid: number | null;
  max_runtime_seconds: number | null;
  last_heartbeat_at: number | null;
  started_at: number | null;
  ended_at: number | null;
  outcome: string | null;
  summary: string | null;
  metadata: Record<string, unknown> | null;
  error: string | null;
}

export interface TaskLinks {
  parents: string[];
  children: string[];
}

export interface TaskDetail {
  task: Task;
  comments: Comment[];
  events: TaskEvent[];
  attachments: Attachment[];
  links: TaskLinks;
  runs: TaskRun[];
}

// ── Boards CRUD ──

export interface BoardMeta {
  slug: string;
  name: string | null;
  description: string | null;
  icon: string | null;
  color: string | null;
  is_current: boolean;
  counts: Record<string, number>;
  total: number;
}

export interface BoardsListResponse {
  boards: BoardMeta[];
  current: string;
}

export interface CreateBoardBody {
  slug: string;
  name?: string;
  description?: string;
  icon?: string;
  color?: string;
  switch?: boolean;
}

// ── Config ──

export interface KanbanConfig {
  default_tenant: string;
  lane_by_profile: boolean;
  include_archived_by_default: boolean;
  render_markdown: boolean;
}

// ── Create task ──

export interface CreateTaskBody {
  title: string;
  body?: string;
  assignee?: string | null;
  tenant?: string;
  priority?: number;
  workspace_kind?: string;
  workspace_path?: string;
  parents?: string[];
  triage?: boolean;
  idempotency_key?: string;
  max_runtime_seconds?: number;
  skills?: string[];
  goal_mode?: boolean;
  goal_max_turns?: number;
}

// ── Update task ──

export interface UpdateTaskBody {
  status?: string;
  assignee?: string;
  priority?: number;
  title?: string;
  body?: string;
  result?: string;
  block_reason?: string;
  summary?: string;
  metadata?: Record<string, unknown>;
}

// ── Bulk update ──

export interface BulkTaskBody {
  ids: string[];
  status?: string;
  assignee?: string;
  priority?: number;
  archive?: boolean;
  result?: string;
  summary?: string;
  metadata?: Record<string, unknown>;
  reclaim_first?: boolean;
}

export interface BulkResult {
  id: string;
  ok: boolean;
  error?: string;
}

export interface BulkResponse {
  results: BulkResult[];
}

// ── Diagnostics ──

export interface DiagnosticEntry {
  task_id: string;
  task_title: string | null;
  task_status: string | null;
  task_assignee: string | null;
  diagnostics: Diagnostic[];
}

export interface DiagnosticsResponse {
  diagnostics: DiagnosticEntry[];
  count: number;
}

// ── Workers ──

export interface ActiveWorker {
  run_id: number;
  task_id: string;
  task_title: string;
  task_status: string;
  task_assignee: string | null;
  profile: string;
  worker_pid: number;
  started_at: number;
  claim_lock: string | null;
  claim_expires: number | null;
  last_heartbeat_at: number | null;
  max_runtime_seconds: number | null;
}

export interface ActiveWorkersResponse {
  workers: ActiveWorker[];
  count: number;
  checked_at: number;
}

// ── Stats ──

export interface BoardStats {
  [key: string]: unknown;
}

// ── Orchestration ──

export interface OrchestrationSettings {
  orchestrator_profile: string;
  default_assignee: string;
  auto_decompose: boolean;
  auto_promote_children: boolean;
  resolved_orchestrator_profile: string;
  resolved_default_assignee: string;
  active_profile: string;
}

// ── Profiles ──

export interface ProfileRoster {
  name: string;
  is_default: boolean;
  model: string;
  provider: string;
  description: string;
  description_auto: boolean;
  skill_count: number;
}

// ── Home channels ──

export interface HomeChannel {
  platform: string;
  chat_id: string;
  thread_id: string;
  name: string;
  subscribed: boolean;
}

// ── WebSocket events ──

export interface WsEventMessage {
  events: TaskEvent[];
  cursor: number;
}

// ── API endpoints ──

export const API_BASE = "/api/plugins/kanban";
export const MIME_TASK = "text/x-hermes-task";
export const DOCS_URL =
  "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban";

// Columns shown by the dashboard, in left-to-right order.
export const BOARD_COLUMNS = [
  "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
] as const;

// Visible columns (scheduled is available but shown conditionally)
export const COLUMN_ORDER = [
  "triage", "todo", "ready", "running", "blocked", "done",
] as const;

export const COLUMN_DOT: Record<string, string> = {
  triage: "hermes-kanban-dot-triage",
  todo: "hermes-kanban-dot-todo",
  ready: "hermes-kanban-dot-ready",
  running: "hermes-kanban-dot-running",
  blocked: "hermes-kanban-dot-blocked",
  done: "hermes-kanban-dot-done",
  archived: "hermes-kanban-dot-archived",
};

export const FALLBACK_COLUMN_LABEL: Record<string, string> = {
  triage: "Triage",
  todo: "Todo",
  ready: "Ready",
  running: "In Progress",
  blocked: "Blocked",
  done: "Done",
  archived: "Archived",
};

export const FALLBACK_COLUMN_HELP: Record<string, string> = {
  triage: "Raw ideas — a specifier will flesh out the spec",
  todo: "Waiting on dependencies or unassigned",
  ready: "Dependencies satisfied; assign a profile to dispatch",
  running: "Claimed by a worker — in-flight",
  blocked: "Worker asked for human input",
  done: "Completed",
  archived: "Archived",
};

export const DESTRUCTIVE_CONFIRM: Record<string, string> = {
  done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
  archived: "Archive this task? It disappears from the default board view.",
  blocked: "Mark this task as blocked? The worker's claim is released.",
};

// localStorage key for the user's selected board.
export const LS_BOARD_KEY = "hermes.kanban.selectedBoard";