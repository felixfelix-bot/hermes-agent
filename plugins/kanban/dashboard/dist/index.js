(function(react) {
	//#region \0rolldown/runtime.js
	var __create = Object.create;
	var __defProp = Object.defineProperty;
	var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
	var __getOwnPropNames = Object.getOwnPropertyNames;
	var __getProtoOf = Object.getPrototypeOf;
	var __hasOwnProp = Object.prototype.hasOwnProperty;
	var __copyProps = (to, from, except, desc) => {
		if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
			key = keys[i];
			if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
				get: ((k) => from[k]).bind(null, key),
				enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
			});
		}
		return to;
	};
	var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", {
		value: mod,
		enumerable: true
	}) : target, mod));
	//#endregion
	react = __toESM(react, 1);
	//#region src/types.ts
	var API_BASE = "/api/plugins/kanban";
	var MIME_TASK = "text/x-hermes-task";
	var DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban";
	var COLUMN_ORDER = [
		"triage",
		"todo",
		"ready",
		"running",
		"blocked",
		"done"
	];
	var COLUMN_DOT = {
		triage: "hermes-kanban-dot-triage",
		todo: "hermes-kanban-dot-todo",
		ready: "hermes-kanban-dot-ready",
		running: "hermes-kanban-dot-running",
		blocked: "hermes-kanban-dot-blocked",
		done: "hermes-kanban-dot-done",
		archived: "hermes-kanban-dot-archived"
	};
	var LS_BOARD_KEY = "hermes.kanban.selectedBoard";
	//#endregion
	//#region src/api.ts
	function readSelectedBoard() {
		try {
			return (window.localStorage.getItem("hermes.kanban.selectedBoard") || "").trim() || null;
		} catch {
			return null;
		}
	}
	function writeSelectedBoard(slug) {
		try {
			if (slug) window.localStorage.setItem(LS_BOARD_KEY, slug);
			else window.localStorage.removeItem(LS_BOARD_KEY);
		} catch {}
	}
	function withBoard(url, board) {
		if (!board) return url;
		return `${url}${url.indexOf("?") >= 0 ? "&" : "?"}board=${encodeURIComponent(board)}`;
	}
	function tx(t, path, fallback, vars) {
		let node = t && t.kanban;
		if (node && typeof node === "object") {
			const parts = path.split(".");
			for (let i = 0; i < parts.length; i++) {
				const obj = node;
				if (obj && typeof obj === "object" && parts[i] in obj) node = obj[parts[i]];
				else {
					node = null;
					break;
				}
			}
		}
		let str = typeof node === "string" ? node : fallback;
		if (vars) for (const k in vars) str = str.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
		return str;
	}
	function getColumnLabel(t, status) {
		return tx(t, "columnLabels." + status, FALLBACK_COLUMN_LABEL_LOOKUP[status] || status);
	}
	function getColumnHelp(t, status) {
		return tx(t, "columnHelp." + status, FALLBACK_COLUMN_HELP_LOOKUP[status] || "");
	}
	var FALLBACK_COLUMN_LABEL_LOOKUP = {
		triage: "Triage",
		todo: "Todo",
		ready: "Ready",
		running: "In Progress",
		blocked: "Blocked",
		done: "Done",
		archived: "Archived"
	};
	var FALLBACK_COLUMN_HELP_LOOKUP = {
		triage: "Raw ideas — a specifier will flesh out the spec",
		todo: "Waiting on dependencies or unassigned",
		ready: "Dependencies satisfied; assign a profile to dispatch",
		running: "Claimed by a worker — in-flight",
		blocked: "Worker asked for human input",
		done: "Completed",
		archived: "Archived"
	};
	function parseApiErrorMessage(err) {
		const raw = err && err instanceof Error ? String(err.message) : String(err || "");
		const m = raw.match(/^(\d{3}):\s*(.*)$/s);
		const body = m ? m[2] : raw;
		try {
			const parsed = JSON.parse(body);
			if (parsed && typeof parsed.detail === "string") return parsed.detail;
			if (parsed && parsed.detail && typeof parsed.detail.message === "string") return parsed.detail.message;
		} catch {}
		return body || raw;
	}
	function selectChangeHandler(setter) {
		return {
			onValueChange: (v) => setter(v == null ? "" : v),
			onChange: (e) => {
				const v = e && typeof e === "object" && e.target ? e.target.value : String(e);
				setter(v == null ? "" : v);
			}
		};
	}
	function withCompletionSummary(patch, count, t) {
		if (!patch || patch.status !== "done") return patch;
		const label = count > 1 ? `${count} selected task(s)` : "this task";
		const value = window.prompt(tx(t, "completionSummary", "Completion summary for {label}. This is stored as the task result.", { label }), "");
		if (value === null) return null;
		const summary = value.trim();
		if (!summary) {
			window.alert(tx(t, "completionSummaryRequired", "Completion summary is required before marking a task done."));
			return null;
		}
		return {
			...patch,
			result: summary,
			summary
		};
	}
	var STALENESS = {
		ready: {
			amber: 3600,
			red: 1440 * 60
		},
		running: {
			amber: 600,
			red: 3600
		},
		blocked: {
			amber: 3600,
			red: 1440 * 60
		},
		todo: {
			amber: 10080 * 60,
			red: 720 * 60 * 60
		}
	};
	function stalenessClass(task) {
		if (!task || !task.age) return "";
		const age = task.status === "running" ? task.age.started_age_seconds : task.age.created_age_seconds;
		const tier = STALENESS[task.status];
		if (!tier || age == null) return "";
		if (age >= tier.red) return "hermes-kanban-card--stale-red";
		if (age >= tier.amber) return "hermes-kanban-card--stale-amber";
		return "";
	}
	function escapeHtml(s) {
		return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
	}
	function renderInline(esc) {
		return esc.replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>").replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_m, text, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`);
	}
	function renderMarkdown(src) {
		if (!src) return "";
		const blocks = [];
		const lines = escapeHtml(String(src).replace(/```([\s\S]*?)```/g, (_m, code) => {
			blocks.push(code);
			return `\u0000CODE${blocks.length - 1}\u0000`;
		})).split(/\r?\n/);
		const out = [];
		let inList = false;
		for (const line of lines) {
			const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
			const heading = /^(#{1,4})\s+(.*)$/.exec(line);
			if (bullet) {
				if (!inList) {
					out.push("<ul>");
					inList = true;
				}
				out.push(`<li>${renderInline(bullet[1])}</li>`);
				continue;
			}
			if (inList) {
				out.push("</ul>");
				inList = false;
			}
			if (heading) {
				const level = heading[1].length;
				out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
			} else if (line.trim() === "") out.push("");
			else out.push(`<p>${renderInline(line)}</p>`);
		}
		if (inList) out.push("</ul>");
		let html = out.join("\n");
		html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) => `<pre class="hermes-kanban-md-code"><code>${escapeHtml(blocks[Number(i)])}</code></pre>`);
		return html;
	}
	function fmtBytes(n) {
		n = Number(n) || 0;
		if (n < 1024) return n + " B";
		if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
		return (n / (1024 * 1024)).toFixed(1) + " MB";
	}
	function attachTouchDrag(el, taskId) {
		if (!el) return () => {};
		function onDown(e) {
			if (e.pointerType !== "touch") return;
			if (!el) return;
			e.preventDefault();
			const proxy = el.cloneNode(true);
			proxy.classList.add("hermes-kanban-touch-proxy");
			document.body.appendChild(proxy);
			let lastTarget = null;
			function move(ev) {
				proxy.style.left = `${ev.clientX - proxy.offsetWidth / 2}px`;
				proxy.style.top = `${ev.clientY - 24}px`;
				proxy.style.display = "none";
				const under = document.elementFromPoint(ev.clientX, ev.clientY);
				proxy.style.display = "";
				const col = under && under.closest ? under.closest("[data-kanban-column]") : null;
				const trash = under && under.closest ? under.closest("[data-kanban-trash]") : null;
				const target = col || trash;
				if (target !== lastTarget) {
					if (lastTarget) lastTarget.classList.remove("hermes-kanban-column--drop");
					if (target) target.classList.add("hermes-kanban-column--drop");
					lastTarget = target;
				}
			}
			function up() {
				document.removeEventListener("pointermove", move);
				document.removeEventListener("pointerup", up);
				document.removeEventListener("pointercancel", up);
				if (lastTarget) {
					lastTarget.classList.remove("hermes-kanban-column--drop");
					const status = lastTarget.getAttribute("data-kanban-column");
					if (lastTarget.hasAttribute("data-kanban-trash")) lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:delete", {
						detail: { taskId },
						bubbles: true
					}));
					else if (status) lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:drop", {
						detail: {
							taskId,
							status
						},
						bubbles: true
					}));
				}
				proxy.remove();
			}
			proxy.style.position = "fixed";
			proxy.style.pointerEvents = "none";
			proxy.style.opacity = "0.85";
			proxy.style.zIndex = "9999";
			proxy.style.width = `${el.offsetWidth}px`;
			proxy.style.left = `${e.clientX - el.offsetWidth / 2}px`;
			proxy.style.top = `${e.clientY - 24}px`;
			document.addEventListener("pointermove", move);
			document.addEventListener("pointerup", up);
			document.addEventListener("pointercancel", up);
		}
		el.addEventListener("pointerdown", onDown);
		return () => el.removeEventListener("pointerdown", onDown);
	}
	//#endregion
	//#region src/components/TaskCard.tsx
	var SDK$5 = (function() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	})();
	var h$5 = SDK$5.React.createElement;
	var { useEffect: useEffect$4, useRef: useRef$4 } = SDK$5.hooks;
	var { Card: Card$1, CardContent: CardContent$1, Badge, Checkbox: Checkbox$3 } = SDK$5.components;
	var { cn: cn$4, timeAgo: timeAgo$1 } = SDK$5.utils;
	var useI18n$5 = SDK$5.useI18n;
	function TaskCard(props) {
		const { t: i18n } = useI18n$5();
		const t = props.task;
		const cardRef = useRef$4(null);
		useEffect$4(function() {
			return attachTouchDrag(cardRef.current, t.id);
		}, [t.id]);
		const handleDragStart = function(e) {
			e.dataTransfer.setData(MIME_TASK, t.id);
			e.dataTransfer.effectAllowed = "move";
			const selectedCards = document.querySelectorAll(".hermes-kanban-card--selected");
			if (selectedCards.length > 1 && props.selected) {
				const ghost = document.createElement("div");
				ghost.className = "hermes-kanban-drag-ghost";
				ghost.textContent = selectedCards.length + " cards";
				document.body.appendChild(ghost);
				e.dataTransfer.setDragImage(ghost, 0, 0);
				requestAnimationFrame(function() {
					if (ghost.parentNode) document.body.removeChild(ghost);
				});
			}
		};
		const handleClick = function(e) {
			if (e.shiftKey) {
				e.preventDefault();
				e.stopPropagation();
				if (props.toggleRange) props.toggleRange(t.id);
				return;
			}
			if (e.ctrlKey || e.metaKey) {
				e.preventDefault();
				e.stopPropagation();
				props.toggleSelected(t.id, true);
				return;
			}
			props.onOpen(t.id);
		};
		const handleKeyDown = function(e) {
			if (e.key === "Enter" || e.key === " ") {
				e.preventDefault();
				props.onOpen(t.id);
			}
			if (e.key === "Escape") {
				if (props.toggleSelected) props.toggleSelected(t.id, false);
			}
		};
		const handleCheckedChange = function() {
			props.toggleSelected(t.id, true);
		};
		const progress = t.progress;
		const needsAssignee = t.status === "ready" && !t.assignee;
		return h$5("div", {
			ref: cardRef,
			"data-task-id": t.id,
			className: cn$4("hermes-kanban-card", props.selected ? "hermes-kanban-card--selected" : "", props.failed ? "hermes-kanban-card--failed" : "", props.draggingSource ? "hermes-kanban-card--dragging-source" : "", stalenessClass(t)),
			draggable: true,
			tabIndex: 0,
			role: "button",
			"aria-label": `${t.title || "untitled"} — ${t.id} — ${t.status}`,
			onDragStart: handleDragStart,
			onClick: handleClick,
			onKeyDown: handleKeyDown
		}, h$5(Card$1, null, h$5(CardContent$1, { className: "hermes-kanban-card-content" }, h$5("div", { className: "hermes-kanban-card-row" }, h$5("label", {
			className: "hermes-kanban-card-check-wrap",
			title: tx(i18n, "selectForBulk", "Select for bulk actions"),
			onClick: function(e) {
				e.stopPropagation();
			}
		}, h$5(Checkbox$3, {
			className: "hermes-kanban-card-check",
			checked: props.selected,
			onCheckedChange: handleCheckedChange,
			onClick: function(e) {
				e.stopPropagation();
			},
			"aria-label": `Select task ${t.id}`
		})), h$5("span", {
			className: "hermes-kanban-card-id",
			title: `Task id: ${t.id}. Use this id with kanban_show, /kanban show, or hermes kanban show.`
		}, t.id), t.warnings && t.warnings.count > 0 ? h$5("span", {
			className: cn$4("hermes-kanban-warning-badge", "hermes-kanban-warning-badge--" + (t.warnings.highest_severity || "warning")),
			title: `${t.warnings.count} active diagnostic` + (t.warnings.count === 1 ? "" : "s") + ` (severity: ${t.warnings.highest_severity || "warning"}). Click to open for details.`
		}, t.warnings.highest_severity === "critical" ? "!!!" : t.warnings.highest_severity === "error" ? "!!" : "⚠") : null, t.priority > 0 ? h$5(Badge, {
			className: "hermes-kanban-priority",
			title: `Priority ${t.priority}. Higher-priority tasks are claimed first by the dispatcher.`
		}, `P${t.priority}`) : null, t.tenant ? h$5(Badge, {
			variant: "outline",
			className: "hermes-kanban-tag",
			title: `Tenant: ${t.tenant}. Free-form tag for grouping tasks (customer, project, team).`
		}, t.tenant) : null, progress ? h$5("span", {
			className: cn$4("hermes-kanban-progress", progress.done === progress.total ? "hermes-kanban-progress--full" : ""),
			title: `${progress.done} of ${progress.total} child tasks done`
		}, `${progress.done}/${progress.total}`) : null, needsAssignee ? h$5(Badge, {
			variant: "outline",
			className: "hermes-kanban-needs-assignee",
			title: tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.")
		}, tx(i18n, "needsAssignee", "Needs assignee")) : null), h$5("div", { className: "hermes-kanban-card-title" }, t.title || tx(i18n, "untitled", "(untitled)")), h$5("div", { className: "hermes-kanban-card-row hermes-kanban-card-meta" }, t.assignee ? h$5("span", {
			className: "hermes-kanban-assignee",
			title: `Assigned to Hermes profile @${t.assignee}`
		}, "@", t.assignee) : h$5("span", {
			className: "hermes-kanban-unassigned",
			title: needsAssignee ? tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.") : "No profile assigned."
		}, tx(i18n, "unassigned", "unassigned")), t.comment_count && t.comment_count > 0 ? h$5("span", {
			className: "hermes-kanban-count",
			title: `${t.comment_count} comment${t.comment_count === 1 ? "" : "s"} on this task`
		}, "💬 ", t.comment_count) : null, t.link_counts && t.link_counts.parents + t.link_counts.children > 0 ? h$5("span", {
			className: "hermes-kanban-count",
			title: `${t.link_counts.parents} parent${t.link_counts.parents === 1 ? "" : "s"}, ${t.link_counts.children} child${t.link_counts.children === 1 ? "" : "ren"}. Children stay blocked until their parent is done.`
		}, "↔ ", t.link_counts.parents + t.link_counts.children) : null, h$5("span", {
			className: "hermes-kanban-ago",
			title: t.created_at ? `Created ${t.created_at}` : ""
		}, timeAgo$1 ? timeAgo$1(t.created_at) : "")))));
	}
	//#endregion
	//#region src/components/Column.tsx
	var SDK$4 = (function() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	})();
	var h$4 = SDK$4.React.createElement;
	var { useState: useState$4, useEffect: useEffect$3, useMemo: useMemo$1, useRef: useRef$3 } = SDK$4.hooks;
	var { Button: Button$3, Input: Input$3, Select: Select$3, SelectOption: SelectOption$3, Checkbox: Checkbox$2 } = SDK$4.components;
	var { cn: cn$3 } = SDK$4.utils;
	var useI18n$4 = SDK$4.useI18n;
	function InlineCreate(props) {
		const { t } = useI18n$4();
		const [title, setTitle] = useState$4("");
		const [assignee, setAssignee] = useState$4("");
		const [priority, setPriority] = useState$4(0);
		const [parent, setParent] = useState$4("");
		const [skills, setSkills] = useState$4("");
		const [workspaceKind, setWorkspaceKind] = useState$4("scratch");
		const [workspacePath, setWorkspacePath] = useState$4("");
		const [goalMode, setGoalMode] = useState$4(false);
		const [goalMaxTurns, setGoalMaxTurns] = useState$4("");
		const submit = function() {
			const trimmed = title.trim();
			if (!trimmed) return;
			const body = {
				title: trimmed,
				assignee: assignee.trim() || null,
				priority: Number(priority) || 0,
				triage: props.columnName === "triage"
			};
			if (parent) body.parents = [parent];
			const skillList = skills.split(",").map(function(s) {
				return s.trim();
			}).filter(function(s) {
				return s.length > 0;
			});
			if (skillList.length > 0) body.skills = skillList;
			if (workspaceKind && workspaceKind !== "scratch") body.workspace_kind = workspaceKind;
			const wpTrim = workspacePath.trim();
			if (wpTrim) body.workspace_path = wpTrim;
			if (goalMode) {
				body.goal_mode = true;
				const gmt = parseInt(goalMaxTurns, 10);
				if (Number.isFinite(gmt) && gmt > 0) body.goal_max_turns = gmt;
			}
			props.onSubmit(body);
			setTitle("");
			setAssignee("");
			setPriority(0);
			setParent("");
			setSkills("");
			setWorkspaceKind("scratch");
			setWorkspacePath("");
			setGoalMode(false);
			setGoalMaxTurns("");
		};
		const showPathInput = workspaceKind !== "scratch";
		const pathPlaceholder = workspaceKind === "dir" ? tx(t, "workspacePathDir", "workspace path (required, e.g. ~/projects/my-app)") : tx(t, "workspacePathOptional", "workspace path (optional, derived from assignee if blank)");
		return h$4("div", { className: "hermes-kanban-inline-create" }, h$4("textarea", {
			value: title,
			onChange: function(e) {
				setTitle(e.target.value);
			},
			onKeyDown: function(e) {
				if (e.key === "Enter" && !e.shiftKey) {
					e.preventDefault();
					submit();
				}
				if (e.key === "Escape") props.onCancel();
			},
			placeholder: props.columnName === "triage" ? tx(t, "triagePlaceholder", "Rough idea — AI will spec it…") : tx(t, "taskTitlePlaceholder", "New task title…"),
			autoFocus: true,
			className: "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
			rows: 2
		}), h$4("div", { className: "flex gap-2" }, h$4(Input$3, {
			value: assignee,
			onChange: function(e) {
				setAssignee(e.target.value);
			},
			placeholder: props.columnName === "triage" ? tx(t, "specifier", "specifier") : tx(t, "assigneePlaceholder", "assignee"),
			className: "h-7 text-xs flex-1",
			title: props.columnName === "triage" ? "Hermes profile that will spec this task (default: the dispatcher's configured specifier). Leave blank to let the dispatcher pick." : "Hermes profile to assign. Leave blank and the dispatcher will pick from available profiles when the task is Ready.",
			style: { textTransform: "none" },
			autoCapitalize: "none",
			autoCorrect: "off",
			spellCheck: false
		}), h$4(Input$3, {
			type: "number",
			value: priority,
			onChange: function(e) {
				setPriority(e.target.value);
			},
			placeholder: "pri",
			className: "h-7 text-xs w-16",
			title: "Priority. Higher-priority tasks are claimed first by the dispatcher. 0 = default."
		})), h$4(Input$3, {
			value: skills,
			onChange: function(e) {
				setSkills(e.target.value);
			},
			placeholder: tx(t, "skillsPlaceholder", "skills (optional, comma-separated): translation, github-code-review"),
			title: "Force-load these skills into the worker (in addition to the built-in kanban-worker).",
			className: "h-7 text-xs"
		}), h$4("div", { className: "flex gap-2 items-center" }, h$4("label", {
			className: "flex items-center gap-1.5 text-xs cursor-pointer select-none",
			title: "Goal mode: the worker keeps going in the same session until a judge agrees the card is done (or the turn budget runs out, which blocks it for review). Best for open-ended cards one shot rarely finishes."
		}, h$4("input", {
			type: "checkbox",
			checked: goalMode,
			onChange: function(e) {
				setGoalMode(!!e.target.checked);
			},
			className: "h-3.5 w-3.5 accent-current"
		}), tx(t, "goalMode", "goal mode")), goalMode ? h$4(Input$3, {
			type: "number",
			value: goalMaxTurns,
			onChange: function(e) {
				setGoalMaxTurns(e.target.value);
			},
			placeholder: tx(t, "goalMaxTurns", "max turns (default 20)"),
			className: "h-7 text-xs w-40",
			title: "Turn budget for the goal loop. Blank = backend default (20).",
			min: 1
		}) : null), h$4("div", { className: "flex gap-2" }, h$4(Select$3, Object.assign({
			value: workspaceKind,
			title: "scratch: isolated temp dir (default). worktree: git worktree on the assignee profile. dir: exact path (required below).",
			className: "h-7 text-xs w-28"
		}, selectChangeHandler(setWorkspaceKind)), h$4(SelectOption$3, { value: "scratch" }, "scratch"), h$4(SelectOption$3, { value: "worktree" }, "worktree"), h$4(SelectOption$3, { value: "dir" }, "dir")), showPathInput ? h$4(Input$3, {
			value: workspacePath,
			onChange: function(e) {
				setWorkspacePath(e.target.value);
			},
			placeholder: pathPlaceholder,
			className: "h-7 text-xs flex-1"
		}) : null), h$4(Select$3, Object.assign({
			value: parent,
			className: "h-7 text-xs",
			title: "Optional parent task. A child stays blocked in its current column until the parent is marked done."
		}, selectChangeHandler(setParent)), h$4(SelectOption$3, { value: "" }, tx(t, "noParent", "— no parent —")), (props.allTasks || []).map(function(task) {
			return h$4(SelectOption$3, {
				key: task.id,
				value: task.id
			}, `${task.id} — ${(task.title || "").slice(0, 50)}`);
		})), h$4("div", { className: "flex gap-2" }, h$4(Button$3, {
			onClick: submit,
			size: "sm"
		}, "Create"), h$4(Button$3, {
			onClick: props.onCancel,
			size: "sm"
		}, tx(t, "cancel", "Cancel"))));
	}
	function Column(props) {
		const { t } = useI18n$4();
		const [dragOver, setDragOver] = useState$4(false);
		const [showCreate, setShowCreate] = useState$4(false);
		const colRef = useRef$3(null);
		useEffect$3(function() {
			if (!colRef.current) return void 0;
			const el = colRef.current;
			function onTouchDrop(e) {
				const detail = e.detail;
				if (detail && detail.status === props.column.name) {
					const taskId = detail.taskId;
					if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1 && props.onMoveSelected) props.onMoveSelected(props.column.name);
					else props.onMove(taskId, props.column.name);
				}
			}
			el.addEventListener("hermes-kanban:drop", onTouchDrop);
			return function() {
				el.removeEventListener("hermes-kanban:drop", onTouchDrop);
			};
		}, [
			props.column.name,
			props.onMove,
			props.selectedIds,
			props.onMoveSelected
		]);
		const handleDragOver = function(e) {
			e.preventDefault();
			e.dataTransfer.dropEffect = "move";
			if (!dragOver) setDragOver(true);
		};
		const handleDragLeave = function() {
			setDragOver(false);
		};
		const handleDrop = function(e) {
			e.preventDefault();
			setDragOver(false);
			const taskId = e.dataTransfer.getData(MIME_TASK);
			if (!taskId) return;
			if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
				if (props.onMoveSelected) props.onMoveSelected(props.column.name);
			} else props.onMove(taskId, props.column.name);
		};
		const lanes = useMemo$1(function() {
			if (!props.laneByProfile || props.column.name !== "running") return null;
			const byProfile = {};
			for (const tk of props.column.tasks) {
				const key = tk.assignee || "(unassigned)";
				(byProfile[key] = byProfile[key] || []).push(tk);
			}
			return Object.keys(byProfile).sort().map(function(k) {
				return {
					assignee: k,
					tasks: byProfile[k]
				};
			});
		}, [props.column, props.laneByProfile]);
		const colHelp = getColumnHelp(t, props.column.name);
		const colLabel = getColumnLabel(t, props.column.name);
		return h$4("div", {
			ref: colRef,
			"data-kanban-column": props.column.name,
			className: cn$3("hermes-kanban-column", dragOver ? "hermes-kanban-column--drop" : ""),
			onDragOver: handleDragOver,
			onDragLeave: handleDragLeave,
			onDrop: handleDrop
		}, h$4("div", {
			className: "hermes-kanban-column-header",
			title: colHelp || ""
		}, h$4(Checkbox$2, {
			className: "hermes-kanban-col-check",
			title: "Select all tasks in this column",
			"aria-label": `Select all tasks in ${colLabel || props.column.name}`,
			checked: props.column.tasks.length > 0 && props.column.tasks.every(function(t) {
				return props.selectedIds.has(t.id);
			}),
			onCheckedChange: function() {
				if (props.selectAllInColumn) props.selectAllInColumn(props.column.name);
			},
			onClick: function(e) {
				e.stopPropagation();
			}
		}), h$4("span", { className: cn$3("hermes-kanban-dot", COLUMN_DOT[props.column.name]) }), h$4("span", { className: "hermes-kanban-column-label" }, colLabel || props.column.name), h$4("span", {
			className: "hermes-kanban-column-count",
			title: `${props.column.tasks.length} task${props.column.tasks.length === 1 ? "" : "s"} in this column`
		}, props.column.tasks.length), h$4("button", {
			type: "button",
			className: "hermes-kanban-column-add",
			title: tx(t, "createTask", "Create task in this column"),
			onClick: function() {
				setShowCreate(function(v) {
					return !v;
				});
			}
		}, showCreate ? "×" : "+")), h$4("div", { className: "hermes-kanban-column-sub" }, colHelp || ""), showCreate ? h$4(InlineCreate, {
			columnName: props.column.name,
			allTasks: props.allTasks,
			onSubmit: function(body) {
				props.onCreate(body).then(function() {
					setShowCreate(false);
				});
			},
			onCancel: function() {
				setShowCreate(false);
			}
		}) : null, h$4("div", { className: "hermes-kanban-column-body" }, props.column.tasks.length === 0 ? h$4("div", { className: "hermes-kanban-empty" }, tx(t, "noTasks", "— no tasks —")) : lanes ? lanes.map(function(lane) {
			return h$4("div", {
				key: lane.assignee,
				className: "hermes-kanban-lane"
			}, h$4("div", { className: "hermes-kanban-lane-head" }, h$4("span", { className: "hermes-kanban-lane-name" }, lane.assignee), h$4("span", { className: "hermes-kanban-lane-count" }, lane.tasks.length)), lane.tasks.map(function(tk) {
				return h$4(TaskCard, {
					key: tk.id,
					task: tk,
					selected: props.selectedIds.has(tk.id),
					failed: !!(props.failedIds && props.failedIds.has(tk.id)),
					draggingTaskId: props.draggingTaskId,
					draggingSource: !!props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
					toggleSelected: props.toggleSelected,
					toggleRange: props.toggleRange,
					onOpen: props.onOpen
				});
			}));
		}) : props.column.tasks.map(function(tk) {
			return h$4(TaskCard, {
				key: tk.id,
				task: tk,
				selected: props.selectedIds.has(tk.id),
				failed: !!(props.failedIds && props.failedIds.has(tk.id)),
				draggingTaskId: props.draggingTaskId,
				draggingSource: !!props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
				toggleSelected: props.toggleSelected,
				toggleRange: props.toggleRange,
				onOpen: props.onOpen
			});
		})));
	}
	//#endregion
	//#region src/components/BoardColumns.tsx
	var SDK$3 = (function() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	})();
	var h$3 = SDK$3.React.createElement;
	var { useState: useState$3, useEffect: useEffect$2, useRef: useRef$2, useCallback: useCallback$2 } = SDK$3.hooks;
	var { cn: cn$2 } = SDK$3.utils;
	var useI18n$3 = SDK$3.useI18n;
	var FALLBACK_TRASH$1 = {
		label: "Trash",
		title: "Drag a card here to permanently delete it",
		confirm: "Permanently delete this task? This cannot be undone.",
		confirmMany: "Permanently delete {n} selected tasks? This cannot be undone.",
		dropHint: "Drop to delete"
	};
	function TrashDropZone(props) {
		const { t } = useI18n$3();
		const [dragOver, setDragOver] = useState$3(false);
		const zoneRef = useRef$2(null);
		useEffect$2(function() {
			if (!zoneRef.current) return void 0;
			const el = zoneRef.current;
			function onTouchDelete(e) {
				const detail = e.detail;
				const taskId = detail && detail.taskId;
				if (taskId && props.onDelete) props.onDelete(taskId);
			}
			el.addEventListener("hermes-kanban:delete", onTouchDelete);
			return function() {
				el.removeEventListener("hermes-kanban:delete", onTouchDelete);
			};
		}, [props.onDelete]);
		const handleDragOver = function(e) {
			e.preventDefault();
			e.dataTransfer.dropEffect = "move";
			if (!dragOver) setDragOver(true);
		};
		const handleDragLeave = function() {
			setDragOver(false);
		};
		const handleDrop = function(e) {
			e.preventDefault();
			setDragOver(false);
			const taskId = e.dataTransfer.getData(MIME_TASK);
			if (!taskId) return;
			if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
				if (window.confirm(tx(t, "trash.confirmMany", FALLBACK_TRASH$1.confirmMany, { n: props.selectedIds.size }))) {
					const ids = Array.from(props.selectedIds);
					Promise.all(ids.map(function(id) {
						return props.onDelete(id);
					})).catch(function() {});
				}
			} else props.onDelete(taskId);
		};
		return h$3("div", {
			ref: zoneRef,
			"data-kanban-trash": "true",
			className: cn$2("hermes-kanban-trash", dragOver ? "hermes-kanban-trash--drop" : "", props.draggingTaskId ? "hermes-kanban-trash--active" : ""),
			onDragOver: handleDragOver,
			onDragLeave: handleDragLeave,
			onDrop: handleDrop
		}, h$3("span", { className: "hermes-kanban-trash-icon" }, "🗑️"), h$3("span", { className: "hermes-kanban-trash-label" }, tx(t, "trash.dropHint", FALLBACK_TRASH$1.dropHint)));
	}
	function BoardColumns(props) {
		const handleDragStart = useCallback$2(function(e) {
			const target = e.target;
			const card = target.closest && target.closest(".hermes-kanban-card");
			if (!card) return;
			const taskId = card.getAttribute("data-task-id");
			if (taskId && props.onDragStart) props.onDragStart(taskId);
		}, [props.onDragStart]);
		const handleDragEnd = useCallback$2(function() {
			if (props.onDragEnd) props.onDragEnd();
		}, [props.onDragEnd]);
		const order = [...COLUMN_ORDER];
		const cols = [];
		const seen = /* @__PURE__ */ new Set();
		for (const name of order) {
			const col = props.board.columns.find(function(c) {
				return c.name === name;
			});
			if (col) {
				cols.push(col);
				seen.add(name);
			}
		}
		for (const col of props.board.columns) if (!seen.has(col.name)) cols.push(col);
		return h$3("div", {
			className: "hermes-kanban-columns",
			onDragStart: handleDragStart,
			onDragEnd: handleDragEnd
		}, cols.map(function(col) {
			return h$3(Column, {
				key: col.name,
				column: col,
				laneByProfile: props.laneByProfile,
				selectedIds: props.selectedIds,
				failedIds: props.failedIds,
				draggingTaskId: props.draggingTaskId,
				toggleSelected: props.toggleSelected,
				toggleRange: props.toggleRange,
				selectAllInColumn: props.selectAllInColumn,
				onMove: props.onMove,
				onMoveSelected: props.onMoveSelected,
				onOpen: props.onOpen,
				onCreate: props.onCreate,
				allTasks: props.allTasks
			});
		}), h$3(TrashDropZone, {
			draggingTaskId: props.draggingTaskId,
			selectedIds: props.selectedIds,
			onDelete: props.onDelete
		}));
	}
	//#endregion
	//#region src/components/TaskDrawer.tsx
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
	var SDK$2 = (function() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	})();
	var h$2 = SDK$2.React.createElement;
	var { useState: useState$2, useEffect: useEffect$1, useCallback: useCallback$1, useRef: useRef$1 } = SDK$2.hooks;
	var { Button: Button$2, Input: Input$2, Select: Select$2, SelectOption: SelectOption$2 } = SDK$2.components;
	var { cn: cn$1, timeAgo } = SDK$2.utils;
	var useI18n$2 = SDK$2.useI18n || (() => ({
		t: { kanban: null },
		locale: "en"
	}));
	var API = API_BASE;
	var DESTRUCTIVE_KEYS$1 = {
		done: "confirmDone",
		archived: "confirmArchive",
		blocked: "confirmBlocked"
	};
	var FALLBACK_DESTRUCTIVE$1 = {
		done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
		archived: "Archive this task? It disappears from the default board view.",
		blocked: "Mark this task as blocked? The worker's claim is released."
	};
	var DIAGNOSTIC_EVENT_KIND_KEYS = {
		completion_blocked_hallucination: "completionBlockedHallucination",
		suspected_hallucinated_references: "suspectedHallucinatedReferences"
	};
	var FALLBACK_DIAGNOSTIC_EVENT_LABELS = {
		completion_blocked_hallucination: "⚠ Completion blocked — phantom card ids",
		suspected_hallucinated_references: "⚠ Prose referenced phantom card ids"
	};
	function getDestructiveConfirm$1(t, status) {
		const key = DESTRUCTIVE_KEYS$1[status];
		if (!key) return null;
		return tx(t, key, FALLBACK_DESTRUCTIVE$1[status] || "");
	}
	function isDiagnosticEvent(kind) {
		return Object.prototype.hasOwnProperty.call(FALLBACK_DIAGNOSTIC_EVENT_LABELS, kind);
	}
	function getDiagnosticEventLabel(t, kind) {
		const key = DIAGNOSTIC_EVENT_KIND_KEYS[kind];
		if (!key) return null;
		return tx(t, key, FALLBACK_DIAGNOSTIC_EVENT_LABELS[kind]);
	}
	function phantomIdsFromEvent(ev) {
		if (!ev || !ev.payload) return [];
		const p = ev.payload;
		const phantom = p.phantom_cards || p.phantom_refs;
		return Array.isArray(phantom) ? phantom : [];
	}
	function MarkdownBlock(props) {
		if (!(props.enabled !== false)) return h$2("pre", { className: "hermes-kanban-pre" }, props.source || "");
		return h$2("div", {
			className: "hermes-kanban-md",
			dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || "") }
		});
	}
	function MetaRow(props) {
		return h$2("div", { className: "hermes-kanban-meta-row" }, h$2("span", { className: "hermes-kanban-meta-label" }, props.label), h$2("span", { className: "hermes-kanban-meta-value" }, props.value));
	}
	function TitleEditor(props) {
		const { t } = useI18n$2();
		const [v, setV] = useState$2(props.initial);
		const save = function() {
			const trimmed = v.trim();
			if (!trimmed) return;
			props.onSave(trimmed);
		};
		return h$2("div", { className: "hermes-kanban-edit-row" }, h$2(Input$2, {
			value: v,
			autoFocus: true,
			onChange: function(e) {
				setV(e.target.value);
			},
			onKeyDown: function(e) {
				if (e.key === "Enter") {
					e.preventDefault();
					save();
				}
				if (e.key === "Escape") props.onCancel();
			},
			className: "h-8 text-sm flex-1"
		}), h$2(Button$2, {
			onClick: save,
			size: "sm"
		}, tx(t, "save", "Save")), h$2(Button$2, {
			onClick: props.onCancel,
			size: "sm"
		}, tx(t, "cancel", "Cancel")));
	}
	function AssigneeEditor(props) {
		const { t } = useI18n$2();
		const [editing, setEditing] = useState$2(false);
		const [v, setV] = useState$2(props.task.assignee || "");
		useEffect$1(function() {
			setV(props.task.assignee || "");
		}, [props.task.assignee]);
		if (!editing) return h$2("div", { className: "hermes-kanban-meta-row" }, h$2("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")), h$2("span", {
			className: "hermes-kanban-meta-value hermes-kanban-editable",
			onClick: function() {
				setEditing(true);
			},
			title: tx(t, "clickToEditAssignee", "Click to edit assignee")
		}, props.task.assignee || tx(t, "unassigned", "unassigned")));
		const save = function() {
			props.onPatch({ assignee: v.trim() || "" }).then(function() {
				setEditing(false);
			});
		};
		return h$2("div", { className: "hermes-kanban-meta-row" }, h$2("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")), h$2(Input$2, {
			value: v,
			autoFocus: true,
			onChange: function(e) {
				setV(e.target.value);
			},
			onKeyDown: function(e) {
				if (e.key === "Enter") {
					e.preventDefault();
					save();
				}
				if (e.key === "Escape") setEditing(false);
			},
			placeholder: tx(t, "emptyAssignee", "(empty = unassign)"),
			className: "h-7 text-xs flex-1",
			style: { textTransform: "none" },
			autoCapitalize: "none",
			autoCorrect: "off",
			spellCheck: false
		}));
	}
	function PriorityEditor(props) {
		const { t } = useI18n$2();
		const [editing, setEditing] = useState$2(false);
		const [v, setV] = useState$2(String(props.task.priority || 0));
		useEffect$1(function() {
			setV(String(props.task.priority || 0));
		}, [props.task.priority]);
		if (!editing) return h$2("div", { className: "hermes-kanban-meta-row" }, h$2("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")), h$2("span", {
			className: "hermes-kanban-meta-value hermes-kanban-editable",
			onClick: function() {
				setEditing(true);
			},
			title: tx(t, "clickToEdit", "Click to edit")
		}, String(props.task.priority)));
		const save = function() {
			props.onPatch({ priority: Number(v) || 0 }).then(function() {
				setEditing(false);
			});
		};
		return h$2("div", { className: "hermes-kanban-meta-row" }, h$2("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")), h$2(Input$2, {
			type: "number",
			value: v,
			autoFocus: true,
			onChange: function(e) {
				setV(e.target.value);
			},
			onKeyDown: function(e) {
				if (e.key === "Enter") {
					e.preventDefault();
					save();
				}
				if (e.key === "Escape") setEditing(false);
			},
			className: "h-7 text-xs w-20"
		}));
	}
	function BodyEditor(props) {
		const { t } = useI18n$2();
		const [editing, setEditing] = useState$2(false);
		const [v, setV] = useState$2(props.task.body || "");
		useEffect$1(function() {
			setV(props.task.body || "");
		}, [props.task.body]);
		const save = function() {
			props.onPatch({ body: v }).then(function() {
				setEditing(false);
			});
		};
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head-row" }, h$2("span", { className: "hermes-kanban-section-head" }, tx(t, "description", "Description")), editing ? h$2("div", { className: "flex gap-1" }, h$2(Button$2, {
			onClick: save,
			size: "sm"
		}, tx(t, "save", "Save")), h$2(Button$2, {
			onClick: function() {
				setEditing(false);
				setV(props.task.body || "");
			},
			size: "sm"
		}, tx(t, "cancel", "Cancel"))) : h$2("button", {
			type: "button",
			onClick: function() {
				setEditing(true);
			},
			className: "hermes-kanban-edit-link",
			title: "Edit description"
		}, tx(t, "edit", "edit"))), editing ? h$2("textarea", {
			className: "hermes-kanban-textarea",
			value: v,
			rows: 8,
			onChange: function(e) {
				setV(e.target.value);
			}
		}) : props.task.body ? h$2(MarkdownBlock, {
			source: props.task.body,
			enabled: props.renderMarkdown
		}) : h$2("div", { className: "text-xs text-muted-foreground italic" }, tx(t, "noDescription", "— no description —")));
	}
	function DependencyEditor(props) {
		const { t } = useI18n$2();
		const { task, links, allTasks } = props;
		const [newParent, setNewParent] = useState$2("");
		const [newChild, setNewChild] = useState$2("");
		const candidatesFor = function(excludeSet) {
			return (allTasks || []).filter(function(tk) {
				return tk.id !== task.id && !excludeSet.has(tk.id);
			});
		};
		const parentExclude = /* @__PURE__ */ new Set([task.id, ...links.parents || []]);
		const childExclude = /* @__PURE__ */ new Set([task.id, ...links.children || []]);
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, tx(t, "dependencies", "Dependencies")), h$2("div", { className: "hermes-kanban-deps-row" }, h$2("span", { className: "hermes-kanban-deps-label" }, tx(t, "parents", "Parents:")), h$2("div", { className: "hermes-kanban-deps-chips" }, (links.parents || []).length === 0 ? h$2("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none")) : (links.parents || []).map(function(id) {
			return h$2("span", {
				key: id,
				className: "hermes-kanban-dep-chip"
			}, id, h$2("button", {
				type: "button",
				className: "hermes-kanban-dep-chip-x",
				onClick: function() {
					props.onRemoveParent(id);
				},
				title: tx(t, "removeDependency", "Remove dependency")
			}, "×"));
		}))), h$2("div", { className: "hermes-kanban-deps-row" }, h$2(Select$2, Object.assign({
			value: newParent,
			className: "h-7 text-xs flex-1"
		}, selectChangeHandler(setNewParent)), h$2(SelectOption$2, { value: "" }, tx(t, "addParent", "— add parent —")), candidatesFor(parentExclude).map(function(tk) {
			return h$2(SelectOption$2, {
				key: tk.id,
				value: tk.id
			}, `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
		})), h$2(Button$2, {
			onClick: function() {
				if (!newParent) return;
				props.onAddParent(newParent).then(function() {
					setNewParent("");
				});
			},
			disabled: !newParent,
			size: "sm"
		}, "+ parent")), h$2("div", { className: "hermes-kanban-deps-row" }, h$2("span", { className: "hermes-kanban-deps-label" }, tx(t, "children", "Children:")), h$2("div", { className: "hermes-kanban-deps-chips" }, (links.children || []).length === 0 ? h$2("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none")) : (links.children || []).map(function(id) {
			return h$2("span", {
				key: id,
				className: "hermes-kanban-dep-chip"
			}, id, h$2("button", {
				type: "button",
				className: "hermes-kanban-dep-chip-x",
				onClick: function() {
					props.onRemoveChild(id);
				},
				title: tx(t, "removeDependency", "Remove dependency")
			}, "×"));
		}))), h$2("div", { className: "hermes-kanban-deps-row" }, h$2(Select$2, Object.assign({
			value: newChild,
			className: "h-7 text-xs flex-1"
		}, selectChangeHandler(setNewChild)), h$2(SelectOption$2, { value: "" }, tx(t, "addChild", "— add child —")), candidatesFor(childExclude).map(function(tk) {
			return h$2(SelectOption$2, {
				key: tk.id,
				value: tk.id
			}, `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
		})), h$2(Button$2, {
			onClick: function() {
				if (!newChild) return;
				props.onAddChild(newChild).then(function() {
					setNewChild("");
				});
			},
			disabled: !newChild,
			size: "sm"
		}, "+ child")));
	}
	function StatusActions(props) {
		const { t } = useI18n$2();
		const task = props.task;
		const [specifyBusy, setSpecifyBusy] = useState$2(false);
		const [specifyMsg, setSpecifyMsg] = useState$2(null);
		const [decomposeBusy, setDecomposeBusy] = useState$2(false);
		const [decomposeMsg, setDecomposeMsg] = useState$2(null);
		const b = function(label, patch, enabled, confirmMsg) {
			return h$2(Button$2, {
				onClick: function() {
					if (enabled !== false) props.onPatch(patch, { confirm: confirmMsg || void 0 });
				},
				disabled: enabled === false,
				size: "sm"
			}, label);
		};
		return h$2("div", null, h$2("div", { className: "hermes-kanban-actions" }, task.status === "triage" && props.onSpecify ? h$2(Button$2, {
			onClick: function() {
				if (specifyBusy) return;
				setSpecifyBusy(true);
				setSpecifyMsg(null);
				props.onSpecify().then(function(res) {
					if (res && res.ok) {
						const suffix = res.new_title ? ` — retitled: ${res.new_title}` : "";
						setSpecifyMsg({
							ok: true,
							text: `Specified${suffix}`
						});
					} else setSpecifyMsg({
						ok: false,
						text: "Specify failed: " + (res && res.reason || "unknown error")
					});
				}).catch(function(err) {
					setSpecifyMsg({
						ok: false,
						text: "Specify failed: " + (err.message || String(err))
					});
				}).then(function() {
					setSpecifyBusy(false);
				});
			},
			disabled: specifyBusy,
			size: "sm"
		}, specifyBusy ? "Specifying…" : "✨ Specify") : null, task.status === "triage" && props.onDecompose ? h$2(Button$2, {
			onClick: function() {
				if (decomposeBusy) return;
				setDecomposeBusy(true);
				setDecomposeMsg(null);
				props.onDecompose().then(function(res) {
					if (res && res.ok) if (res.fanout && res.child_ids && res.child_ids.length) setDecomposeMsg({
						ok: true,
						text: `Decomposed into ${res.child_ids.length} children: ${res.child_ids.join(", ")}`
					});
					else {
						const suffix = res.new_title ? ` — retitled: ${res.new_title}` : "";
						setDecomposeMsg({
							ok: true,
							text: `Single task (no fanout)${suffix}`
						});
					}
					else setDecomposeMsg({
						ok: false,
						text: "Decompose failed: " + (res && res.reason || "unknown error")
					});
				}).catch(function(err) {
					setDecomposeMsg({
						ok: false,
						text: "Decompose failed: " + (err.message || String(err))
					});
				}).then(function() {
					setDecomposeBusy(false);
				});
			},
			disabled: decomposeBusy,
			size: "sm"
		}, decomposeBusy ? "Decomposing…" : "⚗ Decompose") : null, b("→ triage", { status: "triage" }, task.status !== "triage"), b("→ ready", { status: "ready" }, task.status !== "ready"), b(tx(t, "block", "Block"), { status: "blocked" }, task.status === "running" || task.status === "ready", getDestructiveConfirm$1(t, "blocked")), b(tx(t, "unblock", "Unblock"), { status: "ready" }, task.status === "blocked"), b(tx(t, "complete", "Complete"), { status: "done" }, task.status === "running" || task.status === "ready" || task.status === "blocked", getDestructiveConfirm$1(t, "done")), b(tx(t, "archive", "Archive"), { status: "archived" }, task.status !== "archived", getDestructiveConfirm$1(t, "archived"))), specifyMsg ? h$2("div", { className: specifyMsg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err" }, specifyMsg.text) : null, decomposeMsg ? h$2("div", { className: decomposeMsg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err" }, decomposeMsg.text) : null);
	}
	function HomeSubsSection(props) {
		const { t } = useI18n$2();
		const channels = props.homeChannels || [];
		if (channels.length === 0) return null;
		const busy = props.homeBusy || {};
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, tx(t, "notifyHomeChannels", "Notify home channels")), h$2("div", { className: "hermes-kanban-home-subs" }, channels.map(function(hc) {
			const isBusy = !!busy[hc.platform];
			const label = hc.subscribed ? "✓ " + hc.platform : hc.platform;
			const target = `${hc.name} (${hc.chat_id}${hc.thread_id ? " / " + hc.thread_id : ""})`;
			const title = hc.subscribed ? `${tx(t, "sendingUpdates", "Sending updates to")} ${target}. Click to stop.` : `${tx(t, "sendNotifications", "Send completed / blocked / gave_up notifications to")} ${target}.`;
			return h$2(Button$2, {
				key: hc.platform,
				size: "sm",
				title,
				disabled: isBusy || !props.onToggle,
				onClick: function() {
					if (props.onToggle) props.onToggle(hc.platform, hc.subscribed);
				},
				className: hc.subscribed ? "hermes-kanban-home-sub hermes-kanban-home-sub--on" : "hermes-kanban-home-sub"
			}, label);
		})));
	}
	function AttachmentsSection(props) {
		const i18n = props.i18n;
		const atts = props.attachments || [];
		const fileRef = useRef$1(null);
		const [dlErr, setDlErr] = useState$2(null);
		function downloadAttachment(a) {
			const url = withBoard(`${API}/attachments/${a.id}`, props.boardSlug);
			setDlErr(null);
			SDK$2.authedFetch(url).then(function(resp) {
				if (!resp.ok) return resp.text().then(function(txt) {
					throw new Error(parseApiErrorMessage(/* @__PURE__ */ new Error(resp.status + ": " + txt)));
				});
				return resp.blob();
			}).then(function(blob) {
				const objUrl = URL.createObjectURL(blob);
				const link = document.createElement("a");
				link.href = objUrl;
				link.download = a.filename || "attachment";
				document.body.appendChild(link);
				link.click();
				document.body.removeChild(link);
				setTimeout(function() {
					URL.revokeObjectURL(objUrl);
				}, 1e4);
			}).catch(function(e) {
				setDlErr(String(e.message || e));
			});
		}
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, `${tx(i18n, "attachments", "Attachments")} (${atts.length})`), h$2("input", {
			ref: function(el) {
				fileRef.current = el;
			},
			type: "file",
			multiple: true,
			style: { display: "none" },
			onChange: function(e) {
				if (props.onUpload && e.target.files) props.onUpload(e.target.files);
				try {
					e.target.value = "";
				} catch (_e) {}
			}
		}), h$2("div", { className: "flex items-center gap-2 mb-2" }, h$2(Button$2, {
			size: "sm",
			variant: "outline",
			disabled: !!props.uploadBusy,
			onClick: function() {
				if (fileRef.current) fileRef.current.click();
			}
		}, props.uploadBusy ? tx(i18n, "uploading", "Uploading…") : tx(i18n, "uploadFile", "Upload file"))), props.uploadErr || dlErr ? h$2("div", { className: "text-xs text-destructive mb-2" }, props.uploadErr || dlErr) : null, atts.length === 0 ? h$2("div", { className: "text-xs text-muted-foreground" }, tx(i18n, "noAttachments", "— no attachments —")) : atts.map(function(a) {
			return h$2("div", {
				key: a.id,
				className: "flex items-center justify-between gap-2 py-1 text-sm"
			}, h$2("button", {
				type: "button",
				className: "hermes-kanban-attachment-link truncate",
				title: a.filename,
				onClick: function() {
					downloadAttachment(a);
				}
			}, a.filename), h$2("span", { className: "text-xs text-muted-foreground whitespace-nowrap" }, fmtBytes(a.size)), h$2("button", {
				type: "button",
				className: "hermes-kanban-drawer-close",
				title: tx(i18n, "removeAttachment", "Remove attachment"),
				onClick: function() {
					if (window.confirm(tx(i18n, "confirmRemoveAttachment", "Remove this attachment?"))) {
						if (props.onDelete) props.onDelete(a.id);
					}
				}
			}, "×"));
		}));
	}
	function RunHistorySection(props) {
		const { t } = useI18n$2();
		const runs = props.runs || [];
		const [expanded, setExpanded] = useState$2(false);
		if (runs.length === 0) return null;
		const showAll = expanded || runs.length <= 3;
		const visible = showAll ? runs : runs.slice(-3);
		const fmtElapsed = function(run) {
			if (!run || !run.started_at) return "";
			const end = run.ended_at || Math.floor(Date.now() / 1e3);
			const secs = Math.max(0, end - run.started_at);
			if (secs < 60) return `${secs}s`;
			if (secs < 3600) return `${Math.round(secs / 60)}m`;
			return `${(secs / 3600).toFixed(1)}h`;
		};
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head-row" }, h$2("span", { className: "hermes-kanban-section-head" }, `${tx(t, "runHistory", "Run history")} (${runs.length})`), !showAll ? h$2("button", {
			type: "button",
			onClick: function() {
				setExpanded(true);
			},
			className: "hermes-kanban-edit-link",
			title: tx(t, "showAllAttempts", "Show all attempts")
		}, `+${runs.length - 3} earlier`) : null), visible.map(function(r) {
			const outcomeClass = r.ended_at ? `hermes-kanban-run--${r.outcome || r.status || "ended"}` : "hermes-kanban-run--active";
			return h$2("div", {
				key: r.id,
				className: cn$1("hermes-kanban-run", outcomeClass)
			}, h$2("div", { className: "hermes-kanban-run-head" }, h$2("span", { className: "hermes-kanban-run-outcome" }, r.ended_at ? r.outcome || r.status || tx(t, "ended", "ended") : tx(t, "active", "active")), h$2("span", { className: "hermes-kanban-run-profile" }, r.profile ? `@${r.profile}` : tx(t, "noProfile", "(no profile)")), h$2("span", { className: "hermes-kanban-run-elapsed" }, fmtElapsed(r)), h$2("span", { className: "hermes-kanban-run-ago" }, timeAgo ? timeAgo(r.started_at || 0) : "")), r.summary ? h$2("div", { className: "hermes-kanban-run-summary" }, r.summary) : null, r.error ? h$2("div", { className: "hermes-kanban-run-error" }, r.error) : null, r.metadata && Object.keys(r.metadata).length > 0 ? (function() {
				var json = JSON.stringify(r.metadata, null, 2);
				return h$2("details", {
					className: "hermes-kanban-run-meta-block",
					open: !(json.length > 300)
				}, h$2("summary", { className: "hermes-kanban-run-meta-label" }, "Metadata"), h$2("code", { className: "hermes-kanban-run-meta" }, json));
			})() : null);
		}));
	}
	function WorkerLogSection(props) {
		const { t } = useI18n$2();
		const [state, setState] = useState$2({
			loading: false,
			data: null,
			err: null
		});
		const load = useCallback$1(function() {
			setState({
				loading: true,
				data: null,
				err: null
			});
			SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/log?tail=100000`, props.boardSlug)).then(function(d) {
				setState({
					loading: false,
					data: d,
					err: null
				});
			}).catch(function(e) {
				setState({
					loading: false,
					data: null,
					err: String(e.message || e)
				});
			});
		}, [props.taskId, props.boardSlug]);
		useEffect$1(function() {
			load();
		}, [load]);
		const data = state.data;
		let body;
		if (state.loading) body = h$2("div", { className: "text-xs text-muted-foreground" }, tx(t, "loadingLog", "Loading log…"));
		else if (state.err) body = h$2("div", { className: "text-xs text-destructive" }, state.err);
		else if (!data || !data.exists) body = h$2("div", { className: "text-xs text-muted-foreground italic" }, tx(t, "noWorkerLog", "— no worker log yet (task hasn't spawned or log was rotated away) —"));
		else body = h$2("pre", { className: "hermes-kanban-pre hermes-kanban-log" }, data.content || "(empty)");
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head-row" }, h$2("span", { className: "hermes-kanban-section-head" }, tx(t, "workerLog", "Worker log") + (data && data.size_bytes ? ` (${data.size_bytes} B)` : "")), h$2("button", {
			type: "button",
			onClick: load,
			className: "hermes-kanban-edit-link",
			title: "Refresh log"
		}, "refresh")), body, data && data.truncated ? h$2("div", { className: "text-xs text-muted-foreground" }, tx(t, "logTruncated", "(showing last 100 KB — full log at "), data.path, tx(t, "logAt", ")")) : null);
	}
	function DiagnosticActionButton(props) {
		const { t } = useI18n$2();
		const { action, onExec, busy, extra } = props;
		const label = (action.suggested ? "★ " : "") + action.label;
		const cls = cn$1("hermes-kanban-diag-action-btn", action.suggested ? "hermes-kanban-diag-action-btn--suggested" : "");
		if (action.kind === "reclaim" || action.kind === "reassign" || action.kind === "unblock") return h$2("button", {
			className: cls,
			disabled: busy || extra && extra.disabled,
			onClick: function() {
				onExec(action);
			},
			type: "button"
		}, label);
		if (action.kind === "cli_hint") return h$2("button", {
			className: cls,
			disabled: busy,
			onClick: function() {
				onExec(action);
			},
			type: "button",
			title: tx(t, "copyCommand", "Copy command to clipboard")
		}, extra && extra.copied ? tx(t, "copied", "Copied") : label);
		if (action.kind === "comment") return h$2("button", {
			className: cls,
			onClick: function() {
				onExec(action);
			},
			type: "button"
		}, label);
		if (action.kind === "open_docs") return h$2("a", {
			className: cls,
			href: action.payload && action.payload.url || "#",
			target: "_blank",
			rel: "noreferrer"
		}, label);
		return h$2("span", { className: cls + " hermes-kanban-diag-action-btn--unknown" }, label);
	}
	function DiagnosticCard(props) {
		const { t } = useI18n$2();
		const { diag, task, boardSlug, assignees, onRefresh } = props;
		const [busy, setBusy] = useState$2(false);
		const [msg, setMsg] = useState$2(null);
		const [copiedKey, setCopiedKey] = useState$2(null);
		const [reassignProfile, setReassignProfile] = useState$2(task.assignee || "");
		const execAction = function(action) {
			if (busy) return;
			if (action.kind === "cli_hint") {
				const cmd = action.payload && action.payload.command || action.label;
				const fallback = function() {
					window.prompt("Copy this command:", cmd);
				};
				try {
					const p = navigator.clipboard && navigator.clipboard.writeText(cmd);
					if (p && p.then) p.then(function() {
						setCopiedKey(action.label);
						setTimeout(function() {
							setCopiedKey(null);
						}, 2e3);
					}).catch(fallback);
					else fallback();
				} catch (_) {
					fallback();
				}
				return;
			}
			if (action.kind === "comment") {
				const ta = document.querySelector(".hermes-kanban-drawer-comment-row input, .hermes-kanban-drawer-comment-row textarea");
				if (ta) {
					ta.scrollIntoView({
						behavior: "smooth",
						block: "nearest"
					});
					ta.focus();
				}
				return;
			}
			if (action.kind === "unblock") {
				setBusy(true);
				setMsg(null);
				const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}`, boardSlug);
				SDK$2.fetchJSON(url, {
					method: "PATCH",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ status: "ready" })
				}).then(function() {
					setMsg({
						ok: true,
						text: tx(t, "unblockedMessage", "Unblocked {id}. Task is ready for the next tick.", { id: task.id })
					});
					if (onRefresh) onRefresh();
				}).catch(function(err) {
					setMsg({
						ok: false,
						text: tx(t, "unblockFailed", "Unblock failed: ") + (err.message || String(err))
					});
				}).then(function() {
					setBusy(false);
				});
				return;
			}
			if (action.kind === "reclaim") {
				setBusy(true);
				setMsg(null);
				const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reclaim`, boardSlug);
				SDK$2.fetchJSON(url, {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ reason: `recovery action for ${diag.kind}` })
				}).then(function() {
					setMsg({
						ok: true,
						text: tx(t, "reclaimedMessage", "Reclaimed {id}. Task is back to ready.", { id: task.id })
					});
					if (onRefresh) onRefresh();
				}).catch(function(err) {
					setMsg({
						ok: false,
						text: tx(t, "reclaimFailed", "Reclaim failed: ") + (err.message || String(err))
					});
				}).then(function() {
					setBusy(false);
				});
				return;
			}
			if (action.kind === "reassign") {
				if (!reassignProfile) {
					setMsg({
						ok: false,
						text: tx(t, "pickProfileFirst", "Pick a profile first.")
					});
					return;
				}
				setBusy(true);
				setMsg(null);
				const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reassign`, boardSlug);
				const body = {
					profile: reassignProfile || null,
					reclaim_first: !!(action.payload && action.payload.reclaim_first),
					reason: `recovery action for ${diag.kind}`
				};
				SDK$2.fetchJSON(url, {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify(body)
				}).then(function() {
					setMsg({
						ok: true,
						text: tx(t, "reassignedMessage", "Reassigned {id} to {profile}.", {
							id: task.id,
							profile: reassignProfile
						})
					});
					if (onRefresh) onRefresh();
				}).catch(function(err) {
					setMsg({
						ok: false,
						text: tx(t, "reassignFailed", "Reassign failed: ") + (err.message || String(err))
					});
				}).then(function() {
					setBusy(false);
				});
				return;
			}
		};
		const reassignAction = (diag.actions || []).find(function(a) {
			return a.kind === "reassign";
		});
		return h$2("div", { className: cn$1("hermes-kanban-diag", "hermes-kanban-diag--" + (diag.severity || "warning")) }, h$2("div", { className: "hermes-kanban-diag-header" }, h$2("span", { className: "hermes-kanban-diag-sev" }, diag.severity === "critical" ? "!!!" : diag.severity === "error" ? "!!" : "⚠"), h$2("span", { className: "hermes-kanban-diag-title" }, diag.title)), h$2("div", { className: "hermes-kanban-diag-detail" }, diag.detail), diag.data && Object.keys(diag.data).length > 0 ? h$2("div", { className: "hermes-kanban-diag-data" }, Object.keys(diag.data).map(function(k) {
			const v = diag.data[k];
			if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string" && v[0].indexOf("t_") === 0) return h$2("div", {
				key: k,
				className: "hermes-kanban-diag-data-row"
			}, h$2("span", { className: "hermes-kanban-diag-data-key" }, k + ":"), v.map(function(x) {
				return h$2("code", {
					key: x,
					className: "hermes-kanban-event-phantom-chip"
				}, x);
			}));
			return h$2("div", {
				key: k,
				className: "hermes-kanban-diag-data-row"
			}, h$2("span", { className: "hermes-kanban-diag-data-key" }, k + ":"), h$2("span", { className: "hermes-kanban-diag-data-val" }, Array.isArray(v) ? v.join(", ") : String(v)));
		})) : null, reassignAction ? h$2("div", { className: "hermes-kanban-diag-reassign-row" }, h$2("span", { className: "hermes-kanban-diag-reassign-label" }, tx(t, "reassignTo", "Reassign to:")), h$2("select", {
			className: "hermes-kanban-recovery-select",
			value: reassignProfile,
			onChange: function(e) {
				setReassignProfile(e.target.value);
			}
		}, h$2("option", { value: "" }, "(unassigned)"), (assignees || []).map(function(a) {
			return h$2("option", {
				key: a,
				value: a
			}, a);
		}))) : null, h$2("div", { className: "hermes-kanban-diag-actions" }, (diag.actions || []).map(function(a, i) {
			return h$2(DiagnosticActionButton, {
				key: a.kind + String(i),
				action: a,
				onExec: execAction,
				busy,
				extra: {
					copied: copiedKey === a.label,
					disabled: a.kind === "reassign" && !reassignProfile
				}
			});
		})), msg ? h$2("div", { className: cn$1("hermes-kanban-diag-msg", msg.ok ? "hermes-kanban-diag-msg--ok" : "hermes-kanban-diag-msg--err") }, msg.text) : null);
	}
	function DiagnosticsSection(props) {
		const { t } = useI18n$2();
		const diags = props.diagnostics || [];
		const hasOpenDiags = diags.length > 0;
		const [open, setOpen] = useState$2(hasOpenDiags);
		useEffect$1(function() {
			if (hasOpenDiags) setOpen(true);
		}, [hasOpenDiags]);
		if (!hasOpenDiags && !props.alwaysVisible) return null;
		return h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head-row" }, h$2("span", { className: "hermes-kanban-section-head" }, hasOpenDiags ? h$2("span", { className: "hermes-kanban-section-head-warning" }, `⚠ ${tx(t, "diagnostics", "Diagnostics")} (${diags.length})`) : tx(t, "diagnostics", "Diagnostics")), h$2("button", {
			className: "hermes-kanban-section-toggle",
			onClick: function() {
				setOpen(function(x) {
					return !x;
				});
			},
			type: "button"
		}, open ? tx(t, "hide", "Hide") : tx(t, "show", "Show"))), open ? h$2("div", { className: "hermes-kanban-diag-list" }, diags.map(function(d, i) {
			return h$2(DiagnosticCard, {
				key: props.task.id + ":" + d.kind + String(i),
				diag: d,
				task: props.task,
				boardSlug: props.boardSlug,
				assignees: props.assignees,
				onRefresh: props.onRefresh
			});
		})) : null);
	}
	function TaskDetail(props) {
		const { t: i18n } = useI18n$2();
		const t = props.data.task;
		const comments = props.data.comments || [];
		const events = props.data.events || [];
		const attachments = props.data.attachments || [];
		const links = props.data.links || {
			parents: [],
			children: []
		};
		return h$2("div", { className: "hermes-kanban-drawer-body" }, h$2("div", { className: "hermes-kanban-drawer-title" }, h$2("span", { className: cn$1("hermes-kanban-dot", COLUMN_DOT[t.status]) }), props.editing ? h$2(TitleEditor, {
			initial: t.title || "",
			onSave: function(newTitle) {
				return props.onPatch({ title: newTitle }).then(function() {
					props.setEditing(false);
				});
			},
			onCancel: function() {
				props.setEditing(false);
			}
		}) : h$2("span", {
			className: "hermes-kanban-drawer-title-text",
			title: tx(i18n, "clickToEdit", "Click to edit"),
			onClick: function() {
				props.setEditing(true);
			}
		}, t.title || tx(i18n, "untitled", "(untitled)"))), h$2("div", { className: "hermes-kanban-drawer-meta" }, h$2(MetaRow, {
			label: tx(i18n, "status", "Status"),
			value: t.status
		}), h$2(AssigneeEditor, {
			task: t,
			onPatch: props.onPatch
		}), h$2(PriorityEditor, {
			task: t,
			onPatch: props.onPatch
		}), t.tenant ? h$2(MetaRow, {
			label: tx(i18n, "tenant", "Tenant"),
			value: t.tenant
		}) : null, h$2(MetaRow, {
			label: tx(i18n, "workspace", "Workspace"),
			value: `${t.workspace_kind}${t.workspace_path ? ": " + t.workspace_path : ""}`
		}), t.skills && t.skills.length > 0 ? h$2(MetaRow, {
			label: tx(i18n, "skills", "Skills"),
			value: t.skills.join(", ")
		}) : null, t.goal_mode ? h$2(MetaRow, {
			label: tx(i18n, "goalMode", "Goal mode"),
			value: t.goal_max_turns ? `on (max ${t.goal_max_turns} turns)` : "on"
		}) : null, t.created_by ? h$2(MetaRow, {
			label: tx(i18n, "createdBy", "Created by"),
			value: t.created_by
		}) : null), h$2(StatusActions, {
			task: t,
			onPatch: props.onPatch,
			onSpecify: props.onSpecify,
			onDecompose: props.onDecompose
		}), h$2(DiagnosticsSection, {
			task: t,
			boardSlug: props.boardSlug,
			assignees: props.assignees,
			diagnostics: t.diagnostics || [],
			onRefresh: props.onRefresh
		}), h$2(HomeSubsSection, {
			homeChannels: props.homeChannels || [],
			homeBusy: props.homeBusy || {},
			onToggle: props.onToggleHomeSub
		}), h$2(BodyEditor, {
			task: t,
			renderMarkdown: props.renderMarkdown,
			onPatch: props.onPatch
		}), h$2(DependencyEditor, {
			task: t,
			links,
			allTasks: props.allTasks,
			onAddParent: props.onAddParent,
			onRemoveParent: props.onRemoveParent,
			onAddChild: props.onAddChild,
			onRemoveChild: props.onRemoveChild
		}), t.result ? h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")), h$2(MarkdownBlock, {
			source: t.result,
			enabled: props.renderMarkdown
		})) : null, h$2(AttachmentsSection, {
			attachments,
			boardSlug: props.boardSlug,
			onUpload: props.onUpload,
			onDelete: props.onDeleteAttachment,
			uploadBusy: props.uploadBusy,
			uploadErr: props.uploadErr,
			i18n
		}), h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, `${tx(i18n, "comments", "Comments")} (${comments.length})`), comments.length === 0 ? h$2("div", { className: "text-xs text-muted-foreground" }, tx(i18n, "noComments", "— no comments —")) : comments.map(function(c) {
			return h$2("div", {
				key: c.id,
				className: "hermes-kanban-comment"
			}, h$2("div", { className: "hermes-kanban-comment-head" }, h$2("span", { className: "hermes-kanban-comment-author" }, c.author || "anon"), h$2("span", { className: "hermes-kanban-comment-ago" }, timeAgo ? timeAgo(c.created_at) : "")), h$2(MarkdownBlock, {
				source: c.body,
				enabled: props.renderMarkdown
			}));
		})), h$2("div", { className: "hermes-kanban-section" }, h$2("div", { className: "hermes-kanban-section-head" }, `${tx(i18n, "events", "Events")} (${events.length})`), events.slice().reverse().slice(0, 20).map(function(e) {
			const isDiag = isDiagnosticEvent(e.kind);
			const phantoms = isDiag ? phantomIdsFromEvent(e) : [];
			return h$2("div", {
				key: e.id,
				className: cn$1("hermes-kanban-event", isDiag ? "hermes-kanban-event--hallucination" : "")
			}, isDiag ? h$2("div", { className: "hermes-kanban-event-header" }, h$2("span", { className: "hermes-kanban-event-warning-icon" }, "⚠"), h$2("span", { className: "hermes-kanban-event-warning-label" }, getDiagnosticEventLabel(i18n, e.kind) || e.kind), h$2("span", { className: "hermes-kanban-event-ago" }, timeAgo ? timeAgo(e.created_at) : "")) : h$2("div", { className: "hermes-kanban-event-header-plain" }, h$2("span", { className: "hermes-kanban-event-kind" }, e.kind), h$2("span", { className: "hermes-kanban-event-ago" }, timeAgo ? timeAgo(e.created_at) : "")), isDiag && phantoms.length > 0 ? h$2("div", { className: "hermes-kanban-event-phantom-row" }, h$2("span", { className: "hermes-kanban-event-phantom-label" }, tx(i18n, "phantomIds", "Phantom ids:")), phantoms.map(function(pid) {
				return h$2("code", {
					key: pid,
					className: "hermes-kanban-event-phantom-chip"
				}, pid);
			})) : null, e.payload && !isDiag ? h$2("code", { className: "hermes-kanban-event-payload" }, JSON.stringify(e.payload)) : null);
		})), h$2(WorkerLogSection, {
			taskId: t.id,
			boardSlug: props.boardSlug
		}), h$2(RunHistorySection, { runs: props.data.runs || [] }));
	}
	function TaskDrawer(props) {
		const { t } = useI18n$2();
		const [data, setData] = useState$2(null);
		const [loading, setLoading] = useState$2(true);
		const [err, setErr] = useState$2(null);
		const [, setPatchErr] = useState$2(null);
		const [newComment, setNewComment] = useState$2("");
		const [uploadBusy, setUploadBusy] = useState$2(false);
		const [uploadErr, setUploadErr] = useState$2(null);
		const [editing, setEditing] = useState$2(false);
		const [homeChannels, setHomeChannels] = useState$2([]);
		const [homeBusy, setHomeBusy] = useState$2({});
		const boardSlug = props.boardSlug;
		const load = useCallback$1(function() {
			return SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug)).then(function(d) {
				setData(d);
				setErr(null);
				setPatchErr(null);
			}).catch(function(e) {
				setErr(String(e.message || e));
			}).finally(function() {
				setLoading(false);
			});
		}, [props.taskId, boardSlug]);
		const loadHomeChannels = useCallback$1(function() {
			const url = withBoard(`${API}/home-channels?${new URLSearchParams({ task_id: props.taskId })}`, boardSlug);
			return SDK$2.fetchJSON(url).then(function(d) {
				setHomeChannels(d.home_channels || []);
			}).catch(function() {});
		}, [props.taskId, boardSlug]);
		useEffect$1(function() {
			load();
		}, [load, props.eventTick]);
		useEffect$1(function() {
			loadHomeChannels();
		}, [loadHomeChannels]);
		useEffect$1(function() {
			function onKey(e) {
				if (e.key === "Escape" && !editing) props.onClose();
			}
			window.addEventListener("keydown", onKey);
			return function() {
				window.removeEventListener("keydown", onKey);
			};
		}, [props.onClose, editing]);
		const handleComment = function() {
			const body = newComment.trim();
			if (!body) return;
			SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/comments`, boardSlug), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ body })
			}).then(function() {
				setNewComment("");
				load();
				props.onRefresh();
			}).catch(function(e) {
				setErr(String(e.message || e));
			});
		};
		const handleUpload = function(fileList) {
			const files = Array.prototype.slice.call(fileList || []);
			if (!files.length) return;
			setUploadBusy(true);
			setUploadErr(null);
			const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/attachments`, boardSlug);
			let chain = Promise.resolve();
			files.forEach(function(f) {
				chain = chain.then(function() {
					const fd = new FormData();
					fd.append("file", f, f.name);
					return SDK$2.authedFetch(url, {
						method: "POST",
						body: fd
					}).then(function(resp) {
						if (!resp.ok) return resp.text().then(function(txt) {
							throw new Error(parseApiErrorMessage(/* @__PURE__ */ new Error(resp.status + ": " + txt)));
						});
					});
				});
			});
			chain.then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setUploadErr(String(e.message || e));
			}).finally(function() {
				setUploadBusy(false);
			});
		};
		const handleDeleteAttachment = function(attachmentId) {
			return SDK$2.fetchJSON(withBoard(`${API}/attachments/${attachmentId}`, boardSlug), { method: "DELETE" }).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setUploadErr(String(e.message || e));
			});
		};
		const doPatch = function(patch, opts) {
			if (opts && opts.confirm && !window.confirm(opts.confirm)) return Promise.resolve();
			const finalPatch = withCompletionSummary(patch, 1, t);
			if (!finalPatch) return Promise.resolve();
			setPatchErr(null);
			return SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug), {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(finalPatch)
			}).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setPatchErr(parseApiErrorMessage(e));
			});
		};
		const doSpecify = function() {
			return SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/specify`, boardSlug), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({})
			}).then(function(res) {
				load();
				props.onRefresh();
				return res;
			});
		};
		const doDecompose = function() {
			return SDK$2.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/decompose`, boardSlug), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({})
			}).then(function(res) {
				load();
				props.onRefresh();
				return res;
			});
		};
		const addLink = function(parentId) {
			return SDK$2.fetchJSON(withBoard(`${API}/links`, boardSlug), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					parent_id: parentId,
					child_id: props.taskId
				})
			}).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setErr(String(e.message || e));
			});
		};
		const removeLink = function(parentId) {
			const qs = new URLSearchParams({
				parent_id: parentId,
				child_id: props.taskId
			});
			return SDK$2.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" }).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setErr(String(e.message || e));
			});
		};
		const addChild = function(childId) {
			return SDK$2.fetchJSON(withBoard(`${API}/links`, boardSlug), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					parent_id: props.taskId,
					child_id: childId
				})
			}).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setErr(String(e.message || e));
			});
		};
		const removeChild = function(childId) {
			const qs = new URLSearchParams({
				parent_id: props.taskId,
				child_id: childId
			});
			return SDK$2.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" }).then(function() {
				load();
				props.onRefresh();
			}).catch(function(e) {
				setErr(String(e.message || e));
			});
		};
		const toggleHomeSubscription = function(platform, currentlySubscribed) {
			setHomeBusy(function(b) {
				return Object.assign({}, b, { [platform]: true });
			});
			setHomeChannels(function(list) {
				return list.map(function(hc) {
					return hc.platform === platform ? Object.assign({}, hc, { subscribed: !currentlySubscribed }) : hc;
				});
			});
			const method = currentlySubscribed ? "DELETE" : "POST";
			const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/home-subscribe/${encodeURIComponent(platform)}`, boardSlug);
			return SDK$2.fetchJSON(url, { method }).then(function() {
				return loadHomeChannels();
			}).catch(function(e) {
				setHomeChannels(function(list) {
					return list.map(function(hc) {
						return hc.platform === platform ? Object.assign({}, hc, { subscribed: currentlySubscribed }) : hc;
					});
				});
				setErr(String(e.message || e));
			}).finally(function() {
				setHomeBusy(function(b) {
					const next = Object.assign({}, b);
					delete next[platform];
					return next;
				});
			});
		};
		return h$2("div", {
			className: "hermes-kanban-drawer-shade",
			onClick: props.onClose
		}, h$2("div", {
			className: "hermes-kanban-drawer",
			onClick: function(e) {
				e.stopPropagation();
			}
		}, h$2("div", { className: "hermes-kanban-drawer-head" }, h$2("span", { className: "text-xs text-muted-foreground" }, props.taskId), h$2("button", {
			type: "button",
			onClick: props.onClose,
			className: "hermes-kanban-drawer-close",
			title: tx(t, "close", "Close (Esc)")
		}, "×")), loading ? h$2("div", { className: "p-4 text-sm text-muted-foreground" }, tx(t, "loadingDetail", "Loading…")) : err ? h$2("div", { className: "p-4 text-sm text-destructive" }, err) : data ? h$2(TaskDetail, {
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
			uploadErr
		}) : null, data ? h$2("div", { className: "hermes-kanban-drawer-comment-row" }, h$2(Input$2, {
			value: newComment,
			onChange: function(e) {
				setNewComment(e.target.value);
			},
			onKeyDown: function(e) {
				if (e.key === "Enter" && !e.shiftKey) {
					e.preventDefault();
					handleComment();
				}
			},
			placeholder: tx(t, "addComment", "Add a comment… (Enter to submit)"),
			className: "h-8 text-sm flex-1"
		}), h$2(Button$2, {
			onClick: handleComment,
			size: "sm"
		}, tx(t, "comment", "Comment"))) : null));
	}
	//#endregion
	//#region src/components/NewTaskDialog.tsx
	/**
	* Kanban dashboard plugin — New Task dialog.
	*
	* Full-screen modal for creating a new task with all optional fields:
	* title, assignee, priority, description, skills, parent task,
	* workspace kind/path, goal mode + max turns.
	*
	* Follows the NewBoardDialog pattern (backdrop + form + useState fields).
	*/
	function getSDK() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	}
	var SDK$1 = getSDK();
	var h$1 = SDK$1.React.createElement;
	var { useState: useState$1 } = SDK$1.hooks;
	var { Button: Button$1, Input: Input$1, Label: Label$1, Select: Select$1, SelectOption: SelectOption$1 } = SDK$1.components;
	var useI18n$1 = SDK$1.useI18n;
	var Checkbox$1 = SDK$1.components.Checkbox || function(props) {
		const { checked, onCheckedChange, className, onClick, ...rest } = props;
		return h$1("input", {
			type: "checkbox",
			checked: !!checked,
			className,
			onClick,
			onChange: (e) => onCheckedChange?.(e.target.checked),
			...rest
		});
	};
	function NewTaskDialog(props) {
		const { t } = useI18n$1();
		const [title, setTitle] = useState$1("");
		const [assignee, setAssignee] = useState$1("");
		const [priority, setPriority] = useState$1("");
		const [body, setBody] = useState$1("");
		const [skills, setSkills] = useState$1("");
		const [parent, setParent] = useState$1("");
		const [workspaceKind, setWorkspaceKind] = useState$1("scratch");
		const [workspacePath, setWorkspacePath] = useState$1("");
		const [goalMode, setGoalMode] = useState$1(false);
		const [goalMaxTurns, setGoalMaxTurns] = useState$1("");
		const [submitting, setSubmitting] = useState$1(false);
		const [err, setErr] = useState$1(null);
		react.useEffect(() => {
			if (props.open) {
				setTitle("");
				setAssignee("");
				setPriority("");
				setBody("");
				setSkills("");
				setParent("");
				setWorkspaceKind("scratch");
				setWorkspacePath("");
				setGoalMode(false);
				setGoalMaxTurns("");
				setSubmitting(false);
				setErr(null);
			}
		}, [props.open]);
		if (!props.open) return null;
		function onSubmit(ev) {
			ev?.preventDefault();
			const trimmed = title.trim();
			if (!trimmed) {
				setErr("Title is required");
				return;
			}
			setSubmitting(true);
			setErr(null);
			const taskBody = { title: trimmed };
			const assigneeTrim = assignee.trim();
			if (assigneeTrim) taskBody.assignee = assigneeTrim;
			const priNum = Number(priority);
			if (priority && Number.isFinite(priNum)) taskBody.priority = priNum;
			const bodyTrim = body.trim();
			if (bodyTrim) taskBody.body = bodyTrim;
			const skillList = skills.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
			if (skillList.length > 0) taskBody.skills = skillList;
			const parentTrim = parent.trim();
			if (parentTrim) taskBody.parents = [parentTrim];
			if (workspaceKind && workspaceKind !== "scratch") taskBody.workspace_kind = workspaceKind;
			const wpTrim = workspacePath.trim();
			if (wpTrim) taskBody.workspace_path = wpTrim;
			if (goalMode) {
				taskBody.goal_mode = true;
				const gmt = parseInt(goalMaxTurns, 10);
				if (Number.isFinite(gmt) && gmt > 0) taskBody.goal_max_turns = gmt;
			}
			props.onCreate(taskBody).then(() => {
				setSubmitting(false);
			}).catch((e) => {
				setErr(tx(t, "newTaskDialog_error", "Failed to create task") + (e instanceof Error ? ": " + e.message : ": " + String(e)));
				setSubmitting(false);
			});
		}
		function onCancel() {
			props.onClose();
		}
		const showPathInput = workspaceKind !== "scratch";
		return h$1("div", {
			className: "hermes-kanban-dialog-backdrop",
			onClick: (e) => {
				if (e.target === e.currentTarget) onCancel();
			}
		}, h$1("form", {
			className: "hermes-kanban-dialog",
			onSubmit
		}, h$1("div", { className: "hermes-kanban-dialog-title" }, tx(t, "newTaskDialog_title", "New Task")), h$1("div", { className: "flex flex-col gap-3" }, h$1("div", { className: "hermes-kanban-field flex flex-col gap-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_title", "New Task"), " ", h$1("span", { className: "text-destructive" }, "*")), h$1("textarea", {
			value: title,
			onChange: (e) => setTitle(e.target.value),
			placeholder: tx(t, "newTaskDialog_titlePlaceholder", "Enter task title..."),
			autoFocus: true,
			className: "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
			rows: 2
		})), h$1("div", { className: "flex gap-2" }, h$1("div", { className: "hermes-kanban-field flex flex-col gap-1 flex-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_assignee", "Assignee")), h$1(Input$1, {
			value: assignee,
			onChange: (e) => setAssignee(e.target.value),
			placeholder: tx(t, "assigneePlaceholder", "assignee"),
			className: "h-8"
		})), h$1("div", { className: "hermes-kanban-field flex flex-col gap-1 w-32" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_priority", "Priority")), h$1(Select$1, {
			value: priority,
			className: "h-8",
			...selectChangeHandler(setPriority)
		}, h$1(SelectOption$1, { value: "" }, "—"), h$1(SelectOption$1, { value: "1" }, "1"), h$1(SelectOption$1, { value: "2" }, "2"), h$1(SelectOption$1, { value: "3" }, "3")))), h$1("div", { className: "hermes-kanban-field flex flex-col gap-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_description", "Description")), h$1("textarea", {
			value: body,
			onChange: (e) => setBody(e.target.value),
			placeholder: "Optional description...",
			className: "text-sm min-h-[2rem] max-h-32 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
			rows: 3
		})), h$1("div", { className: "hermes-kanban-field flex flex-col gap-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_skills", "Skills")), h$1(Input$1, {
			value: skills,
			onChange: (e) => setSkills(e.target.value),
			placeholder: tx(t, "skillsPlaceholder", "skills (optional, comma-separated): translation, github-code-review"),
			className: "h-8"
		})), h$1("div", { className: "hermes-kanban-field flex flex-col gap-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_parentTask", "Parent Task")), h$1(Input$1, {
			value: parent,
			onChange: (e) => setParent(e.target.value),
			placeholder: "Task ID (optional)",
			className: "h-8"
		})), h$1("div", { className: "flex gap-2" }, h$1("div", { className: "hermes-kanban-field flex flex-col gap-1 w-32" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_workspaceKind", "Workspace Kind")), h$1(Select$1, {
			value: workspaceKind,
			className: "h-8",
			...selectChangeHandler(setWorkspaceKind)
		}, h$1(SelectOption$1, { value: "scratch" }, "scratch"), h$1(SelectOption$1, { value: "worktree" }, "worktree"), h$1(SelectOption$1, { value: "dir" }, "dir"))), showPathInput ? h$1("div", { className: "hermes-kanban-field flex flex-col gap-1 flex-1" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_workspacePath", "Workspace Path")), h$1(Input$1, {
			value: workspacePath,
			onChange: (e) => setWorkspacePath(e.target.value),
			placeholder: workspaceKind === "dir" ? tx(t, "workspacePathDir", "workspace path (required, e.g. ~/projects/my-app)") : tx(t, "workspacePathOptional", "workspace path (optional, derived from assignee if blank)"),
			className: "h-8"
		})) : null), h$1("label", { className: "flex items-center gap-2 text-xs" }, h$1(Checkbox$1, {
			checked: goalMode,
			onCheckedChange: (checked) => setGoalMode(checked === true)
		}), tx(t, "newTaskDialog_goalMode", "Goal Mode")), goalMode ? h$1("div", { className: "hermes-kanban-field flex flex-col gap-1 ml-6" }, h$1(Label$1, { className: "text-xs" }, tx(t, "newTaskDialog_goalMaxTurns", "Max Turns")), h$1(Input$1, {
			type: "number",
			value: goalMaxTurns,
			onChange: (e) => setGoalMaxTurns(e.target.value),
			placeholder: "default 20",
			className: "h-8 w-32",
			min: 1
		})) : null), err ? h$1("div", { className: "text-xs text-destructive mt-2" }, err) : null, h$1("div", { className: "hermes-kanban-dialog-actions" }, h$1(Button$1, {
			type: "button",
			onClick: onCancel,
			size: "sm",
			disabled: submitting
		}, tx(t, "cancel", "Cancel")), h$1(Button$1, {
			type: "submit",
			size: "sm",
			disabled: submitting || !title.trim()
		}, submitting ? tx(t, "newTaskDialog_creating", "Creating...") : tx(t, "newTaskDialog_create", "Create Task")))));
	}
	//#endregion
	//#region src/components/KanbanPage.tsx
	/**
	* Kanban dashboard plugin — main page component.
	*
	* Manages board state, WebSocket live updates, filtering, card selection,
	* task operations, board switching, and the overall layout.
	*/
	var SDK = (function() {
		const s = window.__HERMES_PLUGIN_SDK__;
		if (!s) throw new Error("Plugin SDK not available");
		return s;
	})();
	var h = SDK.React.createElement;
	var { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
	var { Card, CardContent, Button, Input, Label, Select, SelectOption } = SDK.components;
	var { cn } = SDK.utils;
	var useI18n = SDK.useI18n || (() => ({
		t: { kanban: null },
		locale: "en"
	}));
	var Checkbox = SDK.components.Checkbox || function(props) {
		const { checked, onCheckedChange, className, onClick, ...rest } = props;
		return h("input", {
			type: "checkbox",
			checked: !!checked,
			className,
			onClick,
			onChange: (e) => onCheckedChange?.(e.target.checked),
			...rest
		});
	};
	var DESTRUCTIVE_KEYS = {
		done: "confirmDone",
		archived: "confirmArchive",
		blocked: "confirmBlocked"
	};
	var FALLBACK_DESTRUCTIVE = {
		done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
		archived: "Archive this task? It disappears from the default board view.",
		blocked: "Mark this task as blocked? The worker's claim is released."
	};
	var DESTRUCTIVE_TRANSITIONS = {
		blocked: "Block selected tasks? Releases any active claims.",
		done: "Mark selected task(s) as done? Releases claims and unblocks children.",
		archived: "Archive selected task(s)?"
	};
	var FALLBACK_TRASH = {
		label: "Trash",
		title: "Drag a card here to permanently delete it",
		confirm: "Permanently delete this task? This cannot be undone.",
		dropHint: "Drop to delete"
	};
	function getDestructiveConfirm(t, status) {
		const key = DESTRUCTIVE_KEYS[status];
		if (!key) return null;
		return tx(t, key, FALLBACK_DESTRUCTIVE[status] || "");
	}
	function ErrorBoundaryFallback(props) {
		const { t } = useI18n();
		return h(Card, null, h(CardContent, { className: "p-6 text-sm" }, h("div", { className: "text-destructive font-semibold mb-1" }, tx(t, "renderingError", "Kanban tab hit a rendering error")), h("div", { className: "text-muted-foreground text-xs mb-3" }, props.message), h(Button, {
			onClick: props.onReset,
			size: "sm"
		}, tx(t, "reloadView", "Reload view"))));
	}
	var ErrorBoundary = class extends react.Component {
		constructor(props) {
			super(props);
			this.state = { error: null };
		}
		static getDerivedStateFromError(error) {
			return { error };
		}
		componentDidCatch(error, info) {
			console.error("Kanban plugin crashed:", error, info);
		}
		render() {
			if (this.state.error) return h(ErrorBoundaryFallback, {
				message: String(this.state.error.message || this.state.error),
				onReset: () => this.setState({ error: null })
			});
			return this.props.children;
		}
	};
	function collectDiagTasks(boardData) {
		if (!boardData || !boardData.columns) return [];
		const out = [];
		for (const col of boardData.columns) for (const t of col.tasks || []) if (t.diagnostics && t.diagnostics.length > 0) out.push(t);
		else if (t.warnings && t.warnings.count > 0) out.push(t);
		const sevIdx = (s) => s === "critical" ? 3 : s === "error" ? 2 : s === "warning" ? 1 : 0;
		out.sort((a, b) => {
			const aSev = sevIdx(a.warnings?.highest_severity || "warning");
			const bSev = sevIdx(b.warnings?.highest_severity || "warning");
			if (aSev !== bSev) return bSev - aSev;
			return (b.warnings?.latest_at || 0) - (a.warnings?.latest_at || 0);
		});
		return out;
	}
	function AttentionStrip(props) {
		const { t } = useI18n();
		const [expanded, setExpanded] = useState(false);
		const [dismissed, setDismissed] = useState(false);
		const diagTasks = useMemo(() => collectDiagTasks(props.boardData), [props.boardData]);
		if (dismissed || diagTasks.length === 0) return null;
		let topSev = "warning";
		for (const td of diagTasks) {
			const s = td.warnings?.highest_severity || "warning";
			if (s === "critical") {
				topSev = "critical";
				break;
			}
			if (s === "error" && topSev !== "critical") topSev = "error";
		}
		return h("div", { className: cn("hermes-kanban-attention", `hermes-kanban-attention--${topSev}`) }, h("div", { className: "hermes-kanban-attention-bar" }, h("span", { className: "hermes-kanban-attention-icon" }, topSev === "critical" ? "!!!" : topSev === "error" ? "!!" : "⚠"), h("span", { className: "hermes-kanban-attention-text" }, diagTasks.length === 1 ? tx(t, "taskNeedsAttention", "1 task needs attention") : tx(t, "tasksNeedAttention", "{n} tasks need attention", { n: diagTasks.length })), h("button", {
			className: "hermes-kanban-attention-toggle",
			onClick: () => setExpanded((x) => !x),
			type: "button"
		}, expanded ? tx(t, "hide", "Hide") : tx(t, "show", "Show")), h("button", {
			className: "hermes-kanban-attention-dismiss",
			onClick: () => setDismissed(true),
			title: "Hide until next page reload",
			type: "button"
		}, "✕")), expanded ? h("div", { className: "hermes-kanban-attention-list" }, diagTasks.map((task) => {
			const sev = task.warnings?.highest_severity || "warning";
			const kinds = task.warnings?.kinds ? Object.keys(task.warnings.kinds) : [];
			return h("div", {
				key: task.id,
				className: cn("hermes-kanban-attention-row", `hermes-kanban-attention-row--${sev}`)
			}, h("span", { className: "hermes-kanban-attention-row-sev" }, sev === "critical" ? "!!!" : sev === "error" ? "!!" : "⚠"), h("span", { className: "hermes-kanban-attention-row-id" }, task.id), h("span", { className: "hermes-kanban-attention-row-title" }, task.title || tx(t, "untitled", "(untitled)")), h("span", { className: "hermes-kanban-attention-row-meta" }, task.assignee ? "@" + task.assignee : tx(t, "unassigned", "unassigned"), " · ", kinds.length > 0 ? kinds.join(", ") : tx(t, "diagnostic", "diagnostic")), h("button", {
				className: "hermes-kanban-attention-row-btn",
				onClick: () => props.onOpen(task.id),
				type: "button"
			}, tx(t, "open", "Open")));
		})) : null);
	}
	function DocsLink() {
		return h("a", {
			href: DOCS_URL,
			target: "_blank",
			rel: "noopener noreferrer",
			className: "hermes-kanban-docs-link",
			title: "Open Hermes Kanban docs in a new tab",
			"aria-label": "Hermes Kanban documentation"
		}, "?");
	}
	function BoardSwitcher(props) {
		const { t } = useI18n();
		const list = props.boardList || [];
		const current = list.find((b) => b.slug === props.board);
		const currentName = current?.name || props.board || "default";
		const currentTotal = current?.total || 0;
		const hasMultipleBoards = list.length > 1;
		const totalAcrossAllBoards = list.reduce((n, b) => n + (b.total || 0), 0);
		if (!(hasMultipleBoards || totalAcrossAllBoards > 0)) return h("div", {
			className: "hermes-kanban-boardswitcher-compact",
			title: tx(t, "boardSwitcherHint", "Boards let you separate unrelated streams of work")
		}, h(Button, {
			onClick: props.onNewClick,
			size: "sm",
			className: "h-7 text-xs"
		}, tx(t, "newBoard", "+ New board")), h(DocsLink));
		return h("div", { className: "hermes-kanban-boardswitcher" }, h("div", { className: "hermes-kanban-boardswitcher-inner" }, h("div", { className: "flex flex-col gap-0.5" }, h("div", { className: "text-[11px] tracking-wider text-muted-foreground" }, tx(t, "board", "Board")), h("div", { className: "flex items-center gap-2" }, h(Select, {
			value: props.board || "",
			className: "h-8 min-w-[220px]",
			"aria-label": "Switch kanban board",
			...selectChangeHandler((v) => {
				if (v) props.onSwitch(v);
			})
		}, list.map((b) => {
			const label = b.total > 0 ? `${b.name || b.slug} \u00b7 ${b.total}` : b.name || b.slug;
			return h(SelectOption, {
				key: b.slug,
				value: b.slug
			}, label);
		})), h("span", { className: "text-xs text-muted-foreground" }, `${currentTotal} task${currentTotal === 1 ? "" : "s"}`))), h("div", { className: "flex-1" }), h(DocsLink), h(Button, {
			onClick: props.onNewClick,
			size: "sm",
			className: "h-8",
			title: "Create a new board. Useful when you want an unrelated work stream."
		}, tx(t, "newBoard", "+ New board")), props.board !== "default" ? h(Button, {
			onClick: () => {
				const msg = tx(t, "archiveBoardConfirm", "Archive board '{name}'? It will be moved to boards/_archived/. Tasks will no longer appear in the UI.", { name: currentName });
				if (window.confirm(msg)) props.onDeleteBoard(props.board);
			},
			size: "sm",
			className: "h-8",
			title: tx(t, "archiveBoardTitle", "Archive this board")
		}, tx(t, "archive", "Archive")) : null));
	}
	function NewBoardDialog(props) {
		const { t } = useI18n();
		const [slug, setSlug] = useState("");
		const [name, setName] = useState("");
		const [description, setDescription] = useState("");
		const [icon, setIcon] = useState("");
		const [switchTo, setSwitchTo] = useState(true);
		const [submitting, setSubmitting] = useState(false);
		const [err, setErr] = useState(null);
		const autoName = useMemo(() => {
			if (!slug) return "";
			return slug.replace(/[-_]+/g, " ").split(" ").filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
		}, [slug]);
		function onSubmit(ev) {
			ev?.preventDefault();
			if (!slug.trim()) {
				setErr("slug is required");
				return;
			}
			setSubmitting(true);
			setErr(null);
			props.onCreate({
				slug: slug.trim(),
				name: name.trim() || autoName || void 0,
				description: description.trim() || void 0,
				icon: icon.trim() || void 0,
				switch: switchTo
			}).catch((e) => {
				setErr(String(e?.message || e));
				setSubmitting(false);
			});
		}
		return h("div", {
			className: "hermes-kanban-dialog-backdrop",
			onClick: (e) => {
				if (e.target === e.currentTarget) props.onCancel();
			}
		}, h("form", {
			className: "hermes-kanban-dialog",
			onSubmit
		}, h("div", { className: "hermes-kanban-dialog-title" }, tx(t, "newBoardTitle", "New board")), h("div", { className: "text-xs text-muted-foreground mb-2" }, tx(t, "newBoardDescription", "Boards let you separate unrelated streams of work.")), h("div", { className: "flex flex-col gap-3" }, h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs" }, tx(t, "slug", "Slug"), " ", h("span", { className: "text-muted-foreground" }, tx(t, "slugHint", "— lowercase, hyphens, e.g. atm10-server"))), h(Input, {
			value: slug,
			onChange: (e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9\-_]/g, "-")),
			placeholder: "atm10-server",
			autoFocus: true,
			className: "h-8"
		})), h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name"), " ", h("span", { className: "text-muted-foreground" }, "(optional)")), h(Input, {
			value: name,
			onChange: (e) => setName(e.target.value),
			placeholder: autoName || tx(t, "displayName", "Display name"),
			className: "h-8"
		})), h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs" }, tx(t, "description", "Description"), " ", h("span", { className: "text-muted-foreground" }, "(optional)")), h(Input, {
			value: description,
			onChange: (e) => setDescription(e.target.value),
			placeholder: "What goes on this board?",
			className: "h-8"
		})), h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs" }, tx(t, "icon", "Icon"), " ", h("span", { className: "text-muted-foreground" }, "(single character or emoji)")), h(Input, {
			value: icon,
			onChange: (e) => setIcon(e.target.value.slice(0, 4)),
			placeholder: "📦",
			className: "h-8 w-24"
		})), h("label", { className: "flex items-center gap-2 text-xs" }, h(Checkbox, {
			checked: switchTo,
			onCheckedChange: (checked) => setSwitchTo(checked === true)
		}), tx(t, "switchAfterCreate", "Switch to this board after creating it"))), err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null, h("div", { className: "hermes-kanban-dialog-actions" }, h(Button, {
			type: "button",
			onClick: props.onCancel,
			size: "sm",
			disabled: submitting
		}, tx(t, "cancel", "Cancel")), h(Button, {
			type: "submit",
			size: "sm",
			disabled: submitting || !slug.trim()
		}, submitting ? tx(t, "creating", "Creating…") : tx(t, "createBoard", "Create board")))));
	}
	function BoardToolbar(props) {
		const { t } = useI18n();
		const tenants = props.board?.tenants || [];
		const assignees = props.board?.assignees || [];
		return h("div", { className: "flex flex-wrap items-end gap-3" }, h("div", {
			className: "flex flex-col gap-1",
			title: "Fuzzy-match tasks by id, title, or description."
		}, h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "search", "Search")), h(Input, {
			placeholder: tx(t, "filterCards", "Filter cards…"),
			value: props.search,
			onChange: (e) => props.setSearch(e.target.value),
			className: "w-56 h-8"
		})), h("div", {
			className: "flex flex-col gap-1",
			title: "Tenants are free-form tags on a task."
		}, h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "tenant", "Tenant")), h(Select, {
			value: props.tenantFilter,
			className: "h-8",
			...selectChangeHandler(props.setTenantFilter)
		}, h(SelectOption, { value: "" }, tx(t, "allTenants", "All tenants")), tenants.map((tn) => h(SelectOption, {
			key: tn,
			value: tn
		}, tn)))), h("div", {
			className: "flex flex-col gap-1",
			title: "Filter by assigned Hermes profile."
		}, h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "assignee", "Assignee")), h(Select, {
			value: props.assigneeFilter,
			className: "h-8",
			...selectChangeHandler(props.setAssigneeFilter)
		}, h(SelectOption, { value: "" }, tx(t, "allProfiles", "All profiles")), assignees.map((a) => h(SelectOption, {
			key: a,
			value: a
		}, a)))), h("label", {
			className: "flex items-center gap-2 text-xs",
			title: "Include archived tasks in the board view."
		}, h(Checkbox, {
			checked: props.includeArchived,
			onCheckedChange: (checked) => props.setIncludeArchived(checked === true)
		}), tx(t, "showArchived", "Show archived")), h("label", {
			className: "flex items-center gap-2 text-xs",
			title: "Group the Running column by assigned profile"
		}, h(Checkbox, {
			checked: props.laneByProfile,
			onCheckedChange: (checked) => props.setLaneByProfile(checked === true)
		}), tx(t, "lanesByProfile", "Lanes by profile")), h("div", { className: "flex-1" }), h(Button, {
			onClick: props.onNewTask,
			size: "sm",
			title: tx(t, "newTask", "New Task")
		}, tx(t, "newTask", "New Task")), h(Button, {
			onClick: props.onNudgeDispatch,
			size: "sm",
			title: "Wake the dispatcher to claim ready tasks now."
		}, tx(t, "nudgeDispatcher", "Nudge dispatcher")), h(Button, {
			onClick: props.onRefresh,
			size: "sm",
			title: "Reload the board from the database."
		}, tx(t, "refresh", "Refresh")), h(Button, {
			onClick: () => {
				props.setSearch("");
				props.setTenantFilter("");
				props.setAssigneeFilter("");
				props.setIncludeArchived(false);
			},
			size: "sm",
			title: "Clear all active filters."
		}, tx(t, "clearFilters", "Clear filters")));
	}
	function BulkActionBar(props) {
		const { t } = useI18n();
		const [assignee, setAssignee] = useState("");
		const [reclaimFirst, setReclaimFirst] = useState(false);
		const [priority, setPriority] = useState("");
		return h("div", { className: "hermes-kanban-bulk" }, h("span", { className: "hermes-kanban-bulk-count" }, `${props.count} ${tx(t, "selected", "selected")}`), h(Button, {
			onClick: () => props.onApply({ status: "todo" }),
			size: "sm",
			title: "Move to Todo."
		}, "→ todo"), h(Button, {
			onClick: () => props.onApply({ status: "ready" }),
			size: "sm",
			title: "Move to Ready."
		}, "→ ready"), h(Button, {
			onClick: () => props.onApply({ status: "blocked" }, `Block ${props.count} task(s)?`),
			size: "sm",
			title: "Block selected tasks."
		}, "Block"), h(Button, {
			onClick: () => props.onApply({ status: "ready" }, `Unblock ${props.count} task(s)?`),
			size: "sm",
			title: "Unblock selected tasks."
		}, "Unblock"), h(Button, {
			onClick: () => props.onApply({ status: "done" }, tx(t, "markDone", "Mark {n} task(s) as done?", { n: props.count })),
			size: "sm",
			title: "Mark done."
		}, tx(t, "complete", "Complete")), h(Button, {
			onClick: () => props.onApply({ archive: true }, tx(t, "markArchived", "Archive {n} task(s)?", { n: props.count })),
			size: "sm",
			title: "Archive selected."
		}, tx(t, "archive", "Archive")), h(Button, {
			onClick: () => props.onDelete(props.count),
			size: "sm",
			variant: "destructive",
			title: "Permanently delete."
		}, tx(t, "delete", "Delete")), h("div", {
			className: "hermes-kanban-bulk-priority",
			title: "Set priority on selected tasks."
		}, h(Input, {
			type: "number",
			value: priority,
			onChange: (e) => setPriority(e.target.value),
			placeholder: tx(t, "priority", "pri"),
			className: "h-7 text-xs w-16"
		}), h(Button, {
			onClick: () => {
				if (priority !== "") {
					props.onApply({ priority: Number(priority) });
					setPriority("");
				}
			},
			disabled: priority === "",
			size: "sm"
		}, tx(t, "setPriority", "Set priority"))), h("div", {
			className: "hermes-kanban-bulk-reassign",
			title: "Reassign selected tasks."
		}, h(Select, {
			value: assignee,
			className: "h-7 text-xs",
			...selectChangeHandler(setAssignee)
		}, h(SelectOption, { value: "" }, "— reassign —"), h(SelectOption, { value: "__none__" }, "(unassign)"), props.assignees.map((a) => h(SelectOption, {
			key: a,
			value: a
		}, a))), h(Button, {
			onClick: () => {
				if (!assignee) return;
				props.onApply({
					assignee: assignee === "__none__" ? "" : assignee,
					reclaim_first: reclaimFirst
				});
				setAssignee("");
			},
			disabled: !assignee,
			size: "sm"
		}, tx(t, "apply", "Apply"))), h("label", {
			className: "hermes-kanban-bulk-reclaim-first",
			title: "Reclaim any active claims before reassigning"
		}, h(Checkbox, {
			checked: reclaimFirst,
			onCheckedChange: (c) => setReclaimFirst(c === true)
		}), "Reclaim first"), h("div", { className: "flex-1" }), h(Button, {
			onClick: props.onSelectAllVisible,
			size: "sm",
			title: "Select all visible cards."
		}, "Select all visible"), h(Button, {
			onClick: props.onClear,
			size: "sm",
			title: "Deselect all."
		}, tx(t, "clear", "Clear")));
	}
	function OrchestrationPanel() {
		const [expanded, setExpanded] = useState(false);
		const [settings, setSettings] = useState(null);
		const [profiles, setProfiles] = useState([]);
		const [msg, setMsg] = useState(null);
		const loadAll = useCallback(() => {
			Promise.all([SDK.fetchJSON(`${API_BASE}/orchestration`), SDK.fetchJSON(`${API_BASE}/profiles`)]).then((results) => {
				setSettings(results[0] || null);
				setProfiles(results[1]?.profiles || []);
				setMsg(null);
			}).catch((err) => {
				setMsg({
					ok: false,
					text: "Failed to load: " + (err.message || String(err))
				});
			});
		}, []);
		useEffect(() => {
			if (settings === null) loadAll();
		}, [settings, loadAll]);
		const saveSettings = useCallback((patch) => {
			setMsg(null);
			return SDK.fetchJSON(`${API_BASE}/orchestration`, {
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(patch)
			}).then((res) => {
				setSettings(res);
				setMsg({
					ok: true,
					text: "Settings saved."
				});
				return res;
			}).catch((err) => {
				setMsg({
					ok: false,
					text: "Save failed: " + (err.message || String(err))
				});
			});
		}, []);
		const autoOn = !!(settings && settings.auto_decompose);
		const modePill = h("button", {
			type: "button",
			onClick: () => {
				if (settings !== null) saveSettings({ auto_decompose: !autoOn });
			},
			disabled: settings === null,
			title: settings === null ? "Loading mode…" : autoOn ? "Orchestration: Auto — the dispatcher decomposes new triage tasks automatically every tick. Click to switch to Manual." : "Orchestration: Manual — triage tasks stay in triage until you click ⚗ Decompose on each card. Click to switch to Auto.",
			className: "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium " + (autoOn ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "border-muted-foreground/30 bg-muted/30 text-muted-foreground")
		}, "Orchestration: ", h("span", { className: "ml-1 font-semibold" }, settings === null ? "…" : autoOn ? "Auto" : "Manual"));
		if (!expanded) return h("div", { className: "flex items-center gap-3 text-xs" }, modePill, h("button", {
			type: "button",
			onClick: () => setExpanded(true),
			className: "underline text-muted-foreground hover:text-foreground",
			title: "Configure the kanban orchestrator"
		}, "▾ Orchestration settings"));
		return h(Card, { className: "p-3" }, h(CardContent, { className: "p-2 flex flex-col gap-3" }, h("div", { className: "flex items-center justify-between" }, h("button", {
			type: "button",
			onClick: () => setExpanded(false),
			className: "text-sm font-medium underline-offset-2 hover:underline"
		}, "▾ Orchestration settings"), modePill, h(Button, {
			onClick: loadAll,
			size: "sm"
		}, "Reload")), msg ? h("div", { className: msg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err" }, msg.text) : null, settings ? h("div", { className: "grid gap-3 sm:grid-cols-3" }, h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs text-muted-foreground" }, "Orchestrator profile"), h(Select, {
			value: settings.orchestrator_profile || "",
			className: "h-8",
			...selectChangeHandler((v) => saveSettings({ orchestrator_profile: v }))
		}, h(SelectOption, { value: "" }, "(default: " + (settings.active_profile || "default") + ")"), profiles.map((p) => h(SelectOption, {
			key: p.name,
			value: p.name
		}, p.name + (p.is_default ? " (default)" : ""))))), h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs text-muted-foreground" }, "Default assignee"), h(Select, {
			value: settings.default_assignee || "",
			className: "h-8",
			...selectChangeHandler((v) => saveSettings({ default_assignee: v }))
		}, h(SelectOption, { value: "" }, "(default: " + (settings.active_profile || "default") + ")"), profiles.map((p) => h(SelectOption, {
			key: p.name,
			value: p.name
		}, p.name + (p.is_default ? " (default)" : ""))))), h("div", { className: "flex flex-col gap-1" }, h(Label, { className: "text-xs text-muted-foreground" }, "Orchestration mode"), h("label", { className: "flex items-center gap-2 text-xs h-8" }, h(Checkbox, {
			checked: !!settings.auto_decompose,
			onCheckedChange: (checked) => saveSettings({ auto_decompose: checked === true })
		}), "Auto-decompose triage tasks"))) : h("div", { className: "text-xs text-muted-foreground" }, "Loading…")));
	}
	function KanbanPage() {
		const { t } = useI18n();
		const [board, setBoard] = useState(() => readSelectedBoard());
		const [boardList, setBoardList] = useState([]);
		const [showNewBoard, setShowNewBoard] = useState(false);
		const [showNewTask, setShowNewTask] = useState(false);
		const [boardData, setBoardData] = useState(null);
		const [config, setConfig] = useState(null);
		const [loading, setLoading] = useState(true);
		const [error, setError] = useState(null);
		const [tenantFilter, setTenantFilter] = useState("");
		const [assigneeFilter, setAssigneeFilter] = useState("");
		const [includeArchived, setIncludeArchived] = useState(false);
		const [search, setSearch] = useState("");
		const [laneByProfile, setLaneByProfile] = useState(true);
		const [configApplied, setConfigApplied] = useState(false);
		const [selectedTaskId, setSelectedTaskId] = useState(null);
		const [selectedIds, setSelectedIds] = useState(() => /* @__PURE__ */ new Set());
		const [lastSelectedId, setLastSelectedId] = useState(null);
		const [failedIds, setFailedIds] = useState(() => /* @__PURE__ */ new Set());
		const [draggingTaskId, setDraggingTaskId] = useState(null);
		const [taskEventTick, setTaskEventTick] = useState({});
		const cursorRef = useRef(0);
		const reloadTimerRef = useRef(null);
		const wsRef = useRef(null);
		const wsBackoffRef = useRef(1e3);
		const wsClosedRef = useRef(false);
		const handleDragStart = useCallback((taskId) => setDraggingTaskId(taskId), []);
		const handleDragEnd = useCallback(() => setDraggingTaskId(null), []);
		useEffect(() => {
			SDK.fetchJSON(withBoard(`${API_BASE}/config`, board)).then((c) => {
				setConfig(c);
				if (!configApplied) {
					if (c.default_tenant) setTenantFilter(c.default_tenant);
					if (typeof c.lane_by_profile === "boolean") setLaneByProfile(c.lane_by_profile);
					if (typeof c.include_archived_by_default === "boolean") setIncludeArchived(c.include_archived_by_default);
					setConfigApplied(true);
				}
			}).catch(() => setConfig({ render_markdown: true }));
		}, []);
		const loadBoard = useCallback(() => {
			const qs = new URLSearchParams();
			if (tenantFilter) qs.set("tenant", tenantFilter);
			if (includeArchived) qs.set("include_archived", "true");
			const url = qs.toString() ? `${API_BASE}/board?${qs}` : `${API_BASE}/board`;
			return SDK.fetchJSON(withBoard(url, board)).then((data) => {
				setBoardData(data);
				cursorRef.current = data.latest_event_id || 0;
				setError(null);
			}).catch((err) => setError(String(err?.message || err))).finally(() => setLoading(false));
		}, [
			tenantFilter,
			includeArchived,
			board
		]);
		const loadBoardList = useCallback(() => {
			return SDK.fetchJSON(withBoard(`${API_BASE}/boards`, board)).then((data) => {
				const boards = data.boards || [];
				const storedBoard = readSelectedBoard();
				setBoardList(boards);
				if (!storedBoard && !board && data.current) {
					setBoard(data.current);
					return;
				}
				if (board && board !== "default" && !boards.find((b) => b.slug === board)) {
					setBoard("default");
					writeSelectedBoard("default");
				}
			}).catch(() => {});
		}, [board]);
		useEffect(() => {
			loadBoardList();
		}, [loadBoardList]);
		const scheduleReload = useCallback(() => {
			if (reloadTimerRef.current) return;
			reloadTimerRef.current = setTimeout(() => {
				reloadTimerRef.current = null;
				loadBoard();
			}, 250);
		}, [loadBoard]);
		useEffect(() => {
			loadBoard();
			return () => {
				if (reloadTimerRef.current) {
					clearTimeout(reloadTimerRef.current);
					reloadTimerRef.current = null;
				}
			};
		}, [loadBoard]);
		useEffect(() => {
			if (!boardData) return;
			wsClosedRef.current = false;
			function openWs() {
				if (wsClosedRef.current) return;
				const wsParams = { since: String(cursorRef.current || 0) };
				if (board) wsParams.board = board;
				SDK.buildWsUrl(`${API_BASE}/events`, wsParams).then((url) => {
					if (wsClosedRef.current) return;
					let ws;
					try {
						ws = new WebSocket(url);
					} catch {
						return;
					}
					wsRef.current = ws;
					ws.onopen = () => {
						wsBackoffRef.current = 1e3;
					};
					ws.onmessage = (ev) => {
						try {
							const msg = JSON.parse(ev.data);
							if (msg?.events && Array.isArray(msg.events) && msg.events.length > 0) {
								cursorRef.current = msg.cursor || cursorRef.current;
								setTaskEventTick((prev) => {
									const next = { ...prev };
									for (const e of msg.events) if (e?.task_id) next[e.task_id] = (next[e.task_id] || 0) + 1;
									return next;
								});
								scheduleReload();
							}
						} catch {}
					};
					ws.onclose = (ev) => {
						if (wsClosedRef.current) return;
						if (ev?.code === 1008) {
							setError(tx(t, "wsAuthFailed", "WebSocket auth failed — reload the page to refresh the session token."));
							return;
						}
						const delay = Math.min(wsBackoffRef.current, 3e4);
						wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 3e4);
						setTimeout(openWs, delay);
					};
				}).catch(() => {
					if (wsClosedRef.current) return;
					const delay = Math.min(wsBackoffRef.current, 3e4);
					wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 3e4);
					setTimeout(openWs, delay);
				});
			}
			openWs();
			return () => {
				wsClosedRef.current = true;
				try {
					wsRef.current?.close();
				} catch {}
			};
		}, [
			!!boardData,
			board,
			scheduleReload
		]);
		const filteredBoard = useMemo(() => {
			if (!boardData) return null;
			const q = search.trim().toLowerCase();
			const filterTask = (t) => {
				if (tenantFilter && t.tenant !== tenantFilter) return false;
				if (assigneeFilter && t.assignee !== assigneeFilter) return false;
				if (q) {
					if (`${t.id} ${t.title || ""} ${t.body || ""} ${t.result || ""} ${t.latest_summary || ""} ${t.assignee || ""} ${t.tenant || ""}`.toLowerCase().indexOf(q) === -1) return false;
				}
				return true;
			};
			return {
				...boardData,
				columns: boardData.columns.map((col) => ({
					...col,
					tasks: col.tasks.filter(filterTask)
				}))
			};
		}, [
			boardData,
			tenantFilter,
			assigneeFilter,
			search
		]);
		const moveTask = useCallback((taskId, newStatus) => {
			const confirmMsg = getDestructiveConfirm(t, newStatus);
			if (confirmMsg && !window.confirm(confirmMsg)) return;
			const patch = withCompletionSummary({ status: newStatus }, 1, t);
			if (!patch) return;
			setBoardData((b) => {
				if (!b) return b;
				let moved = null;
				const columns = b.columns.map((col) => {
					const next = col.tasks.filter((tk) => {
						if (tk.id === taskId) {
							moved = {
								...tk,
								status: newStatus
							};
							return false;
						}
						return true;
					});
					return {
						...col,
						tasks: next
					};
				});
				if (moved) {
					const dest = columns.find((c) => c.name === newStatus);
					if (dest) dest.tasks = [moved, ...dest.tasks];
				}
				return {
					...b,
					columns
				};
			});
			SDK.fetchJSON(withBoard(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, board), {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(patch)
			}).catch((err) => {
				setError(tx(t, "moveFailed", "Move failed: ") + parseApiErrorMessage(err));
				loadBoard();
			});
		}, [
			loadBoard,
			board,
			t
		]);
		const clearSelected = useCallback(() => {
			setSelectedIds(/* @__PURE__ */ new Set());
			setLastSelectedId(null);
			setFailedIds(/* @__PURE__ */ new Set());
		}, []);
		const moveSelected = useCallback((newStatus) => {
			const confirmMsg = DESTRUCTIVE_TRANSITIONS[newStatus];
			if (confirmMsg && !window.confirm(confirmMsg)) return;
			if (selectedIds.size === 0) return;
			const patch = withCompletionSummary({ status: newStatus }, selectedIds.size, t);
			if (!patch) return;
			const ids = Array.from(selectedIds);
			setBoardData((b) => {
				if (!b) return b;
				const moved = [];
				const columns = b.columns.map((col) => {
					const kept = [];
					for (const tk of col.tasks) if (selectedIds.has(tk.id)) moved.push({
						...tk,
						status: newStatus
					});
					else kept.push(tk);
					return {
						...col,
						tasks: kept
					};
				});
				const dest = columns.find((c) => c.name === newStatus);
				if (dest) dest.tasks = [...moved, ...dest.tasks];
				return {
					...b,
					columns
				};
			});
			SDK.fetchJSON(withBoard(`${API_BASE}/tasks/bulk`, board), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					ids,
					...patch
				})
			}).then((res) => {
				const failed = (res.results || []).filter((r) => !r.ok);
				if (failed.length > 0) {
					setError(`Bulk move: ${failed.length} of ${res.results.length} failed`);
					setFailedIds(new Set(failed.map((f) => f.id)));
				} else setFailedIds(/* @__PURE__ */ new Set());
				setSelectedIds(/* @__PURE__ */ new Set());
				setLastSelectedId(null);
				loadBoard();
			}).catch((err) => {
				setError(`Move failed: ${err.message || err}`);
				setFailedIds(new Set(selectedIds));
				loadBoard();
			});
		}, [
			selectedIds,
			loadBoard,
			board,
			t
		]);
		const createTask = useCallback((body) => {
			return SDK.fetchJSON(withBoard(`${API_BASE}/tasks`, board), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(body)
			}).then((res) => {
				if (res?.warning) setError(tx(t, "taskCreatedWarning", "Task created, but: ") + res.warning);
				loadBoard();
				loadBoardList();
				return res;
			});
		}, [
			loadBoard,
			loadBoardList,
			board,
			t
		]);
		const toggleSelected = useCallback((id, additive) => {
			setSelectedIds((prev) => {
				const next = new Set(additive ? prev : []);
				if (prev.has(id)) next.delete(id);
				else next.add(id);
				return next;
			});
			setLastSelectedId(id);
			setFailedIds((prev) => {
				if (prev.has(id)) {
					const n = new Set(prev);
					n.delete(id);
					return n;
				}
				return prev;
			});
		}, []);
		const toggleRange = useCallback((toId) => {
			setSelectedIds((prev) => {
				const next = new Set(prev);
				if (!filteredBoard?.columns) return next;
				const order = [];
				for (const col of filteredBoard.columns) for (const tk of col.tasks || []) order.push(tk.id);
				const anchor = lastSelectedId;
				if (!anchor || anchor === toId) {
					next.add(toId);
					return next;
				}
				const aIdx = order.indexOf(anchor), bIdx = order.indexOf(toId);
				if (aIdx === -1 || bIdx === -1) {
					next.add(toId);
					return next;
				}
				const lo = Math.min(aIdx, bIdx), hi = Math.max(aIdx, bIdx);
				for (let i = lo; i <= hi; i++) next.add(order[i]);
				return next;
			});
			setLastSelectedId(toId);
		}, [filteredBoard, lastSelectedId]);
		const selectAllVisible = useCallback(() => {
			if (!filteredBoard?.columns) return;
			const next = /* @__PURE__ */ new Set();
			for (const col of filteredBoard.columns) for (const t of col.tasks || []) next.add(t.id);
			setSelectedIds(next);
			if (next.size > 0) setLastSelectedId(Array.from(next)[0]);
		}, [filteredBoard]);
		const selectAllInColumn = useCallback((columnName) => {
			if (!filteredBoard?.columns) return;
			const col = filteredBoard.columns.find((c) => c.name === columnName);
			if (!col) return;
			const allSelected = col.tasks?.length > 0 && col.tasks.every((t) => selectedIds.has(t.id));
			const next = new Set(selectedIds);
			if (allSelected) for (const t of col.tasks || []) next.delete(t.id);
			else for (const t of col.tasks || []) next.add(t.id);
			setSelectedIds(next);
			if (col.tasks?.length) setLastSelectedId(col.tasks[0].id);
		}, [filteredBoard, selectedIds]);
		const applyBulk = useCallback((patch, confirmMsg) => {
			if (selectedIds.size === 0) return;
			if (confirmMsg && !window.confirm(confirmMsg)) return;
			const finalPatch = withCompletionSummary(patch, selectedIds.size, t);
			if (!finalPatch) return;
			const body = {
				ids: Array.from(selectedIds),
				...finalPatch
			};
			if (finalPatch.status) setBoardData((b) => {
				if (!b) return b;
				const moved = [];
				const columns = b.columns.map((col) => {
					const kept = [];
					for (const t of col.tasks) if (selectedIds.has(t.id)) moved.push({
						...t,
						status: finalPatch.status
					});
					else kept.push(t);
					return {
						...col,
						tasks: kept
					};
				});
				const dest = columns.find((c) => c.name === finalPatch.status);
				if (dest) dest.tasks = [...moved, ...dest.tasks];
				return {
					...b,
					columns
				};
			});
			SDK.fetchJSON(withBoard(`${API_BASE}/tasks/bulk`, board), {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(body)
			}).then((res) => {
				const failed = (res.results || []).filter((r) => !r.ok);
				if (failed.length > 0) {
					setError(tx(t, "bulkFailed", "Bulk: ") + `${failed.length} of ${res.results.length} failed: ` + failed.slice(0, 3).map((f) => `${f.id} (${f.error})`).join("; "));
					setFailedIds(new Set(failed.map((f) => f.id)));
				} else setFailedIds(/* @__PURE__ */ new Set());
				setSelectedIds(/* @__PURE__ */ new Set());
				setLastSelectedId(null);
				loadBoard();
			}).catch((e) => {
				setError(String(e.message || e));
				setFailedIds(new Set(selectedIds));
				loadBoard();
			});
		}, [
			selectedIds,
			loadBoard,
			board,
			t
		]);
		const switchBoard = useCallback((nextSlug) => {
			if (!nextSlug || nextSlug === board) return;
			setBoardData(null);
			cursorRef.current = 0;
			setLoading(true);
			setBoard(nextSlug);
			writeSelectedBoard(nextSlug);
			setSearch("");
			setTenantFilter("");
			setAssigneeFilter("");
			setIncludeArchived(false);
			clearSelected();
		}, [board, clearSelected]);
		const createNewBoard = useCallback((payload) => {
			return SDK.fetchJSON(`${API_BASE}/boards`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(payload)
			}).then((res) => {
				loadBoardList();
				const slug = res?.board?.slug;
				if (slug && payload.switch) switchBoard(slug);
				return res;
			});
		}, [loadBoardList, switchBoard]);
		const deleteBoard = useCallback((slug) => {
			if (!slug || slug === "default") return Promise.resolve();
			return SDK.fetchJSON(`${API_BASE}/boards/${encodeURIComponent(slug)}`, { method: "DELETE" }).then(() => {
				loadBoardList();
				if (board === slug) switchBoard("default");
			});
		}, [
			board,
			loadBoardList,
			switchBoard
		]);
		const deleteTask = useCallback((taskId) => {
			if (!window.confirm(tx(t, "trash.confirm", FALLBACK_TRASH.confirm))) return Promise.resolve();
			return SDK.fetchJSON(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" }).then(() => {
				loadBoard();
				setSelectedIds((prev) => {
					const n = new Set(prev);
					n.delete(taskId);
					return n;
				});
			}).catch((e) => setError(String(e.message || e)));
		}, [
			board,
			loadBoard,
			t
		]);
		const deleteSelected = useCallback((count) => {
			if (selectedIds.size === 0) return Promise.resolve();
			if (!window.confirm(tx(t, "trash.confirmMany", "Permanently delete {n} selected tasks? This cannot be undone.", { n: count }))) return Promise.resolve();
			const ids = Array.from(selectedIds);
			setSelectedIds(/* @__PURE__ */ new Set());
			return Promise.all(ids.map((id) => SDK.fetchJSON(`${API_BASE}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" }))).then(() => loadBoard()).catch((e) => setError(String(e.message || e)));
		}, [
			selectedIds,
			board,
			loadBoard,
			t
		]);
		if (loading && !boardData) return h("div", { className: "p-8 text-sm text-muted-foreground" }, tx(t, "loading", "Loading Kanban board…"));
		if (error && !boardData) return h(Card, null, h(CardContent, { className: "p-6" }, h("div", { className: "text-sm text-destructive" }, tx(t, "loadFailed", "Failed to load Kanban board: "), error), h("div", { className: "text-xs text-muted-foreground mt-2" }, tx(t, "loadFailedHint", "The backend auto-creates kanban.db on first read. If this persists, check the dashboard logs."))));
		if (!filteredBoard) return null;
		const renderMd = !config || config.render_markdown !== false;
		const allTasks = boardData.columns.reduce((acc, c) => acc.concat(c.tasks), []);
		return h(ErrorBoundary, null, h("div", { className: "hermes-kanban flex flex-col gap-4" }, h(BoardSwitcher, {
			board,
			boardList,
			onSwitch: switchBoard,
			onNewClick: () => setShowNewBoard(true),
			onDeleteBoard: deleteBoard
		}), showNewBoard ? h(NewBoardDialog, {
			onCancel: () => setShowNewBoard(false),
			onCreate: createNewBoard
		}) : null, h(OrchestrationPanel), h(AttentionStrip, {
			boardData,
			onOpen: setSelectedTaskId
		}), h(BoardToolbar, {
			board: boardData,
			search,
			setSearch,
			tenantFilter,
			setTenantFilter,
			assigneeFilter,
			setAssigneeFilter,
			includeArchived,
			setIncludeArchived,
			laneByProfile,
			setLaneByProfile,
			onNudgeDispatch: () => {
				SDK.fetchJSON(withBoard(`${API_BASE}/dispatch?max=8`, board), { method: "POST" }).then(loadBoard).catch((e) => setError(String(e.message || e)));
			},
			onRefresh: loadBoard,
			onNewTask: () => setShowNewTask(true)
		}), selectedIds.size > 0 ? h(BulkActionBar, {
			count: selectedIds.size,
			assignees: boardData?.assignees || [],
			onApply: applyBulk,
			onClear: clearSelected,
			onSelectAllVisible: selectAllVisible,
			onDelete: deleteSelected
		}) : null, error ? h("div", { className: "text-xs text-destructive px-2" }, error) : null, h(BoardColumns, {
			board: filteredBoard,
			laneByProfile,
			selectedIds,
			failedIds,
			draggingTaskId,
			onDragStart: handleDragStart,
			onDragEnd: handleDragEnd,
			toggleSelected,
			toggleRange,
			selectAllInColumn,
			onMove: moveTask,
			onMoveSelected: moveSelected,
			onDelete: deleteTask,
			onOpen: setSelectedTaskId,
			onCreate: createTask,
			allTasks
		}), selectedTaskId ? h(TaskDrawer, {
			taskId: selectedTaskId,
			boardSlug: board,
			onClose: () => setSelectedTaskId(null),
			onRefresh: loadBoard,
			renderMarkdown: renderMd,
			allTasks,
			assignees: boardData?.assignees || [],
			eventTick: taskEventTick[selectedTaskId] || 0
		}) : null, h(NewTaskDialog, {
			open: showNewTask,
			onClose: () => setShowNewTask(false),
			onCreate: (body) => createTask(body).then(() => {
				setShowNewTask(false);
			})
		})));
	}
	//#endregion
	//#region src/index.ts
	/**
	* Kanban dashboard plugin — entry point.
	*
	* Registers the KanbanPage component with the host dashboard's plugin
	* registry. The host loads this file (compiled) via ``manifest.json``
	* and calls ``window.__HERMES_PLUGINS__.register("kanban", Component)``.
	*/
	if (window.__HERMES_PLUGINS__) window.__HERMES_PLUGINS__.register("kanban", KanbanPage);
	//#endregion
})(React);
