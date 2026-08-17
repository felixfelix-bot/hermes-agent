/**
 * Kanban dashboard plugin — Column component + InlineCreate form.
 *
 * A Column is a drop zone that groups tasks by status. When the
 * ``laneByProfile`` flag is on and the column is ``running``, tasks are
 * sub-grouped by assignee. Includes an inline task-creation form.
 */

import type { Task, Column as ColumnData, CreateTaskBody } from "../types";
import { MIME_TASK, COLUMN_DOT } from "../types";
import {
  tx,
  selectChangeHandler,
  getColumnLabel,
  getColumnHelp,
} from "../api";

import { TaskCard } from "./TaskCard";

// ── SDK singleton ──

interface HermesSDK {
  React: typeof import("react");
  hooks: {
    useState: typeof import("react").useState;
    useEffect: typeof import("react").useEffect;
    useCallback: typeof import("react").useCallback;
    useMemo: typeof import("react").useMemo;
    useRef: typeof import("react").useRef;
  };
  components: {
    Card: React.ComponentType<Record<string, unknown>>;
    CardContent: React.ComponentType<Record<string, unknown>>;
    Badge: React.ComponentType<Record<string, unknown>>;
    Button: React.ComponentType<Record<string, unknown>>;
    Input: React.ComponentType<Record<string, unknown>>;
    Label: React.ComponentType<Record<string, unknown>>;
    Select: React.ComponentType<Record<string, unknown>>;
    SelectOption: React.ComponentType<Record<string, unknown>>;
    Checkbox: React.ComponentType<Record<string, unknown>>;
  };
  utils: {
    cn: (...classes: Array<string | false | null | undefined>) => string;
    timeAgo: (ts: number) => string;
  };
  useI18n: () => { t: Record<string, unknown>; locale: string };
}

const SDK = (function () {
  const s = (window as unknown as { __HERMES_PLUGIN_SDK__?: HermesSDK }).__HERMES_PLUGIN_SDK__;
  if (!s) throw new Error("Plugin SDK not available");
  return s;
})();
const h = SDK.React.createElement;
const { useState, useEffect, useMemo, useRef } = SDK.hooks;
const { Button, Input, Select, SelectOption, Checkbox } = SDK.components;
const { cn } = SDK.utils;
const useI18n = SDK.useI18n;

// ── Column props ──

export interface ColumnProps {
  column: ColumnData;
  laneByProfile: boolean;
  selectedIds: Set<string>;
  failedIds: Set<string> | null;
  draggingTaskId: string | null;
  toggleSelected: (id: string, additive?: boolean) => void;
  toggleRange: (id: string) => void;
  selectAllInColumn: ((column: string) => void) | null;
  onMove: (taskId: string, status: string) => void;
  onMoveSelected: ((status: string) => void) | null;
  onOpen: (id: string) => void;
  onCreate: (body: CreateTaskBody) => Promise<unknown>;
  allTasks: Task[];
}

// ── InlineCreate props ──

export interface InlineCreateProps {
  columnName: string;
  allTasks: Task[];
  onSubmit: (body: CreateTaskBody) => void;
  onCancel: () => void;
}

// ── InlineCreate component ──

export function InlineCreate(props: InlineCreateProps): React.ReactElement {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [assignee, setAssignee] = useState("");
  const [priority, setPriority] = useState<string | number>(0);
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
    const body: CreateTaskBody = {
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
      .map(function (s) {
        return s.trim();
      })
      .filter(function (s) {
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
      onKeyDown: function (e: React.KeyboardEvent<HTMLTextAreaElement>) {
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
      placeholder: tx(
        t,
        "skillsPlaceholder",
        "skills (optional, comma-separated): translation, github-code-review",
      ),
      title:
        "Force-load these skills into the worker (in addition to the built-in kanban-worker).",
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
          title:
            "Optional parent task. A child stays blocked in its current column until the parent is marked done.",
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

// ── Column component ──

export function Column(props: ColumnProps): React.ReactElement {
  const { t } = useI18n();
  const [dragOver, setDragOver] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const colRef = useRef<HTMLDivElement>(null);

  // Listen for our synthetic touch-drop events from attachTouchDrag().
  useEffect(
    function () {
      if (!colRef.current) return undefined;
      const el = colRef.current;
      function onTouchDrop(e: Event) {
        const detail = (e as CustomEvent).detail;
        if (detail && detail.status === props.column.name) {
          const taskId: string = detail.taskId;
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

  const handleDragOver = function (e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!dragOver) setDragOver(true);
  };
  const handleDragLeave = function () {
    setDragOver(false);
  };
  const handleDrop = function (e: React.DragEvent<HTMLDivElement>) {
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
    // Header
    h(
      "div",
      { className: "hermes-kanban-column-header", title: colHelp || "" },
      h(Checkbox, {
        className: "hermes-kanban-col-check",
        title: "Select all tasks in this column",
        "aria-label": `Select all tasks in ${colLabel || props.column.name}`,
        checked:
          props.column.tasks.length > 0 &&
          props.column.tasks.every(function (t: Task) {
            return props.selectedIds.has(t.id);
          }),
        onCheckedChange: function () {
          if (props.selectAllInColumn) props.selectAllInColumn(props.column.name);
        },
        onClick: function (e: React.MouseEvent) {
          e.stopPropagation();
        },
      }),
      h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[props.column.name]) }),
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
          setShowCreate(function (v) {
            return !v;
          });
        },
      }, showCreate ? "×" : "+"),
    ),
    // Sub-header help text
    h("div", { className: "hermes-kanban-column-sub" }, colHelp || ""),
    // Inline create form
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
    // Body
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
                    failed: !!(props.failedIds && props.failedIds.has(tk.id)),
                    draggingTaskId: props.draggingTaskId,
                    draggingSource:
                      !!props.draggingTaskId &&
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
                failed: !!(props.failedIds && props.failedIds.has(tk.id)),
                draggingTaskId: props.draggingTaskId,
                draggingSource:
                  !!props.draggingTaskId &&
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