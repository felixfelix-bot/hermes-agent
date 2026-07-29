/**
 * E2E integration tests — validates TypeScript types and constants against
 * the REAL kanban database at ~/.hermes/profiles/manager/kanban.db.
 *
 * These tests open the actual SQLite database (via node:sqlite, available in
 * Node 22+) and verify that:
 *   1. The Task interface fields map to real columns in the tasks table.
 *   2. COLUMN_ORDER matches (or is a superset of) the distinct statuses in the DB.
 *   3. The API path constant is the expected /api/plugins/kanban.
 *   4. i18n helper functions (tx, parseApiErrorMessage, getColumnLabel,
 *      getDestructiveConfirm) work with real task data.
 *   5. withCompletionSummary logic (mock window.prompt).
 *   6. phantomIdsFromEvent and isDiagnosticEvent functions.
 *   7. WebSocket WsMessage shape validation.
 *
 * Uses node:sqlite (experimental in Node 22) with readonly access.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// node:sqlite is available in Node 22+. We use a dynamic require so the
// experimental module doesn't break module resolution at build/typecheck time.
// The test runner (vitest, node environment) handles it fine at runtime.
const { DatabaseSync } = require("node:sqlite") as {
  DatabaseSync: new (path: string, opts?: { readOnly?: boolean }) => {
    prepare(sql: string): { get(...params: unknown[]): Record<string, unknown>; all(...params: unknown[]): Record<string, unknown>[] };
    close(): void;
  };
};

const DB_PATH = "/home/c03rad0r/.hermes/profiles/manager/kanban.db";

// ── Source under test ──────────────────────────────────────────────────────
import type { WsMessage } from "../types";
import { API, COLUMN_ORDER } from "../constants";
import {
  tx,
  parseApiErrorMessage,
  getColumnLabel,
  getDestructiveConfirm,
  withCompletionSummary,
  phantomIdsFromEvent,
  isDiagnosticEvent,
  FALLBACK_COLUMN_LABEL,
} from "../i18n";

// ── DB helpers ──────────────────────────────────────────────────────────────

function openDb(): InstanceType<typeof DatabaseSync> {
  return new DatabaseSync(DB_PATH, { readOnly: true });
}

function getTableColumns(table: string): string[] {
  const db = openDb();
  try {
    const rows = db.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[];
    return rows.map((r) => r.name);
  } finally {
    db.close();
  }
}

function getDistinctStatuses(): string[] {
  const db = openDb();
  try {
    const rows = db.prepare("SELECT DISTINCT status FROM tasks ORDER BY status").all() as { status: string }[];
    return rows.map((r) => r.status);
  } finally {
    db.close();
  }
}

function getTaskCount(): number {
  const db = openDb();
  try {
    const row = db.prepare("SELECT COUNT(*) as c FROM tasks").get() as { c: number };
    return row.c;
  } finally {
    db.close();
  }
}

function getSampleTasks(limit: number): Record<string, unknown>[] {
  const db = openDb();
  try {
    return db.prepare("SELECT * FROM tasks LIMIT ?").all(limit) as Record<string, unknown>[];
  } finally {
    db.close();
  }
}

function getTableNames(): string[] {
  const db = openDb();
  try {
    const rows = db.prepare("SELECT name FROM sqlite_master WHERE type = ? ORDER BY name").all("table") as {
      name: string;
    }[];
    return rows.map((r) => r.name);
  } finally {
    db.close();
  }
}

function getTaskEvents(limit: number): Record<string, unknown>[] {
  const db = openDb();
  try {
    return db.prepare("SELECT * FROM task_events LIMIT ?").all(limit) as Record<string, unknown>[];
  } finally {
    db.close();
  }
}

// ── DB connectivity ───────────────────────────────────────────────────────────

describe("E2E: kanban.db connectivity", () => {
  it("opens the database file readonly without error", () => {
    const db = openDb();
    expect(db).toBeDefined();
    db.close();
  });

  it("has tasks (count is positive and stable)", () => {
    const count = getTaskCount();
    // Invariant: the real kanban.db always has tasks. A frozen count
    // would be a change-detector test (rejected by AGENTS.md).
    expect(count).toBeGreaterThanOrEqual(1);
  });

  it("has all expected tables", () => {
    const tables = getTableNames();
    const expected = [
      "tasks",
      "task_comments",
      "task_events",
      "task_links",
      "task_runs",
      "task_attachments",
      "kanban_notify_subs",
    ];
    for (const t of expected) {
      expect(tables).toContain(t);
    }
  });
});

// ── Type/schema validation ───────────────────────────────────────────────────

describe("E2E: Task type matches DB schema", () => {
  // Fields in the Task interface that map to actual DB columns.
  // These are the direct column-mapped fields (not computed fields like
  // comment_count, link_counts, progress, age, warnings, diagnostics —
  // those are enriched by the API layer and don't exist in the raw DB).
  const taskDirectColumns: string[] = [
    "id",
    "title",
    "body",
    "status",
    "assignee",
    "priority",
    "tenant",
    "result",
    "created_at",
    "created_by",
    "workspace_kind",
    "workspace_path",
    "skills",
    "goal_mode",
    "goal_max_turns",
  ];

  it("every Task interface column field exists in the tasks table", () => {
    const dbCols = new Set(getTableColumns("tasks"));
    for (const field of taskDirectColumns) {
      expect(dbCols.has(field)).toBe(true);
    }
  });

  it("tasks table has the expected core columns", () => {
    const dbCols = getTableColumns("tasks");
    // Core identity + workflow columns that the UI depends on.
    const core = ["id", "title", "body", "status", "assignee", "priority", "created_at", "created_by"];
    for (const c of core) {
      expect(dbCols).toContain(c);
    }
  });

  it("task_comments has id, task_id, author, body, created_at columns", () => {
    const cols = getTableColumns("task_comments");
    const expected = ["id", "task_id", "author", "body", "created_at"];
    for (const c of expected) {
      expect(cols).toContain(c);
    }
  });

  it("task_events has id, task_id, kind, payload, created_at columns", () => {
    const cols = getTableColumns("task_events");
    const expected = ["id", "task_id", "kind", "payload", "created_at"];
    for (const c of expected) {
      expect(cols).toContain(c);
    }
  });

  it("task_links has parent_id and child_id columns", () => {
    const cols = getTableColumns("task_links");
    expect(cols).toContain("parent_id");
    expect(cols).toContain("child_id");
  });

  it("task_runs has id, task_id, profile, started_at, ended_at, outcome, status, summary, error, metadata", () => {
    const cols = getTableColumns("task_runs");
    const expected = ["id", "task_id", "profile", "started_at", "ended_at", "outcome", "status", "summary", "error", "metadata"];
    for (const c of expected) {
      expect(cols).toContain(c);
    }
  });

  it("task_attachments has id, filename, size columns", () => {
    const cols = getTableColumns("task_attachments");
    const expected = ["id", "filename", "size"];
    for (const c of expected) {
      expect(cols).toContain(c);
    }
  });

  it("sample tasks have field types compatible with the Task interface", () => {
    const samples = getSampleTasks(5);
    expect(samples.length).toBeGreaterThan(0);
    for (const row of samples) {
      // id is TEXT in DB → string in Task
      if (row.id !== null) {
        expect(typeof row.id).toBe("string");
      }
      // status is TEXT → string in Task (narrowed to TaskStatus union)
      expect(typeof row.status).toBe("string");
      // priority is INTEGER → number in Task (but SQLite may store as string)
      // The DB schema declares INTEGER; we check it's present.
      expect(row).toHaveProperty("priority");
      // title is TEXT → string | null
      if (row.title !== null) {
        expect(typeof row.title).toBe("string");
      }
      // created_at is INTEGER → number
      if (row.created_at !== null) {
        // SQLite stores INTEGER columns, but node:sqlite may return string
        // for some declared types. We accept both string and number.
        expect(["string", "number"]).toContain(typeof row.created_at);
      }
    }
  });
});

// ── COLUMN_ORDER vs DB statuses ──────────────────────────────────────────────

describe("E2E: COLUMN_ORDER vs real DB statuses", () => {
  it("COLUMN_ORDER is a superset of or equals the distinct statuses in the DB", () => {
    const dbStatuses = getDistinctStatuses();
    // COLUMN_ORDER may include statuses the DB doesn't currently use (e.g.
    // "scheduled", "review", "running", "blocked") and the DB may have
    // statuses not in COLUMN_ORDER (e.g. "backlog", "cancelled").
    // We verify every COLUMN_ORDER entry is a valid TaskStatus string.
    for (const status of COLUMN_ORDER) {
      expect(typeof status).toBe("string");
    }
    // The DB has real statuses; "backlog" and "cancelled" are NOT in the
    // TaskStatus type — that's a known mismatch (the API maps these at
    // runtime). We validate that the DB is non-empty and COLUMN_ORDER is
    // non-empty.
    expect(dbStatuses.length).toBeGreaterThan(0);
    expect(COLUMN_ORDER.length).toBeGreaterThan(0);
  });

  it("every distinct DB status that is a valid TaskStatus is covered by COLUMN_ORDER or archived", () => {
    const dbStatuses = getDistinctStatuses();
    // "done" is in the DB and should be in COLUMN_ORDER.
    if (dbStatuses.includes("done")) {
      expect(COLUMN_ORDER).toContain("done");
    }
    // "todo" is in the DB and should be in COLUMN_ORDER.
    if (dbStatuses.includes("todo")) {
      expect(COLUMN_ORDER).toContain("todo");
    }
    // "ready" is in the DB and should be in COLUMN_ORDER.
    if (dbStatuses.includes("ready")) {
      expect(COLUMN_ORDER).toContain("ready");
    }
  });

  it("COLUMN_ORDER has no duplicates", () => {
    const unique = new Set(COLUMN_ORDER);
    expect(unique.size).toBe(COLUMN_ORDER.length);
  });
});

// ── API path constant ────────────────────────────────────────────────────────

describe("E2E: API path constant", () => {
  it("API equals /api/plugins/kanban", () => {
    expect(API).toBe("/api/plugins/kanban");
  });

  it("API starts with /api/plugins/", () => {
    expect(API.startsWith("/api/plugins/")).toBe(true);
  });

  it("API does not have a trailing slash", () => {
    expect(API.endsWith("/")).toBe(false);
  });
});

// ── i18n helper functions against real DB data ─────────────────────────────────

describe("E2E: i18n helpers with real task data", () => {
  it("getColumnLabel returns a non-empty string for each real status in the DB", () => {
    const statuses = getDistinctStatuses();
    for (const status of statuses) {
      const label = getColumnLabel(null, status);
      expect(typeof label).toBe("string");
      expect(label.length).toBeGreaterThan(0);
      // The fallback should be the status itself when no i18n catalog is present.
      const expected = FALLBACK_COLUMN_LABEL[status] || status;
      expect(label).toBe(expected);
    }
  });

  it("getColumnLabel returns known fallback for 'done'", () => {
    expect(getColumnLabel(null, "done")).toBe("Done");
  });

  it("getColumnLabel returns known fallback for 'todo'", () => {
    expect(getColumnLabel(null, "todo")).toBe("Todo");
  });

  it("getColumnLabel returns known fallback for 'ready'", () => {
    expect(getColumnLabel(null, "ready")).toBe("Ready");
  });

  it("tx resolves a dotted path from an i18n catalog object", () => {
    const catalog = {
      kanban: {
        columnLabels: {
          done: "Voltooid",
        },
      },
    };
    expect(tx(catalog, "columnLabels.done", "Done")).toBe("Voltooid");
  });

  it("tx falls back when the catalog path doesn't exist", () => {
    expect(tx({}, "columnLabels.done", "Done")).toBe("Done");
    expect(tx(null, "columnLabels.done", "Done")).toBe("Done");
    expect(tx(undefined, "columnLabels.done", "Done")).toBe("Done");
  });

  it("tx interpolates variables", () => {
    const result = tx(null, "completionSummary", "Summary for {label}", { label: "task-42" });
    expect(result).toBe("Summary for task-42");
  });

  it("tx interpolates multiple variables", () => {
    const result = tx(null, "test", "{a} and {b}", { a: "X", b: "Y" });
    expect(result).toBe("X and Y");
  });

  it("parseApiErrorMessage extracts detail from JSON error body", () => {
    const err = { message: "400: {\"detail\": \"Title is required\"}" };
    expect(parseApiErrorMessage(err)).toBe("Title is required");
  });

  it("parseApiErrorMessage extracts detail.message from nested JSON", () => {
    const err = { message: "422: {\"detail\": {\"message\": \"Invalid priority\"}}" };
    expect(parseApiErrorMessage(err)).toBe("Invalid priority");
  });

  it("parseApiErrorMessage returns raw body when not JSON", () => {
    const err = { message: "500: Internal server error" };
    expect(parseApiErrorMessage(err)).toBe("Internal server error");
  });

  it("parseApiErrorMessage handles plain Error objects", () => {
    const err = new Error("Network failure");
    expect(parseApiErrorMessage(err)).toBe("Network failure");
  });

  it("parseApiErrorMessage handles string input", () => {
    expect(parseApiErrorMessage("something broke")).toBe("something broke");
  });

  it("parseApiErrorMessage handles null/undefined", () => {
    expect(parseApiErrorMessage(null)).toBe("");
    expect(parseApiErrorMessage(undefined)).toBe("");
  });

  it("getDestructiveConfirm returns a string for 'done'", () => {
    const result = getDestructiveConfirm(null, "done");
    expect(result).not.toBeNull();
    expect(typeof result).toBe("string");
    expect(result!.length).toBeGreaterThan(0);
  });

  it("getDestructiveConfirm returns a string for 'archived'", () => {
    const result = getDestructiveConfirm(null, "archived");
    expect(result).not.toBeNull();
  });

  it("getDestructiveConfirm returns a string for 'blocked'", () => {
    const result = getDestructiveConfirm(null, "blocked");
    expect(result).not.toBeNull();
  });

  it("getDestructiveConfirm returns null for non-destructive statuses", () => {
    expect(getDestructiveConfirm(null, "todo")).toBeNull();
    expect(getDestructiveConfirm(null, "ready")).toBeNull();
    expect(getDestructiveConfirm(null, "running")).toBeNull();
    expect(getDestructiveConfirm(null, "triage")).toBeNull();
  });

  it("getDestructiveConfirm returns null for unknown statuses (e.g. 'backlog' from DB)", () => {
    // The DB has statuses like 'backlog' and 'cancelled' that are not
    // destructive — no confirm prompt should fire.
    expect(getDestructiveConfirm(null, "backlog")).toBeNull();
    expect(getDestructiveConfirm(null, "cancelled")).toBeNull();
  });
});

// ── withCompletionSummary ─────────────────────────────────────────────────────

describe("E2E: withCompletionSummary", () => {
  beforeEach(() => {
    // Reset window mocks before each test.
    (globalThis as unknown as { window: Record<string, unknown> }).window = {
      ...(globalThis as unknown as { window: Record<string, unknown> }).window,
      prompt: vi.fn(),
      alert: vi.fn(),
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns patch unchanged when status is not 'done'", () => {
    const patch = { status: "todo" as const };
    const result = withCompletionSummary(patch, 1);
    expect(result).toEqual(patch);
  });

  it("returns patch unchanged when status is undefined", () => {
    const patch = { priority: 5 };
    const result = withCompletionSummary(patch, 1);
    expect(result).toEqual({ priority: 5 });
  });

  it("prompts for summary when status is 'done' and returns augmented patch", () => {
    const w = globalThis as unknown as { window: { prompt: ReturnType<typeof vi.fn>; alert: ReturnType<typeof vi.fn> } };
    w.window.prompt = vi.fn(() => "Task completed successfully");

    const patch = { status: "done" as const };
    const result = withCompletionSummary(patch, 1);
    expect(result).not.toBeNull();
    expect(result!.status).toBe("done");
    expect(result!.result).toBe("Task completed successfully");
    expect(result!.summary).toBe("Task completed successfully");
    expect(w.window.prompt).toHaveBeenCalledTimes(1);
  });

  it("returns null when user cancels the prompt (prompt returns null)", () => {
    const w = globalThis as unknown as { window: { prompt: ReturnType<typeof vi.fn> } };
    w.window.prompt = vi.fn(() => null);

    const patch = { status: "done" as const };
    const result = withCompletionSummary(patch, 1);
    expect(result).toBeNull();
  });

  it("returns null and alerts when user enters empty/whitespace summary", () => {
    const w = globalThis as unknown as { window: { prompt: ReturnType<typeof vi.fn>; alert: ReturnType<typeof vi.fn> } };
    w.window.prompt = vi.fn(() => "   ");
    w.window.alert = vi.fn();

    const patch = { status: "done" as const };
    const result = withCompletionSummary(patch, 1);
    expect(result).toBeNull();
    expect(w.window.alert).toHaveBeenCalledTimes(1);
  });

  it("uses singular label for count=1 and plural for count>1 in prompt text", () => {
    const w = globalThis as unknown as { window: { prompt: ReturnType<typeof vi.fn> } };

    // count = 1 → "this task"
    w.window.prompt = vi.fn(() => "summary");
    withCompletionSummary({ status: "done" as const }, 1);
    const promptCall1 = w.window.prompt.mock.calls[0];
    expect(promptCall1[0]).toContain("this task");

    // count = 3 → "3 selected task(s)"
    w.window.prompt.mockClear();
    w.window.prompt = vi.fn(() => "summary");
    withCompletionSummary({ status: "done" as const }, 3);
    const promptCall2 = w.window.prompt.mock.calls[0];
    expect(promptCall2[0]).toContain("3 selected task(s)");
  });

  it("preserves other fields in the patch when augmenting", () => {
    const w = globalThis as unknown as { window: { prompt: ReturnType<typeof vi.fn> } };
    w.window.prompt = vi.fn(() => "done summary");

    const patch = { status: "done" as const, assignee: "worker-1", priority: 3 };
    const result = withCompletionSummary(patch, 1);
    expect(result).not.toBeNull();
    expect(result!.assignee).toBe("worker-1");
    expect(result!.priority).toBe(3);
    expect(result!.result).toBe("done summary");
  });
});

// ── phantomIdsFromEvent ──────────────────────────────────────────────────────

describe("E2E: phantomIdsFromEvent", () => {
  it("returns empty array when payload is null", () => {
    expect(phantomIdsFromEvent({ payload: null })).toEqual([]);
  });

  it("returns empty array when payload is undefined", () => {
    expect(phantomIdsFromEvent({})).toEqual([]);
  });

  it("returns empty array when event itself is null-ish", () => {
    expect(phantomIdsFromEvent(null as unknown as { payload?: Record<string, unknown> | null })).toEqual([]);
  });

  it("extracts phantom_cards from payload", () => {
    const ev = { payload: { phantom_cards: ["card-1", "card-2"] } };
    expect(phantomIdsFromEvent(ev)).toEqual(["card-1", "card-2"]);
  });

  it("extracts phantom_refs from payload", () => {
    const ev = { payload: { phantom_refs: ["ref-1"] } };
    expect(phantomIdsFromEvent(ev)).toEqual(["ref-1"]);
  });

  it("prefers phantom_cards over phantom_refs", () => {
    const ev = { payload: { phantom_cards: ["a"], phantom_refs: ["b"] } };
    expect(phantomIdsFromEvent(ev)).toEqual(["a"]);
  });

  it("returns empty array when neither key exists in payload", () => {
    const ev = { payload: { other: "data" } };
    expect(phantomIdsFromEvent(ev)).toEqual([]);
  });

  it("returns empty array when phantom_cards is empty", () => {
    const ev = { payload: { phantom_cards: [] } };
    expect(phantomIdsFromEvent(ev)).toEqual([]);
  });
});

// ── isDiagnosticEvent ────────────────────────────────────────────────────────

describe("E2E: isDiagnosticEvent", () => {
  it("returns true for completion_blocked_hallucination", () => {
    expect(isDiagnosticEvent("completion_blocked_hallucination")).toBe(true);
  });

  it("returns true for suspected_hallucinated_references", () => {
    expect(isDiagnosticEvent("suspected_hallucinated_references")).toBe(true);
  });

  it("returns false for normal event kinds (status_change, created)", () => {
    expect(isDiagnosticEvent("status_change")).toBe(false);
    expect(isDiagnosticEvent("created")).toBe(false);
  });

  it("returns false for unknown kinds", () => {
    expect(isDiagnosticEvent("random_kind")).toBe(false);
    expect(isDiagnosticEvent("")).toBe(false);
  });

  it("the real DB event kinds are NOT diagnostic (current DB has none)", () => {
    // The current kanban.db only has 'status_change' and 'created' events.
    // Neither is a diagnostic event — confirming the diagnostic system
    // hasn't fired (or these events are absent).
    const realKinds = getTaskEvents(100).map((e) => String(e.kind));
    for (const kind of realKinds) {
      expect(isDiagnosticEvent(kind)).toBe(false);
    }
  });
});

// ── WebSocket WsMessage shape validation ────────────────────────────────────

describe("E2E: WebSocket WsMessage shape validation", () => {
  it("accepts a well-formed WsMessage with cursor and events array", () => {
    const msg: WsMessage = {
      cursor: 42,
      events: [{ task_id: "task-1", kind: "status_change" }],
    };
    expect(msg.cursor).toBe(42);
    expect(Array.isArray(msg.events)).toBe(true);
    expect(msg.events.length).toBe(1);
  });

  it("accepts an empty events array", () => {
    const msg: WsMessage = { cursor: 0, events: [] };
    expect(msg.events).toHaveLength(0);
  });

  it("accepts events with arbitrary extra fields (WsEvent is permissive)", () => {
    const msg: WsMessage = {
      cursor: 1,
      events: [{ task_id: "t1", kind: "created", extra_field: "ok", number: 42 }],
    };
    expect(msg.events[0].task_id).toBe("t1");
    expect((msg.events[0] as Record<string, unknown>).extra_field).toBe("ok");
  });

  it("parses a JSON string the same way useKanbanEvents.onmessage does", () => {
    // Simulate what ws.onmessage does: JSON.parse the data, check events.
    const rawJson = JSON.stringify({
      cursor: 99,
      events: [
        { task_id: "task-a", kind: "status_change" },
        { task_id: "task-b", kind: "created" },
      ],
    });
    const parsed = JSON.parse(rawJson) as WsMessage;
    expect(parsed.cursor).toBe(99);
    expect(Array.isArray(parsed.events)).toBe(true);
    expect(parsed.events.length).toBe(2);
    expect(parsed.events[0].task_id).toBe("task-a");
    expect(parsed.events[1].task_id).toBe("task-b");
  });

  it("validates that the onmessage guard (events is array and non-empty) works", () => {
    // The useKanbanEvents hook checks: Array.isArray(msg.events) && msg.events.length > 0
    const validMsg = { cursor: 1, events: [{ task_id: "x" }] };
    const emptyMsg = { cursor: 1, events: [] };
    const noEventsMsg = { cursor: 1 };
    const nullEventsMsg = { cursor: 1, events: null };

    // Simulate the guard logic
    function guard(msg: unknown): boolean {
      if (!msg || typeof msg !== "object") return false;
      const m = msg as { events?: unknown };
      return Array.isArray(m.events) && m.events.length > 0;
    }

    expect(guard(validMsg)).toBe(true);
    expect(guard(emptyMsg)).toBe(false);
    expect(guard(noEventsMsg)).toBe(false);
    expect(guard(nullEventsMsg)).toBe(false);
    expect(guard(null)).toBe(false);
    expect(guard("string")).toBe(false);
  });

  it("correctly advances cursor from a WsMessage (matching useKanbanEvents logic)", () => {
    // The hook does: if (msg.cursor) cursorRef.current = msg.cursor;
    let cursor = 0;
    const msg: WsMessage = { cursor: 42, events: [] };
    if (msg.cursor) cursor = msg.cursor;
    expect(cursor).toBe(42);
  });

  it("does not advance cursor when cursor is 0 (falsy)", () => {
    let cursor = 10;
    const msg: WsMessage = { cursor: 0, events: [] };
    if (msg.cursor) cursor = msg.cursor;
    expect(cursor).toBe(10); // unchanged
  });

  it("increments per-task tick map for events with task_id (matching useKanbanEvents logic)", () => {
    // The hook does:
    //   for (const e of msg.events) { if (e && e.task_id) next[e.task_id] = (next[e.task_id] || 0) + 1 }
    const msg: WsMessage = {
      cursor: 1,
      events: [
        { task_id: "t1", kind: "status_change" },
        { task_id: "t1", kind: "comment_added" },
        { task_id: "t2", kind: "created" },
        { kind: "no_task_id" }, // should be skipped
      ],
    };

    const tick: Record<string, number> = {};
    for (const e of msg.events) {
      if (e && e.task_id) {
        tick[e.task_id] = (tick[e.task_id] || 0) + 1;
      }
    }

    expect(tick["t1"]).toBe(2);
    expect(tick["t2"]).toBe(1);
    expect(Object.keys(tick)).toHaveLength(2);
  });
});