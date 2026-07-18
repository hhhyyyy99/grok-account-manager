(function () {
  "use strict";
  function orderAccountsById(accounts) {
    return Array.from(accounts || []).sort((left, right) => Number(right.id) - Number(left.id));
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { orderAccountsById };
    return;
  }

  const token = document.querySelector('meta[name="grok-manager-token"]').content;
  const state = {
    accounts: [],
    stats: {},
    tasks: [],
    pagination: { page: 1, pageSize: 50, total: 0, totalPages: 1 },
    selected: new Set(),
    selectedTaskId: "",
    config: null,
    taskTimer: null,
    taskFilter: "all",
    drawerReturnFocus: null,
    lastTaskStates: new Map(),
  };

  const byId = (id) => document.getElementById(id);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const registrationConfigForms = [
    "registration-base-config-form",
    "email-config-form",
    "cpa-config-form",
    "sub2api-config-form",
    "grok2api-config-form",
  ];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeStatus(value) {
    const allowed = new Set([
      "unknown", "active", "expired", "invalid", "needs_login", "limited",
      "error", "checking", "missing_cpa", "queued", "running", "succeeded",
      "failed", "cancelled",
    ]);
    return allowed.has(String(value)) ? String(value) : "unknown";
  }

  function formatTime(value) {
    if (!value) return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  async function api(path, options = {}) {
    const init = {
      method: options.method || "GET",
      headers: { Accept: "application/json", "X-Grok-Manager-Token": token },
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-Grok-Manager-Token"] = token;
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(payload.error || `请求失败 HTTP ${response.status}`);
    }
    return payload;
  }

  function toast(message, error = false) {
    const item = document.createElement("div");
    item.className = `toast${error ? " is-error" : ""}`;
    item.textContent = String(message);
    byId("toast-region").appendChild(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function showView(name) {
    all("[data-view]").forEach((view) => view.classList.toggle("is-active", view.dataset.view === name));
    all("[data-view-target]").forEach((button) => button.classList.toggle("is-active", button.dataset.viewTarget === name));
    document.querySelector(".sidebar").classList.remove("is-open");
    if (name === "settings") loadConfig();
  }

  function showSettingsView(name) {
    all("[data-settings-view]").forEach((view) => view.classList.toggle("is-active", view.dataset.settingsView === name));
    all("[data-settings-target]").forEach((button) => button.classList.toggle("is-active", button.dataset.settingsTarget === name));
  }

  function selectedIds() {
    return Array.from(state.selected).map(Number);
  }

  function clearSelection(render = true) {
    state.selected.clear();
    if (render) renderAccounts();
    else updateSelectionBar();
  }

  function updateSelectionBar() {
    const count = state.selected.size;
    const pageSelected = state.accounts.reduce(
      (total, account) => total + (state.selected.has(Number(account.id)) ? 1 : 0),
      0,
    );
    const hasSelection = count > 0;
    byId("selected-count").textContent = String(count);
    const selectAll = byId("select-all");
    selectAll.checked = state.accounts.length > 0 && pageSelected === state.accounts.length;
    selectAll.indeterminate = pageSelected > 0 && pageSelected < state.accounts.length;
    byId("select-all-results").disabled = (
      Number(state.pagination.total || 0) === 0
      || count === Number(state.pagination.total || 0)
    );
    byId("clear-selection").disabled = !hasSelection;
    byId("login-selected").disabled = !hasSelection;
    byId("reset-password-selected").disabled = !hasSelection;
    byId("delete-selected").disabled = !hasSelection;
    byId("export-format").disabled = !hasSelection;
    byId("export-accounts").disabled = !hasSelection;
    const inspection = state.tasks.find(
      (task) => task.kind === "inspect" && ["queued", "running"].includes(task.state),
    );
    byId("inspect-selected").disabled = !hasSelection || Boolean(inspection);
  }

  async function selectAllResults() {
    const query = new URLSearchParams();
    const search = byId("account-search").value.trim();
    const status = byId("status-filter").value;
    if (search) query.set("search", search);
    if (status) query.set("status", status);
    byId("select-all-results").disabled = true;
    try {
      const payload = await api(`/api/accounts/selection?${query.toString()}`);
      state.selected = new Set((payload.ids || []).map(Number));
      renderAccounts();
      toast(`已选择全部 ${Number(payload.total || state.selected.size)} 个筛选结果`);
    } catch (error) {
      toast(error.message, true);
      updateSelectionBar();
    }
  }

  function renderMetrics(stats) {
    const attention = ["expired", "invalid", "needs_login", "error"]
      .reduce((sum, key) => sum + Number(stats[key] || 0), 0);
    byId("metric-total").textContent = String(stats.total || 0);
    byId("metric-active").textContent = String(Number(stats.active || 0) + Number(stats.limited || 0));
    byId("metric-attention").textContent = String(attention);
    byId("metric-unknown").textContent = String(stats.unknown || 0);
  }

  function renderAccounts() {
    const body = byId("account-table-body");
    body.innerHTML = state.accounts.map((account) => {
      const status = safeStatus(account.status);
      const ssoStatus = safeStatus(account.ssoStatus);
      const cpaStatus = safeStatus(account.cpaStatus);
      const checked = account.lastCheckedAt ? formatTime(account.lastCheckedAt) : "-";
      const credentialDetail = `SSO: ${account.ssoDetail || "未巡检"}\nCPA: ${account.cpaDetail || "未巡检"}`;
      return `
        <tr data-account-id="${Number(account.id)}">
          <td class="check-column"><input class="row-select" type="checkbox" value="${Number(account.id)}" aria-label="选择 ${escapeHtml(account.email)}" ${state.selected.has(Number(account.id)) ? "checked" : ""}></td>
          <td class="email-cell" title="${escapeHtml(account.email)}">${escapeHtml(account.email)}</td>
          <td><span class="status-badge status-${status}">${escapeHtml(account.statusLabel)}</span></td>
          <td title="${escapeHtml(account.ssoDetail || "")}"><span class="status-badge status-${ssoStatus}">${escapeHtml(account.ssoStatusLabel)}</span></td>
          <td title="${escapeHtml(account.cpaDetail || "")}"><span class="status-badge status-${cpaStatus}">${escapeHtml(account.cpaStatusLabel)}</span></td>
          <td class="time-cell">${escapeHtml(checked)}</td>
          <td class="detail-cell" title="${escapeHtml(credentialDetail)}">${escapeHtml(account.detail || "-")}</td>
        </tr>`;
    }).join("");

    all(".row-select", body).forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const id = Number(checkbox.value);
        if (checkbox.checked) state.selected.add(id);
        else state.selected.delete(id);
        updateSelectionBar();
      });
    });
    byId("account-table-wrap").hidden = state.accounts.length === 0;
    byId("account-empty").hidden = state.accounts.length !== 0;
    byId("account-loading").hidden = true;
    updateSelectionBar();
  }

  function renderPagination() {
    const pagination = state.pagination;
    const total = Number(pagination.total || 0);
    const page = Number(pagination.page || 1);
    const pageSize = Number(pagination.pageSize || 50);
    const totalPages = Number(pagination.totalPages || 1);
    const start = total ? ((page - 1) * pageSize) + 1 : 0;
    const end = total ? Math.min(page * pageSize, total) : 0;

    byId("account-pagination").hidden = total === 0;
    byId("pagination-summary").textContent = `第 ${start}-${end} 条，共 ${total} 条`;
    byId("current-page").textContent = String(page);
    byId("total-pages").textContent = String(totalPages);
    byId("page-size").value = String(pageSize);
    byId("first-page").disabled = page <= 1;
    byId("previous-page").disabled = page <= 1;
    byId("next-page").disabled = page >= totalPages;
    byId("last-page").disabled = page >= totalPages;
  }

  async function loadState(options = {}) {
    if (options.page !== undefined) state.pagination.page = Math.max(1, Number(options.page) || 1);
    if (options.loading) byId("account-loading").hidden = false;
    const query = new URLSearchParams();
    const search = byId("account-search").value.trim();
    const status = byId("status-filter").value;
    if (search) query.set("search", search);
    if (status) query.set("status", status);
    query.set("page", String(state.pagination.page));
    query.set("page_size", String(state.pagination.pageSize));
    try {
      const payload = await api(`/api/state?${query.toString()}`);
      state.accounts = orderAccountsById(payload.accounts || []);
      state.stats = payload.stats || {};
      state.tasks = payload.tasks || [];
      state.pagination = payload.pagination || state.pagination;
      renderMetrics(state.stats);
      renderAccounts();
      renderPagination();
      renderTaskList();
      scheduleTaskPolling();
    } catch (error) {
      byId("account-loading").hidden = true;
      byId("account-table-wrap").hidden = true;
      byId("account-pagination").hidden = true;
      byId("account-empty").hidden = false;
      byId("account-empty").querySelector("strong").textContent = "账号数据加载失败";
      byId("account-empty").querySelector("p").textContent = error.message;
      toast(error.message, true);
    }
  }

  function taskLabelState(task) {
    const labels = {
      queued: "等待中",
      running: "运行中",
      succeeded: "已完成",
      failed: "失败",
      cancelled: "已取消",
    };
    return labels[task.state] || task.state;
  }

  function isTaskRunning(task) {
    return !["succeeded", "failed", "cancelled"].includes(task.state);
  }

  function taskKindLabel(task) {
    const labels = {
      inspect: "账号巡检",
      login: "账号登录",
      register: "批量注册",
      import: "账号导入",
      "reset-password": "密码重置",
      diagnostics: "环境检查",
    };
    return labels[task.kind] || "后台任务";
  }

  function taskPercent(task) {
    if (Number(task.total) > 0) {
      return Math.min(100, Math.max(0, Math.round((Number(task.current) / Number(task.total)) * 100)));
    }
    return task.state === "succeeded" ? 100 : 0;
  }

  function formatTaskTime(value) {
    if (!value) return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  function taskMatchesFilter(task, filter = state.taskFilter) {
    if (filter === "running") return isTaskRunning(task);
    if (filter === "succeeded") return task.state === "succeeded";
    if (filter === "failed") return ["failed", "cancelled"].includes(task.state);
    return true;
  }

  function visibleTasks() {
    return state.tasks.filter((task) => taskMatchesFilter(task));
  }

  function renderTaskList() {
    const list = byId("task-list");
    const running = state.tasks.filter(isTaskRunning);
    const succeeded = state.tasks.filter((task) => task.state === "succeeded");
    const failed = state.tasks.filter((task) => ["failed", "cancelled"].includes(task.state));
    const inspection = running.find((task) => task.kind === "inspect");
    const filtered = visibleTasks();
    const trigger = document.querySelector(".task-trigger");

    byId("running-task-count").textContent = String(running.length);
    byId("task-running-summary").textContent = String(running.length);
    byId("task-succeeded-summary").textContent = String(succeeded.length);
    byId("task-failed-summary").textContent = String(failed.length);
    byId("task-drawer-subtitle").textContent = running.length
      ? `${running.length} 个任务正在执行 · 最近 ${state.tasks.length} 条`
      : `当前空闲 · 最近 ${state.tasks.length} 条`;
    trigger.classList.toggle("is-live", running.length > 0);
    trigger.setAttribute("aria-label", running.length ? `打开任务中心，${running.length} 个任务进行中` : "打开任务中心");
    all("[data-task-filter]").forEach((button) => {
      const active = button.dataset.taskFilter === state.taskFilter;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });

    byId("inspect-selected").disabled = Boolean(inspection) || state.selected.size === 0;
    byId("inspect-all").disabled = Boolean(inspection);
    byId("cancel-inspection").disabled = !inspection || Boolean(inspection.cancelRequested);

    if (!state.tasks.length) {
      list.innerHTML = '<div class="empty-task"><strong>还没有任务</strong><p>导入、巡检、登录和注册任务会显示在这里。</p></div>';
      return;
    }
    if (!filtered.length) {
      list.innerHTML = '<div class="empty-task"><strong>没有符合条件的任务</strong><p>切换筛选可查看其他任务记录。</p></div>';
      return;
    }

    list.innerHTML = filtered.map((task) => {
      const status = safeStatus(task.state);
      const percent = taskPercent(task);
      const showProgress = isTaskRunning(task) || Number(task.total) > 0;
      return `
        <button class="task-list-item task-item-${status}${state.selectedTaskId === task.id ? " is-active" : ""}" type="button" data-task-id="${escapeHtml(task.id)}"${state.selectedTaskId === task.id ? ' aria-current="true"' : ""}>
          <span class="task-list-main">
            <span class="task-kind-label">${escapeHtml(taskKindLabel(task))}</span>
            <strong>${escapeHtml(task.label)}</strong>
            <small>${escapeHtml(task.message || "等待任务更新")}</small>
          </span>
          <span class="task-list-meta">
            <span class="task-state task-${status}">${escapeHtml(taskLabelState(task))}</span>
            <small>${escapeHtml(formatTaskTime(task.startedAt || task.createdAt))}</small>
          </span>
          ${showProgress ? `<progress class="task-list-progress" max="100" value="${percent}" aria-label="进度 ${percent}%">${percent}%</progress>` : ""}
        </button>`;
    }).join("");
    all("[data-task-id]", list).forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.taskId)));
  }

  function renderTaskDetail(task) {
    const detail = byId("task-detail");
    const previousTaskId = detail.dataset.taskId || "";
    const previousLog = byId("active-task-log");
    const shouldStickToBottom = Boolean(
      previousLog
      && previousLog.scrollHeight - previousLog.scrollTop - previousLog.clientHeight < 24,
    );
    const stateName = safeStatus(task.state);
    const percent = taskPercent(task);
    const canCancel = ["queued", "running"].includes(task.state)
      && ["register", "login", "reset-password", "inspect"].includes(task.kind)
      && !task.cancelRequested;
    const logs = task.logs || [];
    const logMarkup = logs.map((line) => `
      <div class="task-log-row"><time>${escapeHtml(line.time)}</time><span>${escapeHtml(line.message)}</span></div>`).join("");
    const progressCaption = Number(task.total) > 0
      ? `${Number(task.current)} / ${Number(task.total)}`
      : taskLabelState(task);
    const statusMessage = task.error || task.message || "等待任务更新";

    detail.setAttribute("aria-busy", "false");
    detail.dataset.taskId = task.id;
    detail.innerHTML = `
      <div class="task-detail-heading">
        <div><span class="task-kind-label">${escapeHtml(taskKindLabel(task))}</span><h3>${escapeHtml(task.label)}</h3><p>${escapeHtml(statusMessage)}</p></div>
        <span class="task-state task-${stateName}">${escapeHtml(taskLabelState(task))}</span>
      </div>
      <div class="task-detail-meta">
        <div class="task-detail-meta-item"><span>任务编号</span><strong>${escapeHtml(task.id)}</strong></div>
        <div class="task-detail-meta-item"><span>启动时间</span><strong>${escapeHtml(formatTime(task.startedAt || task.createdAt))}</strong></div>
        <div class="task-detail-meta-item"><span>任务类型</span><strong>${escapeHtml(taskKindLabel(task))}</strong></div>
        <div class="task-detail-meta-item"><span>结束时间</span><strong>${escapeHtml(formatTime(task.finishedAt))}</strong></div>
      </div>
      <div class="task-progress">
        <div class="task-progress-heading"><span>完成进度</span><strong>${percent}%</strong></div>
        <progress class="task-progress-bar" max="100" value="${percent}">${percent}%</progress>
        <small>${escapeHtml(progressCaption)}</small>
      </div>
      <div class="task-action-row">
        ${logs.length ? '<button class="button button-quiet" type="button" id="copy-task-logs">复制日志</button>' : ""}
        ${canCancel ? '<button class="button button-danger" type="button" id="cancel-selected-task">取消任务</button>' : ""}
      </div>
      <div class="task-log-label"><span>执行日志</span><span>${logs.length} 条</span></div>
      <div class="task-log" id="active-task-log">${logMarkup || '<span class="muted">任务还没有产生日志。</span>'}</div>`;

    const logBox = byId("active-task-log");
    if (logBox && (previousTaskId !== task.id || shouldStickToBottom)) {
      logBox.scrollTop = logBox.scrollHeight;
    }
    if (logs.length) {
      byId("copy-task-logs").addEventListener("click", async () => {
        const text = logs.map((line) => `[${line.time}] ${line.message}`).join("\n");
        try {
          await navigator.clipboard.writeText(text);
          toast("任务日志已复制");
        } catch (_) {
          toast("浏览器未允许复制，请在日志区域手动选择", true);
        }
      });
    }
    if (canCancel) {
      byId("cancel-selected-task").addEventListener("click", async () => {
        const confirmed = await confirmOperation("取消任务", `确定取消“${task.label}”吗？已完成的步骤不会回退。`, true);
        if (confirmed) cancelTask(task.id);
      });
    }
    if (task.kind === "diagnostics" && task.result && task.result.checks) {
      renderDiagnostics(task.result.checks);
    }
  }

  async function selectTask(taskId) {
    state.selectedTaskId = taskId;
    renderTaskList();
    const detail = byId("task-detail");
    detail.setAttribute("aria-busy", "true");
    detail.innerHTML = '<div class="task-detail-loading" aria-label="正在加载任务详情"><span></span><span></span><span></span></div>';
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      if (state.selectedTaskId === taskId) renderTaskDetail(task);
    } catch (error) {
      if (state.selectedTaskId !== taskId) return;
      detail.setAttribute("aria-busy", "false");
      detail.innerHTML = `<div class="task-detail-error"><strong>任务详情加载失败</strong><p>${escapeHtml(error.message)}</p><button class="button" type="button" id="retry-task-detail">重试</button></div>`;
      byId("retry-task-detail").addEventListener("click", () => selectTask(taskId));
      toast(error.message, true);
    }
  }

  function setTaskFilter(filter) {
    if (!["all", "running", "succeeded", "failed"].includes(filter)) return;
    state.taskFilter = filter;
    renderTaskList();
    const filtered = visibleTasks();
    if (!filtered.length) {
      state.selectedTaskId = "";
      byId("task-detail").innerHTML = '<div class="empty-task"><strong>此筛选暂无任务</strong><p>切换上方分类继续查看。</p></div>';
      return;
    }
    if (!filtered.some((task) => task.id === state.selectedTaskId)) selectTask(filtered[0].id);
  }

  function setTaskDrawerExpanded(expanded) {
    const drawer = byId("task-drawer");
    drawer.inert = !expanded;
    all("[data-open-tasks]").forEach((button) => button.setAttribute("aria-expanded", String(expanded)));
  }

  function openTaskDrawer(taskId = "", trigger = null) {
    const drawer = byId("task-drawer");
    const wasOpen = drawer.classList.contains("is-open");
    if (!wasOpen) {
      state.drawerReturnFocus = trigger || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    }
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    byId("drawer-backdrop").hidden = false;
    document.body.classList.add("task-drawer-open");
    setTaskDrawerExpanded(true);
    const targetId = taskId
      || (visibleTasks().some((task) => task.id === state.selectedTaskId) ? state.selectedTaskId : "")
      || (visibleTasks()[0] && visibleTasks()[0].id)
      || "";
    if (targetId) selectTask(targetId);
    if (!wasOpen) window.requestAnimationFrame(() => byId("close-task-drawer").focus());
  }

  function closeTaskDrawer() {
    const drawer = byId("task-drawer");
    if (!drawer.classList.contains("is-open")) return;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
    byId("drawer-backdrop").hidden = true;
    document.body.classList.remove("task-drawer-open");
    setTaskDrawerExpanded(false);
    const returnFocus = state.drawerReturnFocus;
    state.drawerReturnFocus = null;
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  }

  function handleTaskDrawerKeydown(event) {
    const drawer = byId("task-drawer");
    if (!drawer.classList.contains("is-open") || document.querySelector("dialog[open]")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeTaskDrawer();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = all('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', drawer)
      .filter((element) => element.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function refreshTaskCenter() {
    const button = byId("refresh-tasks");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const payload = await api("/api/tasks");
      const tasks = payload.tasks || [];
      state.tasks = tasks.map((task) => {
        const copy = { ...task };
        delete copy.logs;
        return copy;
      });
      renderTaskList();
      const selected = tasks.find((task) => task.id === state.selectedTaskId);
      if (selected) renderTaskDetail(selected);
      scheduleTaskPolling();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  async function startTask(path, body, message) {
    try {
      const payload = await api(path, { method: "POST", body });
      const task = payload.task;
      state.lastTaskStates.set(task.id, task.state);
      state.selectedTaskId = task.id;
      toast(message || `${task.label}已开始`);
      openTaskDrawer(task.id);
      await loadState();
      scheduleTaskPolling(true);
      return task;
    } catch (error) {
      toast(error.message, true);
      return null;
    }
  }

  async function cancelTask(taskId) {
    try {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", body: {} });
      toast("已请求取消任务");
      scheduleTaskPolling(true);
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function cancelInspection() {
    const task = state.tasks.find((item) => item.kind === "inspect" && ["queued", "running"].includes(item.state));
    if (!task || task.cancelRequested) return;
    await cancelTask(task.id);
  }

  function scheduleTaskPolling(immediate = false) {
    if (state.taskTimer) window.clearTimeout(state.taskTimer);
    const hasRunning = state.tasks.some((task) => !["succeeded", "failed", "cancelled"].includes(task.state));
    if (!hasRunning && !immediate) return;
    state.taskTimer = window.setTimeout(pollTasks, immediate ? 80 : 900);
  }

  async function pollTasks() {
    try {
      const payload = await api("/api/tasks");
      const tasks = payload.tasks || [];
      tasks.forEach((task) => {
        const previous = state.lastTaskStates.get(task.id);
        if (previous && previous !== task.state && ["succeeded", "failed", "cancelled"].includes(task.state)) {
          toast(task.state === "succeeded" ? task.message : (task.error || task.message), task.state === "failed");
        }
        state.lastTaskStates.set(task.id, task.state);
      });
      state.tasks = tasks.map((task) => {
        const copy = { ...task };
        delete copy.logs;
        return copy;
      });
      renderTaskList();
      if (state.selectedTaskId) {
        const selected = tasks.find((task) => task.id === state.selectedTaskId);
        if (selected) renderTaskDetail(selected);
      }
      await loadState();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function confirmOperation(title, message, danger = false) {
    const dialog = byId("confirm-dialog");
    byId("confirm-title").textContent = title;
    byId("confirm-message").textContent = message;
    byId("confirm-action").className = `button ${danger ? "button-danger" : "button-primary"}`;
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    });
  }

  async function importHistory() {
    await startTask("/api/import", {}, "导入任务已开始");
  }

  async function importFile(file) {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      toast("文件不能超过 5MB", true);
      return;
    }
    const content = await file.text();
    await startTask("/api/import", { filename: file.name, content }, "文件导入已开始");
  }

  async function exportAccounts() {
    const button = byId("export-accounts");
    const format = byId("export-format").value;
    const ids = selectedIds();
    if (!ids.length) return;
    const body = { format, ids };
    button.disabled = true;
    try {
      const response = await fetch("/api/accounts/export", {
        method: "POST",
        headers: {
          Accept: "application/json, application/zip",
          "Content-Type": "application/json",
          "X-Grok-Manager-Token": token,
        },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        let message = `导出失败 HTTP ${response.status}`;
        try {
          const payload = await response.json();
          if (payload.error) message = payload.error;
        } catch (_) {
          // Keep the HTTP fallback when the response is not JSON.
        }
        throw new Error(message);
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
      const extension = format === "cpa" ? "zip" : "json";
      const filename = filenameMatch?.[1] || `grok-${format}-export.${extension}`;
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = filename;
      link.hidden = true;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);

      const exported = Number(response.headers.get("X-Exported-Count") || 0);
      const skipped = Number(response.headers.get("X-Skipped-Count") || 0);
      toast(skipped ? `已导出 ${exported} 个账号，跳过 ${skipped} 个缺少凭据的账号` : `已导出 ${exported} 个账号`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      updateSelectionBar();
    }
  }

  async function inspect(ids, allAccounts = false) {
    await startTask("/api/inspect", { ids, all: allAccounts }, "巡检任务已开始");
  }

  async function login(ids, candidates = false) {
    const count = candidates ? "所有异常账号" : `${ids.length} 个所选账号`;
    const confirmed = await confirmOperation(
      "确认批量登录",
      `将为${count}启动真实浏览器，并使用本地保存的邮箱和密码重新获取 token。是否继续？`,
    );
    if (!confirmed) return;
    await startTask("/api/login", { ids, candidates }, "批量登录已开始");
  }

  async function resetPassword(ids) {
    const confirmed = await confirmOperation(
      "确认重置密码",
      `将使用账号对应的临时邮箱接收验证码，生成新密码并自动重新登录 ${ids.length} 个账号。是否继续？`,
      true,
    );
    if (!confirmed) return;
    await startTask("/api/reset-password", { ids }, "密码重置任务已开始");
  }

  async function deleteSelected() {
    const ids = selectedIds();
    if (!ids.length) return;
    const confirmed = await confirmOperation(
      "删除管理记录",
      `将从本地管理库删除 ${ids.length} 条记录。注册产物和凭据文件不会被修改。`,
      true,
    );
    if (!confirmed) return;
    try {
      const result = await api("/api/accounts/delete", { method: "POST", body: { ids } });
      state.selected.clear();
      toast(`已删除 ${result.deleted} 条管理记录`);
      await loadState();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function setFormValues(form, values) {
    all("[name]", form).forEach((input) => {
      if (!(input.name in values)) return;
      if (input.type === "checkbox") input.checked = Boolean(values[input.name]);
      else input.value = values[input.name] ?? "";
    });
  }

  function formValues(form) {
    const values = {};
    all("[name]", form).forEach((input) => {
      if (input.type === "checkbox") values[input.name] = input.checked;
      else if (input.type === "number") values[input.name] = Number(input.value);
      else values[input.name] = input.value.trim();
    });
    return values;
  }

  async function loadConfig() {
    try {
      const config = await api("/api/config");
      state.config = config;
      setFormValues(byId("manager-config-form"), config.manager || {});
      registrationConfigForms.forEach((formId) => {
        setFormValues(byId(formId), config.registration || {});
      });
      byId("reference-json").value = JSON.stringify(config.registration || {}, null, 2);
      const manager = config.manager || {};
      byId("registration-count-summary").textContent = String(manager.register_count ?? 1);
      byId("registration-threads-summary").textContent = String(manager.register_threads ?? 1);
      byId("registration-mint-summary").textContent = String(manager.mint_workers ?? 1);
      if (config.registrationError) toast(config.registrationError, true);
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function saveManagerConfig(event) {
    const form = event.currentTarget;
    event.preventDefault();
    if (!state.config) await loadConfig();
    const values = { ...(state.config?.manager || {}), ...formValues(form) };
    try {
      const config = await api("/api/config/manager", { method: "POST", body: values });
      state.config = config;
      toast("管理端配置已保存");
      await loadConfig();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function saveRegistrationSection(event) {
    const form = event.currentTarget;
    const label = form.dataset.configLabel || "注册配置";
    event.preventDefault();
    if (!state.config) await loadConfig();
    const values = { ...(state.config?.registration || {}), ...formValues(form) };
    try {
      const config = await api("/api/config/registration", { method: "POST", body: values });
      state.config = config;
      toast(`${label}已保存`);
      await loadConfig();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function saveReferenceJson(event) {
    event.preventDefault();
    let values;
    try {
      values = JSON.parse(byId("reference-json").value);
      if (!values || Array.isArray(values) || typeof values !== "object") throw new Error("根节点必须是 JSON 对象");
    } catch (error) {
      toast(`JSON 无效: ${error.message}`, true);
      return;
    }
    try {
      const config = await api("/api/config/registration", { method: "POST", body: values });
      state.config = config;
      toast("完整注册配置已保存");
      await loadConfig();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderDiagnostics(checks) {
    const markup = (checks || []).map((check) => `
      <div class="diagnostic-item">
        <span class="check-result ${check.ok ? "is-ok" : "is-error"}">${check.ok ? "通过" : "失败"}</span>
        <span>${escapeHtml(check.message)}</span>
      </div>`).join("") || '<p class="muted">检查没有返回结果。</p>';
    byId("diagnostic-results").innerHTML = markup;
    byId("registration-readiness").innerHTML = markup;
  }

  async function runDiagnostics() {
    const task = await startTask("/api/diagnostics", {}, "环境检查已开始");
    if (task) state.selectedTaskId = task.id;
  }

  async function cancelLatestBrowserTask() {
    const task = state.tasks.find((item) => ["register", "login"].includes(item.kind) && ["queued", "running"].includes(item.state));
    if (!task) {
      toast("当前没有可停止的浏览器任务", true);
      return;
    }
    await cancelTask(task.id);
  }

  function bindEvents() {
    all("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
    all("[data-settings-target]").forEach((button) => button.addEventListener("click", () => showSettingsView(button.dataset.settingsTarget)));
    all("[data-open-tasks]").forEach((button) => button.addEventListener("click", () => openTaskDrawer("", button)));
    all("[data-task-filter]").forEach((button) => button.addEventListener("click", () => setTaskFilter(button.dataset.taskFilter)));
    byId("refresh-tasks").addEventListener("click", refreshTaskCenter);
    byId("close-task-drawer").addEventListener("click", closeTaskDrawer);
    byId("drawer-backdrop").addEventListener("click", closeTaskDrawer);
    document.addEventListener("keydown", handleTaskDrawerKeydown);
    byId("mobile-nav-toggle").addEventListener("click", () => document.querySelector(".sidebar").classList.toggle("is-open"));

    byId("import-history-button").addEventListener("click", importHistory);
    byId("empty-import-button").addEventListener("click", importHistory);
    byId("import-file-button").addEventListener("click", () => byId("import-file-input").click());
    byId("import-file-input").addEventListener("change", (event) => importFile(event.target.files[0]));
    byId("refresh-accounts").addEventListener("click", () => loadState({ loading: true }));
    byId("select-all-results").addEventListener("click", selectAllResults);
    byId("clear-selection").addEventListener("click", () => clearSelection());
    byId("export-accounts").addEventListener("click", exportAccounts);
    byId("inspect-selected").addEventListener("click", () => {
      const ids = selectedIds();
      if (!ids.length) toast("请先选择账号", true);
      else inspect(ids);
    });
    byId("inspect-all").addEventListener("click", () => inspect([], true));
    byId("cancel-inspection").addEventListener("click", cancelInspection);
    byId("login-selected").addEventListener("click", () => {
      const ids = selectedIds();
      if (!ids.length) toast("请先选择账号", true);
      else login(ids);
    });
    byId("reset-password-selected").addEventListener("click", () => {
      const ids = selectedIds();
      if (!ids.length) toast("请先选择账号", true);
      else resetPassword(ids);
    });
    byId("login-candidates").addEventListener("click", () => login([], true));
    byId("delete-selected").addEventListener("click", deleteSelected);

    byId("select-all").addEventListener("change", (event) => {
      if (event.target.checked) state.accounts.forEach((account) => state.selected.add(Number(account.id)));
      else state.accounts.forEach((account) => state.selected.delete(Number(account.id)));
      renderAccounts();
    });

    let searchTimer;
    byId("account-search").addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      clearSelection(false);
      searchTimer = window.setTimeout(() => loadState({ page: 1 }), 260);
    });
    byId("status-filter").addEventListener("change", () => {
      clearSelection(false);
      loadState({ page: 1 });
    });
    byId("page-size").addEventListener("change", (event) => {
      state.pagination.pageSize = Number(event.target.value) || 50;
      loadState({ page: 1, loading: true });
    });
    byId("first-page").addEventListener("click", () => loadState({ page: 1, loading: true }));
    byId("previous-page").addEventListener("click", () => loadState({ page: state.pagination.page - 1, loading: true }));
    byId("next-page").addEventListener("click", () => loadState({ page: state.pagination.page + 1, loading: true }));
    byId("last-page").addEventListener("click", () => loadState({ page: state.pagination.totalPages, loading: true }));

    byId("registration-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      await startTask("/api/register", {}, "注册任务已开始");
    });
    byId("open-task-settings").addEventListener("click", () => {
      showView("settings");
      showSettingsView("manager");
    });
    byId("cancel-browser-task").addEventListener("click", cancelLatestBrowserTask);
    byId("registration-diagnostics").addEventListener("click", runDiagnostics);
    byId("run-diagnostics").addEventListener("click", runDiagnostics);
    byId("manager-config-form").addEventListener("submit", saveManagerConfig);
    registrationConfigForms.forEach((formId) => {
      byId(formId).addEventListener("submit", saveRegistrationSection);
    });
    byId("reference-json-form").addEventListener("submit", saveReferenceJson);
    byId("reload-config").addEventListener("click", loadConfig);
  }

  async function bootstrap() {
    bindEvents();
    await Promise.all([loadState({ loading: true }), loadConfig()]);
  }

  bootstrap();
})();
