export type Category = "success" | "info" | "error";

export interface User {
  id: number;
  email: string;
}

export interface Session {
  authenticated: boolean;
  csrf_token: string;
  user: User | null;
  check_interval: number;
  authentik_enabled: boolean;
  authentik_login_url: string | null;
  authentik_login_button_text: string;
  password_login_enabled: boolean;
}

export interface LogoutResponse {
  ok: boolean;
  logout_url?: string | null;
}

export interface Job {
  id?: number | null;
  watch_id?: number | null;
  job_id: string;
  title: string;
  location: string;
  url: string;
  found_at?: string | null;
  notified_at?: string | null;
  company_name?: string | null;
  keywords?: string | null;
}

export interface Diagnostic {
  title: string;
  detail: string;
}

export interface Watch {
  id: number;
  company_name: string;
  careers_url: string;
  keywords: string;
  email_enabled: boolean;
  push_enabled: boolean;
  created_at: string | null;
  last_checked: string | null;
  last_success_at: string | null;
  last_error: string | null;
  diagnostic: Diagnostic | null;
  job_count: number;
  jobs: Job[];
}

export interface Stats {
  alerts: number;
  jobs: number;
  interval: number;
}

export interface Dashboard {
  watches: Watch[];
  stats: Stats;
}

export interface WatchInput {
  company_name: string;
  careers_url: string;
  keywords: string;
}

export interface ActionResponse {
  ok: boolean;
  message: string;
  category: Category;
  watch?: Watch | null;
  watches?: Watch[] | null;
  stats?: Stats | null;
}

export interface PreviewResponse {
  company_name: string;
  careers_url: string;
  keywords: string;
  jobs: Job[];
  error: string | null;
  diagnostic: Diagnostic | null;
}
