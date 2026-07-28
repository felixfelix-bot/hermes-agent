/**
 * Tests for api.ts — validates URL construction and request shapes.
 *
 * Strategy: instead of mocking the sdk module (which fails because api.ts
 * captures `const fetchJSON = getFetchJSON` at import time), we set up
 * `window.__HERMES_PLUGIN_SDK__` with a mock `fetchJSON` spy BEFORE any
 * imports run, using `vi.hoisted()`. The real `getFetchJSON()` then returns
 * our spy, and api.ts captures it at module-eval time.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// vi.hoisted() runs before ALL imports — safe to set up globals here.
const { mockFetch } = vi.hoisted(() => {
  const mockFetch = vi.fn<(url: string, init?: RequestInit) => Promise<unknown>>(() => Promise.resolve({}));

  // Minimal SDK shape — only fetchJSON is exercised by api.ts.
  // Other fields are stubbed to satisfy the HermesPluginSDK interface.
  const g = globalThis as Record<string, unknown>;
  g.window = {
    localStorage: {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
      clear: () => {},
    },
    __HERMES_PLUGIN_SDK__: {
      sdkVersion: "test",
      React: {},
      hooks: {},
      api: {},
      fetchJSON: mockFetch,
      authedFetch: vi.fn(),
      buildWsUrl: vi.fn(),
      buildWsAuthParam: vi.fn().mockResolvedValue(["k", "v"]),
      components: {},
      utils: { cn: () => "", timeAgo: () => "", isoTimeAgo: () => "" },
      useI18n: () => ({}),
    },
  };

  return { mockFetch };
});

// Import after the hoisted block sets up window.__HERMES_PLUGIN_SDK__.
import {
  getBoard,
  getBoards,
  createBoard,
  deleteBoard,
  createTask,
  patchTask,
  deleteTask,
  bulkUpdate,
  addComment,
  addLink,
  removeLink,
  reclaimTask,
  specifyTask,
  decomposeTask,
  reassignTask,
  getConfig,
  getStats,
  getProfiles,
  getOrchestration,
} from "../api";
import { API } from "../constants";

beforeEach(() => {
  mockFetch.mockClear();
});

// ── Board endpoints ─────────────────────────────────────────────────────────

describe("getBoard", () => {
  it("constructs a GET request to /board with no extra params", async () => {
    // Act
    await getBoard(null);

    // Assert — URL ends with /board, no query string, no init
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch).toHaveBeenCalledWith(`${API}/board`);
  });

  it("appends query params when provided", async () => {
    await getBoard(null, { status: "ready", assignee: "alice" });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain(`${API}/board?`);
    expect(url).toContain("status=ready");
    expect(url).toContain("assignee=alice");
  });

  it("appends board param for board-scoped calls", async () => {
    await getBoard("default");

    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain("board=default");
  });
});

describe("getBoards", () => {
  it("constructs a GET request to /boards", async () => {
    await getBoards(null);

    expect(mockFetch).toHaveBeenCalledWith(`${API}/boards`);
  });
});

describe("createBoard", () => {
  it("sends POST with JSON body to /boards", async () => {
    // Arrange
    const payload = { slug: "sprint-1", name: "Sprint 1" };

    // Act
    await createBoard(payload);

    // Assert
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/boards`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init?.body as string)).toEqual(payload);
  });
});

describe("deleteBoard", () => {
  it("sends DELETE to /boards/{slug}", async () => {
    await deleteBoard("sprint-1");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/boards/sprint-1`);
    expect(init?.method).toBe("DELETE");
  });

  it("URL-encodes the slug", async () => {
    await deleteBoard("my/board");

    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain(`${API}/boards/my%2Fboard`);
  });
});

// ── Task endpoints ──────────────────────────────────────────────────────────

describe("createTask", () => {
  it("sends POST with JSON body to /tasks", async () => {
    const body = { title: "New task", assignee: null, priority: 5, triage: false };

    await createTask(body, "default");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks`);
    expect(url).toContain("board=default");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });
});

describe("patchTask", () => {
  it("sends PATCH to /tasks/{id} with correct URL", async () => {
    const patch = { status: "done" as const };

    await patchTask("abc-123", patch, null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/tasks/abc-123`);
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(init?.body as string)).toEqual(patch);
  });

  it("appends board param when provided", async () => {
    await patchTask("abc-123", { priority: 1 }, "work");

    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain("board=work");
  });
});

describe("deleteTask", () => {
  it("sends DELETE to /tasks/{id}", async () => {
    await deleteTask("abc-123");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/tasks/abc-123`);
    expect(init?.method).toBe("DELETE");
  });
});

describe("bulkUpdate", () => {
  it("sends POST to /tasks/bulk with JSON body", async () => {
    const body = { ids: ["a", "b"], status: "todo" as const };

    await bulkUpdate(body, "default");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks/bulk`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });
});

// ── Recovery action endpoints ───────────────────────────────────────────────

describe("reclaimTask", () => {
  it("constructs URL with /reclaim suffix and sends POST", async () => {
    await reclaimTask("abc-123", "stuck too long", null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks/abc-123/reclaim`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ reason: "stuck too long" });
  });
});

describe("specifyTask", () => {
  it("constructs URL with /specify suffix and sends POST", async () => {
    await specifyTask("abc-123", null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks/abc-123/specify`);
    expect(init?.method).toBe("POST");
  });
});

describe("decomposeTask", () => {
  it("constructs URL with /decompose suffix", async () => {
    await decomposeTask("abc-123", "default");

    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain(`${API}/tasks/abc-123/decompose`);
    expect(url).toContain("board=default");
  });
});

describe("reassignTask", () => {
  it("constructs URL with /reassign suffix and sends profile + reason", async () => {
    await reassignTask("abc-123", "manager", true, "better fit", "default");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks/abc-123/reassign`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      profile: "manager",
      reclaim_first: true,
      reason: "better fit",
    });
  });
});

// ── Comments & links ────────────────────────────────────────────────────────

describe("addComment", () => {
  it("sends POST to /tasks/{id}/comments with JSON body", async () => {
    await addComment("abc-123", "Looks good!", null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/tasks/abc-123/comments`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ body: "Looks good!" });
  });
});

describe("addLink", () => {
  it("sends POST to /links with parent_id and child_id", async () => {
    await addLink("parent-1", "child-1", null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/links`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      parent_id: "parent-1",
      child_id: "child-1",
    });
  });
});

describe("removeLink", () => {
  it("sends DELETE to /links with query params", async () => {
    await removeLink("parent-1", "child-1", null);

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`${API}/links`);
    expect(url).toContain("parent_id=parent-1");
    expect(url).toContain("child_id=child-1");
    expect(init?.method).toBe("DELETE");
  });
});

// ── Config & stats ──────────────────────────────────────────────────────────

describe("getConfig", () => {
  it("constructs a GET request to /config", async () => {
    await getConfig(null);

    expect(mockFetch).toHaveBeenCalledWith(`${API}/config`);
  });
});

describe("getStats", () => {
  it("constructs a GET request to /stats with board param", async () => {
    await getStats("default");

    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain(`${API}/stats`);
    expect(url).toContain("board=default");
  });
});

// ── Non-board-scoped endpoints ──────────────────────────────────────────────

describe("getProfiles", () => {
  it("constructs a GET request to /profiles without board param", async () => {
    await getProfiles();

    expect(mockFetch).toHaveBeenCalledWith(`${API}/profiles`);
  });
});

describe("getOrchestration", () => {
  it("constructs a GET request to /orchestration", async () => {
    await getOrchestration();

    expect(mockFetch).toHaveBeenCalledWith(`${API}/orchestration`);
  });
});
