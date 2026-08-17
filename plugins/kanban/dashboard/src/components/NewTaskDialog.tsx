/**
 * Kanban dashboard plugin — New Task dialog.
 *
 * Full-screen modal for creating a new task with all optional fields:
 * title, assignee, priority, description, skills, parent task,
 * workspace kind/path, goal mode + max turns.
 *
 * Follows the NewBoardDialog pattern (backdrop + form + useState fields).
 */

import * as React from "react";
import { tx, selectChangeHandler } from "../api";
import { type CreateTaskBody } from "../types";

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
  components: Record<string, React.ComponentType<any>>;
  utils: { cn: (...c: Array<string | false | null | undefined>) => string };
  useI18n: () => { t: Record<string, unknown>; locale: string };
}

function getSDK(): HermesSDK {
  const s = (window as unknown as { __HERMES_PLUGIN_SDK__?: HermesSDK }).__HERMES_PLUGIN_SDK__;
  if (!s) throw new Error("Plugin SDK not available");
  return s;
}

const SDK = getSDK();
const h = SDK.React.createElement;
const { useState } = SDK.hooks;
const { Button, Input, Label, Select, SelectOption } = SDK.components;
const useI18n = SDK.useI18n;

// Checkbox fallback shim (same pattern as KanbanPage)
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

// ── Props ──

export interface NewTaskDialogProps {
  open: boolean;
  onClose: () => void;
  onCreate: (body: CreateTaskBody) => Promise<void>;
}

// ── Component ──

export function NewTaskDialog(props: NewTaskDialogProps): any {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [assignee, setAssignee] = useState("");
  const [priority, setPriority] = useState<string>("");
  const [body, setBody] = useState("");
  const [skills, setSkills] = useState("");
  const [parent, setParent] = useState("");
  const [workspaceKind, setWorkspaceKind] = useState("scratch");
  const [workspacePath, setWorkspacePath] = useState("");
  const [goalMode, setGoalMode] = useState(false);
  const [goalMaxTurns, setGoalMaxTurns] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reset form when dialog opens
  React.useEffect(() => {
    if (props.open) {
      setTitle("");
      setAssignee("");
      setPriority("");
      setBody("");
      setSkills("");
      setParent("");
      setWorkspaceKind("scratch");
      setWorkspacePath("");
      setGoalMode(false);
      setGoalMaxTurns("");
      setSubmitting(false);
      setErr(null);
    }
  }, [props.open]);

  if (!props.open) return null;

  function onSubmit(ev?: React.FormEvent) {
    ev?.preventDefault();
    const trimmed = title.trim();
    if (!trimmed) {
      setErr("Title is required");
      return;
    }
    setSubmitting(true);
    setErr(null);

    const taskBody: CreateTaskBody = { title: trimmed };

    // Assignee
    const assigneeTrim = assignee.trim();
    if (assigneeTrim) taskBody.assignee = assigneeTrim;

    // Priority
    const priNum = Number(priority);
    if (priority && Number.isFinite(priNum)) taskBody.priority = priNum;

    // Description / body
    const bodyTrim = body.trim();
    if (bodyTrim) taskBody.body = bodyTrim;

    // Skills (comma-separated)
    const skillList = skills
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (skillList.length > 0) taskBody.skills = skillList;

    // Parent task
    const parentTrim = parent.trim();
    if (parentTrim) taskBody.parents = [parentTrim];

    // Workspace kind
    if (workspaceKind && workspaceKind !== "scratch") {
      taskBody.workspace_kind = workspaceKind;
    }
    const wpTrim = workspacePath.trim();
    if (wpTrim) taskBody.workspace_path = wpTrim;

    // Goal mode
    if (goalMode) {
      taskBody.goal_mode = true;
      const gmt = parseInt(goalMaxTurns, 10);
      if (Number.isFinite(gmt) && gmt > 0) taskBody.goal_max_turns = gmt;
    }

    props
      .onCreate(taskBody)
      .then(() => {
        // Dialog will be closed by parent via onClose
        // But also reset state
        setSubmitting(false);
      })
      .catch((e: unknown) => {
        setErr(
          tx(t, "newTaskDialog_error", "Failed to create task") +
            (e instanceof Error ? ": " + e.message : ": " + String(e)),
        );
        setSubmitting(false);
      });
  }

  function onCancel() {
    props.onClose();
  }

  const showPathInput = workspaceKind !== "scratch";

  return h(
    "div",
    {
      className: "hermes-kanban-dialog-backdrop",
      onClick: (e: React.MouseEvent) => {
        if (e.target === e.currentTarget) onCancel();
      },
    },
    h(
      "form",
      { className: "hermes-kanban-dialog", onSubmit },
      // Title
      h("div", { className: "hermes-kanban-dialog-title" },
        tx(t, "newTaskDialog_title", "New Task"),
      ),
      h("div", { className: "flex flex-col gap-3" },
        // Title field (required)
        h("div", { className: "hermes-kanban-field flex flex-col gap-1" },
          h(Label, { className: "text-xs" },
            tx(t, "newTaskDialog_title", "New Task"),
            " ",
            h("span", { className: "text-destructive" }, "*"),
          ),
          h("textarea", {
            value: title,
            onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) =>
              setTitle(e.target.value),
            placeholder: tx(t, "newTaskDialog_titlePlaceholder", "Enter task title..."),
            autoFocus: true,
            className:
              "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
            rows: 2,
          }),
        ),
        // Assignee + Priority row
        h("div", { className: "flex gap-2" },
          h("div", { className: "hermes-kanban-field flex flex-col gap-1 flex-1" },
            h(Label, { className: "text-xs" },
              tx(t, "newTaskDialog_assignee", "Assignee"),
            ),
            h(Input, {
              value: assignee,
              onChange: (e: { target: { value: string } }) =>
                setAssignee(e.target.value),
              placeholder: tx(t, "assigneePlaceholder", "assignee"),
              className: "h-8",
            }),
          ),
          h("div", { className: "hermes-kanban-field flex flex-col gap-1 w-32" },
            h(Label, { className: "text-xs" },
              tx(t, "newTaskDialog_priority", "Priority"),
            ),
            h(Select, {
              value: priority,
              className: "h-8",
              ...selectChangeHandler(setPriority),
            },
              h(SelectOption, { value: "" }, "\u2014"),
              h(SelectOption, { value: "1" }, "1"),
              h(SelectOption, { value: "2" }, "2"),
              h(SelectOption, { value: "3" }, "3"),
            ),
          ),
        ),
        // Description
        h("div", { className: "hermes-kanban-field flex flex-col gap-1" },
          h(Label, { className: "text-xs" },
            tx(t, "newTaskDialog_description", "Description"),
          ),
          h("textarea", {
            value: body,
            onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) =>
              setBody(e.target.value),
            placeholder: "Optional description...",
            className:
              "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
            rows: 3,
          }),
        ),
        // Skills
        h("div", { className: "hermes-kanban-field flex flex-col gap-1" },
          h(Label, { className: "text-xs" },
            tx(t, "newTaskDialog_skills", "Skills"),
          ),
          h(Input, {
            value: skills,
            onChange: (e: { target: { value: string } }) =>
              setSkills(e.target.value),
            placeholder: tx(
              t,
              "skillsPlaceholder",
              "skills (optional, comma-separated): translation, github-code-review",
            ),
            className: "h-8",
          }),
        ),
        // Parent task
        h("div", { className: "hermes-kanban-field flex flex-col gap-1" },
          h(Label, { className: "text-xs" },
            tx(t, "newTaskDialog_parentTask", "Parent Task"),
          ),
          h(Input, {
            value: parent,
            onChange: (e: { target: { value: string } }) =>
              setParent(e.target.value),
            placeholder: "Task ID (optional)",
            className: "h-8",
          }),
        ),
        // Workspace kind + path
        h("div", { className: "flex gap-2" },
          h("div", { className: "hermes-kanban-field flex flex-col gap-1 w-32" },
            h(Label, { className: "text-xs" },
              tx(t, "newTaskDialog_workspaceKind", "Workspace Kind"),
            ),
            h(Select, {
              value: workspaceKind,
              className: "h-8",
              ...selectChangeHandler(setWorkspaceKind),
            },
              h(SelectOption, { value: "scratch" }, "scratch"),
              h(SelectOption, { value: "worktree" }, "worktree"),
              h(SelectOption, { value: "dir" }, "dir"),
            ),
          ),
          showPathInput
            ? h("div", { className: "hermes-kanban-field flex flex-col gap-1 flex-1" },
                h(Label, { className: "text-xs" },
                  tx(t, "newTaskDialog_workspacePath", "Workspace Path"),
                ),
                h(Input, {
                  value: workspacePath,
                  onChange: (e: { target: { value: string } }) =>
                    setWorkspacePath(e.target.value),
                  placeholder:
                    workspaceKind === "dir"
                      ? tx(t, "workspacePathDir", "workspace path (required, e.g. ~/projects/my-app)")
                      : tx(t, "workspacePathOptional", "workspace path (optional, derived from assignee if blank)"),
                  className: "h-8",
                }),
              )
            : null,
        ),
        // Goal mode + max turns
        h("label", { className: "flex items-center gap-2 text-xs" },
          h(Checkbox, {
            checked: goalMode,
            onCheckedChange: (checked: boolean) => setGoalMode(checked === true),
          }),
          tx(t, "newTaskDialog_goalMode", "Goal Mode"),
        ),
        goalMode
          ? h("div", { className: "hermes-kanban-field flex flex-col gap-1 ml-6" },
              h(Label, { className: "text-xs" },
                tx(t, "newTaskDialog_goalMaxTurns", "Max Turns"),
              ),
              h(Input, {
                type: "number",
                value: goalMaxTurns,
                onChange: (e: { target: { value: string } }) =>
                  setGoalMaxTurns(e.target.value),
                placeholder: "default 20",
                className: "h-8 w-32",
                min: 1,
              }),
            )
          : null,
      ),
      // Error
      err
        ? h("div", { className: "text-xs text-destructive mt-2" }, err)
        : null,
      // Actions
      h("div", { className: "hermes-kanban-dialog-actions" },
        h(Button, {
          type: "button",
          onClick: onCancel,
          size: "sm",
          disabled: submitting,
        },
          tx(t, "cancel", "Cancel"),
        ),
        h(Button, {
          type: "submit",
          size: "sm",
          disabled: submitting || !title.trim(),
        },
          submitting
            ? tx(t, "newTaskDialog_creating", "Creating...")
            : tx(t, "newTaskDialog_create", "Create Task"),
        ),
      ),
    ),
  );
}