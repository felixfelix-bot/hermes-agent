/**
 * Kanban dashboard plugin — task drawer (right-side detail panel).
 *
 * Shows a single task's full detail: title, metadata, status actions,
 * diagnostics, home-channel notifications, description, dependencies,
 * result, attachments, comments, events, worker log, and run history.
 *
 * All API calls use SDK.fetchJSON + withBoard (adds ?board=<slug>).
 * File upload/download use SDK.authedFetch (multipart / blob).
 *
 * This is a TypeScript port of the IIFE bundle's TaskDrawer and all
 * its sub-components (lines ~2783–3866 in dist/index.js).
 */
import * as React from "react";

import {
  tx,
  withBoard,
  parseApiErrorMessage,
  selectChangeHandler,
  withCompletionSummary,
  renderMarkdown,
  fmtBytes,
} from "../api";
import {
  API_BASE,
  COLUMN_DOT,
  type TaskDetail,
  type Task,
  type Comment,
  type TaskEvent,
  type Attachment,
  type TaskRun,
  type TaskLinks,
  type HomeChannel,
  type Diagnostic,
  type DiagnosticAction,
} from "../types";

// ── SDK ──

interface HermesSDK {
  React: typeof import("react");
  hooks: {
    useState: typeof import("react").useState;
    useEffect: typeof import("react").useEffect;
    useCallback: typeof import("react").useCallback;
    useMemo: typeof import("react").useMemo;
    useRef: typeof import("react").useRef;
  };
  components: {
    Card: React.ComponentType<any>;
    CardContent: React.ComponentType<any>;
    Badge: React.ComponentType<any>;
    Button: React.ComponentType<any>;
    Input: React.ComponentType<any>;
    Label: React.ComponentType<any>;
    Select: React.ComponentType<any>;
    SelectOption: React.ComponentType<any>;
    Checkbox?: React.ComponentType<any>;
  };
  utils: {
    cn: (...c: Array<string | false | null | undefined>) => string;
    timeAgo: (ts: number) => string;
  };
  useI18n: () => { t: Record<string, unknown>; locale: string };
  fetchJSON: <T = unknown>(url: string, init?: RequestInit) => Promise<T>;
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
}

const SDK = (function () {
  const s = (window as unknown as { __HERMES_PLUGIN_SDK__?: HermesSDK }).__HERMES_PLUGIN_SDK__;
  if (!s) throw new Error("Plugin SDK not available");
  return s;
})();

const h = SDK.React.createElement;
const { useState, useEffect, useCallback, useRef } = SDK.hooks;
const { Button, Input, Select, SelectOption } = SDK.components;
const { cn, timeAgo } = SDK.utils;
const useI18n = SDK.useI18n || (() => ({ t: { kanban: null }, locale: "en" }));

const API = API_BASE;

// ── Destructive confirm / diagnostic helpers ──

const DESTRUCTIVE_KEYS: Record<string, string> = {
  done: "confirmDone",
  archived: "confirmArchive",
  blocked: "confirmBlocked",
};

const FALLBACK_DESTRUCTIVE: Record<string, string> = {
  done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
  archived: "Archive this task? It disappears from the default board view.",
  blocked: "Mark this task as blocked? The worker's claim is released.",
};

const DIAGNOSTIC_EVENT_KIND_KEYS: Record<string, string> = {
  completion_blocked_hallucination: "completionBlockedHallucination",
  suspected_hallucinated_references: "suspectedHallucinatedReferences",
};

const FALLBACK_DIAGNOSTIC_EVENT_LABELS: Record<string, string> = {
  completion_blocked_hallucination: "⚠ Completion blocked — phantom card ids",
  suspected_hallucinated_references: "⚠ Prose referenced phantom card ids",
};

function getDestructiveConfirm(t: Record<string, unknown> | null | undefined, status: string): string | null {
  const key = DESTRUCTIVE_KEYS[status];
  if (!key) return null;
  return tx(t, key, FALLBACK_DESTRUCTIVE[status] || "");
}

function isDiagnosticEvent(kind: string): boolean {
  return Object.prototype.hasOwnProperty.call(FALLBACK_DIAGNOSTIC_EVENT_LABELS, kind);
}

function getDiagnosticEventLabel(t: Record<string, unknown> | null | undefined, kind: string): string | null {
  const key = DIAGNOSTIC_EVENT_KIND_KEYS[kind];
  if (!key) return null;
  return tx(t, key, FALLBACK_DIAGNOSTIC_EVENT_LABELS[kind]);
}

function phantomIdsFromEvent(ev: { payload?: Record<string, unknown> | null }): string[] {
  if (!ev || !ev.payload) return [];
  const p = ev.payload;
  const phantom = (p.phantom_cards || p.phantom_refs) as string[] | undefined;
  return Array.isArray(phantom) ? phantom : [];
}

// ── MarkdownBlock ──

function MarkdownBlock(props: { source: string; enabled?: boolean }): React.ReactElement {
  const enabled = props.enabled !== false;
  if (!enabled) {
    return h("pre", { className: "hermes-kanban-pre" }, props.source || "");
  }
  return h("div", {
    className: "hermes-kanban-md",
    dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || "") },
  });
}

// ── MetaRow ──

function MetaRow(props: { label: string; value: React.ReactNode }): React.ReactElement {
  return h("div", { className: "hermes-kanban-meta-row" },
    h("span", { className: "hermes-kanban-meta-label" }, props.label),
    h("span", { className: "hermes-kanban-meta-value" }, props.value),
  );
}

// ── TitleEditor ──

function TitleEditor(props: {
  initial: string;
  onSave: (title: string) => void;
  onCancel: () => void;
}): React.ReactElement {
  const { t } = useI18n();
  const [v, setV] = useState(props.initial);
  const save = function () {
    const trimmed = v.trim();
    if (!trimmed) return;
    props.onSave(trimmed);
  };
  return h("div", { className: "hermes-kanban-edit-row" },
    h(Input, {
      value: v, autoFocus: true,
      onChange: function (e: { target: { value: string } }) { setV(e.target.value); },
      onKeyDown: function (e: { key: string; preventDefault: () => void }) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") props.onCancel();
      },
      className: "h-8 text-sm flex-1",
    }),
    h(Button, { onClick: save, size: "sm" }, tx(t, "save", "Save")),
    h(Button, { onClick: props.onCancel, size: "sm" }, tx(t, "cancel", "Cancel")),
  );
}

// ── AssigneeEditor ──

function AssigneeEditor(props: {
  task: Task;
  onPatch: (patch: Record<string, unknown>, opts?: { confirm?: string }) => Promise<void>;
}): React.ReactElement {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [v, setV] = useState(props.task.assignee || "");
  useEffect(function () { setV(props.task.assignee || ""); }, [props.task.assignee]);
  if (!editing) {
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
      h("span", {
        className: "hermes-kanban-meta-value hermes-kanban-editable",
        onClick: function () { setEditing(true); },
        title: tx(t, "clickToEditAssignee", "Click to edit assignee"),
      }, props.task.assignee || tx(t, "unassigned", "unassigned")),
    );
  }
  const save = function () {
    props.onPatch({ assignee: v.trim() || "" }).then(function () { setEditing(false); });
  };
  return h("div", { className: "hermes-kanban-meta-row" },
    h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
    h(Input, {
      value: v, autoFocus: true,
      onChange: function (e: { target: { value: string } }) { setV(e.target.value); },
      onKeyDown: function (e: { key: string; preventDefault: () => void }) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") setEditing(false);
      },
      placeholder: tx(t, "emptyAssignee", "(empty = unassign)"),
      className: "h-7 text-xs flex-1",
      style: { textTransform: "none" },
      autoCapitalize: "none",
      autoCorrect: "off",
      spellCheck: false,
    }),
  );
}

// ── PriorityEditor ──

function PriorityEditor(props: {
  task: Task;
  onPatch: (patch: Record<string, unknown>, opts?: { confirm?: string }) => Promise<void>;
}): React.ReactElement {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [v, setV] = useState(String(props.task.priority || 0));
  useEffect(function () { setV(String(props.task.priority || 0)); }, [props.task.priority]);
  if (!editing) {
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
      h("span", {
        className: "hermes-kanban-meta-value hermes-kanban-editable",
        onClick: function () { setEditing(true); },
        title: tx(t, "clickToEdit", "Click to edit"),
      }, String(props.task.priority)),
    );
  }
  const save = function () {
    props.onPatch({ priority: Number(v) || 0 }).then(function () { setEditing(false); });
  };
  return h("div", { className: "hermes-kanban-meta-row" },
    h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
    h(Input, {
      type: "number", value: v, autoFocus: true,
      onChange: function (e: { target: { value: string } }) { setV(e.target.value); },
      onKeyDown: function (e: { key: string; preventDefault: () => void }) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") setEditing(false);
      },
      className: "h-7 text-xs w-20",
    }),
  );
}

// ── BodyEditor ──

function BodyEditor(props: {
  task: Task;
  renderMarkdown: boolean;
  onPatch: (patch: Record<string, unknown>, opts?: { confirm?: string }) => Promise<void>;
}): React.ReactElement {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [v, setV] = useState(props.task.body || "");
  useEffect(function () { setV(props.task.body || ""); }, [props.task.body]);
  const save = function () {
    props.onPatch({ body: v }).then(function () { setEditing(false); });
  };
  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head-row" },
      h("span", { className: "hermes-kanban-section-head" }, tx(t, "description", "Description")),
      editing
        ? h("div", { className: "flex gap-1" },
            h(Button, { onClick: save, size: "sm" }, tx(t, "save", "Save")),
            h(Button, {
              onClick: function () { setEditing(false); setV(props.task.body || ""); },
              size: "sm",
            }, tx(t, "cancel", "Cancel")),
          )
        : h("button", {
            type: "button",
            onClick: function () { setEditing(true); },
            className: "hermes-kanban-edit-link",
            title: "Edit description",
          }, tx(t, "edit", "edit")),
    ),
    editing
      ? h("textarea", {
          className: "hermes-kanban-textarea",
          value: v,
          rows: 8,
          onChange: function (e: { target: { value: string } }) { setV(e.target.value); },
        })
      : props.task.body
        ? h(MarkdownBlock, { source: props.task.body, enabled: props.renderMarkdown })
        : h("div", { className: "text-xs text-muted-foreground italic" },
            tx(t, "noDescription", "— no description —")),
  );
}

// ── DependencyEditor ──

function DependencyEditor(props: {
  task: Task;
  links: TaskLinks;
  allTasks: Task[];
  onAddParent: (id: string) => Promise<void>;
  onRemoveParent: (id: string) => Promise<void>;
  onAddChild: (id: string) => Promise<void>;
  onRemoveChild: (id: string) => Promise<void>;
}): React.ReactElement {
  const { t } = useI18n();
  const { task, links, allTasks } = props;
  const [newParent, setNewParent] = useState("");
  const [newChild, setNewChild] = useState("");
  // Filter out self + existing links when offering the "add" dropdown.
  const candidatesFor = function (excludeSet: Set<string>): Task[] {
    return (allTasks || []).filter(function (tk) {
      return tk.id !== task.id && !excludeSet.has(tk.id);
    });
  };
  const parentExclude = new Set([task.id, ...(links.parents || [])]);
  const childExclude = new Set([task.id, ...(links.children || [])]);

  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head" }, tx(t, "dependencies", "Dependencies")),
    h("div", { className: "hermes-kanban-deps-row" },
      h("span", { className: "hermes-kanban-deps-label" }, tx(t, "parents", "Parents:")),
      h("div", { className: "hermes-kanban-deps-chips" },
        (links.parents || []).length === 0
          ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
          : (links.parents || []).map(function (id) {
              return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                id,
                h("button", {
                  type: "button",
                  className: "hermes-kanban-dep-chip-x",
                  onClick: function () { props.onRemoveParent(id); },
                  title: tx(t, "removeDependency", "Remove dependency"),
                }, "×"),
              );
            }),
      ),
    ),
    h("div", { className: "hermes-kanban-deps-row" },
      h(Select, Object.assign({
        value: newParent,
        className: "h-7 text-xs flex-1",
      }, selectChangeHandler(setNewParent)),
        h(SelectOption, { value: "" }, tx(t, "addParent", "— add parent —")),
        candidatesFor(parentExclude).map(function (tk) {
          return h(SelectOption, { key: tk.id, value: tk.id },
            `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
        }),
      ),
      h(Button, {
        onClick: function () {
          if (!newParent) return;
          props.onAddParent(newParent).then(function () { setNewParent(""); });
        },
        disabled: !newParent,
        size: "sm",
      }, "+ parent"),
    ),
    h("div", { className: "hermes-kanban-deps-row" },
      h("span", { className: "hermes-kanban-deps-label" }, tx(t, "children", "Children:")),
      h("div", { className: "hermes-kanban-deps-chips" },
        (links.children || []).length === 0
          ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
          : (links.children || []).map(function (id) {
              return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                id,
                h("button", {
                  type: "button",
                  className: "hermes-kanban-dep-chip-x",
                  onClick: function () { props.onRemoveChild(id); },
                  title: tx(t, "removeDependency", "Remove dependency"),
                }, "×"),
              );
            }),
      ),
    ),
    h("div", { className: "hermes-kanban-deps-row" },
      h(Select, Object.assign({
        value: newChild,
        className: "h-7 text-xs flex-1",
      }, selectChangeHandler(setNewChild)),
        h(SelectOption, { value: "" }, tx(t, "addChild", "— add child —")),
        candidatesFor(childExclude).map(function (tk) {
          return h(SelectOption, { key: tk.id, value: tk.id },
            `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
        }),
      ),
      h(Button, {
        onClick: function () {
          if (!newChild) return;
          props.onAddChild(newChild).then(function () { setNewChild(""); });
        },
        disabled: !newChild,
        size: "sm",
      }, "+ child"),
    ),
  );
}

// ── StatusActions ──

function StatusActions(props: {
  task: Task;
  onPatch: (patch: Record<string, unknown>, opts?: { confirm?: string }) => Promise<void>;
  onSpecify?: () => Promise<any>;
  onDecompose?: () => Promise<any>;
}): React.ReactElement {
  const { t } = useI18n();
  const task = props.task;
  const [specifyBusy, setSpecifyBusy] = useState(false);
  const [specifyMsg, setSpecifyMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [decomposeBusy, setDecomposeBusy] = useState(false);
  const [decomposeMsg, setDecomposeMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const b = function (label: string, patch: Record<string, unknown>, enabled: boolean | undefined, confirmMsg?: string | null) {
    return h(Button, {
      onClick: function () { if (enabled !== false) props.onPatch(patch, { confirm: confirmMsg || undefined }); },
      disabled: enabled === false,
      size: "sm",
    }, label);
  };

  // "Specify" appears only when the task is in the Triage column.
  const specifyButton = (task.status === "triage" && props.onSpecify)
    ? h(Button, {
        onClick: function () {
          if (specifyBusy) return;
          setSpecifyBusy(true);
          setSpecifyMsg(null);
          props.onSpecify!().then(function (res: any) {
            if (res && res.ok) {
              const suffix = res.new_title ? ` — retitled: ${res.new_title}` : "";
              setSpecifyMsg({ ok: true, text: `Specified${suffix}` });
            } else {
              setSpecifyMsg({
                ok: false,
                text: "Specify failed: " + ((res && res.reason) || "unknown error"),
              });
            }
          }).catch(function (err: Error) {
            setSpecifyMsg({
              ok: false,
              text: "Specify failed: " + (err.message || String(err)),
            });
          }).then(function () {
            setSpecifyBusy(false);
          });
        },
        disabled: specifyBusy,
        size: "sm",
      }, specifyBusy ? "Specifying…" : "✨ Specify")
    : null;

  // "Decompose" is the built-in decomposer fan-out.
  const decomposeButton = (task.status === "triage" && props.onDecompose)
    ? h(Button, {
        onClick: function () {
          if (decomposeBusy) return;
          setDecomposeBusy(true);
          setDecomposeMsg(null);
          props.onDecompose!().then(function (res: any) {
            if (res && res.ok) {
              if (res.fanout && res.child_ids && res.child_ids.length) {
                setDecomposeMsg({
                  ok: true,
                  text: `Decomposed into ${res.child_ids.length} children: ${res.child_ids.join(", ")}`,
                });
              } else {
                const suffix = res.new_title ? ` — retitled: ${res.new_title}` : "";
                setDecomposeMsg({ ok: true, text: `Single task (no fanout)${suffix}` });
              }
            } else {
              setDecomposeMsg({
                ok: false,
                text: "Decompose failed: " + ((res && res.reason) || "unknown error"),
              });
            }
          }).catch(function (err: Error) {
            setDecomposeMsg({
              ok: false,
              text: "Decompose failed: " + (err.message || String(err)),
            });
          }).then(function () {
            setDecomposeBusy(false);
          });
        },
        disabled: decomposeBusy,
        size: "sm",
      }, decomposeBusy ? "Decomposing…" : "⚗ Decompose")
    : null;

  return h("div", null,
    h("div", { className: "hermes-kanban-actions" },
      specifyButton,
      decomposeButton,
      b("→ triage", { status: "triage" }, task.status !== "triage"),
      b("→ ready", { status: "ready" }, task.status !== "ready"),
      // No direct → running button: /tasks/:id PATCH rejects status=running
      // with 400 (issue #19535). Tasks enter running only through the
      // dispatcher's claim_task path.
      b(tx(t, "block", "Block"), { status: "blocked" },
        task.status === "running" || task.status === "ready",
        getDestructiveConfirm(t, "blocked")),
      b(tx(t, "unblock", "Unblock"), { status: "ready" }, task.status === "blocked"),
      b(tx(t, "complete", "Complete"), { status: "done" },
        task.status === "running" || task.status === "ready" || task.status === "blocked",
        getDestructiveConfirm(t, "done")),
      b(tx(t, "archive", "Archive"), { status: "archived" }, task.status !== "archived",
        getDestructiveConfirm(t, "archived")),
    ),
    specifyMsg ? h("div", {
      className: specifyMsg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err",
    }, specifyMsg.text) : null,
    decomposeMsg ? h("div", {
      className: decomposeMsg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err",
    }, decomposeMsg.text) : null,
  );
}

// ── HomeSubsSection ──

function HomeSubsSection(props: {
  homeChannels: HomeChannel[];
  homeBusy: Record<string, boolean>;
  onToggle?: (platform: string, currentlySubscribed: boolean) => void;
}): React.ReactElement | null {
  const { t } = useI18n();
  const channels = props.homeChannels || [];
  if (channels.length === 0) return null;
  const busy = props.homeBusy || {};
  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head" },
      tx(t, "notifyHomeChannels", "Notify home channels")),
    h("div", { className: "hermes-kanban-home-subs" },
      channels.map(function (hc) {
        const isBusy = !!busy[hc.platform];
        const label = hc.subscribed ? "✓ " + hc.platform : hc.platform;
        const target = `${hc.name} (${hc.chat_id}${hc.thread_id ? " / " + hc.thread_id : ""})`;
        const title = hc.subscribed
          ? `${tx(t, "sendingUpdates", "Sending updates to")} ${target}. Click to stop.`
          : `${tx(t, "sendNotifications", "Send completed / blocked / gave_up notifications to")} ${target}.`;
        return h(Button, {
          key: hc.platform,
          size: "sm",
          title: title,
          disabled: isBusy || !props.onToggle,
          onClick: function () {
            if (props.onToggle) props.onToggle(hc.platform, hc.subscribed);
          },
          className: hc.subscribed
            ? "hermes-kanban-home-sub hermes-kanban-home-sub--on"
            : "hermes-kanban-home-sub",
        }, label);
      }),
    ),
  );
}

// ── AttachmentsSection ──

function AttachmentsSection(props: {
  attachments: Attachment[];
  boardSlug: string | null;
  onUpload?: (files: FileList) => void;
  onDelete?: (id: number) => void;
  uploadBusy: boolean;
  uploadErr: string | null;
  i18n: Record<string, unknown>;
}): React.ReactElement {
  const i18n = props.i18n;
  const atts = props.attachments || [];
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [dlErr, setDlErr] = useState<string | null>(null);

  function downloadAttachment(a: Attachment) {
    const url = withBoard(`${API}/attachments/${a.id}`, props.boardSlug);
    setDlErr(null);
    SDK.authedFetch(url)
      .then(function (resp: Response) {
        if (!resp.ok) {
          return resp.text().then(function (txt: string) {
            throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
          });
        }
        return resp.blob();
      })
      .then(function (blob: Blob) {
        const objUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objUrl;
        link.download = a.filename || "attachment";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(objUrl); }, 10000);
      })
      .catch(function (e: Error) { setDlErr(String(e.message || e)); });
  }

  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head" },
      `${tx(i18n, "attachments", "Attachments")} (${atts.length})`),
    h("input", {
      ref: function (el: HTMLInputElement | null) { fileRef.current = el; },
      type: "file",
      multiple: true,
      style: { display: "none" },
      onChange: function (e: { target: HTMLInputElement }) {
        if (props.onUpload && e.target.files) props.onUpload(e.target.files);
        // Reset so selecting the same file again re-triggers onChange.
        try { e.target.value = ""; } catch (_e) { /* ignore */ }
      },
    }),
    h("div", { className: "flex items-center gap-2 mb-2" },
      h(Button, {
        size: "sm",
        variant: "outline",
        disabled: !!props.uploadBusy,
        onClick: function () { if (fileRef.current) fileRef.current.click(); },
      }, props.uploadBusy
        ? tx(i18n, "uploading", "Uploading…")
        : tx(i18n, "uploadFile", "Upload file")),
    ),
    (props.uploadErr || dlErr)
      ? h("div", { className: "text-xs text-destructive mb-2" }, props.uploadErr || dlErr)
      : null,
    atts.length === 0
      ? h("div", { className: "text-xs text-muted-foreground" },
          tx(i18n, "noAttachments", "— no attachments —"))
      : atts.map(function (a: Attachment) {
          return h("div", {
            key: a.id,
            className: "flex items-center justify-between gap-2 py-1 text-sm",
          },
            h("button", {
              type: "button",
              className: "hermes-kanban-attachment-link truncate",
              title: a.filename,
              onClick: function () { downloadAttachment(a); },
            }, a.filename),
            h("span", { className: "text-xs text-muted-foreground whitespace-nowrap" },
              fmtBytes(a.size)),
            h("button", {
              type: "button",
              className: "hermes-kanban-drawer-close",
              title: tx(i18n, "removeAttachment", "Remove attachment"),
              onClick: function () {
                if (window.confirm(tx(i18n, "confirmRemoveAttachment", "Remove this attachment?"))) {
                  if (props.onDelete) props.onDelete(a.id);
                }
              },
            }, "×"),
          );
        }),
  );
}

// ── RunHistorySection ──

function RunHistorySection(props: { runs: TaskRun[] }): React.ReactElement | null {
  const { t } = useI18n();
  const runs = props.runs || [];
  const [expanded, setExpanded] = useState(false);
  if (runs.length === 0) return null;
  const showAll = expanded || runs.length <= 3;
  const visible = showAll ? runs : runs.slice(-3);

  const fmtElapsed = function (run: TaskRun): string {
    if (!run || !run.started_at) return "";
    const end = run.ended_at || Math.floor(Date.now() / 1000);
    const secs = Math.max(0, end - run.started_at);
    if (secs < 60) return `${secs}s`;
    if (secs < 3600) return `${Math.round(secs / 60)}m`;
    return `${(secs / 3600).toFixed(1)}h`;
  };

  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head-row" },
      h("span", { className: "hermes-kanban-section-head" },
        `${tx(t, "runHistory", "Run history")} (${runs.length})`),
      !showAll
        ? h("button", {
            type: "button",
            onClick: function () { setExpanded(true); },
            className: "hermes-kanban-edit-link",
            title: tx(t, "showAllAttempts", "Show all attempts"),
          }, `+${runs.length - 3} earlier`)
        : null,
    ),
    visible.map(function (r: TaskRun) {
      const outcomeClass = r.ended_at
        ? `hermes-kanban-run--${r.outcome || r.status || "ended"}`
        : "hermes-kanban-run--active";
      return h("div", { key: r.id, className: cn("hermes-kanban-run", outcomeClass) },
        h("div", { className: "hermes-kanban-run-head" },
          h("span", { className: "hermes-kanban-run-outcome" },
            r.ended_at ? (r.outcome || r.status || tx(t, "ended", "ended")) : tx(t, "active", "active")),
          h("span", { className: "hermes-kanban-run-profile" },
            r.profile ? `@${r.profile}` : tx(t, "noProfile", "(no profile)")),
          h("span", { className: "hermes-kanban-run-elapsed" }, fmtElapsed(r)),
          h("span", { className: "hermes-kanban-run-ago" },
            timeAgo ? timeAgo(r.started_at || 0) : ""),
        ),
        r.summary ? h("div", { className: "hermes-kanban-run-summary" }, r.summary) : null,
        r.error ? h("div", { className: "hermes-kanban-run-error" }, r.error) : null,
        (r.metadata && Object.keys(r.metadata).length > 0)
          ? (function () {
              var json = JSON.stringify(r.metadata, null, 2);
              var collapsed = json.length > 300;
              return h("details", {
                className: "hermes-kanban-run-meta-block",
                open: !collapsed,
              },
                h("summary", { className: "hermes-kanban-run-meta-label" }, "Metadata"),
                h("code", { className: "hermes-kanban-run-meta" }, json),
              );
            })()
          : null,
      );
    }),
  );
}

// ── WorkerLogSection ──

function WorkerLogSection(props: { taskId: string; boardSlug: string | null }): React.ReactElement {
  const { t } = useI18n();
  const [state, setState] = useState<{
    loading: boolean;
    data: { exists?: boolean; content?: string; size_bytes?: number; truncated?: boolean; path?: string } | null;
    err: string | null;
  }>({ loading: false, data: null, err: null });

  const load = useCallback(function () {
    setState({ loading: true, data: null, err: null });
    SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/log?tail=100000`, props.boardSlug))
      .then(function (d: any) { setState({ loading: false, data: d, err: null }); })
      .catch(function (e: Error) { setState({ loading: false, data: null, err: String(e.message || e) }); });
  }, [props.taskId, props.boardSlug]);

  useEffect(function () { load(); }, [load]);

  const data = state.data;
  let body: React.ReactNode;
  if (state.loading) {
    body = h("div", { className: "text-xs text-muted-foreground" },
      tx(t, "loadingLog", "Loading log…"));
  } else if (state.err) {
    body = h("div", { className: "text-xs text-destructive" }, state.err);
  } else if (!data || !data.exists) {
    body = h("div", { className: "text-xs text-muted-foreground italic" },
      tx(t, "noWorkerLog",
        "— no worker log yet (task hasn't spawned or log was rotated away) —"));
  } else {
    body = h("pre", { className: "hermes-kanban-pre hermes-kanban-log" },
      data.content || "(empty)");
  }

  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head-row" },
      h("span", { className: "hermes-kanban-section-head" },
        tx(t, "workerLog", "Worker log") + (data && data.size_bytes ? ` (${data.size_bytes} B)` : "")),
      h("button", {
        type: "button",
        onClick: load,
        className: "hermes-kanban-edit-link",
        title: "Refresh log",
      }, "refresh"),
    ),
    body,
    data && data.truncated
      ? h("div", { className: "text-xs text-muted-foreground" },
          tx(t, "logTruncated", "(showing last 100 KB — full log at "),
          data.path,
          tx(t, "logAt", ")"))
      : null,
  );
}

// ── DiagnosticActionButton ──

function DiagnosticActionButton(props: {
  action: DiagnosticAction;
  onExec: (action: DiagnosticAction) => void;
  busy: boolean;
  extra?: { copied?: boolean; disabled?: boolean };
}): React.ReactElement {
  const { t } = useI18n();
  const { action, onExec, busy, extra } = props;
  const label = (action.suggested ? "★ " : "") + action.label;
  const cls = cn(
    "hermes-kanban-diag-action-btn",
    action.suggested ? "hermes-kanban-diag-action-btn--suggested" : "",
  );
  if (action.kind === "reclaim" || action.kind === "reassign" || action.kind === "unblock") {
    return h("button", {
      className: cls,
      disabled: busy || (extra && extra.disabled),
      onClick: function () { onExec(action); },
      type: "button",
    }, label);
  }
  if (action.kind === "cli_hint") {
    return h("button", {
      className: cls,
      disabled: busy,
      onClick: function () { onExec(action); },
      type: "button",
      title: tx(t, "copyCommand", "Copy command to clipboard"),
    }, (extra && extra.copied) ? tx(t, "copied", "Copied") : label);
  }
  if (action.kind === "comment") {
    return h("button", {
      className: cls,
      onClick: function () { onExec(action); },
      type: "button",
    }, label);
  }
  if (action.kind === "open_docs") {
    return h("a", {
      className: cls,
      href: (action.payload && (action.payload as Record<string, unknown>).url as string) || "#",
      target: "_blank",
      rel: "noreferrer",
    }, label);
  }
  // Unknown kind — render informational, non-interactive.
  return h("span", { className: cls + " hermes-kanban-diag-action-btn--unknown" }, label);
}

// ── DiagnosticCard ──

function DiagnosticCard(props: {
  diag: Diagnostic;
  task: Task;
  boardSlug: string | null;
  assignees: string[];
  onRefresh?: () => void;
}): React.ReactElement {
  const { t } = useI18n();
  const { diag, task, boardSlug, assignees, onRefresh } = props;
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [reassignProfile, setReassignProfile] = useState(task.assignee || "");

  const execAction = function (action: DiagnosticAction) {
    if (busy) return;
    if (action.kind === "cli_hint") {
      const cmd = (action.payload && (action.payload as Record<string, unknown>).command as string) || action.label;
      const fallback = function () { window.prompt("Copy this command:", cmd); };
      try {
        const p = navigator.clipboard && navigator.clipboard.writeText(cmd);
        if (p && p.then) {
          p.then(function () {
            setCopiedKey(action.label);
            setTimeout(function () { setCopiedKey(null); }, 2000);
          }).catch(fallback);
        } else {
          fallback();
        }
      } catch (_) {
        fallback();
      }
      return;
    }
    if (action.kind === "comment") {
      const ta = document.querySelector(".hermes-kanban-drawer-comment-row input, .hermes-kanban-drawer-comment-row textarea") as HTMLElement | null;
      if (ta) {
        ta.scrollIntoView({ behavior: "smooth", block: "nearest" });
        ta.focus();
      }
      return;
    }
    if (action.kind === "unblock") {
      setBusy(true); setMsg(null);
      const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}`, boardSlug);
      SDK.fetchJSON(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "ready" }),
      }).then(function () {
        setMsg({ ok: true, text: tx(t, "unblockedMessage",
          "Unblocked {id}. Task is ready for the next tick.", { id: task.id }) });
        if (onRefresh) onRefresh();
      }).catch(function (err: Error) {
        setMsg({ ok: false, text: tx(t, "unblockFailed", "Unblock failed: ") + (err.message || String(err)) });
      }).then(function () { setBusy(false); });
      return;
    }
    if (action.kind === "reclaim") {
      setBusy(true); setMsg(null);
      const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reclaim`, boardSlug);
      SDK.fetchJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: `recovery action for ${diag.kind}` }),
      }).then(function () {
        setMsg({ ok: true, text: tx(t, "reclaimedMessage",
          "Reclaimed {id}. Task is back to ready.", { id: task.id }) });
        if (onRefresh) onRefresh();
      }).catch(function (err: Error) {
        setMsg({ ok: false, text: tx(t, "reclaimFailed", "Reclaim failed: ") + (err.message || String(err)) });
      }).then(function () { setBusy(false); });
      return;
    }
    if (action.kind === "reassign") {
      if (!reassignProfile) {
        setMsg({ ok: false, text: tx(t, "pickProfileFirst", "Pick a profile first.") });
        return;
      }
      setBusy(true); setMsg(null);
      const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reassign`, boardSlug);
      const body = {
        profile: reassignProfile || null,
        reclaim_first: !!(action.payload && (action.payload as Record<string, unknown>).reclaim_first),
        reason: `recovery action for ${diag.kind}`,
      };
      SDK.fetchJSON(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function () {
        setMsg({
          ok: true,
          text: tx(t, "reassignedMessage", "Reassigned {id} to {profile}.",
            { id: task.id, profile: reassignProfile }),
        });
        if (onRefresh) onRefresh();
      }).catch(function (err: Error) {
        setMsg({ ok: false, text: tx(t, "reassignFailed", "Reassign failed: ") + (err.message || String(err)) });
      }).then(function () { setBusy(false); });
      return;
    }
  };

  // Pull out the reassign action so we can render its picker inline.
  const reassignAction = (diag.actions || []).find(function (a) {
    return a.kind === "reassign";
  });

  const sevClass = "hermes-kanban-diag--" + (diag.severity || "warning");
  return h("div", { className: cn("hermes-kanban-diag", sevClass) },
    h("div", { className: "hermes-kanban-diag-header" },
      h("span", { className: "hermes-kanban-diag-sev" },
        diag.severity === "critical" ? "!!!" :
        diag.severity === "error" ? "!!" : "⚠"),
      h("span", { className: "hermes-kanban-diag-title" }, diag.title),
    ),
    h("div", { className: "hermes-kanban-diag-detail" }, diag.detail),
    diag.data && Object.keys(diag.data).length > 0
      ? h("div", { className: "hermes-kanban-diag-data" },
          Object.keys(diag.data).map(function (k) {
            const v = (diag.data as Record<string, unknown>)[k];
            if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string" &&
                (v[0] as string).indexOf("t_") === 0) {
              // Task-id list — render as chips.
              return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                (v as string[]).map(function (x) {
                  return h("code", {
                    key: x, className: "hermes-kanban-event-phantom-chip",
                  }, x);
                }),
              );
            }
            return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
              h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
              h("span", { className: "hermes-kanban-diag-data-val" },
                Array.isArray(v) ? (v as unknown[]).join(", ") : String(v)),
            );
          }),
        )
      : null,
    // Inline reassign picker — only shown when the diagnostic offers
    // a reassign action. Profile list comes from the board payload.
    reassignAction
      ? h("div", { className: "hermes-kanban-diag-reassign-row" },
          h("span", { className: "hermes-kanban-diag-reassign-label" },
            tx(t, "reassignTo", "Reassign to:")),
          h("select", {
            className: "hermes-kanban-recovery-select",
            value: reassignProfile,
            onChange: function (e: { target: { value: string } }) { setReassignProfile(e.target.value); },
          },
            h("option", { value: "" }, "(unassigned)"),
            (assignees || []).map(function (a) {
              return h("option", { key: a, value: a }, a);
            }),
          ),
        )
      : null,
    h("div", { className: "hermes-kanban-diag-actions" },
      (diag.actions || []).map(function (a, i) {
        return h(DiagnosticActionButton, {
          key: a.kind + String(i),
          action: a,
          onExec: execAction,
          busy: busy,
          extra: {
            copied: copiedKey === a.label,
            disabled: (a.kind === "reassign" && !reassignProfile),
          },
        });
      }),
    ),
    msg
      ? h("div", {
          className: cn(
            "hermes-kanban-diag-msg",
            msg.ok ? "hermes-kanban-diag-msg--ok" : "hermes-kanban-diag-msg--err",
          ),
        }, msg.text)
      : null,
  );
}

// ── DiagnosticsSection ──

function DiagnosticsSection(props: {
  task: Task;
  boardSlug: string | null;
  assignees: string[];
  diagnostics: Diagnostic[];
  onRefresh?: () => void;
  alwaysVisible?: boolean;
}): React.ReactElement | null {
  const { t } = useI18n();
  const diags = props.diagnostics || [];
  const hasOpenDiags = diags.length > 0;
  const [open, setOpen] = useState(hasOpenDiags);
  useEffect(function () {
    if (hasOpenDiags) setOpen(true);
  }, [hasOpenDiags]);
  if (!hasOpenDiags && !props.alwaysVisible) {
    return null;
  }
  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head-row" },
      h("span", { className: "hermes-kanban-section-head" },
        hasOpenDiags
          ? h("span", { className: "hermes-kanban-section-head-warning" },
              `⚠ ${tx(t, "diagnostics", "Diagnostics")} (${diags.length})`)
          : tx(t, "diagnostics", "Diagnostics"),
      ),
      h("button", {
        className: "hermes-kanban-section-toggle",
        onClick: function () { setOpen(function (x) { return !x; }); },
        type: "button",
      }, open ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
    ),
    open
      ? h("div", { className: "hermes-kanban-diag-list" },
          diags.map(function (d, i) {
            return h(DiagnosticCard, {
              key: props.task.id + ":" + d.kind + String(i),
              diag: d,
              task: props.task,
              boardSlug: props.boardSlug,
              assignees: props.assignees,
              onRefresh: props.onRefresh,
            });
          }),
        )
      : null,
  );
}

// ── TaskDetail ──

function TaskDetail(props: {
  data: TaskDetail;
  editing: boolean;
  setEditing: (v: boolean) => void;
  renderMarkdown: boolean;
  allTasks: Task[];
  assignees: string[];
  boardSlug: string | null;
  onPatch: (patch: Record<string, unknown>, opts?: { confirm?: string }) => Promise<void>;
  onSpecify: () => Promise<any>;
  onDecompose: () => Promise<any>;
  onAddParent: (id: string) => Promise<void>;
  onRemoveParent: (id: string) => Promise<void>;
  onAddChild: (id: string) => Promise<void>;
  onRemoveChild: (id: string) => Promise<void>;
  homeChannels: HomeChannel[];
  homeBusy: Record<string, boolean>;
  onToggleHomeSub: (platform: string, currentlySubscribed: boolean) => void;
  onRefresh: () => void;
  onUpload: (files: FileList) => void;
  onDeleteAttachment: (id: number) => Promise<void>;
  uploadBusy: boolean;
  uploadErr: string | null;
}): React.ReactElement {
  const { t: i18n } = useI18n();
  const t = props.data.task;
  const comments = props.data.comments || [];
  const events = props.data.events || [];
  const attachments = props.data.attachments || [];
  const links = props.data.links || { parents: [], children: [] };

  return h("div", { className: "hermes-kanban-drawer-body" },
    h("div", { className: "hermes-kanban-drawer-title" },
      h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[t.status]) }),
      props.editing
        ? h(TitleEditor, {
            initial: t.title || "",
            onSave: function (newTitle: string) {
              return props.onPatch({ title: newTitle }).then(function () { props.setEditing(false); });
            },
            onCancel: function () { props.setEditing(false); },
          })
        : h("span", {
            className: "hermes-kanban-drawer-title-text",
            title: tx(i18n, "clickToEdit", "Click to edit"),
            onClick: function () { props.setEditing(true); },
          }, t.title || tx(i18n, "untitled", "(untitled)")),
    ),
    h("div", { className: "hermes-kanban-drawer-meta" },
      h(MetaRow, { label: tx(i18n, "status", "Status"), value: t.status }),
      h(AssigneeEditor, { task: t, onPatch: props.onPatch }),
      h(PriorityEditor, { task: t, onPatch: props.onPatch }),
      t.tenant ? h(MetaRow, { label: tx(i18n, "tenant", "Tenant"), value: t.tenant }) : null,
      h(MetaRow, {
        label: tx(i18n, "workspace", "Workspace"),
        value: `${t.workspace_kind}${t.workspace_path ? ": " + t.workspace_path : ""}`,
      }),
      (t.skills && t.skills.length > 0) ? h(MetaRow, {
        label: tx(i18n, "skills", "Skills"),
        value: t.skills.join(", "),
      }) : null,
      t.goal_mode ? h(MetaRow, {
        label: tx(i18n, "goalMode", "Goal mode"),
        value: t.goal_max_turns ? `on (max ${t.goal_max_turns} turns)` : "on",
      }) : null,
      t.created_by ? h(MetaRow, { label: tx(i18n, "createdBy", "Created by"), value: t.created_by }) : null,
    ),
    h(StatusActions, {
      task: t,
      onPatch: props.onPatch,
      onSpecify: props.onSpecify,
      onDecompose: props.onDecompose,
    }),
    h(DiagnosticsSection, {
      task: t,
      boardSlug: props.boardSlug,
      assignees: props.assignees,
      diagnostics: t.diagnostics || [],
      onRefresh: props.onRefresh,
    }),
    h(HomeSubsSection, {
      homeChannels: props.homeChannels || [],
      homeBusy: props.homeBusy || {},
      onToggle: props.onToggleHomeSub,
    }),
    h(BodyEditor, {
      task: t,
      renderMarkdown: props.renderMarkdown,
      onPatch: props.onPatch,
    }),
    h(DependencyEditor, {
      task: t,
      links, allTasks: props.allTasks,
      onAddParent: props.onAddParent,
      onRemoveParent: props.onRemoveParent,
      onAddChild: props.onAddChild,
      onRemoveChild: props.onRemoveChild,
    }),
    t.result ? h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")),
      h(MarkdownBlock, { source: t.result, enabled: props.renderMarkdown }),
    ) : null,
    h(AttachmentsSection, {
      attachments: attachments,
      boardSlug: props.boardSlug,
      onUpload: props.onUpload,
      onDelete: props.onDeleteAttachment,
      uploadBusy: props.uploadBusy,
      uploadErr: props.uploadErr,
      i18n: i18n,
    }),
    h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        `${tx(i18n, "comments", "Comments")} (${comments.length})`),
      comments.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(i18n, "noComments", "— no comments —"))
        : comments.map(function (c: Comment) {
            return h("div", { key: c.id, className: "hermes-kanban-comment" },
              h("div", { className: "hermes-kanban-comment-head" },
                h("span", { className: "hermes-kanban-comment-author" }, c.author || "anon"),
                h("span", { className: "hermes-kanban-comment-ago" },
                  timeAgo ? timeAgo(c.created_at) : ""),
              ),
              h(MarkdownBlock, { source: c.body, enabled: props.renderMarkdown }),
            );
          }),
    ),
    h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        `${tx(i18n, "events", "Events")} (${events.length})`),
      events.slice().reverse().slice(0, 20).map(function (e: TaskEvent) {
        const isDiag = isDiagnosticEvent(e.kind);
        const phantoms = isDiag ? phantomIdsFromEvent(e) : [];
        return h("div", {
          key: e.id,
          className: cn(
            "hermes-kanban-event",
            isDiag ? "hermes-kanban-event--hallucination" : "",
          ),
        },
          isDiag
            ? h("div", { className: "hermes-kanban-event-header" },
                h("span", { className: "hermes-kanban-event-warning-icon" }, "⚠"),
                h("span", { className: "hermes-kanban-event-warning-label" },
                  getDiagnosticEventLabel(i18n, e.kind) || e.kind),
                h("span", { className: "hermes-kanban-event-ago" },
                  timeAgo ? timeAgo(e.created_at) : ""),
              )
            : h("div", { className: "hermes-kanban-event-header-plain" },
                h("span", { className: "hermes-kanban-event-kind" }, e.kind),
                h("span", { className: "hermes-kanban-event-ago" },
                  timeAgo ? timeAgo(e.created_at) : ""),
              ),
          isDiag && phantoms.length > 0
            ? h("div", { className: "hermes-kanban-event-phantom-row" },
                h("span", { className: "hermes-kanban-event-phantom-label" },
                  tx(i18n, "phantomIds", "Phantom ids:")),
                phantoms.map(function (pid) {
                  return h("code", {
                    key: pid,
                    className: "hermes-kanban-event-phantom-chip",
                  }, pid);
                }),
              )
            : null,
          e.payload && !isDiag
            ? h("code", { className: "hermes-kanban-event-payload" },
                JSON.stringify(e.payload))
            : null,
        );
      }),
    ),
    h(WorkerLogSection, { taskId: t.id, boardSlug: props.boardSlug }),
    h(RunHistorySection, { runs: props.data.runs || [] }),
  );
}

// ── TaskDrawer (main export) ──

export interface TaskDrawerProps {
  taskId: string;
  boardSlug: string | null;
  onClose: () => void;
  onRefresh: () => void;
  renderMarkdown: boolean;
  allTasks: Task[];
  assignees: string[];
  eventTick: number;
}

export function TaskDrawer(props: TaskDrawerProps): React.ReactElement {
  const { t } = useI18n();
  const [data, setData] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // Surface PATCH failures (e.g. 409 "parent not done") right next to
  // the drawer's action row — without it, the drawer's only error
  // surface (``err``) is hidden behind the loaded ``data`` and the
  // Ready/Block/Complete buttons feel like no-ops.  See #26744.
  const [, setPatchErr] = useState<string | null>(null);
  const [newComment, setNewComment] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Home-channel notification toggles. homeChannels is the list of platforms
  // the user has a /sethome on; each entry has a `subscribed` bool telling
  // us whether this task is currently subscribed via that platform's home.
  const [homeChannels, setHomeChannels] = useState<HomeChannel[]>([]);
  const [homeBusy, setHomeBusy] = useState<Record<string, boolean>>({});
  const boardSlug = props.boardSlug;

  const load = useCallback(function () {
    return SDK.fetchJSON<TaskDetail>(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug))
      .then(function (d: TaskDetail) { setData(d); setErr(null); setPatchErr(null); })
      .catch(function (e: Error) { setErr(String(e.message || e)); })
      .finally(function () { setLoading(false); });
  }, [props.taskId, boardSlug]);

  const loadHomeChannels = useCallback(function () {
    const qs = new URLSearchParams({ task_id: props.taskId });
    const url = withBoard(`${API}/home-channels?${qs}`, boardSlug);
    return SDK.fetchJSON<{ home_channels?: HomeChannel[] }>(url)
      .then(function (d) { setHomeChannels(d.home_channels || []); })
      .catch(function () { /* silent — endpoint optional on older gateways */ });
  }, [props.taskId, boardSlug]);

  // Reload when the WS stream reports new events for this task id
  useEffect(function () { load(); }, [load, props.eventTick]);
  useEffect(function () { loadHomeChannels(); }, [loadHomeChannels]);
  useEffect(function () {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape" && !editing) props.onClose(); }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [props.onClose, editing]);

  const handleComment = function () {
    const body = newComment.trim();
    if (!body) return;
    SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/comments`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    }).then(function () {
      setNewComment("");
      load();
      props.onRefresh();
    }).catch(function (e: Error) { setErr(String(e.message || e)); });
  };

  // File upload uses raw fetch (not SDK.fetchJSON, which JSON-encodes)
  // so the browser sets the multipart boundary. Auth rides the session
  // cookie + bearer token, matching the rest of the dashboard.
  const handleUpload = function (fileList: FileList) {
    const files = Array.prototype.slice.call(fileList || []) as File[];
    if (!files.length) return;
    setUploadBusy(true);
    setUploadErr(null);
    const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/attachments`, boardSlug);
    // Upload sequentially so a partial failure leaves a clear state.
    let chain: Promise<void> = Promise.resolve();
    files.forEach(function (f: File) {
      chain = chain.then(function () {
        const fd = new FormData();
        fd.append("file", f, f.name);
        return SDK.authedFetch(url, { method: "POST", body: fd })
          .then(function (resp: Response) {
            if (!resp.ok) {
              return resp.text().then(function (txt: string) {
                throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
              });
            }
          });
      });
    });
    chain.then(function () {
      load();
      props.onRefresh();
    }).catch(function (e: Error) {
      setUploadErr(String(e.message || e));
    }).finally(function () {
      setUploadBusy(false);
    });
  };

  const handleDeleteAttachment = function (attachmentId: number) {
    return SDK.fetchJSON(withBoard(`${API}/attachments/${attachmentId}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setUploadErr(String(e.message || e)); });
  };

  const doPatch = function (patch: Record<string, unknown>, opts?: { confirm?: string }): Promise<void> {
    if (opts && opts.confirm && !window.confirm(opts.confirm)) {
      return Promise.resolve();
    }
    const finalPatch = withCompletionSummary(patch, 1, t);
    if (!finalPatch) return Promise.resolve();
    setPatchErr(null);
    return SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(finalPatch),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setPatchErr(parseApiErrorMessage(e)); });
  };

  // Triage specifier — calls the auxiliary LLM to flesh out a rough
  // idea in the Triage column into a concrete spec (title + body with
  // goal, approach, acceptance criteria) and promotes it to todo.
  const doSpecify = function (): Promise<any> {
    return SDK.fetchJSON(
      withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/specify`, boardSlug),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    ).then(function (res: any) {
      load();
      props.onRefresh();
      return res;
    });
  };

  // POST /tasks/:id/decompose — fan a triage task out into a graph
  // of child tasks routed to specialist profiles by description.
  const doDecompose = function (): Promise<any> {
    return SDK.fetchJSON(
      withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/decompose`, boardSlug),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    ).then(function (res: any) {
      load();
      props.onRefresh();
      return res;
    });
  };

  const addLink = function (parentId: string): Promise<void> {
    return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parentId, child_id: props.taskId }),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setErr(String(e.message || e)); });
  };
  const removeLink = function (parentId: string): Promise<void> {
    const qs = new URLSearchParams({ parent_id: parentId, child_id: props.taskId });
    return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setErr(String(e.message || e)); });
  };
  const addChild = function (childId: string): Promise<void> {
    return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: props.taskId, child_id: childId }),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setErr(String(e.message || e)); });
  };
  const removeChild = function (childId: string): Promise<void> {
    const qs = new URLSearchParams({ parent_id: props.taskId, child_id: childId });
    return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: Error) { setErr(String(e.message || e)); });
  };

  const toggleHomeSubscription = function (platform: string, currentlySubscribed: boolean) {
    // Optimistic flip + busy flag to keep double-clicks idempotent.
    setHomeBusy(function (b) { return Object.assign({}, b, { [platform]: true }); });
    setHomeChannels(function (list) {
      return list.map(function (hc) {
        return hc.platform === platform
          ? Object.assign({}, hc, { subscribed: !currentlySubscribed })
          : hc;
      });
    });
    const method = currentlySubscribed ? "DELETE" : "POST";
    const url = withBoard(
      `${API}/tasks/${encodeURIComponent(props.taskId)}/home-subscribe/${encodeURIComponent(platform)}`,
      boardSlug,
    );
    return SDK.fetchJSON(url, { method: method })
      .then(function () { return loadHomeChannels(); })
      .catch(function (e: Error) {
        // Revert optimistic flip on failure.
        setHomeChannels(function (list) {
          return list.map(function (hc) {
            return hc.platform === platform
              ? Object.assign({}, hc, { subscribed: currentlySubscribed })
              : hc;
          });
        });
        setErr(String(e.message || e));
      })
      .finally(function () {
        setHomeBusy(function (b) {
          const next = Object.assign({}, b);
          delete next[platform];
          return next;
        });
      });
  };

  return h("div", { className: "hermes-kanban-drawer-shade", onClick: props.onClose },
    h("div", {
      className: "hermes-kanban-drawer",
      onClick: function (e: { stopPropagation: () => void }) { e.stopPropagation(); },
    },
      h("div", { className: "hermes-kanban-drawer-head" },
        h("span", { className: "text-xs text-muted-foreground" }, props.taskId),
        h("button", {
          type: "button",
          onClick: props.onClose,
          className: "hermes-kanban-drawer-close",
          title: tx(t, "close", "Close (Esc)"),
        }, "×"),
      ),
      loading ? h("div", { className: "p-4 text-sm text-muted-foreground" },
        tx(t, "loadingDetail", "Loading…")) :
      err ? h("div", { className: "p-4 text-sm text-destructive" }, err) :
      data ? h(TaskDetail, {
        data: data,
        editing: editing,
        setEditing: setEditing,
        renderMarkdown: props.renderMarkdown,
        allTasks: props.allTasks,
        assignees: props.assignees || [],
        boardSlug: boardSlug,
        onPatch: doPatch,
        onSpecify: doSpecify,
        onDecompose: doDecompose,
        onAddParent: addLink,
        onRemoveParent: removeLink,
        onAddChild: addChild,
        onRemoveChild: removeChild,
        homeChannels: homeChannels,
        homeBusy: homeBusy,
        onToggleHomeSub: toggleHomeSubscription,
        onRefresh: props.onRefresh,
        onUpload: handleUpload,
        onDeleteAttachment: handleDeleteAttachment,
        uploadBusy: uploadBusy,
        uploadErr: uploadErr,
      }) : null,
      data ? h("div", { className: "hermes-kanban-drawer-comment-row" },
        h(Input, {
          value: newComment,
          onChange: function (e: { target: { value: string } }) { setNewComment(e.target.value); },
          onKeyDown: function (e: { key: string; shiftKey: boolean; preventDefault: () => void }) {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault(); handleComment();
            }
          },
          placeholder: tx(t, "addComment", "Add a comment… (Enter to submit)"),
          className: "h-8 text-sm flex-1",
        }),
        h(Button, {
          onClick: handleComment,
          size: "sm",
        }, tx(t, "comment", "Comment")),
      ) : null,
    ),
  );
}

export default TaskDrawer;