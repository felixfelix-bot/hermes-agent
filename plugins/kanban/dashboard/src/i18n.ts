/**
 * Kanban dashboard plugin — English fallback strings.
 *
 * These mirror the FALLBACK_* dictionaries embedded in the original IIFE.
 * Used when the host i18n catalog is missing a key, and as defaults for
 * helper functions called outside a React component (where there's no `t`).
 */

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

export const FALLBACK_DESTRUCTIVE: Record<string, string> = {
  done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
  archived: "Archive this task? It disappears from the default board view.",
  blocked: "Mark this task as blocked? The worker's claim is released.",
};

export const FALLBACK_DIAGNOSTIC_EVENT_LABELS: Record<string, string> = {
  completion_blocked_hallucination: "⚠ Completion blocked — phantom card ids",
  suspected_hallucinated_references: "⚠ Prose referenced phantom card ids",
};

export const FALLBACK_TRASH = {
  label: "Trash",
  title: "Drag a card here to permanently delete it",
  confirm: "Permanently delete this task? This cannot be undone.",
  dropHint: "Drop to delete",
};

/**
 * Resolve a translation by dotted path under the kanban namespace
 * (e.g. "columnLabels.triage"); fall back to the English string passed in.
 *
 * `t` is the i18n catalog object from useI18n(); `path` is dotted; `fallback`
 * is the English string; `vars` is optional interpolation.
 */
export function tx(
  t: unknown,
  path: string,
  fallback: string,
  vars?: Record<string, string | number>,
): string {
  let node: unknown = (t as Record<string, unknown>)?.kanban;
  if (node) {
    const parts = path.split(".");
    for (let i = 0; i < parts.length; i++) {
      if (node && typeof node === "object" && parts[i] in (node as Record<string, unknown>)) {
        node = (node as Record<string, unknown>)[parts[i]];
      } else {
        node = null;
        break;
      }
    }
  }
  let str = typeof node === "string" ? node : fallback;
  if (vars) {
    for (const k in vars) {
      str = str.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
    }
  }
  return str;
}

/** Pull the human-readable message out of a fetchJSON error. */
export function parseApiErrorMessage(err: unknown): string {
  const raw = err && typeof err === "object" && "message" in err
    ? String((err as Error).message)
    : String(err || "");
  const m = raw.match(/^(\d{3}):\s*(.*)$/s);
  const body = m ? m[2] : raw;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
    if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
      return parsed.detail.message;
    }
  } catch {
    /* not JSON — fall through to raw body */
  }
  return body || raw;
}

export function getColumnLabel(t: unknown, status: string): string {
  return tx(t, "columnLabels." + status, FALLBACK_COLUMN_LABEL[status] || status);
}

export function getColumnHelp(t: unknown, status: string): string {
  return tx(t, "columnHelp." + status, FALLBACK_COLUMN_HELP[status] || "");
}

export function getDestructiveConfirm(t: unknown, status: string): string | null {
  const keys: Record<string, string> = {
    done: "confirmDone",
    archived: "confirmArchive",
    blocked: "confirmBlocked",
  };
  const key = keys[status];
  if (!key) return null;
  return tx(t, key, FALLBACK_DESTRUCTIVE[status]);
}

export function getDiagnosticEventLabel(t: unknown, kind: string): string | null {
  const keys: Record<string, string> = {
    completion_blocked_hallucination: "completionBlockedHallucination",
    suspected_hallucinated_references: "suspectedHallucinatedReferences",
  };
  const key = keys[kind];
  if (!key) return null;
  return tx(t, key, FALLBACK_DIAGNOSTIC_EVENT_LABELS[kind]);
}

export function isDiagnosticEvent(kind: string): boolean {
  return Object.prototype.hasOwnProperty.call(FALLBACK_DIAGNOSTIC_EVENT_LABELS, kind);
}

export function phantomIdsFromEvent(ev: { payload?: Record<string, unknown> | null }): string[] {
  if (!ev || !ev.payload) return [];
  const p = ev.payload;
  return (p.phantom_cards || p.phantom_refs || []) as string[];
}

/** Prompt for completion summary when moving to done. Returns null if user cancels. */
export function withCompletionSummary(
  patch: Record<string, unknown>,
  count: number,
  t?: unknown,
): Record<string, unknown> | null {
  if (!patch || patch.status !== "done") return patch;
  const label = count && count > 1 ? `${count} selected task(s)` : "this task";
  const value = window.prompt(
    tx(t ?? null, "completionSummary",
      "Completion summary for {label}. This is stored as the task result.", { label }),
    "",
  );
  if (value === null) return null;
  const summary = value.trim();
  if (!summary) {
    window.alert(tx(t ?? null, "completionSummaryRequired",
      "Completion summary is required before marking a task done."));
    return null;
  }
  return { ...patch, result: summary, summary };
}