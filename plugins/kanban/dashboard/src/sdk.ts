/**
 * Kanban dashboard plugin — SDK access layer.
 *
 * The plugin builds as an IIFE and receives React + UI components + utilities
 * from the host dashboard at runtime via ``window.__HERMES_PLUGIN_SDK__``.
 * This module centralises all SDK access so individual components don't
 * reach into the global directly.
 *
 * Type declarations are inline (not imported from web/src/plugins/sdk.d.ts)
 * to keep the plugin self-contained and avoid coupling to the host's
 * internal module layout.
 */

import type * as React from "react";

// ── Local type surface (mirrors web/src/plugins/sdk.d.ts) ──────────────────

export type FetchJSON = <T = unknown>(
  url: string,
  init?: RequestInit,
  options?: { allowUnauthorized?: boolean },
) => Promise<T>;

export type AuthedFetch = (url: string, init?: RequestInit) => Promise<Response>;

export type BuildWsUrl = (
  path: string,
  params?: Record<string, string>,
) => Promise<string>;

// React types are available via `@types/react` (devDependency). We import
// the namespace for type references only — the actual React runtime comes
// from the SDK at runtime, not from a bundle import.
/* eslint-disable @typescript-eslint/no-explicit-any */

type ReactLike = any;
type ComponentType = React.ComponentType<Record<string, never>>;

export interface HermesPluginSDK {
  readonly sdkVersion: string;
  React: ReactLike;
  hooks: {
    useState: typeof React.useState;
    useEffect: typeof React.useEffect;
    useCallback: typeof React.useCallback;
    useMemo: typeof React.useMemo;
    useRef: typeof React.useRef;
    useContext: typeof React.useContext;
    createContext: typeof React.createContext;
  };
  api: Record<string, (...args: never[]) => unknown>;
  fetchJSON: FetchJSON;
  authedFetch: AuthedFetch;
  buildWsUrl: BuildWsUrl;
  buildWsAuthParam: () => Promise<[string, string]>;
  components: Record<string, ComponentType>;
  utils: {
    cn: (...classes: Array<string | false | null | undefined>) => string;
    timeAgo: (ts: number) => string;
    isoTimeAgo: (iso: string) => string;
  };
  useI18n: () => unknown;
}

// Augment Window with the plugin SDK globals.
declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__?: HermesPluginSDK;
    __HERMES_PLUGINS__?: {
      register: (name: string, component: React.ComponentType) => void;
      registerSlot: (slot: string, name: string, component: React.ComponentType) => void;
    };
  }
}

// ── SDK access ──────────────────────────────────────────────────────────────

/** Get the SDK (or throw if missing). */
function sdk(): HermesPluginSDK {
  const s = window.__HERMES_PLUGIN_SDK__;
  if (!s) throw new Error("Hermes Plugin SDK not available");
  return s;
}

/** React — use instead of importing/bundling react. */
export function getReact(): HermesPluginSDK["React"] {
  return sdk().React;
}

/** React hooks. */
export function getHooks() {
  return sdk().hooks;
}

/** UI components from the host design system. */
export function getComponents() {
  return sdk().components;
}

/** Utility functions (cn, timeAgo, isoTimeAgo). */
export function getUtils() {
  return sdk().utils;
}

/** fetchJSON — handles auth in both modes (loopback / gated). */
export function getFetchJSON(): FetchJSON {
  return sdk().fetchJSON;
}

/** authedFetch — for non-JSON (FormData uploads, blob downloads). */
export function getAuthedFetch(): AuthedFetch {
  return sdk().authedFetch;
}

/** buildWsUrl — builds an auth'd WebSocket URL for the active mode. */
export function getBuildWsUrl(): BuildWsUrl {
  return sdk().buildWsUrl;
}

/** useI18n hook (with fallback shim). */
export function getUseI18n() {
  return sdk().useI18n;
}

/** The SDK's Checkbox component, or a native shim for older hosts. */
export function getCheckbox() {
  const SDK = sdk();
  const Checkbox = SDK.components.Checkbox;
  if (Checkbox) return Checkbox;
  // Fallback native <input type="checkbox"> shim.
  const { React } = SDK;
  return function CheckboxShim(props: Record<string, unknown>) {
    const { checked, onCheckedChange, className, onClick, ...rest } = props;
    return React.createElement("input", {
      type: "checkbox",
      checked: !!checked,
      className,
      onClick,
      onChange: function (e: React.ChangeEvent<HTMLInputElement>) {
        if (onCheckedChange) (onCheckedChange as (v: boolean) => void)(e.target.checked);
      },
      ...rest,
    });
  };
}

/**
 * The SDK's Select fires ``onValueChange(value)`` directly (shadcn-style).
 * Older code may call ``onChange({target: {value}})``. This helper wires
 * both signatures so a setter works with either API.
 */
export function selectChangeHandler(setter: (v: string) => void): Record<string, unknown> {
  return {
    onValueChange: function (v: string | null) {
      setter(v == null ? "" : v);
    },
    onChange: function (e: { target?: { value: string } } | string) {
      const v = e && typeof e === "object" && e.target ? e.target.value : e;
      setter(v == null ? "" : (v as string));
    },
  };
}

/** Always append ?board=<slug> when we have one picked (including "default"). */
export function withBoard(url: string, board: string | null): string {
  if (!board) return url;
  const sep = url.indexOf("?") >= 0 ? "&" : "?";
  return `${url}${sep}board=${encodeURIComponent(board)}`;
}

/** Read the user's selected board from localStorage. */
export function readSelectedBoard(): string | null {
  try {
    const v = window.localStorage.getItem("hermes.kanban.selectedBoard");
    return (v || "").trim() || null;
  } catch {
    return null;
  }
}

/** Persist the user's selected board to localStorage. */
export function writeSelectedBoard(slug: string | null): void {
  try {
    if (slug) window.localStorage.setItem("hermes.kanban.selectedBoard", slug);
    else window.localStorage.removeItem("hermes.kanban.selectedBoard");
  } catch {
    /* ignore quota / private mode */
  }
}