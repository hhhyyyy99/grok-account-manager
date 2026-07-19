import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { downloadExport, getConfig, getSelection, getState, getTask, post } from "./api";
import type {
  Account,
  ConfigPayload,
  StatePayload,
  Task,
  TaskFailure,
  TaskLog,
  ViewName,
} from "./types";

const STATUS_OPTIONS = [
  ["", "全部状态"],
  ["unknown", "未巡检"],
  ["missing_cpa", "缺少 CPA 凭据"],
  ["checking", "巡检中"],
  ["active", "正常"],
  ["expired", "已过期"],
  ["invalid", "凭据无效"],
  ["needs_login", "待登录"],
  ["limited", "受限但有效"],
  ["error", "巡检异常"],
] as const;

const NAV_ITEMS: Array<{ name: ViewName; label: string; glyph: string }> = [
  { name: "accounts", label: "账号", glyph: "◎" },
  { name: "registration", label: "注册", glyph: "+" },
  { name: "settings", label: "设置", glyph: "≡" },
];

const TERMINAL_STATES = new Set(["succeeded", "partial", "failed", "cancelled"]);

export function orderAccountsById(accounts: Account[]): Account[] {
  return [...accounts].sort((left, right) => Number(right.id) - Number(left.id));
}

function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}

function taskPercent(task: Task): number {
  if (task.total > 0) {
    return Math.min(100, Math.max(0, Math.round((task.current / task.total) * 100)));
  }
  return TERMINAL_STATES.has(task.state) && task.state !== "cancelled" ? 100 : 0;
}

function formatTime(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date);
}

function sourceLabel(value: string): string {
  const clean = String(value || "").replace(/\\/g, "/");
  return clean.split("/").filter(Boolean).pop() || "本地导入";
}

function statusClass(value: string): string {
  return value.replace(/[^a-z0-9_-]/gi, "unknown");
}

function isRunning(task: Task): boolean {
  return !TERMINAL_STATES.has(task.state);
}

function taskKindLabel(kind: string): string {
  return ({
    import: "导入",
    inspect: "巡检",
    login: "登录",
    "refresh-cpa": "CPA 续期",
    "reset-password": "重置密码",
    register: "注册",
    diagnostics: "环境检查",
  } as Record<string, string>)[kind] ?? "后台任务";
}

function taskStateLabel(state: string): string {
  return ({
    queued: "排队中",
    running: "进行中",
    succeeded: "已完成",
    partial: "部分成功",
    failed: "全部失败",
    cancelled: "已取消",
  } as Record<string, string>)[state] ?? state;
}

function parseJsonObject(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON 顶层必须是对象");
  }
  return parsed as Record<string, unknown>;
}

function jsonValidationError(value: string): string {
  try {
    parseJsonObject(value);
    return "";
  } catch (reason) {
    if (reason instanceof Error && reason.message === "JSON 顶层必须是对象") {
      return reason.message;
    }
    return "JSON 格式不正确，请检查括号、逗号和引号";
  }
}

function useManagerState(search: string, status: string, page: number, pageSize: number) {
  const [state, setState] = useState<StatePayload>({
    accounts: [],
    pagination: { page: 1, pageSize, total: 0, totalPages: 1 },
    stats: { total: 0 },
    tasks: [],
  });
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const requestSequence = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestSequence.current;
    setRefreshing(true);
    try {
      const next = await getState({ search, status, page, pageSize });
      if (requestId === requestSequence.current) {
        setState(next);
        setError("");
      }
    } catch (reason) {
      if (requestId === requestSequence.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (requestId === requestSequence.current) {
        setLoaded(true);
        setRefreshing(false);
      }
    }
  }, [search, status, page, pageSize]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const active = state.tasks.some(isRunning);
    const timer = window.setInterval(() => void refresh(), active ? 1200 : 8000);
    return () => window.clearInterval(timer);
  }, [refresh, state.tasks]);

  return { state, error, loading: !loaded, refreshing, refresh };
}

export function App() {
  const [view, setView] = useState<ViewName>("accounts");
  const [searchInput, setSearchInput] = useState("");
  const search = useDebouncedValue(searchInput, 260);
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [taskDetail, setTaskDetail] = useState<Task | undefined>();
  const [taskDrawerOpen, setTaskDrawerOpen] = useState(false);
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);
  const [activeAction, setActiveAction] = useState("");
  const noticeTimer = useRef<number | null>(null);
  const manager = useManagerState(search, status, page, pageSize);
  const accounts = orderAccountsById(manager.state.accounts);
  const selectedTask = manager.state.tasks.find((task) => task.id === selectedTaskId);
  const runningTasks = manager.state.tasks.filter(isRunning);

  useEffect(() => {
    if (!selectedTaskId) return;
    const load = () => void getTask(selectedTaskId).then(setTaskDetail).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 1200);
    return () => window.clearInterval(timer);
  }, [selectedTaskId]);

  useEffect(() => {
    void getConfig().then(setConfig).catch((reason) => {
      setNotice(reason instanceof Error ? reason.message : String(reason));
      setNoticeError(true);
    });
  }, []);

  useEffect(() => {
    setSelected(new Set());
    setPage(1);
  }, [search, status]);

  useEffect(() => () => {
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
  }, []);

  const flash = useCallback((message: string, error = false) => {
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    setNotice(message);
    setNoticeError(error);
    noticeTimer.current = window.setTimeout(() => setNotice(""), 4200);
  }, []);

  const runAction = useCallback(
    async (endpoint: string, body: Record<string, unknown>, success: string) => {
      setActiveAction(endpoint);
      try {
        const result = await post<{ task?: Task }>(endpoint, body);
        if (result.task) {
          setSelectedTaskId(result.task.id);
          setTaskDrawerOpen(true);
        }
        // Batch account ops are fire-and-forget once submitted; keep selection only
        // until the request succeeds so the next action starts from a clean set.
        if (Array.isArray(body.ids) && body.ids.length > 0) {
          setSelected(new Set());
        }
        flash(success);
        await manager.refresh();
      } catch (reason) {
        flash(reason instanceof Error ? reason.message : String(reason), true);
      } finally {
        setActiveAction("");
      }
    },
    [flash, manager],
  );

  const selectedIds = useMemo(() => [...selected], [selected]);
  const pageSelected = accounts.filter((account) => selected.has(account.id)).length;
  const allPageSelected = accounts.length > 0 && pageSelected === accounts.length;

  const toggleAccount = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectPage = () => {
    setSelected((current) => {
      const next = new Set(current);
      if (allPageSelected) accounts.forEach((account) => next.delete(account.id));
      else accounts.forEach((account) => next.add(account.id));
      return next;
    });
  };

  const selectFiltered = async () => {
    try {
      const result = await getSelection({ search, status });
      setSelected(new Set(result.ids));
      flash(`已选择 ${result.ids.length} 个筛选结果`);
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    }
  };

  const exportAccounts = async (format: string) => {
    try {
      await downloadExport({ format, ids: selectedIds, search, status });
      flash("导出已开始下载");
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    }
  };

  const changePageSize = (value: string) => {
    setPageSize(Math.max(1, Math.min(200, Number(value) || 50)));
    setPage(1);
  };

  const importFile = async (file: File) => {
    try {
      const content = await file.text();
      await runAction("/api/import", { content, filename: file.name }, "导入任务已创建");
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    }
  };

  const openTasks = () => {
    setTaskDrawerOpen(true);
    if (!selectedTaskId) setSelectedTaskId(manager.state.tasks[0]?.id ?? "");
  };

  const currentTask = taskDetail?.id === selectedTaskId ? taskDetail : selectedTask;

  return (
    <div className="react-shell">
      <a className="skip-link" href="#manager-main">跳到主内容</a>
      <header className="react-topbar">
        <div className="topbar-inner">
          <button className="react-brand" type="button" onClick={() => setView("accounts")} aria-label="返回账号工作台">
            <span className="brand-mark" aria-hidden="true">G</span>
            <span><strong>Grok Manager</strong><small>Account control</small></span>
          </button>
          <nav className="react-nav" aria-label="主导航">
            {NAV_ITEMS.map((item) => (
              <button
                className={view === item.name ? "active" : ""}
                key={item.name}
                type="button"
                aria-current={view === item.name ? "page" : undefined}
                onClick={() => setView(item.name)}
              >
                <span aria-hidden="true">{item.glyph}</span>{item.label}
              </button>
            ))}
          </nav>
          <div className="topbar-meta">
            <span className={`sync-state ${manager.refreshing ? "refreshing" : ""}`}>
              <i aria-hidden="true" />{manager.refreshing ? "同步中" : "本地已连接"}
            </span>
            <button className="task-trigger" type="button" onClick={openTasks} aria-label="打开任务中心">
              <span aria-hidden="true">≡</span><span>任务</span>
              {runningTasks.length > 0 && <strong>{runningTasks.length}</strong>}
            </button>
          </div>
        </div>
      </header>

      <main className="react-main" id="manager-main">
        {manager.error && <div className="react-error-banner" role="alert">{manager.error}</div>}
        {view === "accounts" && (
          <AccountsView
            accounts={accounts}
            stats={manager.state.stats}
            pagination={manager.state.pagination}
            search={searchInput}
            status={status}
            pageSize={pageSize}
            selected={selected}
            allPageSelected={allPageSelected}
            loading={manager.loading}
            busy={Boolean(activeAction)}
            onSearch={setSearchInput}
            onStatus={setStatus}
            onPage={setPage}
            onPageSize={changePageSize}
            onSelect={toggleAccount}
            onSelectPage={selectPage}
            onSelectFiltered={selectFiltered}
            onClearSelection={() => setSelected(new Set())}
            onClearFilters={() => { setSearchInput(""); setStatus(""); }}
            onAction={runAction}
            onExport={exportAccounts}
            onImportFile={importFile}
            onImportHistory={() => runAction("/api/import", {}, "历史导入任务已创建")}
          />
        )}
        {view === "registration" && (
          <RegistrationView
            config={config}
            busy={activeAction === "/api/register"}
            onAction={runAction}
            onSaved={setConfig}
            flash={flash}
          />
        )}
        {view === "settings" && (
          <SettingsView
            config={config}
            diagnosticsBusy={activeAction === "/api/diagnostics"}
            onSaved={setConfig}
            flash={flash}
            onDiagnostics={() => runAction("/api/diagnostics", {}, "环境检查任务已创建")}
          />
        )}
      </main>

      <TaskDrawer
        open={taskDrawerOpen}
        tasks={manager.state.tasks}
        selectedTask={currentTask}
        selectedTaskId={selectedTaskId}
        onClose={() => setTaskDrawerOpen(false)}
        onSelect={setSelectedTaskId}
        onCancel={(id) => void runAction(`/api/tasks/${id}/cancel`, {}, "已请求取消任务")}
      />
      {notice && (
        <div className={`react-toast ${noticeError ? "error" : ""}`} role={noticeError ? "alert" : "status"}>
          <span aria-hidden="true">{noticeError ? "!" : "✓"}</span>{notice}
        </div>
      )}
      {runningTasks.length > 0 && (
        <button className="floating-tasks" type="button" onClick={openTasks}>
          <span>{runningTasks.length}</span> 个任务进行中
        </button>
      )}
    </div>
  );
}

interface AccountsViewProps {
  accounts: Account[];
  stats: StatePayload["stats"];
  pagination: StatePayload["pagination"];
  search: string;
  status: string;
  pageSize: number;
  selected: Set<number>;
  allPageSelected: boolean;
  loading: boolean;
  busy: boolean;
  onSearch: (value: string) => void;
  onStatus: (value: string) => void;
  onPage: (value: number) => void;
  onPageSize: (value: string) => void;
  onSelect: (id: number) => void;
  onSelectPage: () => void;
  onSelectFiltered: () => void;
  onClearSelection: () => void;
  onClearFilters: () => void;
  onAction: (endpoint: string, body: Record<string, unknown>, success: string) => Promise<void>;
  onExport: (format: string) => Promise<void>;
  onImportFile: (file: File) => void;
  onImportHistory: () => void;
}

function AccountsView(props: AccountsViewProps) {
  const {
    accounts, stats, pagination, search, status, pageSize, selected, allPageSelected,
    loading, busy, onSearch, onStatus, onPage, onPageSize, onSelect, onSelectPage,
    onSelectFiltered, onClearSelection, onClearFilters, onAction, onExport,
    onImportFile, onImportHistory,
  } = props;
  const [exportFormat, setExportFormat] = useState("cpa");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const ids = [...selected];
  const hasFilters = Boolean(search || status);
  const requireSelection = (endpoint: string, label: string) => {
    if (!ids.length || busy) return;
    void onAction(endpoint, { ids }, label);
  };

  return (
    <section className="react-view view-enter">
      <header className="react-heading">
        <div>
          <span className="section-label">账号工作台</span>
          <h1>账号与巡检</h1>
          <p>筛选账号、检查凭据状态并集中处理后台任务。</p>
        </div>
        <div className="react-heading-actions">
          <label className="button quiet file-button">
            <span aria-hidden="true">↑</span>导入文件
            <input hidden type="file" accept=".txt,text/plain" onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) onImportFile(file);
              event.currentTarget.value = "";
            }} />
          </label>
          <button className="button primary" type="button" disabled={busy} onClick={onImportHistory}>
            <span aria-hidden="true">+</span>导入历史产物
          </button>
        </div>
      </header>

      <div className="react-metrics" aria-label="账号概况">
        <Metric label="全部账号" value={stats.total ?? 0} tone="total" />
        <Metric label="状态正常" value={stats.active ?? 0} tone="success" />
        <Metric label="需要处理" value={(stats.expired ?? 0) + (stats.invalid ?? 0) + (stats.error ?? 0) + (stats.needs_login ?? 0)} tone="danger" />
        <Metric label="尚未巡检" value={stats.unknown ?? 0} tone="neutral" />
      </div>

      <section className="react-panel account-panel">
        <div className="react-toolbar">
          <label className="search-control">
            <span>搜索邮箱</span>
            <span className="input-frame">
              <i aria-hidden="true">⌕</i>
              <input type="search" value={search} placeholder="name@example.com" onChange={(event) => onSearch(event.target.value)} />
              {search && <button type="button" title="清除搜索" aria-label="清除搜索" onClick={() => onSearch("")}>×</button>}
            </span>
          </label>
          <label>
            <span>账号状态</span>
            <select value={status} onChange={(event) => onStatus(event.target.value)}>
              {STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <span className="filter-result"><strong>{pagination.total.toLocaleString("zh-CN")}</strong> 个结果</span>
          <div className="filter-actions">
            <button className="button quiet" type="button" disabled={!accounts.length || loading} onClick={onSelectPage}>
              {allPageSelected ? "取消本页" : "选择本页"}
            </button>
            <button className="button quiet" type="button" disabled={!pagination.total || loading} onClick={onSelectFiltered}>
              全选筛选结果
            </button>
          </div>
        </div>

        <div className={`react-selection-bar ${selected.size ? "active" : ""}`} aria-live="polite">
          <div className="selection-summary"><strong>{selected.size}</strong><span>已选择</span></div>
          <div className="selection-actions">
            <button type="button" disabled={!selected.size || busy} onClick={() => requireSelection("/api/inspect", "巡检任务已创建")}><span aria-hidden="true">↻</span>巡检</button>
            <button type="button" disabled={!selected.size || busy} onClick={() => requireSelection("/api/refresh-cpa", "CPA 续期任务已创建")}><span aria-hidden="true">⟳</span>CPA 续期</button>
            <button type="button" disabled={!selected.size || busy} onClick={() => requireSelection("/api/login", "登录任务已创建")}><span aria-hidden="true">→</span>批量登录</button>
            <button type="button" disabled={!selected.size || busy} onClick={() => requireSelection("/api/reset-password", "改密任务已创建")}><span aria-hidden="true">↺</span>重置密码</button>
            <span className="export-control">
              <select aria-label="导出格式" value={exportFormat} onChange={(event) => setExportFormat(event.target.value)}>
                <option value="cpa">CPA ZIP</option>
                <option value="sub2api">Sub2API JSON</option>
                <option value="grok2api">Grok2API JSON</option>
              </select>
              <button type="button" disabled={!selected.size || busy} onClick={() => void onExport(exportFormat)}><span aria-hidden="true">↓</span>导出</button>
            </span>
            <button className="danger-action" type="button" disabled={!selected.size || busy} onClick={() => setConfirmDelete(true)}><span aria-hidden="true">×</span>删除</button>
          </div>
          <button className="selection-clear" type="button" disabled={!selected.size} title="清空选择" aria-label="清空选择" onClick={onClearSelection}>×</button>
        </div>

        <div className="react-table-wrap">
          <table className="react-table" aria-busy={loading}>
            <thead>
              <tr>
                <th><input type="checkbox" checked={allPageSelected} onChange={onSelectPage} aria-label="选择本页" /></th>
                <th>账号</th><th>总状态</th><th>SSO</th><th>CPA</th><th>最后巡检</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {loading
                ? <TableSkeleton />
                : accounts.map((account) => (
                    <AccountRow account={account} selected={selected.has(account.id)} onSelect={onSelect} onAction={onAction} key={account.id} />
                  ))}
            </tbody>
          </table>
          {!loading && !accounts.length && (
            <div className="react-empty-state">
              <span className="empty-mark" aria-hidden="true">00</span>
              <strong>{hasFilters ? "没有匹配结果" : "账号列表为空"}</strong>
              {hasFilters
                ? <button className="button quiet" type="button" onClick={onClearFilters}>清除筛选</button>
                : <button className="button primary" type="button" onClick={onImportHistory}>导入历史产物</button>}
            </div>
          )}
        </div>

        <footer className="react-pagination">
          <span>显示 {pagination.total ? (pagination.page - 1) * pagination.pageSize + 1 : 0}-{Math.min(pagination.page * pagination.pageSize, pagination.total)}，共 {pagination.total.toLocaleString("zh-CN")}</span>
          <label>每页<select value={pageSize} onChange={(event) => onPageSize(event.target.value)}><option value="25">25</option><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></label>
          <button type="button" title="上一页" aria-label="上一页" disabled={pagination.page <= 1 || loading} onClick={() => onPage(pagination.page - 1)}>←</button>
          <strong>第 {pagination.page} / {pagination.totalPages} 页</strong>
          <button type="button" title="下一页" aria-label="下一页" disabled={pagination.page >= pagination.totalPages || loading} onClick={() => onPage(pagination.page + 1)}>→</button>
        </footer>
      </section>

      {confirmDelete && (
        <ConfirmDialog
          title="删除所选账号"
          description={`将从管理库删除 ${selected.size} 个账号。此操作不会删除外部服务中的账号。`}
          confirmLabel="确认删除"
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => {
            setConfirmDelete(false);
            requireSelection("/api/accounts/delete", "删除任务已完成");
          }}
        />
      )}
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <article className={`react-metric ${tone}`}>
      <span className="metric-marker" aria-hidden="true" />
      <span>{label}</span>
      <strong>{value.toLocaleString("zh-CN")}</strong>
    </article>
  );
}

function TableSkeleton() {
  return (
    <>
      {Array.from({ length: 7 }, (_, index) => (
        <tr className="skeleton-row" aria-hidden="true" key={index}>
          <td><span className="skeleton-check" /></td>
          <td><span className="skeleton-line wide" /><span className="skeleton-line short" /></td>
          <td><span className="skeleton-chip" /></td>
          <td><span className="skeleton-chip" /></td>
          <td><span className="skeleton-chip" /></td>
          <td><span className="skeleton-line medium" /></td>
          <td><span className="skeleton-check" /></td>
        </tr>
      ))}
    </>
  );
}

function AccountRow({ account, selected, onSelect, onAction }: {
  account: Account;
  selected: boolean;
  onSelect: (id: number) => void;
  onAction: AccountsViewProps["onAction"];
}) {
  return (
    <tr className={selected ? "selected-row" : ""}>
      <td><input type="checkbox" checked={selected} onChange={() => onSelect(account.id)} aria-label={`选择 ${account.email}`} /></td>
      <td><strong title={account.email}>{account.email}</strong><small title={account.source}>#{account.id} · {sourceLabel(account.source)}</small></td>
      <td><span className={`react-status ${statusClass(account.status)}`}>{account.statusLabel}</span><small title={account.detail}>{account.detail || "-"}</small></td>
      <td><span className={`react-status ${statusClass(account.ssoStatus)}`}>{account.ssoStatusLabel}</span><small>{account.hasSso ? "已配置" : "缺少 cookie"}</small></td>
      <td><span className={`react-status ${statusClass(account.cpaStatus)}`}>{account.cpaStatusLabel}</span><small>{account.hasAccessToken ? "access token" : "缺少 token"}</small></td>
      <td><small>{formatTime(account.lastCheckedAt)}</small></td>
      <td>
        <button className="icon-action" type="button" title="巡检账号" aria-label={`巡检 ${account.email}`} onClick={() => void onAction("/api/inspect", { ids: [account.id] }, "巡检任务已创建")}>↻</button>
      </td>
    </tr>
  );
}

function ConfirmDialog({ title, description, confirmLabel, onCancel, onConfirm }: {
  title: string;
  description: string;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  return (
    <div className="dialog-layer">
      <button className="dialog-backdrop" type="button" aria-label="取消删除" onClick={onCancel} />
      <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-description">
        <span className="dialog-symbol" aria-hidden="true">!</span>
        <div><h2 id="confirm-title">{title}</h2><p id="confirm-description">{description}</p></div>
        <div className="dialog-actions">
          <button className="button quiet" type="button" autoFocus onClick={onCancel}>取消</button>
          <button className="button danger" type="button" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}

function TaskDrawer({ open, tasks, selectedTask, selectedTaskId, onClose, onSelect, onCancel }: {
  open: boolean;
  tasks: Task[];
  selectedTask?: Task;
  selectedTaskId: string;
  onClose: () => void;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="drawer-layer">
      <button className="drawer-backdrop" type="button" aria-label="关闭任务中心" onClick={onClose} />
      <aside className="react-task-drawer" role="dialog" aria-modal="true" aria-label="任务中心">
        <header>
          <div><span className="section-label">后台操作</span><h2>任务中心</h2></div>
          <button className="icon-action close-action" type="button" onClick={onClose} aria-label="关闭任务中心" title="关闭">×</button>
        </header>
        <div className="react-task-layout">
          <div className="react-task-list">
            {tasks.length ? tasks.map((task) => (
              <button key={task.id} className={selectedTaskId === task.id ? "selected" : ""} type="button" onClick={() => onSelect(task.id)}>
                <span><small>{taskKindLabel(task.kind)}</small><strong>{task.label}</strong><em>{task.message}</em></span>
                <span className={`react-status ${statusClass(task.state)}`}>{taskStateLabel(task.state)}</span>
              </button>
            )) : <div className="drawer-empty"><span aria-hidden="true">≡</span><strong>暂无任务</strong></div>}
          </div>
          <TaskDetail task={selectedTask} onCancel={onCancel} />
        </div>
      </aside>
    </div>
  );
}

function taskFailures(task: Task): TaskFailure[] {
  const raw = task.result?.failures;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const value = item as Record<string, unknown>;
      return {
        id: Number(value.id) || 0,
        email: String(value.email || ""),
        detail: String(value.detail || ""),
      };
    })
    .filter((item): item is TaskFailure => item !== null);
}

function TaskDetail({ task, onCancel }: { task?: Task; onCancel: (id: string) => void }) {
  const logRef = useRef<HTMLDivElement>(null);
  const followLogsRef = useRef(true);
  const previousTaskIdRef = useRef<string>();
  const logs: TaskLog[] = task?.logs ?? [];
  const failures = task ? taskFailures(task) : [];
  const failedCount = Number(task?.result?.failed ?? failures.length) || failures.length;
  const succeededCount = Number(task?.result?.succeeded ?? 0) || 0;
  const failureTruncated = Boolean(task?.result?.failureTruncated);

  useEffect(() => {
    const taskChanged = previousTaskIdRef.current !== task?.id;
    previousTaskIdRef.current = task?.id;
    if (taskChanged) followLogsRef.current = true;
    if (!followLogsRef.current || !logRef.current) return;
    logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [task?.id, logs.length]);

  if (!task) return <section className="react-task-detail drawer-empty"><span aria-hidden="true">↗</span><strong>选择任务查看详情</strong></section>;
  return (
    <section className="react-task-detail">
      <div className="task-detail-top">
        <div><small>{taskKindLabel(task.kind)}</small><h3>{task.label}</h3><p>{task.error || task.message}</p></div>
        <span className={`react-status ${statusClass(task.state)}`}>{taskStateLabel(task.state)}</span>
      </div>
      <div className="task-progress-label"><span>{task.current} / {task.total || "-"}</span><strong>{taskPercent(task)}%</strong></div>
      <progress max="100" value={taskPercent(task)} />
      <div className="task-meta">
        <span>任务编号<strong>{task.id}</strong></span>
        <span>启动时间<strong>{formatTime(task.startedAt || task.createdAt)}</strong></span>
        <span>结束时间<strong>{formatTime(task.finishedAt)}</strong></span>
      </div>
      {TERMINAL_STATES.has(task.state) && (succeededCount > 0 || failedCount > 0) && (
        <div className="task-result-summary">
          <span className="ok">成功 <strong>{succeededCount}</strong></span>
          <span className={failedCount ? "bad" : ""}>失败 <strong>{failedCount}</strong></span>
        </div>
      )}
      {failures.length > 0 && (
        <div className="task-failure-panel">
          <div className="task-failure-title">
            <strong>失败账号</strong>
            <span>{failures.length}{failureTruncated ? "+" : ""} 个</span>
          </div>
          <ul className="task-failure-list">
            {failures.map((item) => (
              <li key={`${item.id}-${item.email}`}>
                <strong title={item.email}>{item.email || `#${item.id}`}</strong>
                <span title={item.detail}>{item.detail || "失败"}</span>
              </li>
            ))}
          </ul>
          {failureTruncated && <p className="task-failure-note">列表已截断，完整原因见下方执行日志。</p>}
        </div>
      )}
      {!TERMINAL_STATES.has(task.state) && <button className="button danger" type="button" onClick={() => onCancel(task.id)}>取消任务</button>}
      <div className="task-log-terminal">
        <div className="task-log-title">
          <span className="terminal-mark" aria-hidden="true">&gt;_</span>
          <strong>执行日志</strong>
          <span>{isRunning(task) ? "实时" : "已结束"} · {logs.length} 行</span>
        </div>
        <div
          className="task-log"
          ref={logRef}
          role="log"
          aria-label="任务执行日志"
          onScroll={(event) => {
            const target = event.currentTarget;
            followLogsRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 32;
          }}
        >
          {logs.length ? logs.map((log) => (
            <div className="task-log-line" key={log.seq}>
              <time>{log.time}</time>
              <span className="log-prompt" aria-hidden="true">›</span>
              <span>{log.message}</span>
            </div>
          )) : <div className="log-empty"><span aria-hidden="true">_</span><span>等待任务输出</span></div>}
        </div>
      </div>
    </section>
  );
}

function RegistrationView({ config, busy, onAction, onSaved, flash }: {
  config: ConfigPayload | null;
  busy: boolean;
  onAction: AccountsViewProps["onAction"];
  onSaved: (next: ConfigPayload) => void;
  flash: (message: string, error?: boolean) => void;
}) {
  const [count, setCount] = useState(1);
  const [threads, setThreads] = useState(1);
  const [mintWorkers, setMintWorkers] = useState(1);
  const [json, setJson] = useState("{}");
  const [jsonError, setJsonError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (config) {
      setCount(Number(config.manager.register_count ?? 1));
      setThreads(Number(config.manager.register_threads ?? 1));
      setMintWorkers(Number(config.manager.mint_workers ?? 1));
      const nextJson = JSON.stringify(config.registration, null, 2);
      setJson(nextJson);
      setJsonError("");
    }
  }, [config]);

  const invalidParameters = !Number.isInteger(count) || count < 1 || count > 1000
    || !Number.isInteger(threads) || threads < 1 || threads > 10
    || !Number.isInteger(mintWorkers) || mintWorkers < 0 || mintWorkers > 10;

  const changeJson = (value: string) => {
    setJson(value);
    setJsonError(jsonValidationError(value));
  };

  const save = async () => {
    const validation = jsonValidationError(json);
    if (validation) {
      setJsonError(validation);
      return;
    }
    setSaving(true);
    try {
      const next = await post<ConfigPayload>("/api/config/registration", parseJsonObject(json));
      onSaved(next);
      flash("注册配置已加密保存");
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="react-view view-enter">
      <header className="react-heading">
        <div><span className="section-label">注册任务</span><h1>批量注册</h1><p>设置本次执行规模并维护注册运行时配置。</p></div>
        <button className="button primary" type="button" disabled={busy || invalidParameters} onClick={() => void onAction("/api/register", { count, threads, mintWorkers }, "注册任务已创建")}>
          <span aria-hidden="true">→</span>{busy ? "正在创建" : "开始注册"}
        </button>
      </header>
      <div className="react-form-grid registration-grid">
        <section className="react-panel parameter-panel">
          <header className="panel-heading"><div><span className="panel-kicker">执行参数</span><h2>本次任务</h2></div><span className="panel-index">01</span></header>
          <div className="parameter-fields">
            <label>注册数量<input type="number" min="1" max="1000" value={count} onChange={(event) => setCount(Number(event.target.value))} /><small>1-1000</small></label>
            <label>注册并发<input type="number" min="1" max="10" value={threads} onChange={(event) => setThreads(Number(event.target.value))} /><small>1-10</small></label>
            <label>Mint 并发<input type="number" min="0" max="10" value={mintWorkers} onChange={(event) => setMintWorkers(Number(event.target.value))} /><small>0-10</small></label>
          </div>
          {invalidParameters && <p className="field-error" role="alert">任务参数超出允许范围</p>}
          <dl className="parameter-summary"><div><dt>账号</dt><dd>{count}</dd></div><div><dt>注册线程</dt><dd>{threads}</dd></div><div><dt>Mint 线程</dt><dd>{mintWorkers}</dd></div></dl>
        </section>
        <section className="react-panel editor-panel">
          <header className="panel-heading"><div><span className="panel-kicker">敏感字段自动加密</span><h2>注册配置</h2></div><span className="code-badge">JSON</span></header>
          {config?.registrationError && <p className="field-error" role="alert">{config.registrationError}</p>}
          <textarea className="config-editor" value={json} onChange={(event) => changeJson(event.target.value)} spellCheck={false} aria-invalid={Boolean(jsonError)} disabled={!config} />
          <div className="editor-footer"><span className={jsonError ? "invalid" : "valid"}>{jsonError || "JSON 格式有效"}</span><button className="button primary" type="button" disabled={!config || saving || Boolean(jsonError)} onClick={() => void save()}>{saving ? "保存中" : "保存并加密"}</button></div>
        </section>
      </div>
    </section>
  );
}

type SettingsTab = "manager" | "registration" | "email" | "cpa" | "sub2api" | "grok2api" | "json" | "environment";

const SETTINGS_TABS: Array<{ name: SettingsTab; label: string }> = [
  { name: "manager", label: "任务参数" },
  { name: "registration", label: "注册基础" },
  { name: "email", label: "临时邮箱" },
  { name: "cpa", label: "CPA" },
  { name: "sub2api", label: "Sub2API" },
  { name: "grok2api", label: "Grok2API" },
  { name: "json", label: "完整 JSON" },
  { name: "environment", label: "环境检查" },
];

type ConfigDraft = Record<string, unknown>;
type ConfigFieldType = "text" | "url" | "password" | "number";

function draftValue(values: ConfigDraft, name: string): string {
  const value = values[name];
  return value === undefined || value === null ? "" : String(value);
}

function ConfigField({ values, name, label, onChange, type = "text", secret = false, secrets, placeholder, help, min, max, step, wide }: {
  values: ConfigDraft;
  name: string;
  label: string;
  onChange: (name: string, value: unknown) => void;
  type?: ConfigFieldType;
  secret?: boolean;
  secrets?: Record<string, boolean>;
  placeholder?: string;
  help?: string;
  min?: number;
  max?: number;
  step?: number;
  wide?: boolean;
}) {
  const configured = Boolean(secrets?.[name]);
  const inputType = secret ? "password" : type;
  return (
    <label className={`config-field ${wide ? "field-wide" : ""}`}>
      <span>{label}{secret && configured && <em>已配置</em>}</span>
      <input
        type={inputType}
        value={draftValue(values, name)}
        min={min}
        max={max}
        step={step}
        placeholder={secret && configured ? "已配置，留空保持" : placeholder}
        autoComplete={secret ? "new-password" : undefined}
        onChange={(event) => onChange(name, type === "number" ? (event.target.value === "" ? "" : Number(event.target.value)) : event.target.value)}
      />
      {secret && configured && <button className="secret-clear" type="button" onClick={() => onChange(name, null)}>清除已配置</button>}
      {help && <small>{help}</small>}
    </label>
  );
}

function ConfigSelect({ values, name, label, options, onChange, help }: {
  values: ConfigDraft;
  name: string;
  label: string;
  options: Array<[string, string]>;
  onChange: (name: string, value: unknown) => void;
  help?: string;
}) {
  return (
    <label className="config-field">
      <span>{label}</span>
      <select value={draftValue(values, name)} onChange={(event) => onChange(name, event.target.value)}>
        {options.map(([value, optionLabel]) => <option value={value} key={value}>{optionLabel}</option>)}
      </select>
      {help && <small>{help}</small>}
    </label>
  );
}

function ConfigToggle({ values, name, label, onChange }: {
  values: ConfigDraft;
  name: string;
  label: string;
  onChange: (name: string, value: unknown) => void;
}) {
  return (
    <label className="config-toggle">
      <input type="checkbox" checked={Boolean(values[name])} onChange={(event) => onChange(name, event.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

function ConfigGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="config-group"><h3>{title}</h3><div className="config-fields">{children}</div></section>;
}

function ConfigPanel({ title, kicker, index, children, footer }: { title: string; kicker: string; index: string; children: React.ReactNode; footer: React.ReactNode }) {
  return (
    <section className="react-panel full-config-panel">
      <header className="panel-heading"><div><span className="panel-kicker">{kicker}</span><h2>{title}</h2></div><span className="panel-index">{index}</span></header>
      {children}
      <div className="config-form-footer">{footer}</div>
    </section>
  );
}

function SettingsView({ config, diagnosticsBusy, onSaved, flash, onDiagnostics }: {
  config: ConfigPayload | null;
  diagnosticsBusy: boolean;
  onSaved: (next: ConfigPayload) => void;
  flash: (message: string, error?: boolean) => void;
  onDiagnostics: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("manager");
  const [managerDraft, setManagerDraft] = useState<ConfigDraft>({});
  const [registrationDraft, setRegistrationDraft] = useState<ConfigDraft>({});
  const [registrationSecrets, setRegistrationSecrets] = useState<Record<string, boolean>>({});
  const [registrationJson, setRegistrationJson] = useState("{}");
  const [jsonError, setJsonError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!config) return;
    setManagerDraft({ ...config.manager });
    setRegistrationDraft({ ...config.registration });
    setRegistrationSecrets({ ...(config.registrationSecrets ?? {}) });
    setRegistrationJson(JSON.stringify(config.registration, null, 2));
    setJsonError("");
  }, [config]);

  const setManagerField = (name: string, value: unknown) => setManagerDraft((current) => ({ ...current, [name]: value }));
  const setRegistrationField = (name: string, value: unknown) => {
    setRegistrationDraft((current) => ({ ...current, [name]: value }));
    if (registrationSecrets[name] && value === "") return;
    setRegistrationSecrets((current) => ({ ...current, [name]: Boolean(value) }));
  };

  const saveManager = async () => {
    setSaving(true);
    try {
      const next = await post<ConfigPayload>("/api/config/manager", managerDraft);
      onSaved(next);
      flash("任务参数已保存");
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    } finally {
      setSaving(false);
    }
  };

  const saveRegistration = async (message = "注册配置已加密保存") => {
    setSaving(true);
    try {
      const next = await post<ConfigPayload>("/api/config/registration", registrationDraft);
      onSaved(next);
      flash(message);
    } catch (reason) {
      flash(reason instanceof Error ? reason.message : String(reason), true);
    } finally {
      setSaving(false);
    }
  };

  const changeJson = (value: string) => {
    setRegistrationJson(value);
    const error = jsonValidationError(value);
    setJsonError(error);
    if (!error) setRegistrationDraft(parseJsonObject(value));
  };

  const saveJson = async () => {
    const error = jsonValidationError(registrationJson);
    if (error) {
      setJsonError(error);
      return;
    }
    await saveRegistration("完整注册配置已加密保存");
  };

  const tabButton = (item: { name: SettingsTab; label: string }) => (
    <button
      className={tab === item.name ? "active" : ""}
      type="button"
      role="tab"
      aria-selected={tab === item.name}
      onClick={() => setTab(item.name)}
      key={item.name}
    >{item.label}</button>
  );

  return (
    <section className="react-view view-enter">
      <header className="react-heading settings-heading">
        <div><span className="section-label">本地运行时</span><h1>配置与环境</h1><p>完整保留注册、重新登录、临时邮箱、CPA、Sub2API 与 Grok2API 配置。</p></div>
        <button className="button quiet" type="button" disabled={diagnosticsBusy} onClick={onDiagnostics}><span aria-hidden="true">↻</span>{diagnosticsBusy ? "检查中" : "环境检查"}</button>
      </header>
      <nav className="settings-tabs" role="tablist" aria-label="配置分类">
        {SETTINGS_TABS.map(tabButton)}
      </nav>
      <div className="settings-content">
        {tab === "manager" && <ManagerConfigPanel values={managerDraft} onChange={setManagerField} saving={saving} onSave={saveManager} />}
        {tab === "registration" && <RegistrationConfigPanel values={registrationDraft} secrets={registrationSecrets} onChange={setRegistrationField} saving={saving} onSave={() => saveRegistration()} />}
        {tab === "email" && <EmailConfigPanel values={registrationDraft} secrets={registrationSecrets} onChange={setRegistrationField} saving={saving} onSave={() => saveRegistration("临时邮箱配置已加密保存")} />}
        {tab === "cpa" && <CpaConfigPanel values={registrationDraft} secrets={registrationSecrets} onChange={setRegistrationField} saving={saving} onSave={() => saveRegistration("CPA 配置已加密保存")} />}
        {tab === "sub2api" && <Sub2ApiConfigPanel values={registrationDraft} onChange={setRegistrationField} saving={saving} onSave={() => saveRegistration("Sub2API 配置已保存")} />}
        {tab === "grok2api" && <Grok2ApiConfigPanel values={registrationDraft} secrets={registrationSecrets} onChange={setRegistrationField} saving={saving} onSave={() => saveRegistration("Grok2API 配置已加密保存")} />}
        {tab === "json" && <ConfigPanel title="注册完整 JSON" kicker="高级编辑" index="07" footer={<><span className={jsonError ? "invalid" : "valid"}>{jsonError || "敏感字段留空表示保持原值"}</span><button className="button primary" type="button" disabled={!config || saving || Boolean(jsonError)} onClick={() => void saveJson()}>{saving ? "保存中" : "保存完整 JSON"}</button></>}><textarea className="config-editor full-json-editor" value={registrationJson} onChange={(event) => changeJson(event.target.value)} spellCheck={false} aria-invalid={Boolean(jsonError)} disabled={!config} /></ConfigPanel>}
        {tab === "environment" && <EnvironmentPanel config={config} diagnosticsBusy={diagnosticsBusy} onDiagnostics={onDiagnostics} />}
      </div>
    </section>
  );
}

function ManagerConfigPanel({ values, onChange, saving, onSave }: { values: ConfigDraft; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="任务参数" kicker="管理端设置" index="01" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存任务参数"}</button>}>
    <ConfigGroup title="批量注册"><div className="config-grid three"><ConfigField values={values} name="register_count" label="每批注册数量" type="number" min={1} max={10000} onChange={onChange} /><ConfigField values={values} name="register_threads" label="注册并发" type="number" min={1} max={10} onChange={onChange} /><ConfigField values={values} name="mint_workers" label="CPA Mint 并发" type="number" min={0} max={10} onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="重新登录"><div className="config-grid two"><ConfigField values={values} name="login_workers" label="重新登录并发" type="number" min={1} max={10} onChange={onChange} /><ConfigField values={values} name="login_timeout_seconds" label="单账号超时（秒）" type="number" min={60} max={1800} onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="巡检与导入"><div className="config-grid two"><ConfigField values={values} name="probe_timeout_seconds" label="巡检请求超时（秒）" type="number" min={3} max={120} onChange={onChange} /><ConfigField values={values} name="inspection_workers" label="巡检并发" type="number" min={1} max={32} onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="live_probe" label="巡检时执行在线探测" onChange={onChange} /><ConfigToggle values={values} name="auto_import_on_start" label="启动时自动导入" onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="CPA 守护"><div className="config-grid two"><ConfigField values={values} name="cpa_guard_interval_seconds" label="守护轮询间隔（秒）" type="number" min={30} max={86400} onChange={onChange} /><ConfigField values={values} name="cpa_guard_lead_seconds" label="提前续期窗口（秒）" type="number" min={60} max={21600} onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="cpa_guard_enabled" label="启动管理端时自动运行 CPA 守护" onChange={onChange} /></div><p className="config-hint">默认随 `run.py` / `ui` 后台启动。也可单独：`uv run --locked python run.py cpa-guard`。只处理 CPA 正常账号；refresh 失效会标记 CPA 过期，不会自动浏览器登录。</p></ConfigGroup>
  </ConfigPanel>;
}

function RegistrationConfigPanel({ values, secrets, onChange, saving, onSave }: { values: ConfigDraft; secrets: Record<string, boolean>; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="注册基础" kicker="注册运行时" index="02" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存注册基础"}</button>}>
    <ConfigGroup title="网络与浏览器"><div className="config-grid two"><ConfigField values={values} secrets={secrets} name="proxy" label="注册代理" secret onChange={onChange} placeholder="http://user:pass@host:port" /><ConfigField values={values} name="thread_start_interval" label="线程启动间隔（秒）" type="number" min={0} step={0.1} onChange={onChange} /><ConfigField values={values} name="user_agent" label="浏览器 User-Agent" wide onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="enable_nsfw" label="注册后启用 NSFW" onChange={onChange} /></div></ConfigGroup>
  </ConfigPanel>;
}

function EmailConfigPanel({ values, secrets, onChange, saving, onSave }: { values: ConfigDraft; secrets: Record<string, boolean>; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="临时邮箱" kicker="邮件服务" index="03" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存临时邮箱配置"}</button>}>
    <ConfigGroup title="通用设置"><div className="config-grid three"><ConfigSelect values={values} name="email_provider" label="邮箱服务" options={[["cloudflare", "Cloudflare"], ["duckmail", "DuckMail"], ["yyds", "YYDS"]]} onChange={onChange} /><ConfigField values={values} name="email_prefix" label="邮箱前缀" onChange={onChange} /><ConfigField values={values} name="defaultDomains" label="收信域名（逗号分隔）" onChange={onChange} /><ConfigField values={values} name="max_mail_retry" label="邮箱创建重试次数" type="number" min={1} max={20} onChange={onChange} /><ConfigField values={values} name="code_poll_timeout" label="验证码等待（秒）" type="number" min={15} max={300} onChange={onChange} /><ConfigField values={values} name="code_poll_interval" label="验证码轮询间隔（秒）" type="number" min={1} max={30} onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="Cloudflare"><div className="config-grid two"><ConfigField values={values} name="cloudflare_api_base" label="API 地址" type="url" wide onChange={onChange} /><ConfigField values={values} secrets={secrets} name="cloudflare_api_key" label="API 密钥" secret onChange={onChange} /><ConfigSelect values={values} name="cloudflare_auth_mode" label="认证模式" options={[["none", "none"], ["query-key", "query-key"], ["bearer", "bearer"], ["x-api-key", "x-api-key"], ["x-admin-auth", "x-admin-auth"]]} onChange={onChange} /><ConfigField values={values} name="cloudflare_path_domains" label="域名路径" onChange={onChange} /><ConfigField values={values} name="cloudflare_path_accounts" label="创建邮箱路径" onChange={onChange} /><ConfigField values={values} name="cloudflare_path_token" label="Token 路径" onChange={onChange} /><ConfigField values={values} name="cloudflare_path_messages" label="邮件路径" onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="DuckMail 与 YYDS"><div className="config-grid two"><ConfigField values={values} secrets={secrets} name="duckmail_api_key" label="DuckMail API Key" secret onChange={onChange} /><ConfigField values={values} secrets={secrets} name="yyds_api_key" label="YYDS API Key" secret onChange={onChange} /><ConfigField values={values} secrets={secrets} name="yyds_jwt" label="YYDS JWT" secret wide onChange={onChange} /><ConfigField values={values} name="yyds_preferred_domains" label="YYDS 优先域名（逗号分隔）" onChange={onChange} /><ConfigField values={values} name="yyds_blocked_domains" label="YYDS 排除域名（逗号分隔）" onChange={onChange} /><ConfigSelect values={values} name="yyds_domain_selection" label="YYDS 域名选择" options={[["random", "随机"], ["first", "按顺序"]]} onChange={onChange} /></div></ConfigGroup>
  </ConfigPanel>;
}

function CpaConfigPanel({ values, secrets, onChange, saving, onSave }: { values: ConfigDraft; secrets: Record<string, boolean>; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="CPA" kicker="Token 生成与同步" index="04" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存 CPA 配置"}</button>}>
    <ConfigGroup title="生成与输出"><div className="config-grid two"><ConfigField values={values} name="cpa_base_url" label="CPA Base URL" type="url" wide onChange={onChange} /><ConfigField values={values} secrets={secrets} name="cpa_proxy" label="CPA 代理" secret onChange={onChange} /><ConfigField values={values} name="cpa_mint_timeout_sec" label="Mint 超时（秒）" type="number" min={60} onChange={onChange} /><ConfigField values={values} name="cpa_auth_dir" label="认证文件目录" onChange={onChange} /><ConfigField values={values} name="cpa_hotload_dir" label="热加载目录" onChange={onChange} /><ConfigField values={values} name="cpa_mint_browser_recycle_every" label="浏览器回收间隔" type="number" min={1} onChange={onChange} /><ConfigField values={values} name="api_reverse_tools" label="反向工具配置" wide onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="cpa_export_enabled" label="注册后生成 CPA token" onChange={onChange} /><ConfigToggle values={values} name="cpa_copy_to_hotload" label="复制到热加载目录" onChange={onChange} /><ConfigToggle values={values} name="cpa_headless" label="Mint 浏览器无头" onChange={onChange} /><ConfigToggle values={values} name="cpa_force_standalone" label="使用独立浏览器" onChange={onChange} /><ConfigToggle values={values} name="cpa_mint_required" label="CPA 失败时判定注册失败" onChange={onChange} /><ConfigToggle values={values} name="cpa_probe_after_write" label="生成后在线探测" onChange={onChange} /><ConfigToggle values={values} name="cpa_probe_chat" label="探测聊天接口" onChange={onChange} /><ConfigToggle values={values} name="cpa_mint_cookie_inject" label="注入已有 Cookie" onChange={onChange} /><ConfigToggle values={values} name="cpa_mint_browser_reuse" label="复用 Mint 浏览器" onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="云端同步"><div className="config-grid two"><ConfigField values={values} name="cpa_cloud_api_base" label="云端 API 地址" type="url" wide onChange={onChange} /><ConfigField values={values} secrets={secrets} name="cpa_cloud_management_key" label="管理密钥" secret wide onChange={onChange} /><ConfigField values={values} name="cpa_cloud_upload_timeout" label="上传超时（秒）" type="number" min={1} onChange={onChange} /><ConfigField values={values} name="cpa_cloud_upload_retries" label="上传重试次数" type="number" min={0} onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="cpa_cloud_upload_enabled" label="生成后同步到云端" onChange={onChange} /></div></ConfigGroup>
  </ConfigPanel>;
}

function Sub2ApiConfigPanel({ values, onChange, saving, onSave }: { values: ConfigDraft; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="Sub2API" kicker="导出与合并" index="05" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存 Sub2API 配置"}</button>}><ConfigGroup title="导出文件"><div className="config-grid two"><ConfigField values={values} name="sub2api_export_dir" label="单账号导出目录" onChange={onChange} /><ConfigField values={values} name="sub2api_combined_file" label="合并账号文件" onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="sub2api_export_enabled" label="CPA 生成后自动导出" onChange={onChange} /></div></ConfigGroup></ConfigPanel>;
}

function Grok2ApiConfigPanel({ values, secrets, onChange, saving, onSave }: { values: ConfigDraft; secrets: Record<string, boolean>; onChange: (name: string, value: unknown) => void; saving: boolean; onSave: () => Promise<void> }) {
  return <ConfigPanel title="Grok2API" kicker="SSO Token 同步" index="06" footer={<button className="button primary" type="button" disabled={saving} onClick={() => void onSave()}>{saving ? "保存中" : "保存 Grok2API 配置"}</button>}>
    <ConfigGroup title="Token 池"><div className="config-grid two"><ConfigSelect values={values} name="grok2api_pool_name" label="池名称" options={[["ssoBasic", "ssoBasic"], ["ssoSuper", "ssoSuper"]]} onChange={onChange} /><ConfigField values={values} name="grok2api_local_token_file" label="本地 Token 文件" onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="grok2api_auto_add_local" label="注册/重新登录后更新本地池" onChange={onChange} /></div></ConfigGroup>
    <ConfigGroup title="远端服务"><div className="config-grid two"><ConfigField values={values} name="grok2api_remote_base" label="远端服务地址" type="url" wide onChange={onChange} /><ConfigField values={values} secrets={secrets} name="grok2api_remote_app_key" label="远端 App Key" secret wide onChange={onChange} /></div><div className="toggle-grid"><ConfigToggle values={values} name="grok2api_auto_add_remote" label="注册/重新登录后更新远端池" onChange={onChange} /></div></ConfigGroup>
  </ConfigPanel>;
}

function EnvironmentPanel({ config, diagnosticsBusy, onDiagnostics }: { config: ConfigPayload | null; diagnosticsBusy: boolean; onDiagnostics: () => void }) {
  return <div className="environment-grid"><aside className="react-panel runtime-panel"><header className="panel-heading"><div><span className="panel-kicker">当前进程</span><h2>运行时</h2></div><span className="runtime-dot" aria-hidden="true" /></header><dl><div><dt>保险库</dt><dd><span className="inline-state">已解锁</span></dd></div><div><dt>Python</dt><dd title={config?.runtimePython}>{config?.runtimePython || "-"}</dd></div><div><dt>项目根目录</dt><dd title={config?.runtimeRoot}>{config?.runtimeRoot || "-"}</dd></div></dl></aside><section className="react-panel readiness-panel"><header className="panel-heading"><div><span className="panel-kicker">运行前检查</span><h2>环境检查</h2></div><span className="panel-index">08</span></header><p className="readiness-copy">检查内置注册运行时、Python、浏览器组件和临时邮箱基础配置。</p><button className="button primary" type="button" disabled={diagnosticsBusy} onClick={onDiagnostics}><span aria-hidden="true">↻</span>{diagnosticsBusy ? "检查中" : "运行环境检查"}</button></section></div>;
}
