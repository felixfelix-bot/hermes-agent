/**
 * Kanban dashboard plugin — main page component.
 *
 * Manages board state, WebSocket live updates, filtering, card selection,
 * task operations, board switching, and the overall layout.
 */
import * as React from "react";
import {
  tx,
  withBoard,
  readSelectedBoard,
  writeSelectedBoard,
  selectChangeHandler,
  withCompletionSummary,
  parseApiErrorMessage,
} from "../api";
import {
  API_BASE,
  DOCS_URL,
  type BoardData,
  type Task,
  type CreateTaskBody,
} from "../types";

import { BoardColumns } from "./BoardColumns";
import { TaskDrawer } from "./TaskDrawer";
import { NewTaskDialog } from "./NewTaskDialog";

// ── SDK ──

interface HermesSDK {
  React: typeof import("react");
  hooks: {
    useState: typeof import("react").useState;
    useEffect: typeof import("react").useEffect;
    useCallback: typeof import("react").useCallback;
    useMemo: typeof import("react").useMemo;
    useRef: typeof import("react").useRef;
  };
  components: Record<string, React.ComponentType<any>>;
  utils: { cn: (...c: Array<string | false | null | undefined>) => string; timeAgo: (ts: number) => string };
  useI18n: () => { t: Record<string, unknown>; locale: string };
  fetchJSON: <T = unknown>(url: string, init?: RequestInit) => Promise<T>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
}

const SDK = (function () {
  const s = (window as unknown as { __HERMES_PLUGIN_SDK__?: HermesSDK }).__HERMES_PLUGIN_SDK__;
  if (!s) throw new Error("Plugin SDK not available");
  return s;
})();

const h = SDK.React.createElement;
const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
const { Card, CardContent, Button, Input, Label, Select, SelectOption } = SDK.components;
const { cn } = SDK.utils;
const useI18n = SDK.useI18n || (() => ({ t: { kanban: null }, locale: "en" }));

// Checkbox fallback shim
const Checkbox: React.ComponentType<any> = SDK.components.Checkbox || function (props: any) {
  const { checked, onCheckedChange, className, onClick, ...rest } = props;
  return h("input", {
    type: "checkbox",
    checked: !!checked,
    className,
    onClick,
    onChange: (e: { target: { checked: boolean } }) => onCheckedChange?.(e.target.checked),
    ...rest,
  });
};

// ── Constants ──

const DESTRUCTIVE_KEYS: Record<string, string> = {
  done: "confirmDone",
  archived: "confirmArchive",
  blocked: "confirmBlocked",
};

const FALLBACK_DESTRUCTIVE: Record<string, string> = {
  done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
  archived: "Archive this task? It disappears from the default board view.",
  blocked: "Mark this task as blocked? The worker's claim is released.",
};

const DESTRUCTIVE_TRANSITIONS: Record<string, string> = {
  blocked: "Block selected tasks? Releases any active claims.",
  done: "Mark selected task(s) as done? Releases claims and unblocks children.",
  archived: "Archive selected task(s)?",
};

const FALLBACK_TRASH = {
  label: "Trash",
  title: "Drag a card here to permanently delete it",
  confirm: "Permanently delete this task? This cannot be undone.",
  dropHint: "Drop to delete",
};

function getDestructiveConfirm(t: Record<string, unknown> | null | undefined, status: string): string | null {
  const key = DESTRUCTIVE_KEYS[status];
  if (!key) return null;
  return tx(t, key, FALLBACK_DESTRUCTIVE[status] || "");
}

// ── Error Boundary ──

function ErrorBoundaryFallback(props: { message: string; onReset: () => void }) {
  const { t } = useI18n();
  return h(Card, null,
    h(CardContent, { className: "p-6 text-sm" },
      h("div", { className: "text-destructive font-semibold mb-1" },
        tx(t, "renderingError", "Kanban tab hit a rendering error")),
      h("div", { className: "text-muted-foreground text-xs mb-3" }, props.message),
      h(Button, { onClick: props.onReset, size: "sm" },
        tx(t, "reloadView", "Reload view")),
    ),
  );
}

class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error: Error): { error: Error | null } {
    return { error };
  }
  componentDidCatch(error: Error, info: unknown): void {
    console.error("Kanban plugin crashed:", error, info);
  }
  render(): React.ReactNode {
    if (this.state.error) {
      return h(ErrorBoundaryFallback, {
        message: String(this.state.error.message || this.state.error),
        onReset: () => this.setState({ error: null }),
      });
    }
    return this.props.children;
  }
}

// ── Attention Strip ──

function collectDiagTasks(boardData: BoardData | null): Task[] {
  if (!boardData || !boardData.columns) return [];
  const out: Task[] = [];
  for (const col of boardData.columns) {
    for (const t of col.tasks || []) {
      if (t.diagnostics && t.diagnostics.length > 0) out.push(t);
      else if (t.warnings && t.warnings.count > 0) out.push(t);
    }
  }
  const sevIdx = (s: string) => (s === "critical" ? 3 : s === "error" ? 2 : s === "warning" ? 1 : 0);
  out.sort((a, b) => {
    const aSev = sevIdx((a.warnings?.highest_severity) || "warning");
    const bSev = sevIdx((b.warnings?.highest_severity) || "warning");
    if (aSev !== bSev) return bSev - aSev;
    return (b.warnings?.latest_at || 0) - (a.warnings?.latest_at || 0);
  });
  return out;
}

function AttentionStrip(props: { boardData: BoardData | null; onOpen: (id: string) => void }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const diagTasks = useMemo(() => collectDiagTasks(props.boardData), [props.boardData]);
  if (dismissed || diagTasks.length === 0) return null;
  let topSev = "warning";
  for (const td of diagTasks) {
    const s = (td.warnings?.highest_severity) || "warning";
    if (s === "critical") { topSev = "critical"; break; }
    if (s === "error" && topSev !== "critical") topSev = "error";
  }
  return h("div", { className: cn("hermes-kanban-attention", `hermes-kanban-attention--${topSev}`) },
    h("div", { className: "hermes-kanban-attention-bar" },
      h("span", { className: "hermes-kanban-attention-icon" },
        topSev === "critical" ? "!!!" : topSev === "error" ? "!!" : "⚠"),
      h("span", { className: "hermes-kanban-attention-text" },
        diagTasks.length === 1
          ? tx(t, "taskNeedsAttention", "1 task needs attention")
          : tx(t, "tasksNeedAttention", "{n} tasks need attention", { n: diagTasks.length })),
      h("button", {
        className: "hermes-kanban-attention-toggle",
        onClick: () => setExpanded(x => !x),
        type: "button",
      }, expanded ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
      h("button", {
        className: "hermes-kanban-attention-dismiss",
        onClick: () => setDismissed(true),
        title: "Hide until next page reload",
        type: "button",
      }, "\u2715"),
    ),
    expanded
      ? h("div", { className: "hermes-kanban-attention-list" },
          diagTasks.map(task => {
            const sev = (task.warnings?.highest_severity) || "warning";
            const kinds = task.warnings?.kinds ? Object.keys(task.warnings.kinds) : [];
            return h("div", {
              key: task.id,
              className: cn("hermes-kanban-attention-row", `hermes-kanban-attention-row--${sev}`),
            },
              h("span", { className: "hermes-kanban-attention-row-sev" },
                sev === "critical" ? "!!!" : sev === "error" ? "!!" : "⚠"),
              h("span", { className: "hermes-kanban-attention-row-id" }, task.id),
              h("span", { className: "hermes-kanban-attention-row-title" },
                task.title || tx(t, "untitled", "(untitled)")),
              h("span", { className: "hermes-kanban-attention-row-meta" },
                task.assignee ? "@" + task.assignee : tx(t, "unassigned", "unassigned"),
                " \u00b7 ",
                kinds.length > 0 ? kinds.join(", ") : tx(t, "diagnostic", "diagnostic")),
              h("button", {
                className: "hermes-kanban-attention-row-btn",
                onClick: () => props.onOpen(task.id),
                type: "button",
              }, tx(t, "open", "Open")),
            );
          }),
        )
      : null,
  );
}

// ── Board Switcher ──

function DocsLink() {
  return h("a", {
    href: DOCS_URL,
    target: "_blank",
    rel: "noopener noreferrer",
    className: "hermes-kanban-docs-link",
    title: "Open Hermes Kanban docs in a new tab",
    "aria-label": "Hermes Kanban documentation",
  }, "?");
}

function BoardSwitcher(props: {
  board: string | null;
  boardList: Array<{ slug: string; name: string | null; total: number }>;
  onSwitch: (slug: string) => void;
  onNewClick: () => void;
  onDeleteBoard: (slug: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const list = props.boardList || [];
  const current = list.find(b => b.slug === props.board);
  const currentName = current?.name || props.board || "default";
  const currentTotal = current?.total || 0;
  const hasMultipleBoards = list.length > 1;
  const totalAcrossAllBoards = list.reduce((n, b) => n + (b.total || 0), 0);
  const shouldShow = hasMultipleBoards || totalAcrossAllBoards > 0;
  if (!shouldShow) {
    return h("div", { className: "hermes-kanban-boardswitcher-compact",
      title: tx(t, "boardSwitcherHint", "Boards let you separate unrelated streams of work") },
      h(Button, { onClick: props.onNewClick, size: "sm", className: "h-7 text-xs" },
        tx(t, "newBoard", "+ New board")),
      h(DocsLink),
    );
  }
  return h("div", { className: "hermes-kanban-boardswitcher" },
    h("div", { className: "hermes-kanban-boardswitcher-inner" },
      h("div", { className: "flex flex-col gap-0.5" },
        h("div", { className: "text-[11px] tracking-wider text-muted-foreground" },
          tx(t, "board", "Board")),
        h("div", { className: "flex items-center gap-2" },
          h(Select, {
            value: props.board || "",
            className: "h-8 min-w-[220px]",
            "aria-label": "Switch kanban board",
            ...selectChangeHandler(v => { if (v) props.onSwitch(v); }),
          },
            list.map(b => {
              const label = b.total > 0 ? `${b.name || b.slug} \u00b7 ${b.total}` : (b.name || b.slug);
              return h(SelectOption, { key: b.slug, value: b.slug }, label);
            }),
          ),
          h("span", { className: "text-xs text-muted-foreground" },
            `${currentTotal} task${currentTotal === 1 ? "" : "s"}`),
        ),
      ),
      h("div", { className: "flex-1" }),
      h(DocsLink),
      h(Button, {
        onClick: props.onNewClick,
        size: "sm",
        className: "h-8",
        title: "Create a new board. Useful when you want an unrelated work stream.",
      }, tx(t, "newBoard", "+ New board")),
      props.board !== "default"
        ? h(Button, {
          onClick: () => {
            const msg = tx(t, "archiveBoardConfirm",
              "Archive board '{name}'? It will be moved to boards/_archived/. Tasks will no longer appear in the UI.",
              { name: currentName });
            if (window.confirm(msg)) props.onDeleteBoard(props.board!);
          },
          size: "sm",
          className: "h-8",
          title: tx(t, "archiveBoardTitle", "Archive this board"),
        }, tx(t, "archive", "Archive"))
        : null,
    ),
  );
}

// ── New Board Dialog ──

function NewBoardDialog(props: {
  onCancel: () => void;
  onCreate: (payload: { slug: string; name?: string; description?: string; icon?: string; switch?: boolean }) => Promise<unknown>;
}) {
  const { t } = useI18n();
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [icon, setIcon] = useState("");
  const [switchTo, setSwitchTo] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const autoName = useMemo(() => {
    if (!slug) return "";
    return slug.replace(/[-_]+/g, " ").split(" ").filter(Boolean)
      .map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
  }, [slug]);

  function onSubmit(ev?: React.FormEvent) {
    ev?.preventDefault();
    if (!slug.trim()) { setErr("slug is required"); return; }
    setSubmitting(true);
    setErr(null);
    props.onCreate({
      slug: slug.trim(),
      name: name.trim() || autoName || undefined,
      description: description.trim() || undefined,
      icon: icon.trim() || undefined,
      switch: switchTo,
    }).catch(e => {
      setErr(String(e?.message || e));
      setSubmitting(false);
    });
  }

  return h("div", {
    className: "hermes-kanban-dialog-backdrop",
    onClick: (e: React.MouseEvent) => { if (e.target === e.currentTarget) props.onCancel(); },
  },
    h("form", { className: "hermes-kanban-dialog", onSubmit },
      h("div", { className: "hermes-kanban-dialog-title" }, tx(t, "newBoardTitle", "New board")),
      h("div", { className: "text-xs text-muted-foreground mb-2" },
        tx(t, "newBoardDescription",
          "Boards let you separate unrelated streams of work.")),
      h("div", { className: "flex flex-col gap-3" },
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs" }, tx(t, "slug", "Slug"), " ",
            h("span", { className: "text-muted-foreground" },
              tx(t, "slugHint", "\u2014 lowercase, hyphens, e.g. atm10-server"))),
          h(Input, {
            value: slug,
            onChange: (e: { target: { value: string } }) =>
              setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9\-_]/g, "-")),
            placeholder: "atm10-server",
            autoFocus: true,
            className: "h-8",
          }),
        ),
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name"), " ",
            h("span", { className: "text-muted-foreground" }, "(optional)")),
          h(Input, {
            value: name,
            onChange: (e: { target: { value: string } }) => setName(e.target.value),
            placeholder: autoName || tx(t, "displayName", "Display name"),
            className: "h-8",
          }),
        ),
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs" }, tx(t, "description", "Description"), " ",
            h("span", { className: "text-muted-foreground" }, "(optional)")),
          h(Input, {
            value: description,
            onChange: (e: { target: { value: string } }) => setDescription(e.target.value),
            placeholder: "What goes on this board?",
            className: "h-8",
          }),
        ),
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs" }, tx(t, "icon", "Icon"), " ",
            h("span", { className: "text-muted-foreground" }, "(single character or emoji)")),
          h(Input, {
            value: icon,
            onChange: (e: { target: { value: string } }) => setIcon(e.target.value.slice(0, 4)),
            placeholder: "\u{1F4E6}",
            className: "h-8 w-24",
          }),
        ),
        h("label", { className: "flex items-center gap-2 text-xs" },
          h(Checkbox, {
            checked: switchTo,
            onCheckedChange: (checked: boolean) => setSwitchTo(checked === true),
          }),
          tx(t, "switchAfterCreate", "Switch to this board after creating it"),
        ),
      ),
      err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
      h("div", { className: "hermes-kanban-dialog-actions" },
        h(Button, { type: "button", onClick: props.onCancel, size: "sm", disabled: submitting },
          tx(t, "cancel", "Cancel")),
        h(Button, { type: "submit", size: "sm", disabled: submitting || !slug.trim() },
          submitting ? tx(t, "creating", "Creating\u2026") : tx(t, "createBoard", "Create board")),
      ),
    ),
  );
}

// ── Board Toolbar ──

function BoardToolbar(props: {
  board: BoardData | null;
  search: string;
  setSearch: (v: string) => void;
  tenantFilter: string;
  setTenantFilter: (v: string) => void;
  assigneeFilter: string;
  setAssigneeFilter: (v: string) => void;
  includeArchived: boolean;
  setIncludeArchived: (v: boolean) => void;
  laneByProfile: boolean;
  setLaneByProfile: (v: boolean) => void;
  onNudgeDispatch: () => void;
  onRefresh: () => void;
  onNewTask: () => void;
}) {
  const { t } = useI18n();
  const tenants = (props.board?.tenants) || [];
  const assignees = (props.board?.assignees) || [];
  return h("div", { className: "flex flex-wrap items-end gap-3" },
    h("div", { className: "flex flex-col gap-1",
      title: "Fuzzy-match tasks by id, title, or description." },
      h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "search", "Search")),
      h(Input, {
        placeholder: tx(t, "filterCards", "Filter cards\u2026"),
        value: props.search,
        onChange: (e: { target: { value: string } }) => props.setSearch(e.target.value),
        className: "w-56 h-8",
      }),
    ),
    h("div", { className: "flex flex-col gap-1",
      title: "Tenants are free-form tags on a task." },
      h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "tenant", "Tenant")),
      h(Select, {
        value: props.tenantFilter,
        className: "h-8",
        ...selectChangeHandler(props.setTenantFilter),
      },
        h(SelectOption, { value: "" }, tx(t, "allTenants", "All tenants")),
        tenants.map(tn => h(SelectOption, { key: tn, value: tn }, tn)),
      ),
    ),
    h("div", { className: "flex flex-col gap-1",
      title: "Filter by assigned Hermes profile." },
      h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "assignee", "Assignee")),
      h(Select, {
        value: props.assigneeFilter,
        className: "h-8",
        ...selectChangeHandler(props.setAssigneeFilter),
      },
        h(SelectOption, { value: "" }, tx(t, "allProfiles", "All profiles")),
        assignees.map(a => h(SelectOption, { key: a, value: a }, a)),
      ),
    ),
    h("label", { className: "flex items-center gap-2 text-xs",
      title: "Include archived tasks in the board view." },
      h(Checkbox, {
        checked: props.includeArchived,
        onCheckedChange: (checked: boolean) => props.setIncludeArchived(checked === true),
      }),
      tx(t, "showArchived", "Show archived"),
    ),
    h("label", { className: "flex items-center gap-2 text-xs",
      title: "Group the Running column by assigned profile" },
      h(Checkbox, {
        checked: props.laneByProfile,
        onCheckedChange: (checked: boolean) => props.setLaneByProfile(checked === true),
      }),
      tx(t, "lanesByProfile", "Lanes by profile"),
    ),
    h("div", { className: "flex-1" }),
    h(Button, {
      onClick: props.onNewTask,
      size: "sm",
      title: tx(t, "newTask", "New Task"),
    }, tx(t, "newTask", "New Task")),
    h(Button, {
      onClick: props.onNudgeDispatch,
      size: "sm",
      title: "Wake the dispatcher to claim ready tasks now.",
    }, tx(t, "nudgeDispatcher", "Nudge dispatcher")),
    h(Button, {
      onClick: props.onRefresh,
      size: "sm",
      title: "Reload the board from the database.",
    }, tx(t, "refresh", "Refresh")),
    h(Button, {
      onClick: () => {
        props.setSearch("");
        props.setTenantFilter("");
        props.setAssigneeFilter("");
        props.setIncludeArchived(false);
      },
      size: "sm",
      title: "Clear all active filters.",
    }, tx(t, "clearFilters", "Clear filters")),
  );
}

// ── Bulk Action Bar ──

function BulkActionBar(props: {
  count: number;
  assignees: string[];
  onApply: (patch: Record<string, unknown>, confirmMsg?: string) => void;
  onClear: () => void;
  onSelectAllVisible: () => void;
  onDelete: (count: number) => Promise<void>;
}) {
  const { t } = useI18n();
  const [assignee, setAssignee] = useState("");
  const [reclaimFirst, setReclaimFirst] = useState(false);
  const [priority, setPriority] = useState("");
  return h("div", { className: "hermes-kanban-bulk" },
    h("span", { className: "hermes-kanban-bulk-count" },
      `${props.count} ${tx(t, "selected", "selected")}`),
    h(Button, { onClick: () => props.onApply({ status: "todo" }), size: "sm", title: "Move to Todo." }, "\u2192 todo"),
    h(Button, { onClick: () => props.onApply({ status: "ready" }), size: "sm", title: "Move to Ready." }, "\u2192 ready"),
    h(Button, { onClick: () => props.onApply({ status: "blocked" }, `Block ${props.count} task(s)?`), size: "sm", title: "Block selected tasks." }, "Block"),
    h(Button, { onClick: () => props.onApply({ status: "ready" }, `Unblock ${props.count} task(s)?`), size: "sm", title: "Unblock selected tasks." }, "Unblock"),
    h(Button, { onClick: () => props.onApply({ status: "done" }, tx(t, "markDone", "Mark {n} task(s) as done?", { n: props.count })), size: "sm", title: "Mark done." }, tx(t, "complete", "Complete")),
    h(Button, { onClick: () => props.onApply({ archive: true }, tx(t, "markArchived", "Archive {n} task(s)?", { n: props.count })), size: "sm", title: "Archive selected." }, tx(t, "archive", "Archive")),
    h(Button, { onClick: () => props.onDelete(props.count), size: "sm", variant: "destructive", title: "Permanently delete." }, tx(t, "delete", "Delete")),
    h("div", { className: "hermes-kanban-bulk-priority", title: "Set priority on selected tasks." },
      h(Input, { type: "number", value: priority, onChange: (e: { target: { value: string } }) => setPriority(e.target.value), placeholder: tx(t, "priority", "pri"), className: "h-7 text-xs w-16" }),
      h(Button, { onClick: () => { if (priority !== "") { props.onApply({ priority: Number(priority) }); setPriority(""); } }, disabled: priority === "", size: "sm" }, tx(t, "setPriority", "Set priority")),
    ),
    h("div", { className: "hermes-kanban-bulk-reassign", title: "Reassign selected tasks." },
      h(Select, { value: assignee, className: "h-7 text-xs", ...selectChangeHandler(setAssignee) },
        h(SelectOption, { value: "" }, "\u2014 reassign \u2014"),
        h(SelectOption, { value: "__none__" }, "(unassign)"),
        props.assignees.map(a => h(SelectOption, { key: a, value: a }, a)),
      ),
      h(Button, {
        onClick: () => { if (!assignee) return; props.onApply({ assignee: assignee === "__none__" ? "" : assignee, reclaim_first: reclaimFirst }); setAssignee(""); },
        disabled: !assignee, size: "sm",
      }, tx(t, "apply", "Apply")),
    ),
    h("label", { className: "hermes-kanban-bulk-reclaim-first", title: "Reclaim any active claims before reassigning" },
      h(Checkbox, { checked: reclaimFirst, onCheckedChange: (c: boolean) => setReclaimFirst(c === true) }),
      "Reclaim first",
    ),
    h("div", { className: "flex-1" }),
    h(Button, { onClick: props.onSelectAllVisible, size: "sm", title: "Select all visible cards." }, "Select all visible"),
    h(Button, { onClick: props.onClear, size: "sm", title: "Deselect all." }, tx(t, "clear", "Clear")),
  );
}

// ── Orchestration Panel ──

function OrchestrationPanel() {
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [profiles, setProfiles] = useState<Array<Record<string, unknown>>>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const loadAll = useCallback(() => {
    Promise.all([
      SDK.fetchJSON(`${API_BASE}/orchestration`),
      SDK.fetchJSON(`${API_BASE}/profiles`),
    ]).then((results: unknown[]) => {
      setSettings(results[0] as Record<string, unknown> || null);
      setProfiles(((results[1] as { profiles?: Array<Record<string, unknown>> })?.profiles) || []);
      setMsg(null);
    }).catch((err: Error) => {
      setMsg({ ok: false, text: "Failed to load: " + (err.message || String(err)) });
    });
  }, []);

  useEffect(() => { if (settings === null) loadAll(); }, [settings, loadAll]);

  const saveSettings = useCallback((patch: Record<string, unknown>) => {
    setMsg(null);
    return SDK.fetchJSON(`${API_BASE}/orchestration`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then((res: unknown) => { setSettings(res as Record<string, unknown>); setMsg({ ok: true, text: "Settings saved." }); return res; })
      .catch((err: Error) => { setMsg({ ok: false, text: "Save failed: " + (err.message || String(err)) }); });
  }, []);

  const autoOn = !!(settings && settings.auto_decompose);
  const modePillTitle = settings === null
    ? "Loading mode\u2026"
    : (autoOn
      ? "Orchestration: Auto \u2014 the dispatcher decomposes new triage tasks automatically every tick. Click to switch to Manual."
      : "Orchestration: Manual \u2014 triage tasks stay in triage until you click \u2697 Decompose on each card. Click to switch to Auto.");

  const modePill = h("button", {
    type: "button",
    onClick: () => { if (settings !== null) saveSettings({ auto_decompose: !autoOn }); },
    disabled: settings === null,
    title: modePillTitle,
    className: "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium " +
      (autoOn
        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
        : "border-muted-foreground/30 bg-muted/30 text-muted-foreground"),
  }, "Orchestration: ",
    h("span", { className: "ml-1 font-semibold" },
      settings === null ? "\u2026" : (autoOn ? "Auto" : "Manual")),
  );

  if (!expanded) {
    return h("div", { className: "flex items-center gap-3 text-xs" },
      modePill,
      h("button", {
        type: "button",
        onClick: () => setExpanded(true),
        className: "underline text-muted-foreground hover:text-foreground",
        title: "Configure the kanban orchestrator",
      }, "\u25be Orchestration settings"),
    );
  }

  return h(Card, { className: "p-3" },
    h(CardContent, { className: "p-2 flex flex-col gap-3" },
      h("div", { className: "flex items-center justify-between" },
        h("button", {
          type: "button",
          onClick: () => setExpanded(false),
          className: "text-sm font-medium underline-offset-2 hover:underline",
        }, "\u25be Orchestration settings"),
        modePill,
        h(Button, { onClick: loadAll, size: "sm" }, "Reload"),
      ),
      msg ? h("div", { className: msg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err" }, msg.text) : null,
      settings ? h("div", { className: "grid gap-3 sm:grid-cols-3" },
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs text-muted-foreground" }, "Orchestrator profile"),
          h(Select, {
            value: (settings.orchestrator_profile as string) || "",
            className: "h-8",
            ...selectChangeHandler((v: string) => saveSettings({ orchestrator_profile: v })),
          },
            h(SelectOption, { value: "" },
              "(default: " + (settings.active_profile as string || "default") + ")"),
            profiles.map(p => h(SelectOption, { key: p.name as string, value: p.name as string },
              p.name as string + (p.is_default ? " (default)" : ""))),
          ),
        ),
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs text-muted-foreground" }, "Default assignee"),
          h(Select, {
            value: (settings.default_assignee as string) || "",
            className: "h-8",
            ...selectChangeHandler((v: string) => saveSettings({ default_assignee: v })),
          },
            h(SelectOption, { value: "" },
              "(default: " + (settings.active_profile as string || "default") + ")"),
            profiles.map(p => h(SelectOption, { key: p.name as string, value: p.name as string },
              p.name as string + (p.is_default ? " (default)" : ""))),
          ),
        ),
        h("div", { className: "flex flex-col gap-1" },
          h(Label, { className: "text-xs text-muted-foreground" }, "Orchestration mode"),
          h("label", { className: "flex items-center gap-2 text-xs h-8" },
            h(Checkbox, {
              checked: !!settings.auto_decompose,
              onCheckedChange: (checked: boolean) => saveSettings({ auto_decompose: checked === true }),
            }),
            "Auto-decompose triage tasks",
          ),
        ),
      ) : h("div", { className: "text-xs text-muted-foreground" }, "Loading\u2026"),
    ),
  );
}

// ── Main KanbanPage ──

export function KanbanPage() {
  const { t } = useI18n();
  const [board, setBoard] = useState<string | null>(() => readSelectedBoard());
  const [boardList, setBoardList] = useState<Array<{ slug: string; name: string | null; total: number; is_current: boolean }>>([]);
  const [showNewBoard, setShowNewBoard] = useState(false);
  const [showNewTask, setShowNewTask] = useState(false);
  const [boardData, setBoardData] = useState<BoardData | null>(null);
  const [config, setConfig] = useState<{ render_markdown?: boolean; default_tenant?: string; lane_by_profile?: boolean; include_archived_by_default?: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tenantFilter, setTenantFilter] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [search, setSearch] = useState("");
  const [laneByProfile, setLaneByProfile] = useState(true);
  const [configApplied, setConfigApplied] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [lastSelectedId, setLastSelectedId] = useState<string | null>(null);
  const [failedIds, setFailedIds] = useState<Set<string>>(() => new Set());
  const [draggingTaskId, setDraggingTaskId] = useState<string | null>(null);
  const [taskEventTick, setTaskEventTick] = useState<Record<string, number>>({});

  const cursorRef = useRef(0);
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsBackoffRef = useRef(1000);
  const wsClosedRef = useRef(false);

  // Lazy import TaskDrawer, BoardColumns, etc. to avoid circular deps
  // We'll use dynamic component loading via separate imports in production
  // For now, reference via module-level lazy pattern

  const handleDragStart = useCallback((taskId: string) => setDraggingTaskId(taskId), []);
  const handleDragEnd = useCallback(() => setDraggingTaskId(null), []);

  // Load config once
  useEffect(() => {
    SDK.fetchJSON<Record<string, unknown>>(withBoard(`${API_BASE}/config`, board))
      .then((c: Record<string, unknown>) => {
        setConfig(c as typeof config);
        if (!configApplied) {
          if (c.default_tenant) setTenantFilter(c.default_tenant as string);
          if (typeof c.lane_by_profile === "boolean") setLaneByProfile(c.lane_by_profile);
          if (typeof c.include_archived_by_default === "boolean") setIncludeArchived(c.include_archived_by_default);
          setConfigApplied(true);
        }
      })
      .catch(() => setConfig({ render_markdown: true }));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch board
  const loadBoard = useCallback(() => {
    const qs = new URLSearchParams();
    if (tenantFilter) qs.set("tenant", tenantFilter);
    if (includeArchived) qs.set("include_archived", "true");
    const url = qs.toString() ? `${API_BASE}/board?${qs}` : `${API_BASE}/board`;
    return SDK.fetchJSON<BoardData>(withBoard(url, board))
      .then(data => { setBoardData(data); cursorRef.current = data.latest_event_id || 0; setError(null); })
      .catch((err: Error) => setError(String(err?.message || err)))
      .finally(() => setLoading(false));
  }, [tenantFilter, includeArchived, board]);

  // Load board list for switcher
  const loadBoardList = useCallback(() => {
    return SDK.fetchJSON<{ boards: Array<{ slug: string; name: string | null; total: number; is_current: boolean }>; current: string }>(withBoard(`${API_BASE}/boards`, board))
      .then(data => {
        const boards = data.boards || [];
        const storedBoard = readSelectedBoard();
        setBoardList(boards);
        if (!storedBoard && !board && data.current) { setBoard(data.current); return; }
        if (board && board !== "default" && !boards.find(b => b.slug === board)) {
          setBoard("default");
          writeSelectedBoard("default");
        }
      })
      .catch(() => {});
  }, [board]);

  useEffect(() => { loadBoardList(); }, [loadBoardList]);

  const scheduleReload = useCallback(() => {
    if (reloadTimerRef.current) return;
    reloadTimerRef.current = setTimeout(() => { reloadTimerRef.current = null; loadBoard(); }, 250);
  }, [loadBoard]);

  useEffect(() => {
    loadBoard();
    return () => { if (reloadTimerRef.current) { clearTimeout(reloadTimerRef.current); reloadTimerRef.current = null; } };
  }, [loadBoard]);

  // WebSocket
  useEffect(() => {
    if (!boardData) return;
    wsClosedRef.current = false;
    function openWs() {
      if (wsClosedRef.current) return;
      const wsParams: Record<string, string> = { since: String(cursorRef.current || 0) };
      if (board) wsParams.board = board;
      SDK.buildWsUrl(`${API_BASE}/events`, wsParams).then(url => {
        if (wsClosedRef.current) return;
        let ws: WebSocket;
        try { ws = new WebSocket(url); } catch { return; }
        wsRef.current = ws;
        ws.onopen = () => { wsBackoffRef.current = 1000; };
        ws.onmessage = (ev: MessageEvent) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg?.events && Array.isArray(msg.events) && msg.events.length > 0) {
              cursorRef.current = msg.cursor || cursorRef.current;
              setTaskEventTick(prev => {
                const next = { ...prev };
                for (const e of msg.events) { if (e?.task_id) next[e.task_id] = (next[e.task_id] || 0) + 1; }
                return next;
              });
              scheduleReload();
            }
          } catch { /* ignore */ }
        };
        ws.onclose = (ev: CloseEvent) => {
          if (wsClosedRef.current) return;
          if (ev?.code === 1008) {
            setError(tx(t, "wsAuthFailed", "WebSocket auth failed \u2014 reload the page to refresh the session token."));
            return;
          }
          const delay = Math.min(wsBackoffRef.current, 30000);
          wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
          setTimeout(openWs, delay);
        };
      }).catch(() => {
        if (wsClosedRef.current) return;
        const delay = Math.min(wsBackoffRef.current, 30000);
        wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
        setTimeout(openWs, delay);
      });
    }
    openWs();
    return () => { wsClosedRef.current = true; try { wsRef.current?.close(); } catch { /* noop */ } };
  }, [!!boardData, board, scheduleReload]);

  // Filtering
  const filteredBoard = useMemo(() => {
    if (!boardData) return null;
    const q = search.trim().toLowerCase();
    const filterTask = (t: Task) => {
      if (tenantFilter && t.tenant !== tenantFilter) return false;
      if (assigneeFilter && t.assignee !== assigneeFilter) return false;
      if (q) {
        const hay = `${t.id} ${t.title || ""} ${t.body || ""} ${t.result || ""} ${t.latest_summary || ""} ${t.assignee || ""} ${t.tenant || ""}`.toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    };
    return { ...boardData, columns: boardData.columns.map(col => ({ ...col, tasks: col.tasks.filter(filterTask) })) };
  }, [boardData, tenantFilter, assigneeFilter, search]);

  // Actions
  const moveTask = useCallback((taskId: string, newStatus: string) => {
    const confirmMsg = getDestructiveConfirm(t, newStatus);
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    const patch = withCompletionSummary({ status: newStatus }, 1, t);
    if (!patch) return;
    setBoardData(b => {
      if (!b) return b;
      let moved: Task | null = null;
      const columns = b.columns.map(col => {
        const next = col.tasks.filter(tk => {
          if (tk.id === taskId) { moved = { ...tk, status: newStatus }; return false; }
          return true;
        });
        return { ...col, tasks: next };
      });
      if (moved) {
        const dest = columns.find(c => c.name === newStatus);
        if (dest) dest.tasks = [moved, ...dest.tasks];
      }
      return { ...b, columns };
    });
    SDK.fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, board), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).catch((err: Error) => {
      setError(tx(t, "moveFailed", "Move failed: ") + parseApiErrorMessage(err));
      loadBoard();
    });
  }, [loadBoard, board, t]);

  const clearSelected = useCallback(() => {
    setSelectedIds(new Set()); setLastSelectedId(null); setFailedIds(new Set());
  }, []);

  const moveSelected = useCallback((newStatus: string) => {
    const confirmMsg = DESTRUCTIVE_TRANSITIONS[newStatus];
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    if (selectedIds.size === 0) return;
    const patch = withCompletionSummary({ status: newStatus }, selectedIds.size, t);
    if (!patch) return;
    const ids = Array.from(selectedIds);
    setBoardData(b => {
      if (!b) return b;
      const moved: Task[] = [];
      const columns = b.columns.map(col => {
        const kept: Task[] = [];
        for (const tk of col.tasks) {
          if (selectedIds.has(tk.id)) moved.push({ ...tk, status: newStatus });
          else kept.push(tk);
        }
        return { ...col, tasks: kept };
      });
      const dest = columns.find(c => c.name === newStatus);
      if (dest) dest.tasks = [...moved, ...dest.tasks];
      return { ...b, columns };
    });
    SDK.fetchJSON<{ results: Array<{ id: string; ok: boolean; error?: string }> }>(withBoard(`${API_BASE}/tasks/bulk`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, ...patch }),
    }).then(res => {
      const failed = (res.results || []).filter(r => !r.ok);
      if (failed.length > 0) {
        setError(`Bulk move: ${failed.length} of ${res.results.length} failed`);
        setFailedIds(new Set(failed.map(f => f.id)));
      } else { setFailedIds(new Set()); }
      setSelectedIds(new Set()); setLastSelectedId(null); loadBoard();
    }).catch((err: Error) => {
      setError(`Move failed: ${err.message || err}`);
      setFailedIds(new Set(selectedIds)); loadBoard();
    });
  }, [selectedIds, loadBoard, board, t]);

  const createTask = useCallback((body: CreateTaskBody) => {
    return SDK.fetchJSON<{ warning?: string }>(withBoard(`${API_BASE}/tasks`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((res: { warning?: string }) => {
      if (res?.warning) setError(tx(t, "taskCreatedWarning", "Task created, but: ") + res.warning);
      loadBoard(); loadBoardList();
      return res;
    });
  }, [loadBoard, loadBoardList, board, t]);

  const toggleSelected = useCallback((id: string, additive?: boolean) => {
    setSelectedIds(prev => {
      const next = new Set(additive ? prev : []);
      if (prev.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setLastSelectedId(id);
    setFailedIds(prev => { if (prev.has(id)) { const n = new Set(prev); n.delete(id); return n; } return prev; });
  }, []);

  const toggleRange = useCallback((toId: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (!filteredBoard?.columns) return next;
      const order: string[] = [];
      for (const col of filteredBoard.columns) for (const tk of col.tasks || []) order.push(tk.id);
      const anchor = lastSelectedId;
      if (!anchor || anchor === toId) { next.add(toId); return next; }
      const aIdx = order.indexOf(anchor), bIdx = order.indexOf(toId);
      if (aIdx === -1 || bIdx === -1) { next.add(toId); return next; }
      const lo = Math.min(aIdx, bIdx), hi = Math.max(aIdx, bIdx);
      for (let i = lo; i <= hi; i++) next.add(order[i]);
      return next;
    });
    setLastSelectedId(toId);
  }, [filteredBoard, lastSelectedId]);

  const selectAllVisible = useCallback(() => {
    if (!filteredBoard?.columns) return;
    const next = new Set<string>();
    for (const col of filteredBoard.columns) for (const t of col.tasks || []) next.add(t.id);
    setSelectedIds(next);
    if (next.size > 0) setLastSelectedId(Array.from(next)[0]);
  }, [filteredBoard]);

  const selectAllInColumn = useCallback((columnName: string) => {
    if (!filteredBoard?.columns) return;
    const col = filteredBoard.columns.find(c => c.name === columnName);
    if (!col) return;
    const allSelected = col.tasks?.length > 0 && col.tasks.every(t => selectedIds.has(t.id));
    const next = new Set(selectedIds);
    if (allSelected) { for (const t of col.tasks || []) next.delete(t.id); }
    else { for (const t of col.tasks || []) next.add(t.id); }
    setSelectedIds(next);
    if (col.tasks?.length) setLastSelectedId(col.tasks[0].id);
  }, [filteredBoard, selectedIds]);

  const applyBulk = useCallback((patch: Record<string, unknown>, confirmMsg?: string) => {
    if (selectedIds.size === 0) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    const finalPatch = withCompletionSummary(patch, selectedIds.size, t);
    if (!finalPatch) return;
    const body = { ids: Array.from(selectedIds), ...finalPatch };
    if (finalPatch.status) {
      setBoardData(b => {
        if (!b) return b;
        const moved: Task[] = [];
        const columns = b.columns.map(col => {
          const kept: Task[] = [];
          for (const t of col.tasks) {
            if (selectedIds.has(t.id)) moved.push({ ...t, status: finalPatch.status as string });
            else kept.push(t);
          }
          return { ...col, tasks: kept };
        });
        const dest = columns.find(c => c.name === finalPatch.status);
        if (dest) dest.tasks = [...moved, ...dest.tasks];
        return { ...b, columns };
      });
    }
    SDK.fetchJSON<{ results: Array<{ id: string; ok: boolean; error?: string }> }>(withBoard(`${API_BASE}/tasks/bulk`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(res => {
      const failed = (res.results || []).filter(r => !r.ok);
      if (failed.length > 0) {
        setError(tx(t, "bulkFailed", "Bulk: ") + `${failed.length} of ${res.results.length} failed: ` +
          failed.slice(0, 3).map(f => `${f.id} (${f.error})`).join("; "));
        setFailedIds(new Set(failed.map(f => f.id)));
      } else { setFailedIds(new Set()); }
      setSelectedIds(new Set()); setLastSelectedId(null); loadBoard();
    }).catch((e: Error) => { setError(String(e.message || e)); setFailedIds(new Set(selectedIds)); loadBoard(); });
  }, [selectedIds, loadBoard, board, t]);

  const switchBoard = useCallback((nextSlug: string) => {
    if (!nextSlug || nextSlug === board) return;
    setBoardData(null); cursorRef.current = 0; setLoading(true);
    setBoard(nextSlug); writeSelectedBoard(nextSlug);
    setSearch(""); setTenantFilter(""); setAssigneeFilter(""); setIncludeArchived(false);
    clearSelected();
  }, [board, clearSelected]);

  const createNewBoard = useCallback((payload: { slug: string; name?: string; switch?: boolean }) => {
    return SDK.fetchJSON<{ board?: { slug: string } }>(`${API_BASE}/boards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((res: { board?: { slug: string } }) => {
      loadBoardList();
      const slug = res?.board?.slug;
      if (slug && payload.switch) switchBoard(slug);
      return res;
    });
  }, [loadBoardList, switchBoard]);

  const deleteBoard = useCallback((slug: string) => {
    if (!slug || slug === "default") return Promise.resolve();
    return SDK.fetchJSON(`${API_BASE}/boards/${encodeURIComponent(slug)}`, { method: "DELETE" })
      .then(() => { loadBoardList(); if (board === slug) switchBoard("default"); });
  }, [board, loadBoardList, switchBoard]);

  const deleteTask = useCallback((taskId: string) => {
    if (!window.confirm(tx(t, "trash.confirm", FALLBACK_TRASH.confirm))) return Promise.resolve();
    return SDK.fetchJSON(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" })
      .then(() => {
        loadBoard();
        setSelectedIds(prev => { const n = new Set(prev); n.delete(taskId); return n; });
      }).catch((e: Error) => setError(String(e.message || e)));
  }, [board, loadBoard, t]);

  const deleteSelected = useCallback((count: number) => {
    if (selectedIds.size === 0) return Promise.resolve();
    if (!window.confirm(tx(t, "trash.confirmMany", "Permanently delete {n} selected tasks? This cannot be undone.", { n: count }))) return Promise.resolve();
    const ids = Array.from(selectedIds);
    setSelectedIds(new Set());
    return Promise.all(ids.map(id => SDK.fetchJSON(`${API_BASE}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" })))
      .then(() => loadBoard())
      .catch((e: Error) => setError(String(e.message || e)));
  }, [selectedIds, board, loadBoard, t]);

  // Render
  if (loading && !boardData) {
    return h("div", { className: "p-8 text-sm text-muted-foreground" },
      tx(t, "loading", "Loading Kanban board\u2026"));
  }
  if (error && !boardData) {
    return h(Card, null,
      h(CardContent, { className: "p-6" },
        h("div", { className: "text-sm text-destructive" },
          tx(t, "loadFailed", "Failed to load Kanban board: "), error),
        h("div", { className: "text-xs text-muted-foreground mt-2" },
          tx(t, "loadFailedHint",
            "The backend auto-creates kanban.db on first read. If this persists, check the dashboard logs.")),
      ),
    );
  }
  if (!filteredBoard) return null;

  const renderMd = !config || config.render_markdown !== false;
  const allTasks = boardData!.columns.reduce((acc: Task[], c) => acc.concat(c.tasks), []);

  return h(ErrorBoundary, null,
    h("div", { className: "hermes-kanban flex flex-col gap-4" },
      h(BoardSwitcher, {
        board, boardList,
        onSwitch: switchBoard,
        onNewClick: () => setShowNewBoard(true),
        onDeleteBoard: deleteBoard,
      }),
      showNewBoard ? h(NewBoardDialog, {
        onCancel: () => setShowNewBoard(false),
        onCreate: createNewBoard,
      }) : null,
      h(OrchestrationPanel),
      h(AttentionStrip, { boardData, onOpen: setSelectedTaskId }),
      h(BoardToolbar, {
        board: boardData, search, setSearch,
        tenantFilter, setTenantFilter,
        assigneeFilter, setAssigneeFilter,
        includeArchived, setIncludeArchived,
        laneByProfile, setLaneByProfile,
        onNudgeDispatch: () => {
          SDK.fetchJSON(withBoard(`${API_BASE}/dispatch?max=8`, board), { method: "POST" })
            .then(loadBoard).catch((e: Error) => setError(String(e.message || e)));
        },
        onRefresh: loadBoard,
        onNewTask: () => setShowNewTask(true),
      }),
      selectedIds.size > 0 ? h(BulkActionBar, {
        count: selectedIds.size,
        assignees: boardData?.assignees || [],
        onApply: applyBulk,
        onClear: clearSelected,
        onSelectAllVisible: selectAllVisible,
        onDelete: deleteSelected,
      }) : null,
      error ? h("div", { className: "text-xs text-destructive px-2" }, error) : null,
      h(BoardColumns, {
        board: filteredBoard, laneByProfile,
        selectedIds, failedIds, draggingTaskId,
        onDragStart: handleDragStart, onDragEnd: handleDragEnd,
        toggleSelected, toggleRange, selectAllInColumn,
        onMove: moveTask, onMoveSelected: moveSelected,
        onDelete: deleteTask, onOpen: setSelectedTaskId,
        onCreate: createTask, allTasks,
      }),
      selectedTaskId ? h(TaskDrawer, {
        taskId: selectedTaskId, boardSlug: board,
        onClose: () => setSelectedTaskId(null),
        onRefresh: loadBoard, renderMarkdown: renderMd,
        allTasks, assignees: boardData?.assignees || [],
        eventTick: taskEventTick[selectedTaskId] || 0,
      }) : null,
      h(NewTaskDialog, {
        open: showNewTask,
        onClose: () => setShowNewTask(false),
        onCreate: (body: CreateTaskBody) => createTask(body).then(() => { setShowNewTask(false); }),
      }),
    ),
  );
}