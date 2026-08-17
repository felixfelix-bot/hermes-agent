/**
 * Kanban dashboard plugin — TaskCard component.
 *
 * Renders a single task as a draggable card with checkbox selection,
 * staleness coloring, warning badges, and keyboard accessibility.
 */

import type { Task } from "../types";
import { MIME_TASK } from "../types";
import { tx, stalenessClass, attachTouchDrag } from "../api";

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
const { useEffect, useRef } = SDK.hooks;
const { Card, CardContent, Badge, Checkbox } = SDK.components;
const { cn, timeAgo } = SDK.utils;
const useI18n = SDK.useI18n;

// ── TaskCard props ──

export interface TaskCardProps {
  task: Task;
  selected: boolean;
  failed: boolean;
  draggingTaskId: string | null;
  draggingSource: boolean;
  toggleSelected: (id: string, additive?: boolean) => void;
  toggleRange: (id: string) => void;
  onOpen: (id: string) => void;
}

// ── Component ──

export function TaskCard(props: TaskCardProps): React.ReactElement {
  const { t: i18n } = useI18n();
  const t = props.task;
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(
    function () {
      return attachTouchDrag(cardRef.current, t.id);
    },
    [t.id],
  );

  const handleDragStart = function (e: React.DragEvent<HTMLDivElement>) {
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

  const handleClick = function (e: React.MouseEvent<HTMLDivElement>) {
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

  const handleKeyDown = function (e: React.KeyboardEvent<HTMLDivElement>) {
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
        // Row: checkbox, id, badges, progress, needs-assignee
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
            ? h(
                "span",
                {
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
                    : "⚠",
              )
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
        // Title
        h(
          "div",
          { className: "hermes-kanban-card-title" },
          t.title || tx(i18n, "untitled", "(untitled)"),
        ),
        // Meta row: assignee, comment count, link count, time ago
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
          t.comment_count && t.comment_count > 0
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