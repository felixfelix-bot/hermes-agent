/**
 * Tests for sdk.ts — pure helper functions.
 *
 * Covers withBoard(), readSelectedBoard(), writeSelectedBoard(), and
 * selectChangeHandler(). These functions touch window.localStorage but do
 * not require the full plugin SDK (React, components, etc.), so we mock only
 * localStorage.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import {
  withBoard,
  readSelectedBoard,
  writeSelectedBoard,
  selectChangeHandler,
} from "../sdk";

// ── localStorage mock ───────────────────────────────────────────────────────
// Node environment has no `window` or `localStorage`, so we provide a
// minimal in-memory implementation on globalThis.

const store: Record<string, string> = {};

const localStorageMock = {
  getItem: vi.fn((key: string): string | null => store[key] ?? null),
  setItem: vi.fn((key: string, value: string): void => {
    store[key] = value;
  }),
  removeItem: vi.fn((key: string): void => {
    delete store[key];
  }),
  clear: vi.fn((): void => {
    for (const k of Object.keys(store)) delete store[k];
  }),
  key: vi.fn((_index: number): string | null => null),
  get length(): number {
    return Object.keys(store).length;
  },
};

// Install on globalThis.window before any test runs.
(globalThis as unknown as { window: Record<string, unknown> }).window = {
  localStorage: localStorageMock,
};

beforeEach(() => {
  // Clear the in-memory store and reset all mock call counts.
  for (const k of Object.keys(store)) delete store[k];
  vi.clearAllMocks();
});

// ── withBoard ───────────────────────────────────────────────────────────────

describe("withBoard", () => {
  it("returns the URL unchanged when board is null", () => {
    // Arrange
    const url = "/api/plugins/kanban/board";
    const board = null;

    // Act
    const result = withBoard(url, board);

    // Assert
    expect(result).toBe(url);
  });

  it("appends ?board=<slug> when the URL has no query string", () => {
    const result = withBoard("/api/plugins/kanban/board", "default");
    expect(result).toBe("/api/plugins/kanban/board?board=default");
  });

  it("appends &board=<slug> when the URL already has a query string", () => {
    const result = withBoard("/api/plugins/kanban/board?foo=1", "my-board");
    expect(result).toBe("/api/plugins/kanban/board?foo=1&board=my-board");
  });

  it("URL-encodes special characters in the board slug", () => {
    // Spaces → %20, & → %26
    const result = withBoard("/api/board", "my board & more");
    expect(result).toBe("/api/board?board=my%20board%20%26%20more");
  });

  it("returns the URL unchanged when board is empty string", () => {
    // Empty string is falsy → treated like null
    const result = withBoard("/api/board", "");
    expect(result).toBe("/api/board");
  });
});

// ── readSelectedBoard ───────────────────────────────────────────────────────

describe("readSelectedBoard", () => {
  it("returns null when localStorage is empty", () => {
    const result = readSelectedBoard();
    expect(result).toBeNull();
  });

  it("returns the stored value", () => {
    store["hermes.kanban.selectedBoard"] = "work-board";
    const result = readSelectedBoard();
    expect(result).toBe("work-board");
  });

  it("returns null when the stored value is only whitespace", () => {
    store["hermes.kanban.selectedBoard"] = "   ";
    const result = readSelectedBoard();
    expect(result).toBeNull();
  });

  it("trims whitespace from the stored value", () => {
    store["hermes.kanban.selectedBoard"] = "  trimmed-board  ";
    const result = readSelectedBoard();
    expect(result).toBe("trimmed-board");
  });
});

// ── writeSelectedBoard ──────────────────────────────────────────────────────

describe("writeSelectedBoard", () => {
  it("writes the slug to localStorage under the canonical key", () => {
    writeSelectedBoard("test");
    expect(localStorageMock.setItem).toHaveBeenCalledWith(
      "hermes.kanban.selectedBoard",
      "test",
    );
    expect(store["hermes.kanban.selectedBoard"]).toBe("test");
  });

  it("removes the entry from localStorage when slug is null", () => {
    store["hermes.kanban.selectedBoard"] = "old-board";
    writeSelectedBoard(null);
    expect(localStorageMock.removeItem).toHaveBeenCalledWith(
      "hermes.kanban.selectedBoard",
    );
    expect(store["hermes.kanban.selectedBoard"]).toBeUndefined();
  });
});

// ── selectChangeHandler ─────────────────────────────────────────────────────

describe("selectChangeHandler", () => {
  it("calls the setter with the value via onValueChange", () => {
    // Arrange
    const setter = vi.fn();
    const handler = selectChangeHandler(setter) as {
      onValueChange: (v: string | null) => void;
      onChange: (e: { target?: { value: string } } | string) => void;
    };

    // Act — shadcn-style direct value callback
    handler.onValueChange("x");

    // Assert
    expect(setter).toHaveBeenCalledWith("x");
  });

  it("calls the setter with the value via onChange (event target)", () => {
    const setter = vi.fn();
    const handler = selectChangeHandler(setter) as {
      onValueChange: (v: string | null) => void;
      onChange: (e: { target?: { value: string } } | string) => void;
    };

    handler.onChange({ target: { value: "y" } });

    expect(setter).toHaveBeenCalledWith("y");
  });

  it("converts null in onValueChange to empty string", () => {
    const setter = vi.fn();
    const handler = selectChangeHandler(setter) as {
      onValueChange: (v: string | null) => void;
      onChange: (e: { target?: { value: string } } | string) => void;
    };

    handler.onValueChange(null);

    expect(setter).toHaveBeenCalledWith("");
  });

  it("handles a raw string passed to onChange", () => {
    const setter = vi.fn();
    const handler = selectChangeHandler(setter) as {
      onValueChange: (v: string | null) => void;
      onChange: (e: { target?: { value: string } } | string) => void;
    };

    handler.onChange("z");

    expect(setter).toHaveBeenCalledWith("z");
  });
});
