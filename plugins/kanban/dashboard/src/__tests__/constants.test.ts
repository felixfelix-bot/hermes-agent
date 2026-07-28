/**
 * Tests for constants.ts — validates all exported constant values.
 *
 * These tests ensure the frontend column order, staleness thresholds, MIME
 * types, and localStorage keys match the expected contracts (and the Python
 * backend's BOARD_COLUMNS).
 */
import { describe, it, expect } from "vitest";

import {
  API,
  MIME_TASK,
  COLUMN_ORDER,
  LS_BOARD_KEY,
  COLUMN_DOT,
  STALENESS,
  DOCS_URL,
  DOCS_TUTORIAL_URL,
  DIAGNOSTIC_EVENT_KIND_KEYS,
  DESTRUCTIVE_KEYS,
} from "../constants";

// BOARD_COLUMNS as defined in plugin_api.py (line 150-152).
// Imported here as a literal so the test is self-contained; the "matches
// backend" check validates that COLUMN_ORDER equals this exact sequence.
const BACKEND_BOARD_COLUMNS = [
  "triage",
  "todo",
  "scheduled",
  "ready",
  "running",
  "blocked",
  "review",
  "done",
] as const;

describe("API", () => {
  it("equals the canonical kanban API base path", () => {
    // Arrange — expected path from plugin_api.py mount point
    const expected = "/api/plugins/kanban";

    // Act — read the exported constant
    const actual = API;

    // Assert
    expect(actual).toBe(expected);
  });
});

describe("MIME_TASK", () => {
  it("equals the custom Hermes task drag-and-drop MIME type", () => {
    expect(MIME_TASK).toBe("text/x-hermes-task");
  });
});

describe("COLUMN_ORDER", () => {
  it("has exactly 8 entries", () => {
    expect(COLUMN_ORDER).toHaveLength(8);
  });

  it("matches the expected column sequence", () => {
    expect(COLUMN_ORDER).toEqual([
      "triage",
      "todo",
      "scheduled",
      "ready",
      "running",
      "blocked",
      "review",
      "done",
    ]);
  });

  it("matches BOARD_COLUMNS in plugin_api.py", () => {
    // Cross-reference: the Python backend defines the same 8-column list.
    expect([...COLUMN_ORDER]).toEqual([...BACKEND_BOARD_COLUMNS]);
  });

  it("contains no duplicate statuses", () => {
    const unique = new Set(COLUMN_ORDER);
    expect(unique.size).toBe(COLUMN_ORDER.length);
  });
});

describe("LS_BOARD_KEY", () => {
  it("uses the canonical localStorage key name", () => {
    expect(LS_BOARD_KEY).toBe("hermes.kanban.selectedBoard");
  });
});

describe("COLUMN_DOT", () => {
  const expectedStatuses = [
    "triage",
    "todo",
    "ready",
    "running",
    "blocked",
    "done",
    "archived",
  ];

  it("has a CSS class entry for every expected status", () => {
    for (const status of expectedStatuses) {
      expect(COLUMN_DOT[status]).toBeTruthy();
      expect(typeof COLUMN_DOT[status]).toBe("string");
    }
  });

  it("uses the hermes-kanban-dot-* naming convention", () => {
    for (const status of expectedStatuses) {
      expect(COLUMN_DOT[status]).toBe(`hermes-kanban-dot-${status}`);
    }
  });
});

describe("STALENESS", () => {
  it("defines amber and red thresholds for ready", () => {
    // ready: amber = 1 hour, red = 24 hours
    expect(STALENESS.ready).toEqual({ amber: 3600, red: 86400 });
  });

  it("defines amber and red thresholds for running", () => {
    // running: amber = 10 minutes, red = 1 hour
    expect(STALENESS.running).toEqual({ amber: 600, red: 3600 });
  });

  it("defines amber and red thresholds for blocked", () => {
    expect(STALENESS.blocked).toEqual({ amber: 3600, red: 86400 });
  });

  it("defines amber and red thresholds for todo", () => {
    // todo: amber = 7 days, red = 30 days
    expect(STALENESS.todo).toEqual({ amber: 604800, red: 2592000 });
  });

  it("amber is always less than red within each tier", () => {
    for (const [, { amber, red }] of Object.entries(STALENESS)) {
      expect(amber).toBeGreaterThan(0);
      expect(red).toBeGreaterThan(amber);
    }
  });
});

describe("DOCS_URL", () => {
  it("points to the kanban docs page", () => {
    expect(DOCS_URL).toContain("kanban");
    expect(DOCS_URL).toMatch(/^https:\/\//);
  });

  it("DOCS_TUTORIAL_URL points to the tutorial", () => {
    expect(DOCS_TUTORIAL_URL).toContain("kanban-tutorial");
  });
});

describe("DIAGNOSTIC_EVENT_KIND_KEYS", () => {
  it("maps completion_blocked_hallucination to its i18n key", () => {
    expect(DIAGNOSTIC_EVENT_KIND_KEYS.completion_blocked_hallucination).toBe(
      "completionBlockedHallucination",
    );
  });

  it("maps suspected_hallucinated_references to its i18n key", () => {
    expect(DIAGNOSTIC_EVENT_KIND_KEYS.suspected_hallucinated_references).toBe(
      "suspectedHallucinatedReferences",
    );
  });
});

describe("DESTRUCTIVE_KEYS", () => {
  it("maps done to confirmDone", () => {
    expect(DESTRUCTIVE_KEYS.done).toBe("confirmDone");
  });

  it("maps archived to confirmArchive", () => {
    expect(DESTRUCTIVE_KEYS.archived).toBe("confirmArchive");
  });

  it("maps blocked to confirmBlocked", () => {
    expect(DESTRUCTIVE_KEYS.blocked).toBe("confirmBlocked");
  });
});
