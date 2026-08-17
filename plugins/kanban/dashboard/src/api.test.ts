/**
 * Unit tests for src/api.ts — API client + pure helpers.
 *
 * Runs in vitest's default "node" environment (no jsdom installed). Every
 * browser global the module touches — `window.localStorage`,
 * `window.prompt`/`window.alert`, and `window.__HERMES_PLUGIN_SDK__` — is
 * stubbed per test with `vi.stubGlobal`, so nothing reaches the network or
 * the real kanban backend.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fmtBytes,
  getColumnHelp,
  getColumnLabel,
  kanbanApi,
  parseApiErrorMessage,
  readSelectedBoard,
  renderMarkdown,
  selectChangeHandler,
  stalenessClass,
  tx,
  withBoard,
  withCompletionSummary,
  writeSelectedBoard,
} from "./api";
import { API_BASE, COLUMN_ORDER, LS_BOARD_KEY } from "./types";

// ── withBoard ──

describe("withBoard", function () {
  it("returns the url unchanged when board is null", function () {
    expect(withBoard("/api/plugins/kanban/board", null)).toBe("/api/plugins/kanban/board");
  });

  it("returns the url unchanged when board is an empty string", function () {
    expect(withBoard("/x", "")).toBe("/x");
  });

  it("appends ?board=… when the url has no query params", function () {
    expect(withBoard("/x", "work")).toBe("/x?board=work");
  });

  it("appends &board=… when the url already has query params", function () {
    expect(withBoard("/x?since=5", "work")).toBe("/x?since=5&board=work");
  });

  it("URI-encodes the board slug", function () {
    expect(withBoard("/x", "my board&co")).toBe("/x?board=my%20board%26co");
  });
});

// ── Board selection persistence ──

function makeLocalStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
  };
}

describe("readSelectedBoard / writeSelectedBoard", function () {
  beforeEach(function () {
    vi.stubGlobal("window", { localStorage: makeLocalStorage() });
  });
  afterEach(function () {
    vi.unstubAllGlobals();
  });

  it("round-trips a board slug through localStorage", function () {
    writeSelectedBoard("work");
    expect(window.localStorage.getItem(LS_BOARD_KEY)).toBe("work");
    expect(readSelectedBoard()).toBe("work");
  });

  it("writes under the documented LS_BOARD_KEY", function () {
    writeSelectedBoard("ops");
    expect(window.localStorage.getItem("hermes.kanban.selectedBoard")).toBe("ops");
  });

  it("writeSelectedBoard(null) removes the stored value", function () {
    writeSelectedBoard("work");
    writeSelectedBoard(null);
    expect(window.localStorage.getItem(LS_BOARD_KEY)).toBeNull();
    expect(readSelectedBoard()).toBeNull();
  });

  it("readSelectedBoard trims whitespace and maps blank to null", function () {
    window.localStorage.setItem(LS_BOARD_KEY, "   ");
    expect(readSelectedBoard()).toBeNull();
    window.localStorage.setItem(LS_BOARD_KEY, "  ab  ");
    expect(readSelectedBoard()).toBe("ab");
  });

  it("readSelectedBoard returns null when localStorage access throws", function () {
    vi.stubGlobal("window", {
      get localStorage(): Storage {
        throw new Error("denied (private mode)");
      },
    });
    expect(readSelectedBoard()).toBeNull();
  });

  it("writeSelectedBoard swallows localStorage errors", function () {
    vi.stubGlobal("window", {
      get localStorage(): Storage {
        throw new Error("quota exceeded");
      },
    });
    expect(function () {
      writeSelectedBoard("work");
      writeSelectedBoard(null);
    }).not.toThrow();
  });
});

// ── i18n helpers ──

describe("tx", function () {
  it("returns the fallback when t is null/undefined", function () {
    expect(tx(null, "a.b", "fb")).toBe("fb");
    expect(tx(undefined, "a.b", "fb")).toBe("fb");
  });

  it("resolves dot paths under the kanban namespace", function () {
    const t = { kanban: { columnLabels: { done: "Fertig" } } };
    expect(tx(t, "columnLabels.done", "fb")).toBe("Fertig");
  });

  it("returns the fallback when the path is missing or dead-ends", function () {
    expect(tx({ kanban: {} }, "columnLabels.done", "fb")).toBe("fb");
    expect(tx({ kanban: { columnLabels: "not-an-object" } }, "columnLabels.done", "fb")).toBe("fb");
  });

  it("ignores keys outside the kanban namespace", function () {
    expect(tx({ other: { a: "x" } }, "a", "fb")).toBe("fb");
  });

  it("interpolates {var} placeholders, including repeats", function () {
    const t = { kanban: { greet: "Hi {name}, bye {name}" } };
    expect(tx(t, "greet", "fb", { name: "Ada" })).toBe("Hi Ada, bye Ada");
  });

  it("stringifies numeric interpolation values", function () {
    const t = { kanban: { count: "{n} tasks" } };
    expect(tx(t, "count", "fb", { n: 3 })).toBe("3 tasks");
  });
});

describe("getColumnLabel / getColumnHelp", function () {
  it("getColumnLabel falls back to the English label for known statuses", function () {
    expect(getColumnLabel(null, "running")).toBe("In Progress");
    expect(getColumnLabel(null, "triage")).toBe("Triage");
    expect(getColumnLabel(null, "archived")).toBe("Archived");
  });

  it("getColumnLabel prefers the translation when present", function () {
    const t = { kanban: { columnLabels: { running: "Läuft" } } };
    expect(getColumnLabel(t, "running")).toBe("Läuft");
    expect(getColumnLabel(t, "blocked")).toBe("Blocked"); // untouched status still falls back
  });

  it("getColumnLabel echoes unknown statuses as-is", function () {
    expect(getColumnLabel(null, "weird")).toBe("weird");
  });

  it("getColumnHelp falls back to the English help text for known statuses", function () {
    expect(getColumnHelp(null, "blocked")).toBe("Worker asked for human input");
    expect(getColumnHelp(null, "ready")).toBe("Dependencies satisfied; assign a profile to dispatch");
  });

  it("getColumnHelp prefers the translation and yields '' for unknown statuses", function () {
    expect(getColumnHelp({ kanban: { columnHelp: { done: "Fertig" } } }, "done")).toBe("Fertig");
    expect(getColumnHelp(null, "weird")).toBe("");
  });

  it("every visible column has both a label and a help fallback", function () {
    for (const col of COLUMN_ORDER) {
      const label = getColumnLabel(null, col);
      expect(label, `label for ${col}`).toMatch(/\S/);
      expect(label, `label for ${col}`).not.toBe(col);
      expect(getColumnHelp(null, col), `help for ${col}`).toMatch(/\S/);
    }
  });
});

// ── Error parsing ──

describe("parseApiErrorMessage", function () {
  it("passes through plain strings", function () {
    expect(parseApiErrorMessage("boom")).toBe("boom");
  });

  it("unwraps Error instances", function () {
    expect(parseApiErrorMessage(new Error("nope"))).toBe("nope");
  });

  it("maps nullish input to an empty string", function () {
    expect(parseApiErrorMessage(null)).toBe("");
    expect(parseApiErrorMessage("")).toBe("");
  });

  it("extracts detail from a 'NNN: {json}' fetch error", function () {
    expect(parseApiErrorMessage('400: {"detail":"missing board"}')).toBe("missing board");
  });

  it("extracts nested detail.message", function () {
    expect(parseApiErrorMessage('422: {"detail":{"message":"nested fail"}}')).toBe("nested fail");
  });

  it("returns the raw body when it is not JSON", function () {
    expect(parseApiErrorMessage("404: plain text")).toBe("plain text");
  });

  it("returns the raw JSON body when detail is neither string nor {message}", function () {
    expect(parseApiErrorMessage('400: {"detail":42}')).toBe('{"detail":42}');
  });
});

// ── Select change handler ──

describe("selectChangeHandler", function () {
  it("normalizes radix onValueChange values", function () {
    const setter = vi.fn();
    const h = selectChangeHandler(setter);
    h.onValueChange("ready");
    h.onValueChange(null);
    expect(setter.mock.calls).toEqual([["ready"], [""]]);
  });

  it("handles native change events and raw string values", function () {
    const setter = vi.fn();
    const h = selectChangeHandler(setter);
    h.onChange({ target: { value: "done" } });
    h.onChange("archived");
    expect(setter.mock.calls).toEqual([["done"], ["archived"]]);
  });
});

// ── Completion summary ──

describe("withCompletionSummary", function () {
  let prompt: ReturnType<typeof vi.fn>;
  let alert: ReturnType<typeof vi.fn>;

  beforeEach(function () {
    prompt = vi.fn();
    alert = vi.fn();
    vi.stubGlobal("window", { prompt, alert });
  });
  afterEach(function () {
    vi.unstubAllGlobals();
  });

  it("passes non-done patches through without prompting", function () {
    const patch = { status: "todo", title: "x" };
    expect(withCompletionSummary(patch, 1)).toBe(patch);
    expect(prompt).not.toHaveBeenCalled();
  });

  it("returns null when the user cancels the prompt", function () {
    prompt.mockReturnValue(null);
    expect(withCompletionSummary({ status: "done" }, 1)).toBeNull();
    expect(alert).not.toHaveBeenCalled();
  });

  it("alerts and returns null when the summary is blank", function () {
    prompt.mockReturnValue("   ");
    expect(withCompletionSummary({ status: "done" }, 1)).toBeNull();
    expect(alert).toHaveBeenCalledTimes(1);
  });

  it("attaches the trimmed summary as both result and summary", function () {
    prompt.mockReturnValue("  shipped it  ");
    expect(withCompletionSummary({ status: "done" }, 1)).toEqual({
      status: "done",
      result: "shipped it",
      summary: "shipped it",
    });
  });

  it("mentions the selection count for bulk completion and the singular label for one", function () {
    prompt.mockReturnValue("ok");
    withCompletionSummary({ status: "done" }, 3);
    expect(prompt).toHaveBeenCalledWith(expect.stringContaining("3 selected task(s)"), "");

    prompt.mockClear();
    withCompletionSummary({ status: "done" }, 1);
    expect(prompt).toHaveBeenCalledWith(expect.stringContaining("this task"), "");
  });
});

// ── Staleness tiers ──

describe("stalenessClass", function () {
  it("marks a long-running task red past the 1h threshold", function () {
    const out = stalenessClass({ status: "running", age: { started_age_seconds: 3700, created_age_seconds: null } });
    expect(out).toBe("hermes-kanban-card--stale-red");
  });

  it("marks a running task amber between 10m and 1h", function () {
    const out = stalenessClass({ status: "running", age: { started_age_seconds: 601, created_age_seconds: null } });
    expect(out).toBe("hermes-kanban-card--stale-amber");
  });

  it("stays neutral below the amber threshold", function () {
    const out = stalenessClass({ status: "running", age: { started_age_seconds: 599, created_age_seconds: null } });
    expect(out).toBe("");
  });

  it("uses started_age_seconds for running, ignoring created age", function () {
    const out = stalenessClass({ status: "running", age: { started_age_seconds: 100, created_age_seconds: 999999 } });
    expect(out).toBe("");
  });

  it("uses created_age_seconds for ready and honors its tiers", function () {
    expect(stalenessClass({ status: "ready", age: { started_age_seconds: null, created_age_seconds: 7200 } })).toBe(
      "hermes-kanban-card--stale-amber",
    );
    expect(stalenessClass({ status: "ready", age: { started_age_seconds: null, created_age_seconds: 90000 } })).toBe(
      "hermes-kanban-card--stale-red",
    );
  });

  it("applies the one-week amber tier to todo", function () {
    expect(stalenessClass({ status: "todo", age: { started_age_seconds: null, created_age_seconds: 8 * 86400 } })).toBe(
      "hermes-kanban-card--stale-amber",
    );
  });

  it("returns '' for statuses without a tier (e.g. done)", function () {
    expect(stalenessClass({ status: "done", age: { started_age_seconds: null, created_age_seconds: 999999 } })).toBe("");
  });

  it("returns '' when age is null or the relevant seconds value is null", function () {
    expect(stalenessClass({ status: "running", age: null })).toBe("");
    expect(stalenessClass({ status: "running", age: { started_age_seconds: null, created_age_seconds: null } })).toBe("");
  });
});

// ── Markdown renderer ──

describe("renderMarkdown", function () {
  it("renders an empty string as empty", function () {
    expect(renderMarkdown("")).toBe("");
  });

  it("wraps plain lines in paragraphs", function () {
    expect(renderMarkdown("hello")).toBe("<p>hello</p>");
  });

  it("renders # headings up to level 4 with inline formatting", function () {
    expect(renderMarkdown("# Title")).toBe("<h1>Title</h1>");
    expect(renderMarkdown("#### Deep *note*")).toBe("<h4>Deep <em>note</em></h4>");
  });

  it("groups consecutive bullets into a single list", function () {
    const html = renderMarkdown("- a\n- b");
    expect(html).toContain("<ul>");
    expect(html).toContain("<li>a</li>");
    expect(html).toContain("<li>b</li>");
    expect(html).toContain("</ul>");
  });

  it("closes the list when a non-bullet line follows", function () {
    const html = renderMarkdown("- a\n\nafter");
    expect(html).toContain("</ul>");
    expect(html).toContain("<p>after</p>");
  });

  it("renders inline code, bold, and italic", function () {
    const html = renderMarkdown("run `npm test`, **bold**, *italic*");
    expect(html).toContain("<code>npm test</code>");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>italic</em>");
  });

  it("renders safe http(s)/mailto links with rel=noopener", function () {
    const html = renderMarkdown("[docs](https://example.com/x?a=1)");
    expect(html).toContain('<a href="https://example.com/x?a=1" target="_blank" rel="noopener noreferrer">docs</a>');
  });

  it("escapes raw HTML in prose", function () {
    const html = renderMarkdown("<img src=x onerror=alert(1)>");
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
  });

  it("preserves fenced code blocks, escaped, as <pre><code>", function () {
    const html = renderMarkdown('```\nlet a = 1 < 2 && "x";\n```');
    expect(html).toContain('<pre class="hermes-kanban-md-code"><code>');
    expect(html).toContain("let a = 1 &lt; 2 &amp;&amp; &quot;x&quot;");
  });
});

// ── Byte formatter ──

describe("fmtBytes", function () {
  it("formats sub-KB values in bytes", function () {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1023)).toBe("1023 B");
  });

  it("formats kilobytes with one decimal", function () {
    expect(fmtBytes(2048)).toBe("2.0 KB");
  });

  it("formats megabytes with one decimal", function () {
    expect(fmtBytes(1024 * 1024)).toBe("1.0 MB");
    expect(fmtBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("coerces garbage input to 0 B", function () {
    expect(fmtBytes(Number.NaN)).toBe("0 B");
    expect(fmtBytes(Number("garbage" as unknown as number))).toBe("0 B");
  });
});

// ── kanbanApi client ──

describe("kanbanApi", function () {
  let fetchJSON: ReturnType<typeof vi.fn>;
  let authedFetch: ReturnType<typeof vi.fn>;

  beforeEach(function () {
    fetchJSON = vi.fn().mockResolvedValue({});
    authedFetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("window", { __HERMES_PLUGIN_SDK__: { fetchJSON, authedFetch } });
  });
  afterEach(function () {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("getBoard threads the board slug into the query string", async function () {
    await kanbanApi.getBoard("work");
    expect(fetchJSON).toHaveBeenCalledTimes(1);
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/board?board=work`);
  });

  it("getBoard leaves the URL bare without a board and resolves the SDK value", async function () {
    const board = { columns: [], tenants: [], assignees: [], latest_event_id: 7, now: 123 };
    fetchJSON.mockResolvedValueOnce(board);
    await expect(kanbanApi.getBoard(null)).resolves.toBe(board);
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/board`);
  });

  it("getBoard keeps existing query params and appends the board", async function () {
    await kanbanApi.getBoard("work", new URLSearchParams({ since: "5" }));
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/board?since=5&board=work`);
  });

  it("getTask URL-encodes the task id and threads the board", async function () {
    await kanbanApi.getTask("task/1", "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/task%2F1?board=work`);
  });

  it("createTask POSTs the JSON body with the board in the query", async function () {
    const body = { title: "Fix login", priority: 2 };
    await kanbanApi.createTask(body, "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks?board=work`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("updateTask sends a PATCH with the status-change body and the board", async function () {
    const body = { status: "done", result: "shipped" };
    await kanbanApi.updateTask("t-1", body, "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/t-1?board=work`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("deleteTask issues a DELETE against the bare task URL (board param unused)", async function () {
    // Current behavior: deleteTask accepts a board but does not thread it into
    // the URL — deletion is keyed by task id alone.
    await kanbanApi.deleteTask("t-1", "work");
    expect(fetchJSON).toHaveBeenCalledTimes(1);
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/t-1`, { method: "DELETE" });
  });

  it("bulkUpdate POSTs to tasks/bulk with the board", async function () {
    const body = { ids: ["a", "b"], status: "archived" };
    await kanbanApi.bulkUpdate(body, "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/bulk?board=work`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });

  it("listDiagnostics appends severity and board to the query", async function () {
    await kanbanApi.listDiagnostics("work", "error");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/diagnostics?severity=error&board=work`);
    await kanbanApi.listDiagnostics(null);
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/diagnostics`);
  });

  it("getStats and listActiveWorkers thread the board", async function () {
    await kanbanApi.getStats("work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/stats?board=work`);
    await kanbanApi.listActiveWorkers("work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/workers/active?board=work`);
  });

  it("toggleHomeSubscription maps subscribe→POST and unsubscribe→DELETE", async function () {
    await kanbanApi.toggleHomeSubscription("t-1", "telegram", true, "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/t-1/home-subscribe/telegram?board=work`, {
      method: "POST",
    });
    await kanbanApi.toggleHomeSubscription("t-1", "telegram", false, "work");
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/tasks/t-1/home-subscribe/telegram?board=work`, {
      method: "DELETE",
    });
  });

  it("board-independent endpoints skip the board param entirely", async function () {
    await kanbanApi.getConfig();
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/config`);
    await kanbanApi.listBoards();
    expect(fetchJSON).toHaveBeenCalledWith(`${API_BASE}/boards`);
  });

  it("uploadAttachment POSTs multipart via authedFetch with the board in the URL", async function () {
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    await kanbanApi.uploadAttachment("t-1", file, "work");
    expect(fetchJSON).not.toHaveBeenCalled();
    expect(authedFetch).toHaveBeenCalledTimes(1);
    const [url, init] = authedFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/tasks/t-1/attachments?board=work`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const sent = (init.body as FormData).get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("notes.txt");
  });

  it("throws a clear error when the plugin SDK is unavailable", function () {
    vi.stubGlobal("window", {}); // overwrite the beforeEach stub: no __HERMES_PLUGIN_SDK__
    expect(function () {
      kanbanApi.getConfig();
    }).toThrow("Plugin SDK not available");
  });
});
