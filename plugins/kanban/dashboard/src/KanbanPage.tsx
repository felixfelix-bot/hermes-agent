/**
 * Kanban dashboard plugin — main page component.
 *
 * Orchestrates board loading, WebSocket live updates, drag-and-drop card
 * movement, bulk selection, task creation, the detail drawer, and the
 * board switcher. All sub-components are imported from the sibling modules.
 *
 * All React + UI components are obtained at runtime from the host SDK via
 * ``window.__HERMES_PLUGIN_SDK__`` (see ``./sdk``). No React is bundled.
 */

import type {
  Board,
  BoardListItem,
  BoardListResponse,
  CreateTaskBody,
  DashboardConfig,
  Task,
} from "./types";
import {
  getReact,
  getHooks,
  getComponents,
  getUseI18n,
  getFetchJSON,
  withBoard,
  readSelectedBoard,
  writeSelectedBoard,
} from "./sdk";
import { API } from "./constants";
import {
  tx,
  parseApiErrorMessage,
  getDestructiveConfirm,
  withCompletionSummary,
} from "./i18n";
import {
  BoardSwitcher,
  BoardToolbar,
  BulkActionBar,
  NewBoardDialog,
  OrchestrationPanel,
  AttentionStrip,
} from "./board-ui";
import { BoardColumns, TrashDropZone } from "./components";
import { TaskDrawer } from "./drawer";
import { ErrorBoundary } from "./ErrorBoundary";
import { useKanbanEvents } from "./useKanbanEvents";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ── Runtime handles (resolved once at module load) ──────────────────────────

const ReactRuntime = getReact();
const { createElement: h } = ReactRuntime;

const hooks = getHooks();
const { useState, useEffect, useCallback, useMemo } = hooks;

const components = getComponents();
const Button = components.Button as any;

const useI18n = getUseI18n() as () => { t: unknown };
const fetchJSON = getFetchJSON();

// ── Helpers ─────────────────────────────────────────────────────────────────

/** Fuzzy-filter tasks by the search box across id, title, and body. */
function matchesSearch(task: Task, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  const hay = [
    task.id || "",
    task.title || "",
    task.body || "",
    task.assignee || "",
    task.tenant || "",
  ]
    .join(" ")
    .toLowerCase();
  return hay.indexOf(q) >= 0;
}

/** Flatten all tasks from all columns into a single array. */
function allTasksFromBoard(board: Board | null): Task[] {
  if (!board || !board.columns) return [];
  const out: Task[] = [];
  for (const col of board.columns) {
    if (col && col.tasks) {
      for (const t of col.tasks) out.push(t);
    }
  }
  return out;
}

/** Apply tenant + assignee + search filters to produce a filtered board. */
function filterBoard(
  board: Board | null,
  search: string,
  tenantFilter: string,
  assigneeFilter: string,
): Board | null {
  if (!board) return null;
  const filtered: Board = {
    ...board,
    columns: board.columns.map(function (col) {
      return {
        name: col.name,
        tasks: (col.tasks || []).filter(function (t) {
          if (tenantFilter && t.tenant !== tenantFilter) return false;
          if (assigneeFilter && t.assignee !== assigneeFilter) return false;
          return matchesSearch(t, search);
        }),
      };
    }),
  };
  return filtered;
}

// ── KanbanPage ──────────────────────────────────────────────────────────────

export function KanbanPage() {
  const { t } = useI18n();

  // ── Board + loading state ───────────────────────────────────────────────
  const [board, setBoard] = useState<Board | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  // ── Board selection ─────────────────────────────────────────────────────
  const [selectedBoard, setSelectedBoard] = useState<string>(
    readSelectedBoard() || "default",
  );
  const [boardList, setBoardList] = useState<BoardListItem[]>([]);
  const [showNewBoard, setShowNewBoard] = useState(false);

  // ── Filters ─────────────────────────────────────────────────────────────
  const [tenantFilter, setTenantFilter] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [laneByProfile, setLaneByProfile] = useState(false);
  const [search, setSearch] = useState("");

  // ── Config ──────────────────────────────────────────────────────────────
  const [config, setConfig] = useState<DashboardConfig | null>(null);

  // ── Task drawer ─────────────────────────────────────────────────────────
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);

  // ── Bulk selection ──────────────────────────────────────────────────────
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [failedIds, setFailedIds] = useState<Set<string> | null>(null);
  const [lastClickedId, setLastClickedId] = useState<string | null>(null);

  // ── Drag state ──────────────────────────────────────────────────────────
  const [draggingTaskId, setDraggingTaskId] = useState<string | null>(null);

  // ── Load board data ─────────────────────────────────────────────────────

  const loadBoard = useCallback(
    function () {
      setLoading(true);
      setError(null);
      const params: Record<string, string> = {};
      if (includeArchived) params.include_archived = "true";
      const qs = new URLSearchParams(params);
      const url = qs.toString()
        ? `${API}/board?${qs}`
        : `${API}/board`;
      return fetchJSON<Board>(withBoard(url, selectedBoard))
        .then(function (data: Board) {
          setBoard(data);
          setError(null);
        })
        .catch(function (e: any) {
          setError(parseApiErrorMessage(e));
        })
        .finally(function () {
          setLoading(false);
        });
    },
    [selectedBoard, includeArchived],
  );

  const loadBoardList = useCallback(function () {
    return fetchJSON<BoardListResponse>(withBoard(`${API}/boards`, selectedBoard))
      .then(function (data: BoardListResponse) {
        setBoardList(data.boards || []);
        // Update selectedBoard if the server reports a different current board.
        if (data.current && data.current !== selectedBoard) {
          setSelectedBoard(data.current);
          writeSelectedBoard(data.current);
        }
      })
      .catch(function () {
        /* silent — board list is non-critical */
      });
  }, [selectedBoard]);

  const loadConfig = useCallback(function () {
    return fetchJSON<DashboardConfig>(withBoard(`${API}/config`, selectedBoard))
      .then(function (cfg: DashboardConfig) {
        setConfig(cfg);
        // Apply config defaults for lane-by-profile.
        if (cfg.lane_by_profile !== undefined) {
          setLaneByProfile(cfg.lane_by_profile);
        }
      })
      .catch(function () {
        /* silent */
      });
  }, [selectedBoard]);

  // ── Initial load ────────────────────────────────────────────────────────

  useEffect(function () {
    loadBoard();
    loadBoardList();
    loadConfig();
  }, [loadBoard, loadBoardList, loadConfig]);

  // ── WebSocket live events ───────────────────────────────────────────────

  const { taskEventTick, scheduleReload } = useKanbanEvents(
    board,
    selectedBoard,
    loadBoard,
  );

  // ── Derived data ────────────────────────────────────────────────────────

  const allTasks = useMemo(
    function () {
      return allTasksFromBoard(board);
    },
    [board],
  );

  const filteredBoard = useMemo(
    function () {
      return filterBoard(board, search, tenantFilter, assigneeFilter);
    },
    [board, search, tenantFilter, assigneeFilter],
  );

  const assignees = (board && board.assignees) || [];

  // ── Task operations ─────────────────────────────────────────────────────

  const handleMove = useCallback(
    function (taskId: string, status: string) {
      const patch: Record<string, unknown> = { status: status as any };

      // Prompt for completion summary when moving to done.
      const enriched = withCompletionSummary(patch, 1, t);
      if (enriched === null) {
        // User cancelled the completion prompt.
        scheduleReload();
        return;
      }

      // Confirm destructive transitions.
      const confirmMsg = getDestructiveConfirm(t, status);
      if (confirmMsg && !window.confirm(confirmMsg)) {
        scheduleReload();
        return;
      }

      const url = withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, selectedBoard);
      fetchJSON(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(enriched),
      })
        .then(function () {
          loadBoard();
        })
        .catch(function (e: any) {
          const msg = parseApiErrorMessage(e);
          window.alert(
            tx(t, "moveFailed", "Failed to move task: {error}", {
              error: msg,
            }),
          );
          loadBoard();
        });
    },
    [selectedBoard, loadBoard, scheduleReload, t],
  );

  const handleCreate = useCallback(
    function (body: CreateTaskBody): Promise<unknown> {
      const url = withBoard(`${API}/tasks`, selectedBoard);
      return fetchJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(function (resp: any) {
          setWarning(resp && resp.warning ? resp.warning : null);
          loadBoard();
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "createFailed", "Failed to create task: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
        });
    },
    [selectedBoard, loadBoard, t],
  );

  const handleDelete = useCallback(
    function (taskId: string): Promise<unknown> {
      const url = withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, selectedBoard);
      return fetchJSON(url, { method: "DELETE" })
        .then(function () {
          loadBoard();
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "deleteFailed", "Failed to delete task: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
        });
    },
    [selectedBoard, loadBoard, t],
  );

  // ── Bulk operations ─────────────────────────────────────────────────────

  const handleBulkApply = useCallback(
    function (patch: Record<string, unknown>, confirmMsg?: string) {
      if (selectedIds.size === 0) return;
      if (confirmMsg && !window.confirm(confirmMsg)) return;

      const ids = Array.from(selectedIds);
      const body: Record<string, unknown> = {
        ...patch,
        ids: ids,
      };

      fetchJSON(withBoard(`${API}/tasks/bulk`, selectedBoard), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then(function (resp: any) {
          // Track per-id failures.
          if (resp && resp.results) {
            const failed = new Set<string>();
            for (const r of resp.results) {
              if (!r.ok) failed.add(r.id);
            }
            setFailedIds(failed.size > 0 ? failed : null);
            if (failed.size > 0) {
              window.alert(
                tx(t, "bulkPartialFail", "{failed} of {total} tasks failed.", {
                  failed: failed.size,
                  total: ids.length,
                }),
              );
            }
          }
          setSelectedIds(new Set());
          loadBoard();
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "bulkFailed", "Bulk operation failed: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
        });
    },
    [selectedIds, selectedBoard, loadBoard, t],
  );

  const handleBulkDelete = useCallback(
    function (count: number) {
      if (selectedIds.size === 0) return;
      const msg = tx(
        t,
        "confirmBulkDelete",
        "Permanently delete {count} selected task(s)? This cannot be undone.",
        { count: count },
      );
      if (!window.confirm(msg)) return;

      const ids = Array.from(selectedIds);
      let chain: Promise<void> = Promise.resolve();
      ids.forEach(function (id) {
        chain = chain.then(function () {
          return fetchJSON(
            withBoard(`${API}/tasks/${encodeURIComponent(id)}`, selectedBoard),
            { method: "DELETE" },
          ).then(function () {
            /* ok */
          });
        });
      });
      chain
        .then(function () {
          setSelectedIds(new Set());
          loadBoard();
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "deleteFailed", "Failed to delete task: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
          loadBoard();
        });
    },
    [selectedIds, selectedBoard, loadBoard, t],
  );

  // ── Selection helpers ───────────────────────────────────────────────────

  const toggleSelected = useCallback(
    function (id: string, toggle: boolean) {
      setSelectedIds(function (prev: Set<string>) {
        const next = new Set(prev);
        if (toggle) next.add(id);
        else next.delete(id);
        return next;
      });
      setLastClickedId(id);
    },
    [],
  );

  const toggleRange = useCallback(
    function (id: string) {
      // Shift-click range selection: select from last clicked to current.
      setSelectedIds(function (prev: Set<string>) {
        if (!lastClickedId) {
          const next = new Set(prev);
          if (next.has(id)) next.delete(id);
          else next.add(id);
          return next;
        }
        const next = new Set(prev);
        // Build a flat ordered list of visible task ids.
        const ordered: string[] = [];
        if (filteredBoard) {
          for (const col of filteredBoard.columns) {
            for (const task of col.tasks || []) {
              ordered.push(task.id);
            }
          }
        }
        const startIdx = ordered.indexOf(lastClickedId);
        const endIdx = ordered.indexOf(id);
        if (startIdx >= 0 && endIdx >= 0) {
          const lo = Math.min(startIdx, endIdx);
          const hi = Math.max(startIdx, endIdx);
          for (let i = lo; i <= hi; i++) {
            next.add(ordered[i]);
          }
        }
        return next;
      });
      setLastClickedId(id);
    },
    [lastClickedId, filteredBoard],
  );

  const selectAllVisible = useCallback(
    function () {
      const ids = new Set<string>();
      if (filteredBoard) {
        for (const col of filteredBoard.columns) {
          for (const task of col.tasks || []) {
            ids.add(task.id);
          }
        }
      }
      setSelectedIds(ids);
    },
    [filteredBoard],
  );

  const selectAllInColumn = useCallback(
    function (colName: string) {
      setSelectedIds(function (prev: Set<string>) {
        const next = new Set(prev);
        if (filteredBoard) {
          const col = filteredBoard.columns.find(function (c) {
            return c.name === colName;
          });
          if (col) {
            for (const task of col.tasks || []) {
              next.add(task.id);
            }
          }
        }
        return next;
      });
    },
    [filteredBoard],
  );

  // ── Board switching ─────────────────────────────────────────────────────

  const handleBoardSwitch = useCallback(
    function (slug: string) {
      setSelectedBoard(slug);
      writeSelectedBoard(slug);
      setOpenTaskId(null);
      setSelectedIds(new Set());
      // Trigger reload — the loadBoard callback depends on selectedBoard.
    },
    [],
  );

  const handleDeleteBoard = useCallback(
    function (slug: string) {
      fetchJSON(`${API}/boards/${encodeURIComponent(slug)}`, {
        method: "DELETE",
      })
        .then(function () {
          if (slug === selectedBoard) {
            setSelectedBoard("default");
            writeSelectedBoard("default");
          }
          loadBoardList();
          loadBoard();
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "deleteBoardFailed", "Failed to delete board: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
        });
    },
    [selectedBoard, loadBoardList, loadBoard, t],
  );

  // ── Dispatch nudge ──────────────────────────────────────────────────────

  const handleNudgeDispatch = useCallback(
    function () {
      fetchJSON(withBoard(`${API}/dispatch?max=8`, selectedBoard), {
        method: "POST",
      })
        .then(function (resp: any) {
          if (resp && resp.dispatched) {
            loadBoard();
          }
        })
        .catch(function (e: any) {
          window.alert(
            tx(t, "dispatchFailed", "Dispatch failed: {error}", {
              error: parseApiErrorMessage(e),
            }),
          );
        });
    },
    [selectedBoard, loadBoard, t],
  );

  // ── Drawer event tick ───────────────────────────────────────────────────

  const drawerEventTick = openTaskId
    ? taskEventTick[openTaskId] || 0
    : 0;

  // ── Render ──────────────────────────────────────────────────────────────

  // Loading state (first load only).
  if (loading && !board) {
    return h(
      "div",
      {
        className: "hermes-kanban hermes-kanban--loading",
        style: { padding: "3rem", textAlign: "center", color: "var(--muted-foreground)" },
      },
      h(
        "p",
        { className: "text-lg" },
        tx(t, "loadingBoard", "Loading kanban board…"),
      ),
    );
  }

  // Error state.
  if (error && !board) {
    return h(
      "div",
      {
        className: "hermes-kanban hermes-kanban--error",
        style: { padding: "3rem", textAlign: "center" },
      },
      h(
        "p",
        { style: { color: "var(--destructive)", marginBottom: "1rem" } },
        tx(t, "loadFailed", "Failed to load board: {error}", { error }),
      ),
      h(
        Button,
        { onClick: loadBoard, size: "sm" },
        tx(t, "retry", "Retry"),
      ),
    );
  }

  return h(
    ErrorBoundary,
    null,
    h(
      "div",
      { className: "hermes-kanban" },

      // ── Board switcher ──
      h(BoardSwitcher, {
        board: selectedBoard,
        boardList: boardList,
        onSwitch: handleBoardSwitch,
        onNewClick: function () {
          setShowNewBoard(true);
        },
        onDeleteBoard: handleDeleteBoard,
      }),

      // ── Toolbar ──
      h(BoardToolbar, {
        board: board,
        tenantFilter: tenantFilter,
        setTenantFilter: setTenantFilter,
        assigneeFilter: assigneeFilter,
        setAssigneeFilter: setAssigneeFilter,
        includeArchived: includeArchived,
        setIncludeArchived: function (v: boolean) {
          setIncludeArchived(v);
        },
        laneByProfile: laneByProfile,
        setLaneByProfile: setLaneByProfile,
        search: search,
        setSearch: setSearch,
        onNudgeDispatch: handleNudgeDispatch,
        onRefresh: loadBoard,
      }),

      // ── Warning banner (e.g. dispatcher not running) ──
      warning
        ? h(
            "div",
            {
              className: "hermes-kanban-warning",
              style: {
                padding: "0.5rem 0.75rem",
                margin: "0.5rem 0",
                borderRadius: "0.375rem",
                background: "var(--warning-bg, #fef3c7)",
                color: "var(--warning-fg, #92400e)",
                fontSize: "0.875rem",
              },
            },
            warning,
            h(
              "button",
              {
                onClick: function () {
                  setWarning(null);
                },
                style: {
                  marginLeft: "0.5rem",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  color: "inherit",
                  textDecoration: "underline",
                  fontSize: "0.75rem",
                },
              },
              tx(t, "dismiss", "Dismiss"),
            ),
          )
        : null,

      // ── Attention strip (diagnostics) ──
      h(AttentionStrip, {
        boardData: board,
        onOpen: setOpenTaskId,
      }),

      // ── Orchestration panel ──
      h(OrchestrationPanel),

      // ── Bulk action bar (when tasks are selected) ──
      selectedIds.size > 0
        ? h(BulkActionBar, {
            count: selectedIds.size,
            assignees: assignees,
            onApply: handleBulkApply,
            onClear: function () {
              setSelectedIds(new Set());
            },
            onSelectAllVisible: selectAllVisible,
            onDelete: handleBulkDelete,
          })
        : null,

      // ── Board columns + trash zone ──
      filteredBoard
        ? h(
            "div",
            { className: "hermes-kanban-main" },
            h(BoardColumns, {
              board: filteredBoard,
              laneByProfile: laneByProfile,
              selectedIds: selectedIds,
              failedIds: failedIds,
              draggingTaskId: draggingTaskId,
              toggleSelected: toggleSelected,
              toggleRange: toggleRange,
              selectAllInColumn: selectAllInColumn,
              onMove: handleMove,
              onMoveSelected: null,
              onOpen: setOpenTaskId,
              onCreate: handleCreate,
              onDelete: handleDelete,
              allTasks: allTasks,
              onDragStart: setDraggingTaskId,
              onDragEnd: function () {
                setDraggingTaskId(null);
              },
            }),
            h(TrashDropZone, {
              draggingTaskId: draggingTaskId,
              selectedIds: selectedIds,
              onDelete: handleDelete,
            }),
          )
        : null,

      // ── Task drawer ──
      openTaskId
        ? h(TaskDrawer, {
            taskId: openTaskId,
            boardSlug: selectedBoard !== "default" ? selectedBoard : null,
            onClose: function () {
              setOpenTaskId(null);
            },
            onRefresh: loadBoard,
            renderMarkdown: config ? config.render_markdown : true,
            allTasks: allTasks,
            assignees: assignees,
            eventTick: drawerEventTick,
          })
        : null,

      // ── New board dialog ──
      showNewBoard
        ? h(NewBoardDialog, {
            onCancel: function () {
              setShowNewBoard(false);
            },
            onCreate: function (body: Record<string, unknown>) {
              return fetchJSON(`${API}/boards`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
              })
                .then(function () {
                  setShowNewBoard(false);
                  if (body.switch) {
                    const slug = body.slug as string;
                    handleBoardSwitch(slug);
                  }
                  loadBoardList();
                })
                .catch(function (e: any) {
                  throw new Error(parseApiErrorMessage(e));
                });
            },
          })
        : null,
    ),
  );
}
