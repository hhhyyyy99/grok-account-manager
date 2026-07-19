export type ViewName = "accounts" | "registration" | "settings";

export interface Account {
  id: number;
  email: string;
  status: string;
  statusLabel: string;
  detail: string;
  expiresAt: string;
  lastCheckedAt: string;
  lastLoginAt: string;
  hasPassword: boolean;
  hasSso: boolean;
  hasAccessToken: boolean;
  ssoStatus: string;
  ssoStatusLabel: string;
  ssoDetail: string;
  ssoExpiresAt: string;
  cpaStatus: string;
  cpaStatusLabel: string;
  cpaDetail: string;
  missingCpa: boolean;
  source: string;
}

export interface Pagination {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}

export interface Stats {
  total: number;
  active?: number;
  unknown?: number;
  expired?: number;
  invalid?: number;
  needs_login?: number;
  error?: number;
  [key: string]: number | undefined;
}

export interface TaskLog {
  seq: number;
  time: string;
  message: string;
}

export interface Task {
  id: string;
  kind: string;
  label: string;
  state: string;
  createdAt: string;
  startedAt: string;
  finishedAt: string;
  current: number;
  total: number;
  message: string;
  error: string;
  result: Record<string, unknown>;
  cancelRequested: boolean;
  logs?: TaskLog[];
}

export interface StatePayload {
  accounts: Account[];
  pagination: Pagination;
  stats: Stats;
  tasks: Task[];
}

export interface ConfigPayload {
  manager: Record<string, string | number | boolean>;
  registration: Record<string, unknown>;
  registrationSecrets?: Record<string, boolean>;
  registrationError: string;
  runtimePython: string;
  runtimeRoot: string;
}
