/**
 * Kanban dashboard plugin — TypeScript type definitions.
 *
 * These interfaces describe the shapes returned by the kanban backend
 * (/api/plugins/kanban/*) and the WebSocket event stream. They are
 * intentionally permissive (optional fields, string-typed unions) because
 * the Python backend evolves independently — the UI degrades gracefully
 * when a field is absent.
 */

// ── Core task ──────────────────────────────────────────────────────────────

export type TaskStatus =
  | "triage"
  | "todo"
  | "scheduled"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done"
  | "archived";

export type WorkspaceKind = "scratch" | "worktree" | "dir";

export interface TaskProgress {
  done: number;
  total: number;
}

export interface TaskAge {
  created_age_seconds: number | null;
  started_age_seconds: number | null;
}

export interface TaskWarnings {
  count: number;
  highest_severity: "warning" | "error" | "critical";
  kinds?: Record<string, number>;
  latest_at?: number;
}

export interface DiagnosticAction {
  kind: "reclaim" | "reassign" | "unblock" | "comment" | "cli_hint" | "open_docs" | string;
  label: string;
  suggested?: boolean;
  payload?: {
    command?: string;
    url?: string;
    reclaim_first?: boolean;
    [k: string]: unknown;
  };
}

export interface Diagnostic {
  kind: string;
  severity: "warning" | "error" | "critical" | string;
  title: string;
  detail: string;
  data?: Record<string, unknown>;
  actions?: DiagnosticAction[];
}

export interface TaskLinkCounts {
  parents: number;
  children: number;
}

export interface Task {
  id: string;
  title: string | null;
  body: string | null;
  status: TaskStatus;
  assignee: string | null;
  priority: number;
  tenant: string | null;
  result: string | null;
  latest_summary: string | null;
  created_at: number;
  created_by: string | null;
  comment_count: number;
  link_counts?: TaskLinkCounts;
  progress?: TaskProgress | null;
  age?: TaskAge;
  warnings?: TaskWarnings | null;
  diagnostics?: Diagnostic[];
  workspace_kind: WorkspaceKind;
  workspace_path: string | null;
  skills: string[];
  goal_mode: boolean;
  goal_max_turns: number | null;
}

// ── Board ──────────────────────────────────────────────────────────────────

export interface Column {
  name: TaskStatus;
  tasks: Task[];
}

export interface Board {
  columns: Column[];
  tenants: string[];
  assignees: string[];
  latest_event_id: number;
}

export interface BoardListItem {
  slug: string;
  name: string;
  total: number;
  icon?: string;
  description?: string;
}

export interface BoardListResponse {
  boards: BoardListItem[];
  current: string;
}

// ── Task detail ─────────────────────────────────────────────────────────────

export interface Comment {
  id: number;
  author: string | null;
  body: string;
  created_at: number;
}

export interface TaskEvent {
  id: number;
  task_id: string;
  kind: string;
  payload: Record<string, unknown> | null;
  created_at: number;
}

export interface Attachment {
  id: number;
  filename: string;
  size: number;
}

export interface TaskLinks {
  parents: string[];
  children: string[];
}

export interface Run {
  id: number;
  profile: string | null;
  started_at: number;
  ended_at: number | null;
  outcome: string | null;
  status: string | null;
  summary: string | null;
  error: string | null;
  metadata: Record<string, unknown> | null;
}

export interface TaskDetail {
  task: Task;
  comments: Comment[];
  events: TaskEvent[];
  attachments: Attachment[];
  links: TaskLinks;
  runs: Run[];
}

// ── WebSocket ───────────────────────────────────────────────────────────────

export interface WsEvent {
  task_id?: string;
  kind?: string;
  [k: string]: unknown;
}

export interface WsMessage {
  cursor: number;
  events: WsEvent[];
}

// ── API response shapes ─────────────────────────────────────────────────────

export interface BulkResult {
  id: string;
  ok: boolean;
  error?: string;
}

export interface BulkResponse {
  results: BulkResult[];
}

export interface CreateTaskResponse {
  task?: Task;
  warning?: string;
  [k: string]: unknown;
}

export interface CreateBoardResponse {
  board: BoardListItem;
}

export interface SpecifyResponse {
  ok: boolean;
  new_title?: string;
  reason?: string;
}

export interface DecomposeResponse {
  ok: boolean;
  fanout?: boolean;
  child_ids?: string[];
  new_title?: string;
  reason?: string;
}

export interface HomeChannel {
  platform: string;
  name: string;
  chat_id: string;
  thread_id?: string;
  subscribed: boolean;
}

export interface HomeChannelsResponse {
  home_channels: HomeChannel[];
}

export interface DashboardConfig {
  render_markdown: boolean;
  default_tenant?: string;
  lane_by_profile?: boolean;
  include_archived_by_default?: boolean;
  [k: string]: unknown;
}

export interface OrchestrationSettings {
  orchestrator_profile: string | null;
  default_assignee: string | null;
  auto_decompose: boolean;
  active_profile?: string;
  resolved_orchestrator_profile?: string;
  resolved_default_assignee?: string;
}

export interface Profile {
  name: string;
  is_default: boolean;
  description: string | null;
  description_auto: boolean;
}

export interface ProfileRoster {
  profiles: Profile[];
}

export interface WorkerLog {
  exists: boolean;
  content: string;
  size_bytes: number;
  path: string;
  truncated: boolean;
}

// ── Patch bodies ────────────────────────────────────────────────────────────

export interface TaskPatch {
  status?: TaskStatus;
  assignee?: string;
  priority?: number;
  title?: string;
  body?: string;
  result?: string;
  summary?: string;
  archive?: boolean;
}

export interface BulkPatch extends TaskPatch {
  ids: string[];
  reclaim_first?: boolean;
}

export interface CreateTaskBody {
  title: string;
  assignee: string | null;
  priority: number;
  triage: boolean;
  parents?: string[];
  skills?: string[];
  workspace_kind?: WorkspaceKind;
  workspace_path?: string;
  goal_mode?: boolean;
  goal_max_turns?: number;
}

export interface CreateBoardBody {
  slug: string;
  name?: string;
  description?: string;
  icon?: string;
  switch?: boolean;
}