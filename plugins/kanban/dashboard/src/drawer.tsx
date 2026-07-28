/**
 * Kanban dashboard plugin — TaskDrawer and all its sub-components.
 *
 * The right-side detail panel that opens when a task is selected. Shows
 * the full task detail (description, comments, events, runs, diagnostics,
 * worker log, attachments, dependencies, home-channel subscriptions) and
 * provides inline editing for status, assignee, priority, title, and body.
 *
 * All React + UI components are obtained at runtime from the host SDK via
 * ``window.__HERMES_PLUGIN_SDK__`` (see ``./sdk``). No React is bundled.
 */

import type {
  Task,
  TaskDetail as TaskDetailType,
  Comment,
  TaskEvent,
  Run,
  Diagnostic,
  DiagnosticAction,
  HomeChannel,
  TaskLinks,
} from "./types";
import {
  getReact,
  getHooks,
  getComponents,
  getUtils,
  getUseI18n,
  getFetchJSON,
  getAuthedFetch,
  selectChangeHandler,
  withBoard,
} from "./sdk";
import { API, COLUMN_DOT } from "./constants";
import {
  tx,
  parseApiErrorMessage,
  getDestructiveConfirm,
  getDiagnosticEventLabel,
  isDiagnosticEvent,
  phantomIdsFromEvent,
  withCompletionSummary,
} from "./i18n";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ── Runtime handles (resolved once at module load) ──────────────────────────

const ReactRuntime = getReact();
const { createElement: h } = ReactRuntime;

const hooks = getHooks();
const { useState, useEffect, useCallback, useRef } = hooks;

const components = getComponents();
const Button = components.Button as any;
const Input = components.Input as any;
const Select = components.Select as any;
const SelectOption = components.SelectOption as any;

const utils = getUtils();
const { cn, timeAgo } = utils;

const useI18n = getUseI18n() as () => { t: unknown };
const fetchJSON = getFetchJSON();
const authedFetch = getAuthedFetch();

// ── Markdown rendering (safe minimal inline renderer) ───────────────────────

function escapeHtml(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderInline(esc: string): string {
  return esc
    .replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(
      /\[([^]\n]+)\]\((https?:\/\/[^\\s)]+|mailto:[^\s)]+)\)/g,
      (_m, text, href) =>
        `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`,
    );
}

function renderMarkdown(src: string): string {
  if (!src) return "";
  const blocks: string[] = [];
  let working = String(src).replace(/```([\s\S]*?)```/g, (_m, code) => {
    blocks.push(code);
    return `\u0000CODE${blocks.length - 1}\u0000`;
  });
  const escaped = escapeHtml(working);
  const lines = escaped.split(/\r?\n/);
  const out: string[] = [];
  let inList = false;
  for (const raw of lines) {
    const line = raw;
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
    } else if (line.trim() === "") {
      out.push("");
    } else {
      out.push(`<p>${renderInline(line)}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  let html = out.join("\n");
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) =>
    `<pre class="hermes-kanban-md-code"><code>${escapeHtml(blocks[Number(i)])}</code></pre>`,
  );
  return html;
}

function MarkdownBlock(props: { source: string; enabled?: boolean }) {
  const enabled = props.enabled !== false;
  if (!enabled) {
    return h("pre", { className: "hermes-kanban-pre" }, props.source || "");
  }
  return h("div", {
    className: "hermes-kanban-md",
    dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || "") },
  });
}

// ── MetaRow ──────────────────────────────────────────────────────────────────

function MetaRow(props: { label: string; value: string }) {
  return h("div", { className: "hermes-kanban-meta-row" },
    h("span", { className: "hermes-kanban-meta-label" }, props.label),
    h("span", { className: "hermes-kanban-meta-value" }, props.value),
  );
}

// ── TitleEditor ──────────────────────────────────────────────────────────────

function TitleEditor(props: { initial: string; onSave: (v: string) => void; onCancel: () => void }) {
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
      onChange: function (e: any) { setV(e.target.value); },
      onKeyDown: function (e: any) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") props.onCancel();
      },
      className: "h-8 text-sm flex-1",
    }),
    h(Button, { onClick: save, size: "sm" }, tx(t, "save", "Save")),
    h(Button, { onClick: props.onCancel, size: "sm" }, tx(t, "cancel", "Cancel")),
  );
}

// ── AssigneeEditor ───────────────────────────────────────────────────────────

function AssigneeEditor(props: { task: Task; onPatch: (p: any) => Promise<void> }) {
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
      onChange: function (e: any) { setV(e.target.value); },
      onKeyDown: function (e: any) {
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

// ── PriorityEditor ───────────────────────────────────────────────────────────

function PriorityEditor(props: { task: Task; onPatch: (p: any) => Promise<void> }) {
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
      onChange: function (e: any) { setV(e.target.value); },
      onKeyDown: function (e: any) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") setEditing(false);
      },
      className: "h-7 text-xs w-20",
    }),
  );
}

// ── BodyEditor ────────────────────────────────────────────────────────────────

function BodyEditor(props: { task: Task; renderMarkdown: boolean; onPatch: (p: any) => Promise<void> }) {
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
          onChange: function (e: any) { setV(e.target.value); },
        })
      : props.task.body
        ? h(MarkdownBlock, { source: props.task.body, enabled: props.renderMarkdown })
        : h("div", { className: "text-xs text-muted-foreground italic" },
            tx(t, "noDescription", "\u2014 no description \u2014")),
  );
}

// ── DependencyEditor ──────────────────────────────────────────────────────────

function DependencyEditor(props: {
  task: Task;
  links: TaskLinks;
  allTasks: Task[];
  onAddParent: (id: string) => Promise<void>;
  onRemoveParent: (id: string) => Promise<void>;
  onAddChild: (id: string) => Promise<void>;
  onRemoveChild: (id: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const { task, links, allTasks } = props;
  const [newParent, setNewParent] = useState("");
  const [newChild, setNewChild] = useState("");
  const candidatesFor = function (excludeSet: Set<string>) {
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
                }, "\u00d7"),
              );
            }),
      ),
    ),
    h("div", { className: "hermes-kanban-deps-row" },
      h(Select, Object.assign({
        value: newParent,
        className: "h-7 text-xs flex-1",
      }, selectChangeHandler(setNewParent)),
        h(SelectOption, { value: "" }, tx(t, "addParent", "\u2014 add parent \u2014")),
        candidatesFor(parentExclude).map(function (tk) {
          return h(SelectOption, { key: tk.id, value: tk.id },
            `${tk.id} \u2014 ${(tk.title || "").slice(0, 50)}`);
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
                }, "\u00d7"),
              );
            }),
      ),
    ),
    h("div", { className: "hermes-kanban-deps-row" },
      h(Select, Object.assign({
        value: newChild,
        className: "h-7 text-xs flex-1",
      }, selectChangeHandler(setNewChild)),
        h(SelectOption, { value: "" }, tx(t, "addChild", "\u2014 add child \u2014")),
        candidatesFor(childExclude).map(function (tk) {
          return h(SelectOption, { key: tk.id, value: tk.id },
            `${tk.id} \u2014 ${(tk.title || "").slice(0, 50)}`);
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

// ── StatusActions ─────────────────────────────────────────────────────────────

function StatusActions(props: {
  task: Task;
  onPatch: (p: any, opts?: any) => Promise<void>;
  onSpecify?: () => Promise<any>;
  onDecompose?: () => Promise<any>;
}) {
  const { t } = useI18n();
  const task = props.task;
  const [specifyBusy, setSpecifyBusy] = useState(false);
  const [specifyMsg, setSpecifyMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [decomposeBusy, setDecomposeBusy] = useState(false);
  const [decomposeMsg, setDecomposeMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const b = function (label: string, patch: any, enabled: boolean | undefined, confirmMsg?: string | null) {
    return h(Button, {
      onClick: function () { if (enabled !== false) props.onPatch(patch, { confirm: confirmMsg }); },
      disabled: enabled === false,
      size: "sm",
    }, label);
  };

  const specifyButton = (task.status === "triage" && props.onSpecify)
    ? h(Button, {
        onClick: function () {
          if (specifyBusy) return;
          setSpecifyBusy(true);
          setSpecifyMsg(null);
          props.onSpecify!().then(function (res: any) {
            if (res && res.ok) {
              const suffix = res.new_title ? ` \u2014 retitled: ${res.new_title}` : "";
              setSpecifyMsg({ ok: true, text: `Specified${suffix}` });
            } else {
              setSpecifyMsg({ ok: false, text: "Specify failed: " + ((res && res.reason) || "unknown error") });
            }
          }).catch(function (err: any) {
            setSpecifyMsg({ ok: false, text: "Specify failed: " + (err.message || String(err)) });
          }).then(function () { setSpecifyBusy(false); });
        },
        disabled: specifyBusy,
        size: "sm",
      }, specifyBusy ? "Specifying\u2026" : "\u2728 Specify")
    : null;

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
                const suffix = res.new_title ? ` \u2014 retitled: ${res.new_title}` : "";
                setDecomposeMsg({ ok: true, text: `Single task (no fanout)${suffix}` });
              }
            } else {
              setDecomposeMsg({ ok: false, text: "Decompose failed: " + ((res && res.reason) || "unknown error") });
            }
          }).catch(function (err: any) {
            setDecomposeMsg({ ok: false, text: "Decompose failed: " + (err.message || String(err)) });
          }).then(function () { setDecomposeBusy(false); });
        },
        disabled: decomposeBusy,
        size: "sm",
      }, decomposeBusy ? "Decomposing\u2026" : "\u2697 Decompose")
    : null;

  return h("div", null,
    h("div", { className: "hermes-kanban-actions" },
      specifyButton,
      decomposeButton,
      b("\u2192 triage", { status: "triage" }, task.status !== "triage"),
      b("\u2192 ready", { status: "ready" }, task.status !== "ready"),
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

// ── DiagnosticActionButton ───────────────────────────────────────────────────

function DiagnosticActionButton(props: {
  action: DiagnosticAction;
  onExec: (a: DiagnosticAction) => void;
  busy: boolean;
  extra?: { copied?: boolean; disabled?: boolean };
}) {
  const { t } = useI18n();
  const { action, onExec, busy, extra } = props;
  const label = (action.suggested ? "\u2606 " : "") + action.label;
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
      href: (action.payload && action.payload.url) || "#",
      target: "_blank",
      rel: "noreferrer",
    }, label);
  }
  return h("span", { className: cls + " hermes-kanban-diag-action-btn--unknown" }, label);
}

// ── DiagnosticCard ────────────────────────────────────────────────────────────

function DiagnosticCard(props: {
  diag: Diagnostic;
  task: Task;
  boardSlug: string | null;
  assignees: string[];
  onRefresh?: () => void;
}) {
  const { t } = useI18n();
  const { diag, task, boardSlug, assignees, onRefresh } = props;
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [reassignProfile, setReassignProfile] = useState(task.assignee || "");

  const execAction = function (action: DiagnosticAction) {
    if (busy) return;
    if (action.kind === "cli_hint") {
      const cmd = (action.payload && action.payload.command) || action.label;
      const fallback = function () { window.prompt("Copy this command:", cmd); };
      try {
        const p = navigator.clipboard && navigator.clipboard.writeText(cmd);
        if (p && p.then) {
          p.then(function () {
            setCopiedKey(action.label);
            setTimeout(function () { setCopiedKey(null); }, 2000);
          }).catch(fallback);
        } else { fallback(); }
      } catch (_) { fallback(); }
      return;
    }
    if (action.kind === "comment") {
      const ta = document.querySelector(".hermes-kanban-drawer-comment-row input, .hermes-kanban-drawer-comment-row textarea") as any;
      if (ta) { ta.scrollIntoView({ behavior: "smooth", block: "nearest" }); ta.focus(); }
      return;
    }
    if (action.kind === "unblock") {
      setBusy(true); setMsg(null);
      fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(task.id)}`, boardSlug), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "ready" }),
      }).then(function () {
        setMsg({ ok: true, text: tx(t, "unblockedMessage", "Unblocked {id}. Task is ready for the next tick.", { id: task.id }) });
        if (onRefresh) onRefresh();
      }).catch(function (err: any) {
        setMsg({ ok: false, text: tx(t, "unblockFailed", "Unblock failed: ") + (err.message || err) });
      }).then(function () { setBusy(false); });
      return;
    }
    if (action.kind === "reclaim") {
      setBusy(true); setMsg(null);
      fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reclaim`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: `recovery action for ${diag.kind}` }),
      }).then(function () {
        setMsg({ ok: true, text: tx(t, "reclaimedMessage", "Reclaimed {id}. Task is back to ready.", { id: task.id }) });
        if (onRefresh) onRefresh();
      }).catch(function (err: any) {
        setMsg({ ok: false, text: tx(t, "reclaimFailed", "Reclaim failed: ") + (err.message || err) });
      }).then(function () { setBusy(false); });
      return;
    }
    if (action.kind === "reassign") {
      if (!reassignProfile) { setMsg({ ok: false, text: tx(t, "pickProfileFirst", "Pick a profile first.") }); return; }
      setBusy(true); setMsg(null);
      fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reassign`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile: reassignProfile || null,
          reclaim_first: !!(action.payload && action.payload.reclaim_first),
          reason: `recovery action for ${diag.kind}`,
        }),
      }).then(function () {
        setMsg({ ok: true, text: tx(t, "reassignedMessage", "Reassigned {id} to {profile}.", { id: task.id, profile: reassignProfile }) });
        if (onRefresh) onRefresh();
      }).catch(function (err: any) {
        setMsg({ ok: false, text: tx(t, "reassignFailed", "Reassign failed: ") + (err.message || err) });
      }).then(function () { setBusy(false); });
      return;
    }
  };

  const reassignAction = (diag.actions || []).find(function (a) { return a.kind === "reassign"; });
  const sevClass = "hermes-kanban-diag--" + (diag.severity || "warning");

  return h("div", { className: cn("hermes-kanban-diag", sevClass) },
    h("div", { className: "hermes-kanban-diag-header" },
      h("span", { className: "hermes-kanban-diag-sev" },
        diag.severity === "critical" ? "!!!" : diag.severity === "error" ? "!!" : "\u26a0"),
      h("span", { className: "hermes-kanban-diag-title" }, diag.title),
    ),
    h("div", { className: "hermes-kanban-diag-detail" }, diag.detail),
    diag.data && Object.keys(diag.data).length > 0
      ? h("div", { className: "hermes-kanban-diag-data" },
          Object.keys(diag.data).map(function (k) {
            const v = (diag.data as any)[k];
            if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string" && v[0].indexOf("t_") === 0) {
              return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                v.map(function (x: string) {
                  return h("code", { key: x, className: "hermes-kanban-event-phantom-chip" }, x);
                }),
              );
            }
            return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
              h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
              h("span", { className: "hermes-kanban-diag-data-val" },
                Array.isArray(v) ? v.join(", ") : String(v)),
            );
          }),
        )
      : null,
    reassignAction
      ? h("div", { className: "hermes-kanban-diag-reassign-row" },
          h("span", { className: "hermes-kanban-diag-reassign-label" }, tx(t, "reassignTo", "Reassign to:")),
          h("select", {
            className: "hermes-kanban-recovery-select",
            value: reassignProfile,
            onChange: function (e: any) { setReassignProfile(e.target.value); },
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
          key: a.kind + i,
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
    msg ? h("div", {
      className: cn(
        "hermes-kanban-diag-msg",
        msg.ok ? "hermes-kanban-diag-msg--ok" : "hermes-kanban-diag-msg--err",
      ),
    }, msg.text) : null,
  );
}

// ── DiagnosticsSection ───────────────────────────────────────────────────────

function DiagnosticsSection(props: {
  task: Task;
  boardSlug: string | null;
  assignees: string[];
  diagnostics: Diagnostic[];
  onRefresh?: () => void;
}) {
  const { t } = useI18n();
  const diags = props.diagnostics || [];
  const hasOpenDiags = diags.length > 0;
  const [open, setOpen] = useState(hasOpenDiags);
  useEffect(function () { if (hasOpenDiags) setOpen(true); }, [hasOpenDiags]);
  if (!hasOpenDiags) return null;
  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head-row" },
      h("span", { className: "hermes-kanban-section-head" },
        h("span", { className: "hermes-kanban-section-head-warning" },
          `\u26a0 ${tx(t, "diagnostics", "Diagnostics")} (${diags.length})`),
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
              key: props.task.id + ":" + d.kind + i,
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

// ── HomeSubsSection ──────────────────────────────────────────────────────────

function HomeSubsSection(props: {
  homeChannels: HomeChannel[];
  homeBusy: Record<string, boolean>;
  onToggle: (platform: string, subscribed: boolean) => void;
}) {
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
        const label = hc.subscribed ? "\u2713 " + hc.platform : hc.platform;
        const target = `${hc.name} (${hc.chat_id}${hc.thread_id ? " / " + hc.thread_id : ""})`;
        const title = hc.subscribed
          ? `${tx(t, "sendingUpdates", "Sending updates to")} ${target}. Click to stop.`
          : `${tx(t, "sendNotifications", "Send completed / blocked / gave_up notifications to")} ${target}.`;
        return h(Button, {
          key: hc.platform,
          size: "sm",
          title: title,
          disabled: isBusy,
          onClick: function () { props.onToggle(hc.platform, hc.subscribed); },
          className: hc.subscribed
            ? "hermes-kanban-home-sub hermes-kanban-home-sub--on"
            : "hermes-kanban-home-sub",
        }, label);
      }),
    ),
  );
}

// ── AttachmentsSection ───────────────────────────────────────────────────────

function _fmtBytes(n: number): string {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function AttachmentsSection(props: {
  attachments: any[];
  boardSlug: string | null;
  onUpload: (files: FileList) => void;
  onDelete: (id: number) => void;
  uploadBusy: boolean;
  uploadErr: string | null;
  i18n: unknown;
}) {
  const i18n = props.i18n;
  const atts = props.attachments || [];
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [dlErr, setDlErr] = useState<string | null>(null);

  function downloadAttachment(a: any) {
    const url = withBoard(`${API}/attachments/${a.id}`, props.boardSlug);
    setDlErr(null);
    authedFetch(url)
      .then(function (resp: Response) {
        if (!resp.ok) {
          return resp.text().then(function (txt) {
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
      .catch(function (e: any) { setDlErr(String(e.message || e)); });
  }

  return h("div", { className: "hermes-kanban-section" },
    h("div", { className: "hermes-kanban-section-head" },
      `${tx(i18n, "attachments", "Attachments")} (${atts.length})`),
    h("input", {
      ref: fileRef as any,
      type: "file",
      multiple: true,
      style: { display: "none" },
      onChange: function (e: any) {
        if (props.onUpload) props.onUpload(e.target.files);
        try { e.target.value = ""; } catch (_e) { /* ignore */ }
      },
    }),
    h("div", { className: "flex items-center gap-2 mb-2" },
      h(Button, {
        size: "sm",
        variant: "outline",
        disabled: !!props.uploadBusy,
        onClick: function () { if (fileRef.current) (fileRef.current as any).click(); },
      }, props.uploadBusy ? tx(i18n, "uploading", "Uploading\u2026") : tx(i18n, "uploadFile", "Upload file")),
    ),
    (props.uploadErr || dlErr)
      ? h("div", { className: "text-xs text-destructive mb-2" }, props.uploadErr || dlErr)
      : null,
    atts.length === 0
      ? h("div", { className: "text-xs text-muted-foreground" },
          tx(i18n, "noAttachments", "\u2014 no attachments \u2014"))
      : atts.map(function (a) {
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
              _fmtBytes(a.size)),
            h("button", {
              type: "button",
              className: "hermes-kanban-drawer-close",
              title: tx(i18n, "removeAttachment", "Remove attachment"),
              onClick: function () {
                if (window.confirm(tx(i18n, "confirmRemoveAttachment", "Remove this attachment?"))) {
                  if (props.onDelete) props.onDelete(a.id);
                }
              },
            }, "\u00d7"),
          );
        }),
  );
}

// ── WorkerLogSection ─────────────────────────────────────────────────────────

function WorkerLogSection(props: { taskId: string; boardSlug: string | null }) {
  const { t } = useI18n();
  const [state, setState] = useState<{ loading: boolean; data: any; err: string | null }>({
    loading: false, data: null, err: null,
  });
  const load = useCallback(function () {
    setState({ loading: true, data: null, err: null });
    fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/log?tail=100000`, props.boardSlug))
      .then(function (d: any) { setState({ loading: false, data: d, err: null }); })
      .catch(function (e: any) { setState({ loading: false, data: null, err: String(e.message || e) }); });
  }, [props.taskId, props.boardSlug]);

  useEffect(function () { load(); }, [load]);

  const data = state.data;
  let body: any;
  if (state.loading) {
    body = h("div", { className: "text-xs text-muted-foreground" }, tx(t, "loadingLog", "Loading log\u2026"));
  } else if (state.err) {
    body = h("div", { className: "text-xs text-destructive" }, state.err);
  } else if (!data || !data.exists) {
    body = h("div", { className: "text-xs text-muted-foreground italic" },
      tx(t, "noWorkerLog", "\u2014 no worker log yet (task hasn't spawned or log was rotated away) \u2014"));
  } else {
    body = h("pre", { className: "hermes-kanban-pre hermes-kanban-log" }, data.content || "(empty)");
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
          tx(t, "logTruncated", "(showing last 100 KB \u2014 full log at "),
          data.path,
          tx(t, "logAt", ")"))
      : null,
  );
}

// ── RunHistorySection ────────────────────────────────────────────────────────

function RunHistorySection(props: { runs: Run[] }) {
  const { t } = useI18n();
  const runs = props.runs || [];
  const [expanded, setExpanded] = useState(false);
  if (runs.length === 0) return null;
  const showAll = expanded || runs.length <= 3;
  const visible = showAll ? runs : runs.slice(-3);

  const fmtElapsed = function (run: Run) {
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
    visible.map(function (r) {
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
            timeAgo ? timeAgo(r.started_at) : ""),
        ),
        r.summary ? h("div", { className: "hermes-kanban-run-summary" }, r.summary) : null,
        r.error ? h("div", { className: "hermes-kanban-run-error" }, r.error) : null,
        (r.metadata && Object.keys(r.metadata as any).length > 0)
          ? (function () {
              const json = JSON.stringify(r.metadata, null, 2);
              const collapsed = json.length > 300;
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

// ── TaskDetail (inner content of the drawer) ──────────────────────────────────

function TaskDetail(props: {
  data: TaskDetailType;
  editing: boolean;
  setEditing: (v: boolean) => void;
  renderMarkdown: boolean;
  allTasks: Task[];
  assignees: string[];
  boardSlug: string | null;
  onPatch: (p: any, opts?: any) => Promise<void>;
  onSpecify?: () => Promise<any>;
  onDecompose?: () => Promise<any>;
  onAddParent: (id: string) => Promise<void>;
  onRemoveParent: (id: string) => Promise<void>;
  onAddChild: (id: string) => Promise<void>;
  onRemoveChild: (id: string) => Promise<void>;
  homeChannels: HomeChannel[];
  homeBusy: Record<string, boolean>;
  onToggleHomeSub: (platform: string, subscribed: boolean) => void;
  onRefresh: () => void;
  onUpload: (files: FileList) => void;
  onDeleteAttachment: (id: number) => void;
  uploadBusy: boolean;
  uploadErr: string | null;
}) {
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
      homeChannels: props.homeChannels,
      homeBusy: props.homeBusy,
      onToggle: props.onToggleHomeSub,
    }),
    h(BodyEditor, {
      task: t,
      renderMarkdown: props.renderMarkdown,
      onPatch: props.onPatch,
    }),
    h(DependencyEditor, {
      task: t,
      links,
      allTasks: props.allTasks,
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
      attachments,
      boardSlug: props.boardSlug,
      onUpload: props.onUpload,
      onDelete: props.onDeleteAttachment,
      uploadBusy: props.uploadBusy,
      uploadErr: props.uploadErr,
      i18n,
    }),
    h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        `${tx(i18n, "comments", "Comments")} (${comments.length})`),
      comments.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(i18n, "noComments", "\u2014 no comments \u2014"))
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
                h("span", { className: "hermes-kanban-event-warning-icon" }, "\u26a0"),
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
                phantoms.map(function (pid: string) {
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

// ── TaskDrawer (main exported component) ─────────────────────────────────────

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

export function TaskDrawer(props: TaskDrawerProps) {
  const { t } = useI18n();
  const [data, setData] = useState<TaskDetailType | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [, setPatchErr] = useState<string | null>(null);
  const [newComment, setNewComment] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [homeChannels, setHomeChannels] = useState<HomeChannel[]>([]);
  const [homeBusy, setHomeBusy] = useState<Record<string, boolean>>({});
  const boardSlug = props.boardSlug;

  const load = useCallback(function () {
    return fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug))
      .then(function (d: unknown) { setData(d as TaskDetailType); setErr(null); setPatchErr(null); })
      .catch(function (e: any) { setErr(String(e.message || e)); })
      .finally(function () { setLoading(false); });
  }, [props.taskId, boardSlug]);

  const loadHomeChannels = useCallback(function () {
    const qs = new URLSearchParams({ task_id: props.taskId });
    const url = withBoard(`${API}/home-channels?${qs}`, boardSlug);
    return fetchJSON(url)
      .then(function (d: any) { setHomeChannels(d.home_channels || []); })
      .catch(function () { /* silent */ });
  }, [props.taskId, boardSlug]);

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
    fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/comments`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    }).then(function () {
      setNewComment("");
      load();
      props.onRefresh();
    }).catch(function (e: any) { setErr(String(e.message || e)); });
  };

  const handleUpload = function (fileList: FileList) {
    const files = Array.prototype.slice.call(fileList || []) as File[];
    if (!files.length) return;
    setUploadBusy(true);
    setUploadErr(null);
    const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/attachments`, boardSlug);
    let chain: Promise<void> = Promise.resolve();
    files.forEach(function (f) {
      chain = chain.then(function () {
        const fd = new FormData();
        fd.append("file", f, f.name);
        return authedFetch(url, { method: "POST", body: fd })
          .then(function (resp: Response) {
            if (!resp.ok) {
              return resp.text().then(function (txt) {
                throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
              });
            }
          });
      });
    });
    chain.then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setUploadErr(String(e.message || e)); })
      .finally(function () { setUploadBusy(false); });
  };

  const handleDeleteAttachment = function (attachmentId: number) {
    return fetchJSON(withBoard(`${API}/attachments/${attachmentId}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setUploadErr(String(e.message || e)); });
  };

  const doPatch = function (patch: any, opts?: any) {
    if (opts && opts.confirm && !window.confirm(opts.confirm)) return Promise.resolve();
    const finalPatch = withCompletionSummary(patch, 1);
    if (!finalPatch) return Promise.resolve();
    setPatchErr(null);
    return fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(finalPatch),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setPatchErr(parseApiErrorMessage(e)); });
  };

  const doSpecify = function () {
    return fetchJSON(
      withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/specify`, boardSlug),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) },
    ).then(function (res: any) { load(); props.onRefresh(); return res; });
  };

  const doDecompose = function () {
    return fetchJSON(
      withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/decompose`, boardSlug),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) },
    ).then(function (res: any) { load(); props.onRefresh(); return res; });
  };

  const addLink = function (parentId: string) {
    return fetchJSON(withBoard(`${API}/links`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: parentId, child_id: props.taskId }),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setErr(String(e.message || e)); });
  };
  const removeLink = function (parentId: string) {
    const qs = new URLSearchParams({ parent_id: parentId, child_id: props.taskId });
    return fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setErr(String(e.message || e)); });
  };
  const addChild = function (childId: string) {
    return fetchJSON(withBoard(`${API}/links`, boardSlug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_id: props.taskId, child_id: childId }),
    }).then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setErr(String(e.message || e)); });
  };
  const removeChild = function (childId: string) {
    const qs = new URLSearchParams({ parent_id: props.taskId, child_id: childId });
    return fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
      .then(function () { load(); props.onRefresh(); })
      .catch(function (e: any) { setErr(String(e.message || e)); });
  };

  const toggleHomeSubscription = function (platform: string, currentlySubscribed: boolean) {
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
    return fetchJSON(url, { method: method as any })
      .then(function () { return loadHomeChannels(); })
      .catch(function (e: any) {
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
      onClick: function (e: any) { e.stopPropagation(); },
    },
      h("div", { className: "hermes-kanban-drawer-head" },
        h("span", { className: "text-xs text-muted-foreground" }, props.taskId),
        h("button", {
          type: "button",
          onClick: props.onClose,
          className: "hermes-kanban-drawer-close",
          title: tx(t, "close", "Close (Esc)"),
        }, "\u00d7"),
      ),
      loading
        ? h("div", { className: "p-4 text-sm text-muted-foreground" },
            tx(t, "loadingDetail", "Loading\u2026"))
        : err
          ? h("div", { className: "p-4 text-sm text-destructive" }, err)
          : data
            ? h(TaskDetail, {
                data,
                editing,
                setEditing,
                renderMarkdown: props.renderMarkdown,
                allTasks: props.allTasks,
                assignees: props.assignees || [],
                boardSlug,
                onPatch: doPatch,
                onSpecify: doSpecify,
                onDecompose: doDecompose,
                onAddParent: addLink,
                onRemoveParent: removeLink,
                onAddChild: addChild,
                onRemoveChild: removeChild,
                homeChannels,
                homeBusy,
                onToggleHomeSub: toggleHomeSubscription,
                onRefresh: props.onRefresh,
                onUpload: handleUpload,
                onDeleteAttachment: handleDeleteAttachment,
                uploadBusy,
                uploadErr,
              })
            : null,
      data
        ? h("div", { className: "hermes-kanban-drawer-comment-row" },
            h(Input, {
              value: newComment,
              onChange: function (e: any) { setNewComment(e.target.value); },
              onKeyDown: function (e: any) {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleComment(); }
              },
              placeholder: tx(t, "addComment", "Add a comment\u2026 (Enter to submit)"),
              className: "h-8 text-sm flex-1",
            }),
            h(Button, {
              onClick: handleComment,
              size: "sm",
            }, tx(t, "comment", "Comment")),
          )
        : null,
    ),
  );
}