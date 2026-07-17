(function () {
  "use strict";

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
      "error", "checking", "logging_in", "missing_cpa", "queued", "running", "succeeded",
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

  function updateSelectionBar() {
    const count = state.selected.size;
    byId("selected-count").textContent = String(count);
    byId("selection-bar").hidden = count === 0;
    const selectAll = byId("select-all");
    selectAll.checked = state.accounts.length > 0 && count === state.accounts.length;
    selectAll.indeterminate = count > 0 && count < state.accounts.length;
    const exportButton = byId("export-accounts");
    exportButton.textContent = count ? `导出所选 (${count})` : "导出筛选结果";
    exportButton.disabled = count === 0 && Number(state.pagination.total || 0) === 0;
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
      state.accounts = payload.accounts || [];
      state.stats = payload.stats || {};
      state.tasks = payload.tasks || [];
      state.pagination = payload.pagination || state.pagination;
      const visibleIds = new Set(state.accounts.map((account) => Number(account.id)));
      state.selected = new Set(Array.from(state.selected).filter((id) => visibleIds.has(id)));
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

  function renderTaskList() {
    const list = byId("task-list");
    const running = state.tasks.filter((task) => !["succeeded", "failed", "cancelled"].includes(task.state));
    const inspection = running.find((task) => task.kind === "inspect");
    byId("running-task-count").textContent = String(running.length);
    byId("inspect-selected").disabled = Boolean(inspection);
    byId("inspect-all").disabled = Boolean(inspection);
    byId("cancel-inspection").disabled = !inspection || Boolean(inspection.cancelRequested);
    if (!state.tasks.length) {
      list.innerHTML = '<div class="empty-task"><strong>还没有任务</strong><p>导入、巡检、登录和注册任务会显示在这里。</p></div>';
      return;
    }
    list.innerHTML = state.tasks.map((task) => {
      const status = safeStatus(task.state);
      return `
        <button class="task-list-item${state.selectedTaskId === task.id ? " is-active" : ""}" type="button" data-task-id="${escapeHtml(task.id)}">
          <span><strong>${escapeHtml(task.label)}</strong><small>${escapeHtml(task.message || formatTime(task.createdAt))}</small></span>
          <span class="task-state task-${status}">${escapeHtml(taskLabelState(task))}</span>
        </button>`;
    }).join("");
    all("[data-task-id]", list).forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.taskId)));
  }

  function renderTaskDetail(task) {
    const detail = byId("task-detail");
    const stateName = safeStatus(task.state);
    const percent = task.total > 0 ? Math.min(100, Math.round((task.current / task.total) * 100)) : (task.state === "succeeded" ? 100 : 0);
    const canCancel = ["queued", "running"].includes(task.state) && ["register", "login", "inspect"].includes(task.kind) && !task.cancelRequested;
    const logs = (task.logs || []).map((line) => `
      <div class="task-log-row"><time>${escapeHtml(line.time)}</time><span>${escapeHtml(line.message)}</span></div>`).join("");
    detail.innerHTML = `
      <div class="task-detail-heading">
        <div><h3>${escapeHtml(task.label)}</h3><p>${escapeHtml(task.message || "")}</p></div>
        <span class="task-state task-${stateName}">${escapeHtml(taskLabelState(task))}</span>
      </div>
      <div class="task-progress">
        <progress class="task-progress-bar" max="100" value="${percent}">${percent}%</progress>
        <small>${task.total > 0 ? `${Number(task.current)} / ${Number(task.total)}` : formatTime(task.startedAt || task.createdAt)}</small>
      </div>
      ${canCancel ? '<button class="button button-danger" type="button" id="cancel-selected-task">取消任务</button>' : ""}
      <div class="task-log" id="active-task-log">${logs || '<span class="muted">任务还没有产生日志。</span>'}</div>`;
    const logBox = byId("active-task-log");
    if (logBox) logBox.scrollTop = logBox.scrollHeight;
    if (canCancel) byId("cancel-selected-task").addEventListener("click", () => cancelTask(task.id));
    if (task.kind === "diagnostics" && task.result && task.result.checks) {
      renderDiagnostics(task.result.checks);
    }
  }

  async function selectTask(taskId) {
    state.selectedTaskId = taskId;
    renderTaskList();
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      renderTaskDetail(task);
    } catch (error) {
      toast(error.message, true);
    }
  }

  function openTaskDrawer(taskId = "") {
    byId("task-drawer").classList.add("is-open");
    byId("task-drawer").setAttribute("aria-hidden", "false");
    byId("drawer-backdrop").hidden = false;
    if (taskId) selectTask(taskId);
    else if (state.selectedTaskId) selectTask(state.selectedTaskId);
  }

  function closeTaskDrawer() {
    byId("task-drawer").classList.remove("is-open");
    byId("task-drawer").setAttribute("aria-hidden", "true");
    byId("drawer-backdrop").hidden = true;
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
    const body = { format, ids };
    if (!ids.length) {
      body.search = byId("account-search").value.trim();
      body.status = byId("status-filter").value;
    }
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
    event.preventDefault();
    if (!state.config) await loadConfig();
    const values = { ...(state.config?.manager || {}), ...formValues(event.currentTarget) };
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
    event.preventDefault();
    if (!state.config) await loadConfig();
    const values = { ...(state.config?.registration || {}), ...formValues(event.currentTarget) };
    try {
      const config = await api("/api/config/registration", { method: "POST", body: values });
      state.config = config;
      toast(`${event.currentTarget.dataset.configLabel || "注册配置"}已保存`);
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
    all("[data-open-tasks]").forEach((button) => button.addEventListener("click", () => openTaskDrawer()));
    byId("close-task-drawer").addEventListener("click", closeTaskDrawer);
    byId("drawer-backdrop").addEventListener("click", closeTaskDrawer);
    byId("mobile-nav-toggle").addEventListener("click", () => document.querySelector(".sidebar").classList.toggle("is-open"));

    byId("import-history-button").addEventListener("click", importHistory);
    byId("empty-import-button").addEventListener("click", importHistory);
    byId("import-file-button").addEventListener("click", () => byId("import-file-input").click());
    byId("import-file-input").addEventListener("change", (event) => importFile(event.target.files[0]));
    byId("refresh-accounts").addEventListener("click", () => loadState({ loading: true }));
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
    byId("login-candidates").addEventListener("click", () => login([], true));
    byId("delete-selected").addEventListener("click", deleteSelected);

    byId("select-all").addEventListener("change", (event) => {
      if (event.target.checked) state.accounts.forEach((account) => state.selected.add(Number(account.id)));
      else state.selected.clear();
      renderAccounts();
    });

    let searchTimer;
    byId("account-search").addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => loadState({ page: 1 }), 260);
    });
    byId("status-filter").addEventListener("change", () => loadState({ page: 1 }));
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
