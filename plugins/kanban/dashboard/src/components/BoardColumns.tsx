/**
 * Kanban dashboard plugin — BoardColumns component + TrashDropZone.
 *
 * Renders the column layout (ordered by COLUMN_ORDER), delegates drag
 * start/end at the container level, and includes a trash drop zone for
 * deleting tasks by dropping them outside any column.
 */

import type { Task, BoardData, Column as ColumnData, CreateTaskBody } from "../types";
import { MIME_TASK, COLUMN_ORDER } from "../types";
import { tx } from "../api";

import { Column } from "./Column";

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
  components: Record<string, React.ComponentType<never>>;
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
const { useState, useEffect, useRef, useCallback } = SDK.hooks;
const { cn } = SDK.utils;
const useI18n = SDK.useI18n;

// ── Fallback strings for the trash zone ──

const FALLBACK_TRASH = {
  label: "Trash",
  title: "Drag a card here to permanently delete it",
  confirm: "Permanently delete this task? This cannot be undone.",
  confirmMany: "Permanently delete {n} selected tasks? This cannot be undone.",
  dropHint: "Drop to delete",
};

// ── BoardColumns props ──

export interface BoardColumnsProps {
  board: BoardData;
  laneByProfile: boolean;
  selectedIds: Set<string>;
  failedIds: Set<string> | null;
  draggingTaskId: string | null;
  onDragStart: ((taskId: string) => void) | null;
  onDragEnd: (() => void) | null;
  toggleSelected: (id: string, additive?: boolean) => void;
  toggleRange: (id: string) => void;
  selectAllInColumn: ((column: string) => void) | null;
  onMove: (taskId: string, status: string) => void;
  onMoveSelected: ((status: string) => void) | null;
  onOpen: (id: string) => void;
  onCreate: (body: CreateTaskBody) => Promise<unknown>;
  onDelete: (taskId: string) => Promise<unknown>;
  allTasks: Task[];
}

// ── TrashDropZone ──

interface TrashDropZoneProps {
  draggingTaskId: string | null;
  selectedIds: Set<string>;
  onDelete: (taskId: string) => Promise<unknown>;
}

function TrashDropZone(props: TrashDropZoneProps): React.ReactElement {
  const { t } = useI18n();
  const [dragOver, setDragOver] = useState(false);
  const zoneRef = useRef<HTMLDivElement>(null);

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
      if (
        window.confirm(
          tx(t, "trash.confirmMany", FALLBACK_TRASH.confirmMany, { n: props.selectedIds.size }),
        )
      ) {
        const ids = Array.from(props.selectedIds);
        Promise.all(
          ids.map(function (id) {
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

// ── BoardColumns ──

export function BoardColumns(props: BoardColumnsProps): React.ReactElement {
  const handleDragStart = useCallback(
    function (e: React.DragEvent<HTMLDivElement>) {
      const target = e.target as HTMLElement;
      const card = target.closest && target.closest(".hermes-kanban-card");
      if (!card) return;
      const taskId = card.getAttribute("data-task-id");
      if (taskId && props.onDragStart) props.onDragStart(taskId);
    },
    [props.onDragStart],
  );
  const handleDragEnd = useCallback(
    function () {
      if (props.onDragEnd) props.onDragEnd();
    },
    [props.onDragEnd],
  );

  // Order columns by COLUMN_ORDER; include any columns not in the order at the end.
  const order: string[] = [...COLUMN_ORDER];
  const cols: ColumnData[] = [];
  const seen = new Set<string>();
  for (const name of order) {
    const col = props.board.columns.find(function (c) {
      return c.name === name;
    });
    if (col) {
      cols.push(col);
      seen.add(name);
    }
  }
  for (const col of props.board.columns) {
    if (!seen.has(col.name)) cols.push(col);
  }

  return h(
    "div",
    { className: "hermes-kanban-columns", onDragStart: handleDragStart, onDragEnd: handleDragEnd },
    cols.map(function (col: ColumnData) {
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