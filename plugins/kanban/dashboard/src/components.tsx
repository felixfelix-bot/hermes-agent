/**
 * Kanban dashboard plugin — board, column, and card components.
 *
 * All React + UI components are obtained at runtime from the host SDK via
 * ``window.__HERMES_PLUGIN_SDK__`` (see ``../sdk``). No React is bundled.
 *
 * Exports:
 *  - ``BoardColumns``  — renders all columns + TrashDropZone
 *  - ``Column``        — single column with drag-and-drop, lane-by-profile
 *  - ``TaskCard``      — draggable card
 *  - ``InlineCreate``  — inline task creation form
 *  - ``TrashDropZone`` — drag-and-drop delete zone
 */

import type { Task, Board, CreateTaskBody } from "./types";
import type { Column as ColumnType } from "./types";
import {
  getReact,
  getHooks,
  getComponents,
  getUtils,
  getUseI18n,
  getCheckbox,
  selectChangeHandler,
} from "./sdk";
import { MIME_TASK, COLUMN_DOT, STALENESS } from "./constants";
import {
  tx,
  getColumnLabel,
  getColumnHelp,
  FALLBACK_TRASH,
} from "./i18n";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ── Runtime handles (resolved once at module load) ──────────────────────────

const ReactRuntime = getReact();
const { createElement: h } = ReactRuntime;

const hooks = getHooks();
const { useState, useEffect, useCallback, useMemo, useRef } = hooks;

const components = getComponents();
const Card = components.Card as any;
const CardContent = components.CardContent as any;
const Badge = components.Badge as any;
const Button = components.Button as any;
const Input = components.Input as any;
const Select = components.Select as any;
const SelectOption = components.SelectOption as any;
const Checkbox = getCheckbox() as any;

const utils = getUtils();
const { cn, timeAgo } = utils;

const useI18n = getUseI18n() as () => { t: unknown };

// ── Touch drag-drop helper ──────────────────────────────────────────────────
//
// HTML5 DnD is desktop-only. On touch devices we attach a pointerdown
// handler that simulates a drag proxy and fires a custom event on the
// column under the finger when released. Columns listen for both the
// standard `drop` event and our `hermes-kanban:drop` event.

function attachTouchDrag(el: HTMLElement | null, taskId: string): (() => void) | undefined {
  if (!el) return;
  const elRef = el;
  function onDown(e: PointerEvent) {
    if (e.pointerType !== "touch") return;
    e.preventDefault();
    const proxy = elRef.cloneNode(true) as HTMLElement;
    proxy.classList.add("hermes-kanban-touch-proxy");
    document.body.appendChild(proxy);
    let lastTarget: HTMLElement | null = null;

    function move(ev: PointerEvent) {
      proxy.style.left = `${ev.clientX - proxy.offsetWidth / 2}px`;
      proxy.style.top = `${ev.clientY - 24}px`;
      proxy.style.display = "none";
      const under = document.elementFromPoint(ev.clientX, ev.clientY);
      proxy.style.display = "";
      const col = under && under.closest && under.closest("[data-kanban-column]");
      const trash = under && under.closest && under.closest("[data-kanban-trash]");
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
          lastTarget.dispatchEvent(
            new CustomEvent("hermes-kanban:delete", {
              detail: { taskId },
              bubbles: true,
            }),
          );
        } else if (status) {
          lastTarget.dispatchEvent(
            new CustomEvent("hermes-kanban:drop", {
              detail: { taskId, status },
              bubbles: true,
            }),
          );
        }
      }
      proxy.remove();
    }
    // Kick off proxy at the pointer origin.
    proxy.style.position = "fixed";
    proxy.style.pointerEvents = "none";
    proxy.style.opacity = "0.85";
    proxy.style.zIndex = "9999";
    proxy.style.width = `${elRef.offsetWidth}px`;
    proxy.style.left = `${e.clientX - elRef.offsetWidth / 2}px`;
    proxy.style.top = `${e.clientY - 24}px`;
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  }
  elRef.addEventListener("pointerdown", onDown);
  return function () {
    elRef.removeEventListener("pointerdown", onDown);
  };
}

// ── Staleness ───────────────────────────────────────────────────────────────

function stalenessClass(task: Task): string {
  if (!task || !task.age) return "";
  const age =
    task.status === "running"
      ? task.age.started_age_seconds
      : task.age.created_age_seconds;
  const tier = STALENESS[task.status];
  if (!tier || age == null) return "";
  if (age >= tier.red) return "hermes-kanban-card--stale-red";
  if (age >= tier.amber) return "hermes-kanban-card--stale-amber";
  return "";
}

// ── Prop interfaces ──────────────────────────────────────────────────────────

export interface BoardColumnsProps {
  board: Board;
  laneByProfile: boolean;
  selectedIds: Set<string>;
  failedIds: Set<string> | null;
  draggingTaskId: string | null;
  toggleSelected: (id: string, toggle: boolean) => void;
  toggleRange: (id: string) => void;
  selectAllInColumn: ((col: string) => void) | null;
  onMove: (taskId: string, status: string) => void;
  onMoveSelected: ((status: string) => void) | null;
  onOpen: (taskId: string) => void;
  onCreate: (body: CreateTaskBody) => Promise<unknown>;
  onDelete: (taskId: string) => Promise<unknown>;
  allTasks: Task[];
  onDragStart: ((taskId: string) => void) | null;
  onDragEnd: (() => void) | null;
}

export interface ColumnProps {
  column: ColumnType;
  laneByProfile: boolean;
  selectedIds: Set<string>;
  failedIds: Set<string> | null;
  draggingTaskId: string | null;
  toggleSelected: (id: string, toggle: boolean) => void;
  toggleRange: (id: string) => void;
  selectAllInColumn: ((col: string) => void) | null;
  onMove: (taskId: string, status: string) => void;
  onMoveSelected: ((status: string) => void) | null;
  onOpen: (taskId: string) => void;
  onCreate: (body: CreateTaskBody) => Promise<unknown>;
  allTasks: Task[];
}

export interface TaskCardProps {
  task: Task;
  selected: boolean;
  failed: boolean | undefined;
  draggingTaskId: string | null;
  draggingSource: boolean | null;
  toggleSelected: (id: string, toggle: boolean) => void;
  toggleRange: ((id: string) => void) | null;
  onOpen: (taskId: string) => void;
}

export interface InlineCreateProps {
  columnName: string;
  allTasks: Task[];
  onSubmit: (body: CreateTaskBody) => void;
  onCancel: () => void;
}

export interface TrashDropZoneProps {
  draggingTaskId: string | null;
  selectedIds: Set<string>;
  onDelete: (taskId: string) => Promise<unknown>;
}

// ── BoardColumns ────────────────────────────────────────────────────────────

export function BoardColumns(props: BoardColumnsProps) {
  const handleDragStart = useCallback(
    function (e: React.DragEvent) {
      const target = e.target as HTMLElement;
      const card = target.closest && target.closest(".hermes-kanban-card");
      if (!card) return;
      const taskId = card.getAttribute("data-task-id");
      if (taskId && props.onDragStart) props.onDragStart(taskId);
    },
    [props.onDragStart],
  );
  const handleDragEnd = useCallback(function () {
    if (props.onDragEnd) props.onDragEnd();
  }, [props.onDragEnd]);

  return h(
    "div",
    {
      className: "hermes-kanban-columns",
      onDragStart: handleDragStart,
      onDragEnd: handleDragEnd,
    },
    props.board.columns.map(function (col: ColumnType) {
      return h(Column, {
        key: col.name,
        column: col,
        laneByProfile: props.laneByProfile,
        selectedIds: props.selectedIds,
        failedIds: props.failedIds,
        draggingTaskId: props.draggingTaskId,
        toggleSelected: props.toggleSelected,
        toggleRange: props.toggleRange,
        selectAllInColumn: props.selectAllInColumn,
        onMove: props.onMove,
        onMoveSelected: props.onMoveSelected,
        onOpen: props.onOpen,
        onCreate: props.onCreate,
        allTasks: props.allTasks,
      });
    }),
    h(TrashDropZone, {
      draggingTaskId: props.draggingTaskId,
      selectedIds: props.selectedIds,
      onDelete: props.onDelete,
    }),
  );
}

// ── Column ──────────────────────────────────────────────────────────────────

export function Column(props: ColumnProps) {
  const { t } = useI18n();
  const [dragOver, setDragOver] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const colRef = useRef<HTMLElement | null>(null);

  // Listen for our synthetic touch-drop events from attachTouchDrag().
  useEffect(
    function () {
      if (!colRef.current) return undefined;
      const el = colRef.current;
      function onTouchDrop(e: Event) {
        const detail = (e as CustomEvent).detail;
        if (detail && detail.status === props.column.name) {
          const taskId = detail.taskId;
          if (
            props.selectedIds &&
            props.selectedIds.has(taskId) &&
            props.selectedIds.size > 1 &&
            props.onMoveSelected
          ) {
            props.onMoveSelected(props.column.name);
          } else {
            props.onMove(taskId, props.column.name);
          }
        }
      }
      el.addEventListener("hermes-kanban:drop", onTouchDrop);
      return function () {
        el.removeEventListener("hermes-kanban:drop", onTouchDrop);
      };
    },
    [props.column.name, props.onMove, props.selectedIds, props.onMoveSelected],
  );

  const handleDragOver = function (e: React.DragEvent) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!dragOver) setDragOver(true);
  };
  const handleDragLeave = function () {
    setDragOver(false);
  };
  const handleDrop = function (e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const taskId = e.dataTransfer.getData(MIME_TASK);
    if (!taskId) return;
    if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
      if (props.onMoveSelected) props.onMoveSelected(props.column.name);
    } else {
      props.onMove(taskId, props.column.name);
    }
  };

  const lanes = useMemo(
    function () {
      if (!props.laneByProfile || props.column.name !== "running") return null;
      const byProfile: Record<string, Task[]> = {};
      for (const tk of props.column.tasks) {
        const key = tk.assignee || "(unassigned)";
        (byProfile[key] = byProfile[key] || []).push(tk);
      }
      return Object.keys(byProfile)
        .sort()
        .map(function (k) {
          return { assignee: k, tasks: byProfile[k] };
        });
    },
    [props.column, props.laneByProfile],
  );

  const colHelp = getColumnHelp(t, props.column.name);
  const colLabel = getColumnLabel(t, props.column.name);

  return h(
    "div",
    {
      ref: colRef,
      "data-kanban-column": props.column.name,
      className: cn(
        "hermes-kanban-column",
        dragOver ? "hermes-kanban-column--drop" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
    h(
      "div",
      {
        className: "hermes-kanban-column-header",
        title: colHelp || "",
      },
      h(Checkbox, {
        className: "hermes-kanban-col-check",
        title: "Select all tasks in this column",
        "aria-label": `Select all tasks in ${colLabel || props.column.name}`,
        checked:
          props.column.tasks.length > 0 &&
          props.column.tasks.every(function (tk: Task) {
            return props.selectedIds.has(tk.id);
          }),
        onCheckedChange: function () {
          if (props.selectAllInColumn) props.selectAllInColumn(props.column.name);
        },
        onClick: function (e: React.MouseEvent) {
          e.stopPropagation();
        },
      }),
      h("span", {
        className: cn("hermes-kanban-dot", COLUMN_DOT[props.column.name]),
      }),
      h("span", { className: "hermes-kanban-column-label" }, colLabel || props.column.name),
      h(
        "span",
        {
          className: "hermes-kanban-column-count",
          title: `${props.column.tasks.length} task${props.column.tasks.length === 1 ? "" : "s"} in this column`,
        },
        props.column.tasks.length,
      ),
      h("button", {
        type: "button",
        className: "hermes-kanban-column-add",
        title: tx(t, "createTask", "Create task in this column"),
        onClick: function () {
          setShowCreate(function (v: boolean) {
            return !v;
          });
        },
      }, showCreate ? "×" : "+"),
    ),
    h("div", { className: "hermes-kanban-column-sub" }, colHelp || ""),
    showCreate
      ? h(InlineCreate, {
          columnName: props.column.name,
          allTasks: props.allTasks,
          onSubmit: function (body: CreateTaskBody) {
            props.onCreate(body).then(function () {
              setShowCreate(false);
            });
          },
          onCancel: function () {
            setShowCreate(false);
          },
        })
      : null,
    h(
      "div",
      { className: "hermes-kanban-column-body" },
      props.column.tasks.length === 0
        ? h("div", { className: "hermes-kanban-empty" }, tx(t, "noTasks", "— no tasks —"))
        : lanes
          ? lanes.map(function (lane: { assignee: string; tasks: Task[] }) {
              return h(
                "div",
                { key: lane.assignee, className: "hermes-kanban-lane" },
                h(
                  "div",
                  { className: "hermes-kanban-lane-head" },
                  h("span", { className: "hermes-kanban-lane-name" }, lane.assignee),
                  h("span", { className: "hermes-kanban-lane-count" }, lane.tasks.length),
                ),
                lane.tasks.map(function (tk: Task) {
                  return h(TaskCard, {
                    key: tk.id,
                    task: tk,
                    selected: props.selectedIds.has(tk.id),
                    failed: props.failedIds && props.failedIds.has(tk.id),
                    draggingTaskId: props.draggingTaskId,
                    draggingSource:
                      props.draggingTaskId &&
                      props.selectedIds.has(props.draggingTaskId) &&
                      props.selectedIds.size > 1 &&
                      props.selectedIds.has(tk.id),
                    toggleSelected: props.toggleSelected,
                    toggleRange: props.toggleRange,
                    onOpen: props.onOpen,
                  });
                }),
              );
            })
          : props.column.tasks.map(function (tk: Task) {
              return h(TaskCard, {
                key: tk.id,
                task: tk,
                selected: props.selectedIds.has(tk.id),
                failed: props.failedIds && props.failedIds.has(tk.id),
                draggingTaskId: props.draggingTaskId,
                draggingSource:
                  props.draggingTaskId &&
                  props.selectedIds.has(props.draggingTaskId) &&
                  props.selectedIds.size > 1 &&
                  props.selectedIds.has(tk.id),
                toggleSelected: props.toggleSelected,
                toggleRange: props.toggleRange,
                onOpen: props.onOpen,
              });
            }),
    ),
  );
}

// ── TaskCard ─────────────────────────────────────────────────────────────────

export function TaskCard(props: TaskCardProps) {
  const { t: i18n } = useI18n();
  const t = props.task;
  const cardRef = useRef<HTMLElement | null>(null);

  useEffect(function () {
    return attachTouchDrag(cardRef.current as HTMLElement | null, t.id);
  }, [t.id]);

  const handleDragStart = function (e: React.DragEvent) {
    e.dataTransfer.setData(MIME_TASK, t.id);
    e.dataTransfer.effectAllowed = "move";
    const selectedCards = document.querySelectorAll(".hermes-kanban-card--selected");
    if (selectedCards.length > 1 && props.selected) {
      const ghost = document.createElement("div");
      ghost.className = "hermes-kanban-drag-ghost";
      ghost.textContent = selectedCards.length + " cards";
      document.body.appendChild(ghost);
      e.dataTransfer.setDragImage(ghost, 0, 0);
      requestAnimationFrame(function () {
        if (ghost.parentNode) document.body.removeChild(ghost);
      });
    }
  };
  const handleClick = function (e: React.MouseEvent) {
    if (e.shiftKey) {
      e.preventDefault();
      e.stopPropagation();
      if (props.toggleRange) props.toggleRange(t.id);
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
      props.toggleSelected(t.id, true);
      return;
    }
    props.onOpen(t.id);
  };
  const handleKeyDown = function (e: React.KeyboardEvent) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      props.onOpen(t.id);
    }
    if (e.key === "Escape") {
      if (props.toggleSelected) props.toggleSelected(t.id, false);
    }
  };
  const handleCheckedChange = function () {
    props.toggleSelected(t.id, true);
  };

  const progress = t.progress;
  const needsAssignee = t.status === "ready" && !t.assignee;

  return h(
    "div",
    {
      ref: cardRef,
      "data-task-id": t.id,
      className: cn(
        "hermes-kanban-card",
        props.selected ? "hermes-kanban-card--selected" : "",
        props.failed ? "hermes-kanban-card--failed" : "",
        props.draggingSource ? "hermes-kanban-card--dragging-source" : "",
        stalenessClass(t),
      ),
      draggable: true,
      tabIndex: 0,
      role: "button",
      "aria-label": `${t.title || "untitled"} — ${t.id} — ${t.status}`,
      onDragStart: handleDragStart,
      onClick: handleClick,
      onKeyDown: handleKeyDown,
    },
    h(
      Card,
      null,
      h(
        CardContent,
        { className: "hermes-kanban-card-content" },
        h(
          "div",
          { className: "hermes-kanban-card-row" },
          h(
            "label",
            {
              className: "hermes-kanban-card-check-wrap",
              title: tx(i18n, "selectForBulk", "Select for bulk actions"),
              onClick: function (e: React.MouseEvent) {
                e.stopPropagation();
              },
            },
            h(Checkbox, {
              className: "hermes-kanban-card-check",
              checked: props.selected,
              onCheckedChange: handleCheckedChange,
              onClick: function (e: React.MouseEvent) {
                e.stopPropagation();
              },
              "aria-label": `Select task ${t.id}`,
            }),
          ),
          h(
            "span",
            {
              className: "hermes-kanban-card-id",
              title: `Task id: ${t.id}. Use this id with kanban_show, /kanban show, or hermes kanban show.`,
            },
            t.id,
          ),
          t.warnings && t.warnings.count > 0
            ? h("span", {
                className: cn(
                  "hermes-kanban-warning-badge",
                  "hermes-kanban-warning-badge--" + (t.warnings.highest_severity || "warning"),
                ),
                title:
                  `${t.warnings.count} active diagnostic` +
                  (t.warnings.count === 1 ? "" : "s") +
                  ` (severity: ${t.warnings.highest_severity || "warning"}). ` +
                  `Click to open for details.`,
              },
              t.warnings.highest_severity === "critical"
                ? "!!!"
                : t.warnings.highest_severity === "error"
                  ? "!!"
                  : "⚠")
            : null,
          t.priority > 0
            ? h(
                Badge,
                {
                  className: "hermes-kanban-priority",
                  title: `Priority ${t.priority}. Higher-priority tasks are claimed first by the dispatcher.`,
                },
                `P${t.priority}`,
              )
            : null,
          t.tenant
            ? h(
                Badge,
                {
                  variant: "outline",
                  className: "hermes-kanban-tag",
                  title: `Tenant: ${t.tenant}. Free-form tag for grouping tasks (customer, project, team).`,
                },
                t.tenant,
              )
            : null,
          progress
            ? h(
                "span",
                {
                  className: cn(
                    "hermes-kanban-progress",
                    progress.done === progress.total ? "hermes-kanban-progress--full" : "",
                  ),
                  title: `${progress.done} of ${progress.total} child tasks done`,
                },
                `${progress.done}/${progress.total}`,
              )
            : null,
          needsAssignee
            ? h(
                Badge,
                {
                  variant: "outline",
                  className: "hermes-kanban-needs-assignee",
                  title: tx(
                    i18n,
                    "needsAssigneeHint",
                    "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.",
                  ),
                },
                tx(i18n, "needsAssignee", "Needs assignee"),
              )
            : null,
        ),
        h("div", { className: "hermes-kanban-card-title" }, t.title || tx(i18n, "untitled", "(untitled)")),
        h(
          "div",
          { className: "hermes-kanban-card-row hermes-kanban-card-meta" },
          t.assignee
            ? h(
                "span",
                {
                  className: "hermes-kanban-assignee",
                  title: `Assigned to Hermes profile @${t.assignee}`,
                },
                "@",
                t.assignee,
              )
            : h(
                "span",
                {
                  className: "hermes-kanban-unassigned",
                  title: needsAssignee
                    ? tx(
                        i18n,
                        "needsAssigneeHint",
                        "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.",
                      )
                    : "No profile assigned.",
                },
                tx(i18n, "unassigned", "unassigned"),
              ),
          t.comment_count > 0
            ? h(
                "span",
                {
                  className: "hermes-kanban-count",
                  title: `${t.comment_count} comment${t.comment_count === 1 ? "" : "s"} on this task`,
                },
                "💬 ",
                t.comment_count,
              )
            : null,
          t.link_counts && t.link_counts.parents + t.link_counts.children > 0
            ? h(
                "span",
                {
                  className: "hermes-kanban-count",
                  title: `${t.link_counts.parents} parent${t.link_counts.parents === 1 ? "" : "s"}, ${t.link_counts.children} child${t.link_counts.children === 1 ? "" : "ren"}. Children stay blocked until their parent is done.`,
                },
                "↔ ",
                t.link_counts.parents + t.link_counts.children,
              )
            : null,
          h(
            "span",
            {
              className: "hermes-kanban-ago",
              title: t.created_at ? `Created ${t.created_at}` : "",
            },
            timeAgo ? timeAgo(t.created_at) : "",
          ),
        ),
      ),
    ),
  );
}

// ── InlineCreate ─────────────────────────────────────────────────────────────

export function InlineCreate(props: InlineCreateProps) {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [assignee, setAssignee] = useState("");
  const [priority, setPriority] = useState<number | string>(0);
  const [parent, setParent] = useState("");
  const [skills, setSkills] = useState("");
  // Workspace controls. `scratch` (default) ignores path; `worktree` optionally
  // takes a path (dispatcher derives one from the assignee profile otherwise);
  // `dir` requires a path. Backend enforces the rule — we only hide/show the
  // input here to save vertical space in the common `scratch` case.
  const [workspaceKind, setWorkspaceKind] = useState("scratch");
  const [workspacePath, setWorkspacePath] = useState("");
  // Goal-mode: when on, the dispatched worker runs the Ralph-style /goal
  // loop — a judge re-checks the card after each turn and the worker keeps
  // going in the same session until done, or the turn budget runs out
  // (which blocks the card for review). goalMaxTurns is optional; blank
  // = backend default.
  const [goalMode, setGoalMode] = useState(false);
  const [goalMaxTurns, setGoalMaxTurns] = useState("");

  const submit = function () {
    const trimmed = title.trim();
    if (!trimmed) return;
    const body: any = {
      title: trimmed,
      assignee: assignee.trim() || null,
      priority: Number(priority) || 0,
      triage: props.columnName === "triage",
    };
    if (parent) body.parents = [parent];
    // Parse comma-separated skills into a clean list. Blank = no
    // extras (omit key so backend leaves it null). The dispatcher
    // always auto-loads kanban-worker; these are extras on top.
    const skillList = skills
      .split(",")
      .map(function (s: string) {
        return s.trim();
      })
      .filter(function (s: string) {
        return s.length > 0;
      });
    if (skillList.length > 0) body.skills = skillList;
    // Only send workspace_kind when it's non-default. Keeps the request
    // shape small and interoperable with older dispatcher versions.
    if (workspaceKind && workspaceKind !== "scratch") {
      body.workspace_kind = workspaceKind;
    }
    const wpTrim = workspacePath.trim();
    if (wpTrim) body.workspace_path = wpTrim;
    // Goal-mode toggle. Only send the keys when enabled so the request
    // shape stays small and old dispatchers ignore it cleanly.
    if (goalMode) {
      body.goal_mode = true;
      const gmt = parseInt(goalMaxTurns, 10);
      if (Number.isFinite(gmt) && gmt > 0) body.goal_max_turns = gmt;
    }
    props.onSubmit(body);
    setTitle("");
    setAssignee("");
    setPriority(0);
    setParent("");
    setSkills("");
    setWorkspaceKind("scratch");
    setWorkspacePath("");
    setGoalMode(false);
    setGoalMaxTurns("");
  };

  const showPathInput = workspaceKind !== "scratch";
  const pathPlaceholder =
    workspaceKind === "dir"
      ? tx(t, "workspacePathDir", "workspace path (required, e.g. ~/projects/my-app)")
      : tx(t, "workspacePathOptional", "workspace path (optional, derived from assignee if blank)");

  return h(
    "div",
    { className: "hermes-kanban-inline-create" },
    h("textarea", {
      value: title,
      onChange: function (e: React.ChangeEvent<HTMLTextAreaElement>) {
        setTitle(e.target.value);
      },
      onKeyDown: function (e: React.KeyboardEvent) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          submit();
        }
        if (e.key === "Escape") props.onCancel();
      },
      placeholder:
        props.columnName === "triage"
          ? tx(t, "triagePlaceholder", "Rough idea — AI will spec it…")
          : tx(t, "taskTitlePlaceholder", "New task title…"),
      autoFocus: true,
      className:
        "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
      rows: 2,
    }),
    h(
      "div",
      { className: "flex gap-2" },
      h(Input, {
        value: assignee,
        onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
          setAssignee(e.target.value);
        },
        placeholder:
          props.columnName === "triage"
            ? tx(t, "specifier", "specifier")
            : tx(t, "assigneePlaceholder", "assignee"),
        className: "h-7 text-xs flex-1",
        title:
          props.columnName === "triage"
            ? "Hermes profile that will spec this task (default: the dispatcher's configured specifier). Leave blank to let the dispatcher pick."
            : "Hermes profile to assign. Leave blank and the dispatcher will pick from available profiles when the task is Ready.",
        style: { textTransform: "none" },
        autoCapitalize: "none",
        autoCorrect: "off",
        spellCheck: false,
      }),
      h(Input, {
        type: "number",
        value: priority,
        onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
          setPriority(e.target.value);
        },
        placeholder: "pri",
        className: "h-7 text-xs w-16",
        title: "Priority. Higher-priority tasks are claimed first by the dispatcher. 0 = default.",
      }),
    ),
    h(Input, {
      value: skills,
      onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
        setSkills(e.target.value);
      },
      placeholder: tx(t, "skillsPlaceholder", "skills (optional, comma-separated): translation, github-code-review"),
      title: "Force-load these skills into the worker (in addition to the built-in kanban-worker).",
      className: "h-7 text-xs",
    }),
    h(
      "div",
      { className: "flex gap-2 items-center" },
      h(
        "label",
        {
          className: "flex items-center gap-1.5 text-xs cursor-pointer select-none",
          title:
            "Goal mode: the worker keeps going in the same session until a judge agrees the card is done (or the turn budget runs out, which blocks it for review). Best for open-ended cards one shot rarely finishes.",
        },
        h("input", {
          type: "checkbox",
          checked: goalMode,
          onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
            setGoalMode(!!e.target.checked);
          },
          className: "h-3.5 w-3.5 accent-current",
        }),
        tx(t, "goalMode", "goal mode"),
      ),
      goalMode
        ? h(Input, {
            type: "number",
            value: goalMaxTurns,
            onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
              setGoalMaxTurns(e.target.value);
            },
            placeholder: tx(t, "goalMaxTurns", "max turns (default 20)"),
            className: "h-7 text-xs w-40",
            title: "Turn budget for the goal loop. Blank = backend default (20).",
            min: 1,
          })
        : null,
    ),
    h(
      "div",
      { className: "flex gap-2" },
      h(
        Select,
        Object.assign(
          {
            value: workspaceKind,
            title:
              "scratch: isolated temp dir (default). worktree: git worktree on the assignee profile. dir: exact path (required below).",
            className: "h-7 text-xs w-28",
          },
          selectChangeHandler(setWorkspaceKind),
        ),
        h(SelectOption, { value: "scratch" }, "scratch"),
        h(SelectOption, { value: "worktree" }, "worktree"),
        h(SelectOption, { value: "dir" }, "dir"),
      ),
      showPathInput
        ? h(Input, {
            value: workspacePath,
            onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
              setWorkspacePath(e.target.value);
            },
            placeholder: pathPlaceholder,
            className: "h-7 text-xs flex-1",
          })
        : null,
    ),
    h(
      Select,
      Object.assign(
        {
          value: parent,
          className: "h-7 text-xs",
          title: "Optional parent task. A child stays blocked in its current column until the parent is marked done.",
        },
        selectChangeHandler(setParent),
      ),
      h(SelectOption, { value: "" }, tx(t, "noParent", "— no parent —")),
      (props.allTasks || []).map(function (task: Task) {
        return h(
          SelectOption,
          { key: task.id, value: task.id },
          `${task.id} — ${(task.title || "").slice(0, 50)}`,
        );
      }),
    ),
    h(
      "div",
      { className: "flex gap-2" },
      h(
        Button,
        {
          onClick: submit,
          size: "sm",
        },
        "Create",
      ),
      h(
        Button,
        {
          onClick: props.onCancel,
          size: "sm",
        },
        tx(t, "cancel", "Cancel"),
      ),
    ),
  );
}

// ── TrashDropZone ────────────────────────────────────────────────────────────

export function TrashDropZone(props: TrashDropZoneProps) {
  const { t } = useI18n();
  const [dragOver, setDragOver] = useState(false);
  const zoneRef = useRef<HTMLElement | null>(null);

  useEffect(
    function () {
      if (!zoneRef.current) return undefined;
      const el = zoneRef.current;
      function onTouchDelete(e: Event) {
        const detail = (e as CustomEvent).detail;
        const taskId = detail && detail.taskId;
        if (taskId && props.onDelete) props.onDelete(taskId);
      }
      el.addEventListener("hermes-kanban:delete", onTouchDelete);
      return function () {
        el.removeEventListener("hermes-kanban:delete", onTouchDelete);
      };
    },
    [props.onDelete],
  );

  const handleDragOver = function (e: React.DragEvent) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!dragOver) setDragOver(true);
  };
  const handleDragLeave = function () {
    setDragOver(false);
  };
  const handleDrop = function (e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const taskId = e.dataTransfer.getData(MIME_TASK);
    if (!taskId) return;
    if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
      if (
        window.confirm(
          tx(t, "trash.confirmMany", "Permanently delete {n} selected tasks? This cannot be undone.", {
            n: props.selectedIds.size,
          }),
        )
      ) {
        const ids = Array.from(props.selectedIds);
        Promise.all(
          ids.map(function (id: string) {
            return props.onDelete(id);
          }),
        ).catch(function () {});
      }
    } else {
      props.onDelete(taskId);
    }
  };

  return h(
    "div",
    {
      ref: zoneRef,
      "data-kanban-trash": "true",
      className: cn(
        "hermes-kanban-trash",
        dragOver ? "hermes-kanban-trash--drop" : "",
        props.draggingTaskId ? "hermes-kanban-trash--active" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
    h("span", { className: "hermes-kanban-trash-icon" }, "🗑️"),
    h("span", { className: "hermes-kanban-trash-label" }, tx(t, "trash.dropHint", FALLBACK_TRASH.dropHint)),
  );
}