import type {
  ActionResponse,
  Dashboard,
  Job,
  JobStatus,
  LogoutResponse,
  PreviewResponse,
  PushConfig,
  PushSubscriptionPayload,
  PushSubscriptionResponse,
  Session,
  UserSettings,
  WatchInput
} from "./types";

let csrfToken = "";

export function setCsrfToken(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);

  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin"
  });

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;

  if (!response.ok) {
    const detail = payload?.detail || payload?.message || "Request failed.";
    throw new ApiError(detail, response.status);
  }

  return payload as T;
}

export const api = {
  session: () => apiFetch<Session>("/api/session"),
  login: (email: string, password: string) =>
    apiFetch<Session>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password })
    }),
  register: (email: string, password: string) =>
    apiFetch<Session>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password })
    }),
  logout: () => apiFetch<LogoutResponse>("/api/auth/logout", { method: "POST" }),
  pushConfig: () => apiFetch<PushConfig>("/api/push/config"),
  savePushSubscription: (subscription: PushSubscriptionPayload) =>
    apiFetch<PushSubscriptionResponse>("/api/push/subscriptions", {
      method: "POST",
      body: JSON.stringify(subscription)
    }),
  deletePushSubscription: (endpoint: string) =>
    apiFetch<PushSubscriptionResponse>("/api/push/subscriptions", {
      method: "DELETE",
      body: JSON.stringify({ endpoint })
    }),
  settings: () => apiFetch<UserSettings>("/api/settings"),
  updateSettings: (settings: UserSettings) =>
    apiFetch<UserSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settings)
    }),
  applyNotificationDefaults: () =>
    apiFetch<ActionResponse>("/api/settings/notifications/apply-defaults", { method: "POST" }),
  testNotification: () =>
    apiFetch<ActionResponse>("/api/settings/test-notification", { method: "POST" }),
  exportData: () => apiFetch<Record<string, unknown>>("/api/data/export"),
  restoreData: (data: Record<string, unknown>) =>
    apiFetch<ActionResponse>("/api/data/restore", {
      method: "POST",
      body: JSON.stringify(data)
    }),
  dashboard: () => apiFetch<Dashboard>("/api/dashboard"),
  jobs: () => apiFetch<Job[]>("/api/jobs"),
  watchJobs: (watchId: number) => apiFetch<Job[]>(`/api/watches/${watchId}/jobs`),
  updateJobMeta: (jobId: number, status: JobStatus, notes: string) =>
    apiFetch<Job>(`/api/jobs/${jobId}`, {
      method: "PATCH",
      body: JSON.stringify({ status, notes })
    }),
  preview: (input: WatchInput) =>
    apiFetch<PreviewResponse>("/api/preview", {
      method: "POST",
      body: JSON.stringify(input)
    }),
  createWatch: (input: WatchInput) =>
    apiFetch<ActionResponse>("/api/watches", {
      method: "POST",
      body: JSON.stringify(input)
    }),
  updateWatch: (watchId: number, input: WatchInput) =>
    apiFetch<ActionResponse>(`/api/watches/${watchId}`, {
      method: "PUT",
      body: JSON.stringify(input)
    }),
  deleteWatch: (watchId: number) =>
    apiFetch<ActionResponse>(`/api/watches/${watchId}`, { method: "DELETE" }),
  updateNotifications: (watchId: number, emailEnabled: boolean, pushEnabled: boolean) =>
    apiFetch<ActionResponse>(`/api/watches/${watchId}/notifications`, {
      method: "PATCH",
      body: JSON.stringify({ email_enabled: emailEnabled, push_enabled: pushEnabled })
    }),
  checkWatch: (watchId: number) =>
    apiFetch<ActionResponse>(`/api/watches/${watchId}/check`, { method: "POST" }),
  checkAll: () => apiFetch<ActionResponse>("/api/check-now", { method: "POST" }),
  reorder: (watchIds: number[]) =>
    apiFetch<ActionResponse>("/api/watches/reorder", {
      method: "POST",
      body: JSON.stringify({ watch_ids: watchIds })
    })
};
