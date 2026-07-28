/**
 * Kanban dashboard plugin — board-level UI components.
 *
 * All React + UI components are obtained at runtime from the host SDK via
 * ``window.__HERMES_PLUGIN_SDK__`` (see ``./sdk``). No React is bundled.
 *
 * Exports:
 *  - ``BoardSwitcher``         — board dropdown selector with task counts
 *  - ``BoardToolbar``          — search, filters, and action buttons
 *  - ``BulkActionBar``         — bulk actions for selected cards
 *  - ``NewBoardDialog``        — modal dialog for creating a new board
 *  - ``OrchestrationPanel``    — collapsible orchestrator settings panel
 *  - ``ProfileDescriptionRow`` — single profile description editor row
 *  - ``AttentionStrip``        — surfaces tasks with active diagnostics
 */

import type {
  Board,
  BoardListItem,
  Task,
  OrchestrationSettings,
  Profile,
} from "./types";
import {
  getReact,
  getHooks,
  getComponents,
  getUtils,
  getUseI18n,
  getCheckbox,
  selectChangeHandler,
  getFetchJSON,
} from "./sdk";
import { API, DOCS_URL } from "./constants";
import { tx } from "./i18n";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ── Runtime handles (resolved once at module load) ──────────────────────────

const ReactRuntime = getReact();
const { createElement: h } = ReactRuntime;

const hooks = getHooks();
const { useState, useEffect, useCallback, useMemo } = hooks;

const components = getComponents();
const Card = components.Card as any;
const CardContent = components.CardContent as any;
const Button = components.Button as any;
const Input = components.Input as any;
const Label = components.Label as any;
const Select = components.Select as any;
const SelectOption = components.SelectOption as any;
const Checkbox = getCheckbox() as any;

const utils = getUtils();
const { cn } = utils;

const useI18n = getUseI18n() as () => { t: unknown };

const fetchJSON = getFetchJSON();

// ── DocsLink (internal helper) ──────────────────────────────────────────────

/** Small `?` affordance next to the board controls. Opens the kanban docs page. */
function DocsLink() {
  return h(
    "a",
    {
      href: DOCS_URL,
      target: "_blank",
      rel: "noopener noreferrer",
      className: "hermes-kanban-docs-link",
      title: "Open Hermes Kanban docs in a new tab",
      "aria-label": "Hermes Kanban documentation",
    },
    "?",
  );
}

// ── BoardSwitcher ───────────────────────────────────────────────────────────

export interface BoardSwitcherProps {
  board: string;
  boardList: BoardListItem[];
  onSwitch: (slug: string) => void;
  onNewClick: () => void;
  onDeleteBoard: (slug: string) => void;
}

export function BoardSwitcher(props: BoardSwitcherProps) {
  const { t } = useI18n();
  const list = props.boardList || [];
  const current = list.find(function (b) {
    return b.slug === props.board;
  });
  const currentName = current && current.name ? current.name : props.board;
  const currentTotal = current ? current.total : 0;
  const hasMultipleBoards = list.length > 1;

  // Hide entirely when only the default board exists AND it's empty —
  // single-project users never see boards UI unless they ask for it.
  const totalAcrossAllBoards = list.reduce(function (n, b) {
    return n + (b.total || 0);
  }, 0);
  const shouldShow = hasMultipleBoards || totalAcrossAllBoards > 0;

  if (!shouldShow) {
    return h(
      "div",
      {
        className: "hermes-kanban-boardswitcher-compact",
        title: tx(
          t,
          "boardSwitcherHint",
          "Boards let you separate unrelated streams of work",
        ),
      },
      h(
        Button,
        {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-7 text-xs",
        },
        tx(t, "newBoard", "+ New board"),
      ),
      h(DocsLink, null),
    );
  }

  return h(
    "div",
    { className: "hermes-kanban-boardswitcher" },
    h(
      "div",
      { className: "hermes-kanban-boardswitcher-inner" },
      h(
        "div",
        { className: "flex flex-col gap-0.5" },
        h(
          "div",
          { className: "text-[11px] tracking-wider text-muted-foreground" },
          tx(t, "board", "Board"),
        ),
        h(
          "div",
          { className: "flex items-center gap-2" },
          h(
            Select,
            Object.assign(
              {
                value: props.board,
                className: "h-8 min-w-[220px]",
                "aria-label": "Switch kanban board",
                title:
                  "Boards are independent work streams. Each board has its own tasks, tenants, and assignees.",
              },
              selectChangeHandler(function (v) {
                if (v) props.onSwitch(v);
              }),
            ),
            list.map(function (b) {
              const label =
                b.total > 0
                  ? `${b.name || b.slug} · ${b.total}`
                  : b.name || b.slug;
              return h(SelectOption, { key: b.slug, value: b.slug }, label);
            }),
          ),
          h(
            "span",
            { className: "text-xs text-muted-foreground" },
            `${currentTotal || 0} task${currentTotal === 1 ? "" : "s"}`,
          ),
        ),
      ),
      h("div", { className: "flex-1" }),
      h(DocsLink, null),
      h(
        Button,
        {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-8",
          title:
            "Create a new board. Useful when you want an unrelated work stream (different project, different team, isolated scratch area).",
        },
        tx(t, "newBoard", "+ New board"),
      ),
      props.board !== "default"
        ? h(
            Button,
            {
              onClick: function () {
                const msg = tx(
                  t,
                  "archiveBoardConfirm",
                  "Archive board '{name}'? It will be moved to boards/_archived/ so you can recover it later. Tasks on this board will no longer appear anywhere in the UI.",
                  { name: currentName },
                );
                if (window.confirm(msg)) props.onDeleteBoard(props.board);
              },
              size: "sm",
              className: "h-8",
              title: tx(t, "archiveBoardTitle", "Archive this board"),
            },
            tx(t, "archive", "Archive"),
          )
        : null,
    ),
  );
}

// ── BoardToolbar ────────────────────────────────────────────────────────────

export interface BoardToolbarProps {
  board: Board | null;
  tenantFilter: string;
  setTenantFilter: (v: string) => void;
  assigneeFilter: string;
  setAssigneeFilter: (v: string) => void;
  includeArchived: boolean;
  setIncludeArchived: (v: boolean) => void;
  laneByProfile: boolean;
  setLaneByProfile: (v: boolean) => void;
  search: string;
  setSearch: (v: string) => void;
  onNudgeDispatch: () => void;
  onRefresh: () => void;
}

export function BoardToolbar(props: BoardToolbarProps) {
  const { t } = useI18n();
  const tenants = (props.board && props.board.tenants) || [];
  const assignees = (props.board && props.board.assignees) || [];
  return h(
    "div",
    { className: "flex flex-wrap items-end gap-3" },
    h(
      "div",
      {
        className: "flex flex-col gap-1",
        title:
          "Fuzzy-match tasks by id, title, or description. Matches across all columns.",
      },
      h(
        Label,
        { className: "text-xs text-muted-foreground" },
        tx(t, "search", "Search"),
      ),
      h(Input, {
        placeholder: tx(t, "filterCards", "Filter cards…"),
        value: props.search,
        onChange: function (e: any) {
          props.setSearch(e.target.value);
        },
        className: "w-56 h-8",
      }),
    ),
    h(
      "div",
      {
        className: "flex flex-col gap-1",
        title:
          "Tenants are free-form tags on a task (e.g. customer, project, team). Set them via the task drawer or kanban_create.",
      },
      h(
        Label,
        { className: "text-xs text-muted-foreground" },
        tx(t, "tenant", "Tenant"),
      ),
      h(
        Select,
        Object.assign(
          {
            value: props.tenantFilter,
            className: "h-8",
          },
          selectChangeHandler(props.setTenantFilter),
        ),
        h(SelectOption, { value: "" }, tx(t, "allTenants", "All tenants")),
        tenants.map(function (tn) {
          return h(SelectOption, { key: tn, value: tn }, tn);
        }),
      ),
    ),
    h(
      "div",
      {
        className: "flex flex-col gap-1",
        title:
          "Filter by assigned Hermes profile. Profiles are the named agent identities that claim and work on tasks.",
      },
      h(
        Label,
        { className: "text-xs text-muted-foreground" },
        tx(t, "assignee", "Assignee"),
      ),
      h(
        Select,
        Object.assign(
          {
            value: props.assigneeFilter,
            className: "h-8",
          },
          selectChangeHandler(props.setAssigneeFilter),
        ),
        h(SelectOption, { value: "" }, tx(t, "allProfiles", "All profiles")),
        assignees.map(function (a) {
          return h(SelectOption, { key: a, value: a }, a);
        }),
      ),
    ),
    h(
      "label",
      {
        className: "flex items-center gap-2 text-xs",
        title:
          "Include archived tasks in the board view. Archived tasks are hidden by default.",
      },
      h(Checkbox, {
        checked: props.includeArchived,
        onCheckedChange: function (checked: any) {
          props.setIncludeArchived(checked === true);
        },
      }),
      tx(t, "showArchived", "Show archived"),
    ),
    h(
      "label",
      {
        className: "flex items-center gap-2 text-xs",
        title: "Group the Running column by assigned profile",
      },
      h(Checkbox, {
        checked: props.laneByProfile,
        onCheckedChange: function (checked: any) {
          props.setLaneByProfile(checked === true);
        },
      }),
      tx(t, "lanesByProfile", "Lanes by profile"),
    ),
    h("div", { className: "flex-1" }),
    h(
      Button,
      {
        onClick: props.onNudgeDispatch,
        size: "sm",
        title:
          "Wake the dispatcher to claim ready tasks now instead of waiting for the next tick. Use this after adding tasks if you want them picked up immediately.",
      },
      tx(t, "nudgeDispatcher", "Nudge dispatcher"),
    ),
    h(
      Button,
      {
        onClick: props.onRefresh,
        size: "sm",
        title:
          "Reload the board from the database. The board auto-refreshes on task events; this is for forcing a re-read.",
      },
      tx(t, "refresh", "Refresh"),
    ),
    h(
      Button,
      {
        onClick: function () {
          props.setSearch("");
          props.setTenantFilter("");
          props.setAssigneeFilter("");
          props.setIncludeArchived(false);
        },
        size: "sm",
        title:
          "Clear all active filters (search, tenant, assignee, archived).",
      },
      tx(t, "clearFilters", "Clear filters"),
    ),
  );
}

// ── BulkActionBar ──────────────────────────────────────────────────────────

export interface BulkActionBarProps {
  count: number;
  assignees: string[];
  onApply: (patch: Record<string, unknown>, confirmMsg?: string) => void;
  onClear: () => void;
  onSelectAllVisible: () => void;
  onDelete: (count: number) => void;
}

export function BulkActionBar(props: BulkActionBarProps) {
  const { t } = useI18n();
  const [assignee, setAssignee] = useState("");
  const [reclaimFirst, setReclaimFirst] = useState(false);
  const [priority, setPriority] = useState("");
  return h(
    "div",
    { className: "hermes-kanban-bulk" },
    h(
      "span",
      { className: "hermes-kanban-bulk-count" },
      `${props.count} ${tx(t, "selected", "selected")}`,
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply({ status: "todo" });
        },
        size: "sm",
        title: "Move selected tasks to Todo.",
      },
      "→ todo",
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply({ status: "ready" });
        },
        size: "sm",
        title:
          "Move selected tasks to Ready. Ready tasks are picked up by the dispatcher on the next tick.",
      },
      "→ ready",
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply({ status: "blocked" }, `Block ${props.count} task(s)?`);
        },
        size: "sm",
        title: "Block selected tasks. Releases any active claims.",
      },
      "Block",
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply(
            { status: "ready" },
            `Unblock ${props.count} task(s)?`,
          );
        },
        size: "sm",
        title: "Unblock selected tasks (promote to Ready).",
      },
      "Unblock",
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply(
            { status: "done" },
            tx(t, "markDone", "Mark {n} task(s) as done?", { n: props.count }),
          );
        },
        size: "sm",
        title:
          "Mark selected tasks as done. Releases any claims and unblocks dependent children. You'll be asked for a completion summary.",
      },
      tx(t, "complete", "Complete"),
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onApply(
            { archive: true },
            tx(t, "markArchived", "Archive {n} task(s)?", { n: props.count }),
          );
        },
        size: "sm",
        title:
          "Archive selected tasks. They disappear from the default board view but remain in the database.",
      },
      tx(t, "archive", "Archive"),
    ),
    h(
      Button,
      {
        onClick: function () {
          props.onDelete(props.count);
        },
        size: "sm",
        variant: "destructive",
        title: "Permanently delete selected tasks. This cannot be undone.",
      },
      tx(t, "delete", "Delete"),
    ),
    h(
      "div",
      {
        className: "hermes-kanban-bulk-priority",
        title: "Set priority on selected tasks. Higher = claimed first.",
      },
      h(Input, {
        type: "number",
        value: priority,
        onChange: function (e: any) {
          setPriority(e.target.value);
        },
        placeholder: tx(t, "priority", "pri"),
        className: "h-7 text-xs w-16",
      }),
      h(
        Button,
        {
          onClick: function () {
            if (priority === "") return;
            props.onApply({ priority: Number(priority) });
            setPriority("");
          },
          disabled: priority === "",
          size: "sm",
        },
        tx(t, "setPriority", "Set priority"),
      ),
    ),
    h(
      "div",
      {
        className: "hermes-kanban-bulk-reassign",
        title:
          "Reassign selected tasks to a different Hermes profile. Pick a profile (or unassign) and click Apply.",
      },
      h(
        Select,
        Object.assign(
          {
            value: assignee,
            className: "h-7 text-xs",
          },
          selectChangeHandler(setAssignee),
        ),
        h(SelectOption, { value: "" }, "— reassign —"),
        h(SelectOption, { value: "__none__" }, "(unassign)"),
        props.assignees.map(function (a) {
          return h(SelectOption, { key: a, value: a }, a);
        }),
      ),
      h(
        Button,
        {
          onClick: function () {
            if (!assignee) return;
            props.onApply({
              assignee: assignee === "__none__" ? "" : assignee,
              reclaim_first: reclaimFirst,
            });
            setAssignee("");
          },
          disabled: !assignee,
          size: "sm",
          title: "Apply the selected assignee to all selected tasks.",
        },
        tx(t, "apply", "Apply"),
      ),
    ),
    h(
      "label",
      {
        className: "hermes-kanban-bulk-reclaim-first",
        title: "Reclaim any active claims before reassigning",
      },
      h(Checkbox, {
        checked: reclaimFirst,
        onCheckedChange: function (checked: any) {
          setReclaimFirst(checked === true);
        },
      }),
      "Reclaim first",
    ),
    h("div", { className: "flex-1" }),
    h(
      Button,
      {
        onClick: props.onSelectAllVisible,
        size: "sm",
        title: "Select all visible cards across columns.",
      },
      "Select all visible",
    ),
    h(
      Button,
      {
        onClick: props.onClear,
        size: "sm",
        title: "Deselect all tasks and hide this bar.",
      },
      tx(t, "clear", "Clear"),
    ),
  );
}

// ── NewBoardDialog ──────────────────────────────────────────────────────────

export interface NewBoardDialogProps {
  onCancel: () => void;
  onCreate: (body: Record<string, unknown>) => Promise<void>;
}

export function NewBoardDialog(props: NewBoardDialogProps) {
  const { t } = useI18n();
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [icon, setIcon] = useState("");
  const [switchTo, setSwitchTo] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Auto-derive a name from the slug if the user hasn't typed one.
  const autoName = useMemo(
    function () {
      if (!slug) return "";
      return slug
        .replace(/[-_]+/g, " ")
        .split(" ")
        .filter(Boolean)
        .map(function (w) {
          return w[0].toUpperCase() + w.slice(1);
        })
        .join(" ");
    },
    [slug],
  );

  function onSubmit(ev: any) {
    if (ev) ev.preventDefault();
    if (!slug.trim()) {
      setErr("slug is required");
      return;
    }
    setSubmitting(true);
    setErr(null);
    props
      .onCreate({
        slug: slug.trim(),
        name: name.trim() || autoName || undefined,
        description: description.trim() || undefined,
        icon: icon.trim() || undefined,
        switch: switchTo,
      })
      .catch(function (e: unknown) {
        setErr(String(e && (e as Error).message ? (e as Error).message : e));
        setSubmitting(false);
      });
  }

  return h(
    "div",
    {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e: any) {
        if (e.target === e.currentTarget) props.onCancel();
      },
    },
    h(
      "form",
      {
        className: "hermes-kanban-dialog",
        onSubmit: onSubmit,
      },
      h(
        "div",
        { className: "hermes-kanban-dialog-title" },
        tx(t, "newBoardTitle", "New board"),
      ),
      h(
        "div",
        { className: "text-xs text-muted-foreground mb-2" },
        tx(
          t,
          "newBoardDescription",
          "Boards let you separate unrelated streams of work — one per project, repo, or domain. Workers on one board never see another board's tasks.",
        ),
      ),
      h(
        "div",
        { className: "flex flex-col gap-3" },
        h(
          "div",
          { className: "flex flex-col gap-1" },
          h(
            Label,
            { className: "text-xs" },
            tx(t, "slug", "Slug"),
            " ",
            h(
              "span",
              { className: "text-muted-foreground" },
              tx(t, "slugHint", "— lowercase, hyphens, e.g. atm10-server"),
            ),
          ),
          h(Input, {
            value: slug,
            onChange: function (e: any) {
              setSlug(
                e.target.value
                  .toLowerCase()
                  .replace(/[^a-z0-9\-_]/g, "-"),
              );
            },
            placeholder: "atm10-server",
            autoFocus: true,
            className: "h-8",
          }),
        ),
        h(
          "div",
          { className: "flex flex-col gap-1" },
          h(
            Label,
            { className: "text-xs" },
            tx(t, "displayName", "Display name"),
            " ",
            h(
              "span",
              { className: "text-muted-foreground" },
              tx(t, "displayNameHint", "(optional)"),
            ),
          ),
          h(Input, {
            value: name,
            onChange: function (e: any) {
              setName(e.target.value);
            },
            placeholder: autoName || tx(t, "displayName", "Display name"),
            className: "h-8",
          }),
        ),
        h(
          "div",
          { className: "flex flex-col gap-1" },
          h(
            Label,
            { className: "text-xs" },
            tx(t, "description", "Description"),
            " ",
            h(
              "span",
              { className: "text-muted-foreground" },
              tx(t, "descriptionHint", "(optional)"),
            ),
          ),
          h(Input, {
            value: description,
            onChange: function (e: any) {
              setDescription(e.target.value);
            },
            placeholder: "What goes on this board?",
            className: "h-8",
          }),
        ),
        h(
          "div",
          { className: "flex flex-col gap-1" },
          h(
            Label,
            { className: "text-xs" },
            tx(t, "icon", "Icon"),
            " ",
            h(
              "span",
              { className: "text-muted-foreground" },
              tx(t, "iconHint", "(single character or emoji)"),
            ),
          ),
          h(Input, {
            value: icon,
            onChange: function (e: any) {
              setIcon(e.target.value.slice(0, 4));
            },
            placeholder: "📦",
            className: "h-8 w-24",
          }),
        ),
        h(
          "label",
          { className: "flex items-center gap-2 text-xs" },
          h(Checkbox, {
            checked: switchTo,
            onCheckedChange: function (checked: any) {
              setSwitchTo(checked === true);
            },
          }),
          tx(t, "switchAfterCreate", "Switch to this board after creating it"),
        ),
      ),
      err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
      h(
        "div",
        { className: "hermes-kanban-dialog-actions" },
        h(
          Button,
          {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
            disabled: submitting,
          },
          tx(t, "cancel", "Cancel"),
        ),
        h(
          Button,
          {
            type: "submit",
            size: "sm",
            disabled: submitting || !slug.trim(),
          },
          submitting
            ? tx(t, "creating", "Creating…")
            : tx(t, "createBoard", "Create board"),
        ),
      ),
    ),
  );
}

// ── ProfileDescriptionRow ───────────────────────────────────────────────────

export interface ProfileDescriptionRowProps {
  profile: Profile;
  busy: string | null;
  onSave: (name: string, description: string) => Promise<void>;
  onAuto: (name: string, overwrite: boolean) => Promise<void>;
}

export function ProfileDescriptionRow(props: ProfileDescriptionRowProps) {
  const p = props.profile;
  const [draft, setDraft] = useState(p.description || "");
  const busy = props.busy;

  // Re-sync the local draft if the server-side description changes (e.g.
  // after auto-generate).
  useEffect(
    function () {
      setDraft(p.description || "");
    },
    [p.description],
  );

  return h(
    "div",
    {
      className: "flex flex-col gap-1 border-l-2 pl-2",
      style: { borderColor: p.description ? "#888" : "#cc6" },
    },
    h(
      "div",
      { className: "flex items-center gap-2 text-xs" },
      h("span", { className: "font-medium" }, p.name),
      p.is_default
        ? h(
            "span",
            { className: "text-[10px] text-muted-foreground" },
            "(default)",
          )
        : null,
      p.description_auto && p.description
        ? h(
            "span",
            { className: "text-[10px] text-yellow-600" },
            "auto — review",
          )
        : null,
      !p.description
        ? h(
            "span",
            { className: "text-[10px] text-yellow-600" },
            "⚠ no description",
          )
        : null,
    ),
    h(
      "div",
      { className: "flex items-center gap-2" },
      h(Input, {
        value: draft,
        onChange: function (e: any) {
          setDraft(e.target.value);
        },
        placeholder: "Describe what this profile does…",
        className: "h-7 text-xs flex-1",
        disabled: !!busy,
      }),
      h(
        Button,
        {
          onClick: function () {
            props.onSave(p.name, draft);
          },
          size: "sm",
          disabled: !!busy || draft === (p.description || ""),
          title: "Save the description above as user-authored",
        },
        busy === "save" ? "Saving…" : "Save",
      ),
      h(
        Button,
        {
          onClick: function () {
            props.onAuto(p.name, true);
          },
          size: "sm",
          disabled: !!busy,
          title:
            "Auto-generate a description from this profile's skills and model",
        },
        busy === "auto" ? "Generating…" : "⚗ Auto",
      ),
    ),
  );
}

// ── OrchestrationPanel ──────────────────────────────────────────────────────

export function OrchestrationPanel() {
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<OrchestrationSettings | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const loadAll = useCallback(
    function () {
      Promise.all([
        fetchJSON(`${API}/orchestration`),
        fetchJSON(`${API}/profiles`),
      ])
        .then(function (results: any[]) {
          setSettings(results[0] || null);
          setProfiles((results[1] && results[1].profiles) || []);
          setMsg(null);
        })
        .catch(function (err: unknown) {
          setMsg({
            ok: false,
            text: "Failed to load: " + ((err as Error)?.message || String(err)),
          });
        });
    },
    [],
  );

  useEffect(
    function () {
      // Load on mount so the collapsed pill shows the real mode without
      // requiring the user to expand the panel first.
      if (settings === null) loadAll();
    },
    [settings, loadAll],
  );

  const saveSettings = function (patch: Record<string, unknown>) {
    setMsg(null);
    return fetchJSON(`${API}/orchestration`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
      .then(function (res: any) {
        setSettings(res);
        setMsg({ ok: true, text: "Settings saved." });
        return res;
      })
      .catch(function (err: unknown) {
        setMsg({
          ok: false,
          text: "Save failed: " + ((err as Error)?.message || String(err)),
        });
      });
  };

  const saveProfileDescription = function (
    name: string,
    description: string,
  ) {
    setBusy(function (b) {
      return Object.assign({}, b, { [name]: "save" });
    });
    return fetchJSON(`${API}/profiles/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: description }),
    })
      .then(function () {
        loadAll();
        setMsg({ ok: true, text: `Description saved for ${name}.` });
      })
      .catch(function (err: unknown) {
        setMsg({
          ok: false,
          text: "Save failed: " + ((err as Error)?.message || String(err)),
        });
      })
      .then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b);
          delete next[name];
          return next;
        });
      });
  };

  const autoGenerateDescription = function (
    name: string,
    overwrite: boolean,
  ) {
    setBusy(function (b) {
      return Object.assign({}, b, { [name]: "auto" });
    });
    return fetchJSON(`${API}/profiles/${encodeURIComponent(name)}/describe-auto`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overwrite: !!overwrite }),
    })
      .then(function (res: any) {
        if (res && res.ok) {
          loadAll();
          setMsg({ ok: true, text: `Auto-generated description for ${name}.` });
        } else {
          setMsg({
            ok: false,
            text:
              "Auto-generate failed: " +
              ((res && res.reason) || "unknown error"),
          });
        }
      })
      .catch(function (err: unknown) {
        setMsg({
          ok: false,
          text:
            "Auto-generate failed: " + ((err as Error)?.message || String(err)),
        });
      })
      .then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b);
          delete next[name];
          return next;
        });
      });
  };

  const headerLabel = expanded
    ? "▾ Orchestration settings"
    : "▸ Orchestration settings";

  // Mode pill — always visible (collapsed or expanded).
  const autoOn = !!(settings && settings.auto_decompose);
  const modePillTitle =
    settings === null
      ? "Loading mode…"
      : autoOn
        ? "Orchestration: Auto — the dispatcher decomposes new triage tasks automatically every tick. Click to switch to Manual (pre-PR behavior)."
        : "Orchestration: Manual — triage tasks stay in triage until you click ⚗ Decompose on each card. Click to switch to Auto.";
  const modePill = h(
    "button",
    {
      type: "button",
      onClick: function () {
        if (settings === null) return; // not loaded yet
        saveSettings({ auto_decompose: !autoOn });
      },
      disabled: settings === null,
      title: modePillTitle,
      className:
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 " +
        "text-xs font-medium " +
        (autoOn
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : "border-muted-foreground/30 bg-muted/30 text-muted-foreground"),
    },
    "Orchestration: ",
    h(
      "span",
      { className: "ml-1 font-semibold" },
      settings === null ? "…" : autoOn ? "Auto" : "Manual",
    ),
  );

  if (!expanded) {
    return h(
      "div",
      { className: "flex items-center gap-3 text-xs" },
      modePill,
      h(
        "button",
        {
          type: "button",
          onClick: function () {
            setExpanded(true);
          },
          className: "underline text-muted-foreground hover:text-foreground",
          title:
            "Configure the kanban orchestrator (profile picker, default assignee, auto-decompose, profile descriptions)",
        },
        headerLabel,
      ),
    );
  }

  const profileOptions = profiles.map(function (p) {
    const tag = p.is_default ? " (default)" : "";
    return h(SelectOption, { key: p.name, value: p.name }, p.name + tag);
  });

  return h(
    Card,
    { className: "p-3" },
    h(
      CardContent,
      { className: "p-2 flex flex-col gap-3" },
      h(
        "div",
        { className: "flex items-center justify-between" },
        h(
          "button",
          {
            type: "button",
            onClick: function () {
              setExpanded(false);
            },
            className: "text-sm font-medium underline-offset-2 hover:underline",
          },
          headerLabel,
        ),
        modePill,
        h(Button, { onClick: loadAll, size: "sm" }, "Reload"),
      ),
      msg
        ? h(
            "div",
            {
              className: msg.ok
                ? "hermes-kanban-msg-ok"
                : "hermes-kanban-msg-err",
            },
            msg.text,
          )
        : null,
      settings
        ? h(
            "div",
            { className: "grid gap-3 sm:grid-cols-3" },
            h(
              "div",
              { className: "flex flex-col gap-1" },
              h(
                Label,
                { className: "text-xs text-muted-foreground" },
                "Orchestrator profile",
              ),
              h(
                Select,
                Object.assign(
                  {
                    value: settings.orchestrator_profile || "",
                    className: "h-8",
                  },
                  selectChangeHandler(function (v) {
                    saveSettings({ orchestrator_profile: v });
                  }),
                ),
                h(
                  SelectOption,
                  { value: "" },
                  "(default: " +
                    (settings.active_profile || "default") +
                    ")",
                ),
                profileOptions,
              ),
              h(
                "div",
                { className: "text-[10px] text-muted-foreground" },
                "Resolved: " +
                  (settings.resolved_orchestrator_profile || "default"),
              ),
              h(
                "div",
                { className: "text-[10px] text-muted-foreground" },
                "Owns the root task after fan-out (wakes back up to judge completion). Does not drive how tasks split — configure the decomposer model under auxiliary.kanban_decomposer.",
              ),
            ),
            h(
              "div",
              { className: "flex flex-col gap-1" },
              h(
                Label,
                { className: "text-xs text-muted-foreground" },
                "Default assignee",
              ),
              h(
                Select,
                Object.assign(
                  {
                    value: settings.default_assignee || "",
                    className: "h-8",
                  },
                  selectChangeHandler(function (v) {
                    saveSettings({ default_assignee: v });
                  }),
                ),
                h(
                  SelectOption,
                  { value: "" },
                  "(default: " +
                    (settings.active_profile || "default") +
                    ")",
                ),
                profileOptions,
              ),
              h(
                "div",
                { className: "text-[10px] text-muted-foreground" },
                "Resolved: " +
                  (settings.resolved_default_assignee || "default"),
              ),
            ),
            h(
              "div",
              { className: "flex flex-col gap-1" },
              h(
                Label,
                { className: "text-xs text-muted-foreground" },
                "Orchestration mode",
              ),
              h(
                "label",
                { className: "flex items-center gap-2 text-xs h-8" },
                h(Checkbox, {
                  checked: !!settings.auto_decompose,
                  onCheckedChange: function (checked: any) {
                    saveSettings({ auto_decompose: checked === true });
                  },
                }),
                "Auto-decompose triage tasks",
              ),
              h(
                "div",
                { className: "text-[10px] text-muted-foreground" },
                settings.auto_decompose
                  ? "The dispatcher decomposes new triage tasks automatically."
                  : "Triage tasks stay in triage until you click ⚗ Decompose.",
              ),
            ),
          )
        : h(
            "div",
            { className: "text-xs text-muted-foreground" },
            "Loading…",
          ),
      h(
        "div",
        { className: "border-t pt-3" },
        h(
          Label,
          { className: "text-xs text-muted-foreground" },
          "Profile descriptions",
        ),
        h(
          "div",
          { className: "text-[10px] text-muted-foreground pb-2" },
          "Descriptions guide the decomposer's routing. Click ⚗ to auto-generate, or edit and save.",
        ),
        profiles.length === 0
          ? h(
              "div",
              { className: "text-xs text-muted-foreground" },
              "No profiles installed.",
            )
          : h(
              "div",
              { className: "flex flex-col gap-2" },
              profiles.map(function (p) {
                return h(ProfileDescriptionRow, {
                  key: p.name,
                  profile: p,
                  busy: busy[p.name] || null,
                  onSave: saveProfileDescription,
                  onAuto: autoGenerateDescription,
                });
              }),
            ),
      ),
    ),
  );
}

// ── AttentionStrip ──────────────────────────────────────────────────────────

export interface AttentionStripProps {
  boardData: Board | null;
  onOpen: (taskId: string) => void;
}

/** Collect tasks with active diagnostics or warnings, sorted by severity. */
function collectDiagTasks(boardData: Board | null): Task[] {
  if (!boardData || !boardData.columns) return [];
  const out: Task[] = [];
  for (const col of boardData.columns) {
    for (const t of col.tasks || []) {
      if (t.diagnostics && t.diagnostics.length > 0) out.push(t);
      else if (t.warnings && t.warnings.count > 0) out.push(t);
    }
  }
  const sevIdx = function (s: string): number {
    if (s === "critical") return 3;
    if (s === "error") return 2;
    if (s === "warning") return 1;
    return 0;
  };
  out.sort(function (a, b) {
    const aSev = sevIdx(
      (a.warnings && a.warnings.highest_severity) || "warning",
    );
    const bSev = sevIdx(
      (b.warnings && b.warnings.highest_severity) || "warning",
    );
    if (aSev !== bSev) return bSev - aSev;
    const aLa = (a.warnings && a.warnings.latest_at) || 0;
    const bLa = (b.warnings && b.warnings.latest_at) || 0;
    return bLa - aLa;
  });
  return out;
}

export function AttentionStrip(props: AttentionStripProps) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const diagTasks = useMemo(
    function () {
      return collectDiagTasks(props.boardData);
    },
    [props.boardData],
  );

  if (dismissed || diagTasks.length === 0) return null;

  // Pick the highest severity present so we can colour the strip.
  let topSev = "warning";
  for (const td of diagTasks) {
    const s = (td.warnings && td.warnings.highest_severity) || "warning";
    if (s === "critical") {
      topSev = "critical";
      break;
    }
    if (s === "error" && topSev !== "critical") topSev = "error";
  }

  return h(
    "div",
    {
      className: cn(
        "hermes-kanban-attention",
        "hermes-kanban-attention--" + topSev,
      ),
    },
    h(
      "div",
      { className: "hermes-kanban-attention-bar" },
      h(
        "span",
        { className: "hermes-kanban-attention-icon" },
        topSev === "critical" ? "!!!" : topSev === "error" ? "!!" : "⚠",
      ),
      h(
        "span",
        { className: "hermes-kanban-attention-text" },
        diagTasks.length === 1
          ? tx(t, "taskNeedsAttention", "1 task needs attention")
          : tx(t, "tasksNeedAttention", "{n} tasks need attention", {
              n: diagTasks.length,
            }),
      ),
      h(
        "button",
        {
          className: "hermes-kanban-attention-toggle",
          onClick: function () {
            setExpanded(function (x) {
              return !x;
            });
          },
          type: "button",
        },
        expanded ? tx(t, "hide", "Hide") : tx(t, "show", "Show"),
      ),
      h(
        "button",
        {
          className: "hermes-kanban-attention-dismiss",
          onClick: function () {
            setDismissed(true);
          },
          title: "Hide until next page reload",
          type: "button",
        },
        "\u2715",
      ),
    ),
    expanded
      ? h(
          "div",
          { className: "hermes-kanban-attention-list" },
          diagTasks.map(function (task) {
            const sev =
              (task.warnings && task.warnings.highest_severity) || "warning";
            const kinds =
              task.warnings && task.warnings.kinds
                ? Object.keys(task.warnings.kinds)
                : [];
            return h(
              "div",
              {
                key: task.id,
                className: cn(
                  "hermes-kanban-attention-row",
                  "hermes-kanban-attention-row--" + sev,
                ),
              },
              h(
                "span",
                { className: "hermes-kanban-attention-row-sev" },
                sev === "critical" ? "!!!" : sev === "error" ? "!!" : "⚠",
              ),
              h(
                "span",
                { className: "hermes-kanban-attention-row-id" },
                task.id,
              ),
              h(
                "span",
                { className: "hermes-kanban-attention-row-title" },
                task.title || tx(t, "untitled", "(untitled)"),
              ),
              h(
                "span",
                { className: "hermes-kanban-attention-row-meta" },
                task.assignee
                  ? "@" + task.assignee
                  : tx(t, "unassigned", "unassigned"),
                " \u00b7 ",
                kinds.length > 0
                  ? kinds.join(", ")
                  : tx(t, "diagnostic", "diagnostic"),
              ),
              h(
                "button",
                {
                  className: "hermes-kanban-attention-row-btn",
                  onClick: function () {
                    props.onOpen(task.id);
                  },
                  type: "button",
                },
                tx(t, "open", "Open"),
              ),
            );
          }),
        )
      : null,
  );
}