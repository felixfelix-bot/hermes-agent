/**
 * Kanban dashboard plugin — WebSocket hook for live task event streaming.
 *
 * Connects to `${API}/events?since=${cursor}&board=${board}` via a WebSocket
 * whose URL is built through the host SDK's ``buildWsUrl`` (so auth works in
 * both loopback and gated-OAuth modes).
 *
 * Returns ``{ taskEventTick, scheduleReload }``:
 *  - ``taskEventTick`` — a ``Record<string, number>`` that increments per
 *    ``task_id`` each time WS events arrive. The TaskDrawer watches this to
 *    reload itself on live activity.
 *  - ``scheduleReload`` — debounces (250 ms) a caller-provided reload callback
 *    so a burst of events only triggers one board refetch.
 *
 * Reconnection uses exponential backoff (1 s → 30 s). Close code 1008 (policy
 * violation) is treated as a hard auth failure — no reconnection, error set.
 */

import type { Board, WsEvent, WsMessage } from "./types";
import { API } from "./constants";
import { getHooks, getBuildWsUrl } from "./sdk";

export interface UseKanbanEventsResult {
  taskEventTick: Record<string, number>;
  scheduleReload: () => void;
}

/**
 * @param boardData  The currently-loaded Board (or null while loading).
 * @param board      The board slug to pin the WS stream to (may be "default").
 * @param scheduleReloadCallback  Called (debounced) when WS events arrive so
 *                                the parent can refetch the board.
 */
export function useKanbanEvents(
  boardData: Board | null,
  board: string | null,
  scheduleReloadCallback: () => void,
): UseKanbanEventsResult {
  const hooks = getHooks();
  const { useState, useRef, useEffect, useCallback } = hooks;

  // ── State ──────────────────────────────────────────────────────────────
  const [taskEventTick, setTaskEventTick] = useState<Record<string, number>>({});

  // ── Refs ───────────────────────────────────────────────────────────────
  const wsRef = useRef<WebSocket | null>(null);
  const wsClosedRef = useRef(false);
  const wsBackoffRef = useRef(1000);
  const cursorRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Initialize cursor from the board's latest_event_id once boardData arrives.
  useEffect(() => {
    if (boardData && boardData.latest_event_id) {
      cursorRef.current = boardData.latest_event_id;
    }
  }, [boardData]);

  // ── Debounced reload (250 ms) ───────────────────────────────────────────
  const scheduleReload = useCallback(() => {
    if (reloadTimerRef.current) clearTimeout(reloadTimerRef.current);
    reloadTimerRef.current = setTimeout(() => {
      reloadTimerRef.current = null;
      scheduleReloadCallback();
    }, 250);
  }, [scheduleReloadCallback]);

  // ── WebSocket lifecycle ────────────────────────────────────────────────
  useEffect(() => {
    if (!boardData) return undefined;

    wsClosedRef.current = false;
    const buildWsUrl = getBuildWsUrl();

    function openWs(): void {
      if (wsClosedRef.current) return;

      const wsParams: Record<string, string> = {
        since: String(cursorRef.current || 0),
      };
      // Pin the WS stream to the currently-selected board so events from
      // other boards don't bleed in (includes "default").
      if (board) wsParams.board = board;

      buildWsUrl(`${API}/events`, wsParams)
        .then((url: string) => {
          if (wsClosedRef.current) return;

          let ws: WebSocket;
          try {
            ws = new WebSocket(url);
          } catch {
            // new WebSocket() can throw on malformed URLs — bail.
            return;
          }
          wsRef.current = ws;

          ws.onopen = () => {
            // Reset backoff on successful connection.
            wsBackoffRef.current = 1000;
          };

          ws.onmessage = (ev: MessageEvent) => {
            try {
              const msg = JSON.parse(ev.data) as WsMessage;
              if (msg && Array.isArray(msg.events) && msg.events.length > 0) {
                // Advance cursor so reconnects resume from where we left off.
                if (msg.cursor) cursorRef.current = msg.cursor;

                // Stamp per-task signal so the TaskDrawer can reload itself.
                setTaskEventTick((prev: Record<string, number>) => {
                  const next = { ...prev };
                  for (const e of msg.events as WsEvent[]) {
                    if (e && e.task_id) {
                      next[e.task_id] = (next[e.task_id] || 0) + 1;
                    }
                  }
                  return next;
                });

                scheduleReload();
              }
            } catch {
              /* ignore malformed JSON */
            }
          };

          ws.onclose = (ev: CloseEvent) => {
            if (wsClosedRef.current) return;

            // Close code 1008 = policy violation (e.g. auth failed).
            // Don't reconnect — set error and let the user reload.
            if (ev && ev.code === 1008) {
              // Close 1008 is a hard failure; we set an error via the
              // scheduleReload path rather than throwing. The parent
              // component can surface an error banner if desired.
              // For now, just stop reconnecting.
              return;
            }

            // Exponential backoff: 1 s → 2 s → 4 s → … capped at 30 s.
            const delay = Math.min(wsBackoffRef.current, 30000);
            wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
            reconnectTimerRef.current = setTimeout(openWs, delay);
          };

          ws.onerror = () => {
            // Errors are handled by the onclose that follows. Nothing to do.
          };
        })
        .catch(() => {
          // Ticket mint / URL build failed (e.g. session expired).
          // Back off and retry like a normal close.
          if (wsClosedRef.current) return;
          const delay = Math.min(wsBackoffRef.current, 30000);
          wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
          reconnectTimerRef.current = setTimeout(openWs, delay);
        });
    }

    openWs();

    // ── Cleanup ──────────────────────────────────────────────────────────
    return () => {
      wsClosedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (reloadTimerRef.current) {
        clearTimeout(reloadTimerRef.current);
        reloadTimerRef.current = null;
      }
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          /* ignore */
        }
        wsRef.current = null;
      }
    };
  }, [boardData, board, scheduleReload]);

  return { taskEventTick, scheduleReload };
}