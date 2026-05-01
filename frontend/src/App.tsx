import { Fragment, useEffect, useRef, useState } from "react";
import type { AnchorHTMLAttributes, ButtonHTMLAttributes, CSSProperties, FormEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  BriefcaseBusiness,
  Check,
  ChevronsUpDown,
  Download,
  Edit3,
  ExternalLink,
  Eye,
  FileText,
  GripVertical,
  Heart,
  Info,
  Monitor,
  MoreVertical,
  Moon,
  Plus,
  RefreshCcw,
  Settings as SettingsIcon,
  ShieldCheck,
  Sun,
  Trash2,
  Upload,
  X
} from "lucide-react";
import { api, ApiError, setCsrfToken } from "./api";
import type { ActionResponse, Appearance, Category, Dashboard, Job, JobStatus, PreviewResponse, PushConfig, PushSubscriptionPayload, Session, Stats, UserSettings, Watch, WatchInput } from "./types";

type Toast = {
  id: number;
  category: Category;
  message: string;
  leaving?: boolean;
};

type DragFrame = {
  pointerId: number;
  offsetX: number;
  offsetY: number;
  width: number;
  height: number;
  left: number;
  top: number;
};

type ButtonVariant = "primary" | "ghost" | "danger";
type ButtonSize = "default" | "sm";
type SegmentedOption<T extends string> = {
  value: T;
  label: string;
  icon?: ReactNode;
};
type ActionHandler = (action: ActionResponse, showToast?: boolean) => void;
type SettingsSaveRequest = {
  settings: UserSettings;
  requestId: number;
  requirePushSubscription?: boolean;
};

const UI_EXIT_MS = 180;
const APP_ICON_SRC = "/static/logo.png?v=20260501-brand";
const APP_BUILD = "v0.1";
const APP_DEVELOPER = "Neal Overbay";
const PRODUCT_TAGLINE = "self-hosted job alerts";
const appearanceOptions: Array<SegmentedOption<Appearance>> = [
  { value: "system", label: "System", icon: <Monitor size={16} /> },
  { value: "light", label: "Light", icon: <Sun size={16} /> },
  { value: "dark", label: "Dark", icon: <Moon size={16} /> }
];
const jobStatusOptions: Array<SegmentedOption<JobStatus>> = [
  { value: "", label: "None" },
  { value: "ignored", label: "Ignore" },
  { value: "interested", label: "Interested" },
  { value: "applied", label: "Applied" }
];

const emptyWatchInput: WatchInput = {
  company_name: "",
  careers_url: "",
  keywords: ""
};

function messageFromError(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

function deriveStats(watches: Watch[], interval: number, weeklyNewJobs = 0): Stats {
  return {
    alerts: watches.length,
    jobs: watches.reduce((sum, watch) => sum + watch.job_count, 0),
    interval,
    weekly_new_jobs: weeklyNewJobs
  };
}

function updateWatchList(watches: Watch[], updated: Watch) {
  const exists = watches.some((watch) => watch.id === updated.id);
  if (!exists) return [updated, ...watches];
  return watches.map((watch) => (watch.id === updated.id ? updated : watch));
}

function applyActionToDashboard(current: Dashboard | undefined, action: ActionResponse): Dashboard | undefined {
  if (!current) return current;
  if (action.watches) {
    return {
      watches: action.watches,
      stats: action.stats || deriveStats(action.watches, current.stats.interval, current.stats.weekly_new_jobs)
    };
  }
  if (action.watch) {
    const watches = updateWatchList(current.watches, action.watch);
    return {
      watches,
      stats: action.stats || deriveStats(watches, current.stats.interval, current.stats.weekly_new_jobs)
    };
  }
  if (action.stats) {
    return { ...current, stats: action.stats };
  }
  return current;
}

function formatDate(value: string | null) {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

function editableJobStatus(status: JobStatus | null | undefined): JobStatus {
  return status === "saved" ? "" : status || "";
}

function formatJobStatus(status: JobStatus | null | undefined) {
  if (!status || status === "saved") return "";
  if (status === "ignored") return "Ignore";
  return capitalizeLabel(status);
}

function urlBase64ToUint8Array(value: string) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = `${value}${padding}`.replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const output = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i += 1) {
    output[i] = rawData.charCodeAt(i);
  }
  return output;
}

function subscriptionToPayload(subscription: PushSubscription): PushSubscriptionPayload {
  const payload = subscription.toJSON();
  if (!payload.endpoint || !payload.keys?.p256dh || !payload.keys?.auth) {
    throw new Error("Could not read this browser's push subscription.");
  }
  return {
    endpoint: payload.endpoint,
    keys: {
      p256dh: payload.keys.p256dh,
      auth: payload.keys.auth
    }
  };
}

function pushSupportMessage() {
  if (!("serviceWorker" in navigator)) return "This browser does not support service workers.";
  if (!("PushManager" in window)) return "This browser does not support push notifications.";
  if (!("Notification" in window)) return "This browser does not support notifications.";
  return "";
}

function resolvedSystemAppearance(): Exclude<Appearance, "system"> {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function capitalizeLabel(value: string) {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function formatAppearance(value: Appearance | Exclude<Appearance, "system">) {
  return capitalizeLabel(value);
}

function normalizeCheckInterval(hours: number | string) {
  return Math.min(168, Math.max(1, Math.trunc(Number(hours) || 1)));
}

function useResolvedAppearance(appearance: Appearance) {
  const [resolved, setResolved] = useState<Exclude<Appearance, "system">>(() =>
    appearance === "system" ? resolvedSystemAppearance() : appearance
  );

  useEffect(() => {
    if (appearance !== "system") {
      setResolved(appearance);
      return;
    }

    const media = window.matchMedia("(prefers-color-scheme: light)");
    const update = () => setResolved(media.matches ? "light" : "dark");
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [appearance]);

  return resolved;
}

async function ensurePushSubscription(config?: PushConfig) {
  const unsupported = pushSupportMessage();
  if (unsupported) throw new Error(unsupported);

  const resolvedConfig = config || await api.pushConfig();
  if (!resolvedConfig.enabled || !resolvedConfig.public_key) {
    throw new Error("Browser push is not configured on the server yet.");
  }
  if (Notification.permission === "denied") {
    throw new Error("Browser notifications are blocked for this site.");
  }

  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();

  if (!subscription) {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error("Notification permission was not granted.");
    }
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(resolvedConfig.public_key)
    });
  }

  await api.savePushSubscription(subscriptionToPayload(subscription));
}

function useRoute() {
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = (nextPath: string) => {
    window.history.pushState({}, "", nextPath);
    setPath(nextPath);
  };

  return { path, navigate };
}

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function usePresence(active: boolean) {
  const [present, setPresent] = useState(active);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (active) {
      setPresent(true);
      setClosing(false);
      return;
    }

    if (!present) return;

    setClosing(true);
    const timer = window.setTimeout(() => {
      setPresent(false);
      setClosing(false);
    }, UI_EXIT_MS);

    return () => window.clearTimeout(timer);
  }, [active, present]);

  return {
    present: active || present,
    closing: !active && closing
  };
}

function buttonClasses({
  variant,
  size,
  fullWidth,
  className
}: {
  variant: ButtonVariant;
  size: ButtonSize;
  fullWidth?: boolean;
  className?: string;
}) {
  return cx(
    "btn",
    variant === "primary" && "btn-primary",
    variant === "ghost" && "btn-ghost",
    variant === "danger" && "btn-danger-solid",
    size === "sm" && "btn-sm",
    fullWidth && "full-width",
    className
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  loading?: boolean;
  icon?: ReactNode;
};

function Button({
  variant = "primary",
  size = "default",
  fullWidth,
  loading,
  icon,
  children,
  className,
  disabled,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      className={buttonClasses({ variant, size, fullWidth, className })}
      type={type}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Spinner /> : icon}
      {children}
    </button>
  );
}

type ButtonLinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  icon?: ReactNode;
};

function ButtonLink({
  variant = "ghost",
  size = "default",
  fullWidth,
  icon,
  children,
  className,
  ...props
}: ButtonLinkProps) {
  return (
    <a className={buttonClasses({ variant, size, fullWidth, className })} {...props}>
      {icon}
      {children}
    </a>
  );
}

type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  compact?: boolean;
};

function IconButton({ compact, children, className, type = "button", ...props }: IconButtonProps) {
  return (
    <button className={cx("icon-btn", compact && "compact", className)} type={type} {...props}>
      {children}
    </button>
  );
}

function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange
}: {
  label: string;
  value: T;
  options: Array<SegmentedOption<T>>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented-control" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          className={cx("segment-option", value === option.value && "active")}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  );
}

function ToggleOption({
  title,
  detail,
  checked,
  disabled,
  muted,
  onChange
}: {
  title: string;
  detail: string;
  checked: boolean;
  disabled?: boolean;
  muted?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className={cx("notification-option", muted && "disabled")}>
      <span>
        <span className="notification-title">{title}</span>
        <span className="notification-copy">{detail}</span>
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
    </label>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const { path, navigate } = useRoute();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmLogoutOpen, setConfirmLogoutOpen] = useState(false);
  const toastTimersRef = useRef<{ hide: number | null; remove: number | null }>({
    hide: null,
    remove: null
  });

  const clearToastTimers = () => {
    if (toastTimersRef.current.hide) window.clearTimeout(toastTimersRef.current.hide);
    if (toastTimersRef.current.remove) window.clearTimeout(toastTimersRef.current.remove);
    toastTimersRef.current.hide = null;
    toastTimersRef.current.remove = null;
  };

  const notify = (message: string, category: Category = "info") => {
    const id = Date.now() + Math.random();
    clearToastTimers();
    setToasts([{ id, message, category }]);
    toastTimersRef.current.hide = window.setTimeout(() => {
      setToasts((items) => items.map((toast) => (toast.id === id ? { ...toast, leaving: true } : toast)));
      toastTimersRef.current.remove = window.setTimeout(() => {
        setToasts((items) => items.filter((toast) => toast.id !== id));
        toastTimersRef.current.remove = null;
      }, UI_EXIT_MS);
    }, 5000);
  };

  const closeToast = (id: number) => {
    clearToastTimers();
    setToasts((items) => items.map((toast) => (toast.id === id ? { ...toast, leaving: true } : toast)));
    toastTimersRef.current.remove = window.setTimeout(() => {
      setToasts((items) => items.filter((toast) => toast.id !== id));
      toastTimersRef.current.remove = null;
    }, UI_EXIT_MS);
  };

  useEffect(() => () => clearToastTimers(), []);

  const logoutMutation = useMutation({
    mutationFn: async () => {
      const logout = await api.logout();
      if (logout.logout_url) return { logout, session: null };
      return { logout, session: await api.session() };
    },
    onSuccess: ({ logout, session: fresh }) => {
      queryClient.removeQueries({ queryKey: ["dashboard"] });
      queryClient.removeQueries({ queryKey: ["jobs"] });
      queryClient.removeQueries({ queryKey: ["settings"] });
      setConfirmLogoutOpen(false);
      if (logout.logout_url) {
        window.location.assign(logout.logout_url);
        return;
      }
      if (fresh) {
        setCsrfToken(fresh.csrf_token);
        queryClient.setQueryData(["session"], fresh);
      }
      navigate("/");
    },
    onError: () => undefined
  });

  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: api.session,
    staleTime: 5 * 60 * 1000
  });

  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: api.settings,
    enabled: Boolean(sessionQuery.data?.authenticated),
    staleTime: 5 * 60 * 1000
  });

  useEffect(() => {
    if (sessionQuery.data?.csrf_token) setCsrfToken(sessionQuery.data.csrf_token);
  }, [sessionQuery.data?.csrf_token]);

  useEffect(() => {
    const appearance = sessionQuery.data?.authenticated ? settingsQuery.data?.appearance || "system" : "system";
    document.documentElement.dataset.theme = appearance;
  }, [sessionQuery.data?.authenticated, settingsQuery.data?.appearance]);

  useEffect(() => {
    if ("serviceWorker" in navigator) {
      const register = () => {
        navigator.serviceWorker.register("/sw.js").catch(() => undefined);
      };
      if (document.readyState === "complete") {
        register();
      } else {
        window.addEventListener("load", register);
        return () => window.removeEventListener("load", register);
      }
    }
  }, []);

  if (sessionQuery.isLoading) {
    return <Splash />;
  }

  if (!sessionQuery.data?.authenticated) {
    return (
      <>
        <AuthView notify={notify} session={sessionQuery.data || null} />
        <ToastStack toasts={toasts} onDismiss={closeToast} />
      </>
    );
  }

  const session = sessionQuery.data;
  const closeLogoutConfirm = () => {
    if (!logoutMutation.isPending) setConfirmLogoutOpen(false);
  };

  return (
    <>
      <TopNav session={session} path={path} navigate={navigate} logout={() => setConfirmLogoutOpen(true)} />
      <main className="container">
        {path.startsWith("/jobs") ? (
          <JobsPage notify={notify} />
        ) : path.startsWith("/settings") ? (
          <SettingsPage notify={notify} />
        ) : (
          <DashboardPage notify={notify} />
        )}
      </main>
      <footer>job-tracker / {PRODUCT_TAGLINE} / build {APP_BUILD}</footer>
      <ConfirmModal
        open={confirmLogoutOpen}
        title="Log out?"
        detail="You will return to the sign-in screen on this device."
        confirmLabel="Log Out"
        pending={logoutMutation.isPending}
        onClose={closeLogoutConfirm}
        onConfirm={() => logoutMutation.mutate()}
      />
      <ToastStack toasts={toasts} onDismiss={closeToast} />
    </>
  );
}

function Splash() {
  return (
    <div className="splash">
      <img src={APP_ICON_SRC} alt="" />
      <div>Loading Job Tracker</div>
    </div>
  );
}

function TopNav({
  session,
  path,
  navigate,
  logout
}: {
  session: Session;
  path: string;
  navigate: (path: string) => void;
  logout: () => void;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [installPrompt, setInstallPrompt] = useState<any>(null);

  useEffect(() => {
    const handler = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);

  const navLink = (label: string, href: string, mobile = false) => (
    <button
      className={cx(mobile ? "nav-mobile-link" : "nav-link", path === href && "active")}
      type="button"
      onClick={() => {
        navigate(href);
        setMobileOpen(false);
      }}
    >
      {label}
    </button>
  );

  const requestLogout = () => {
    setMobileOpen(false);
    logout();
  };

  return (
    <nav>
      <div className="nav-inner">
        <button className="nav-logo" type="button" onClick={() => navigate("/")}>
          <span className="nav-logo-mark">
            <img src={APP_ICON_SRC} alt="" />
          </span>
          <span>
            <span className="nav-logo-text">Job Tracker</span>
            <span className="nav-logo-sub">{PRODUCT_TAGLINE}</span>
          </span>
        </button>

        <div className="nav-right">
          <span className="nav-user">{session.user?.email}</span>
          {navLink("Dashboard", "/")}
          {navLink("Jobs", "/jobs")}
          {navLink("Settings", "/settings")}
          {installPrompt && (
            <button className="nav-link" type="button" onClick={() => installPrompt.prompt()}>
              Install
            </button>
          )}
          <button className="nav-link" type="button" onClick={requestLogout}>
            Logout
          </button>
        </div>

        <button className={`nav-hamburger ${mobileOpen ? "open" : ""}`} type="button" onClick={() => setMobileOpen(!mobileOpen)} aria-label="Menu">
          <span />
          <span />
          <span />
        </button>
      </div>

      <div className={`nav-mobile-menu ${mobileOpen ? "open" : ""}`}>
        <div className="nav-mobile-email">{session.user?.email}</div>
        {navLink("Dashboard", "/", true)}
        {navLink("Jobs", "/jobs", true)}
        {navLink("Settings", "/settings", true)}
        {installPrompt && (
          <button className="nav-mobile-link" type="button" onClick={() => installPrompt.prompt()}>
            Install app
          </button>
        )}
        <button className="nav-mobile-link" type="button" onClick={requestLogout}>
          Logout
        </button>
      </div>
    </nav>
  );
}

function AuthView({ notify, session }: { notify: (message: string, category?: Category) => void; session: Session | null }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [ssoPending, setSsoPending] = useState(false);
  const passwordLoginEnabled = session?.password_login_enabled ?? true;
  const registrationEnabled = session?.registration_enabled ?? true;
  const authentikEnabled = Boolean(session?.authentik_enabled && session.authentik_login_url);
  const authentikButtonText = session?.authentik_login_button_text || "Log in with Authentik";
  const authentikLoginUrl = session?.authentik_login_url
    ? `${session.authentik_login_url}${session.authentik_login_url.includes("?") ? "&" : "?"}${new URLSearchParams({
        next: `${window.location.pathname}${window.location.search}`
      }).toString()}`
    : "";

  const mutation = useMutation({
    mutationFn: () => (mode === "login" ? api.login(email, password) : api.register(email, password)),
    onSuccess: (session) => {
      setCsrfToken(session.csrf_token);
      queryClient.setQueryData(["session"], session);
    },
    onError: () => undefined
  });

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authError = params.get("auth_error");
    if (!authError) return;

    params.delete("auth_error");
    const nextSearch = params.toString();
    window.history.replaceState(
      {},
      "",
      `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`
    );
  }, [notify]);

  useEffect(() => {
    if (!passwordLoginEnabled && mode !== "login") setMode("login");
    if (!registrationEnabled && mode === "register") setMode("login");
  }, [mode, passwordLoginEnabled, registrationEnabled]);

  useEffect(() => {
    document.documentElement.classList.add("auth-static");
    document.body.classList.add("auth-static");
    return () => {
      document.documentElement.classList.remove("auth-static");
      document.body.classList.remove("auth-static");
    };
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!passwordLoginEnabled) return;
    if (mode === "register" && !registrationEnabled) return;
    mutation.mutate();
  };

  return (
    <main className="auth-wrap">
      <section className="auth-card">
        <div className="auth-logo">
          <span className="auth-logo-mark">
            <img src={APP_ICON_SRC} alt="" />
          </span>
          <span>
            <span className="auth-title">Job Tracker</span>
            <span className="auth-sub">Taking the "job" out of "job search"</span>
          </span>
        </div>

        {passwordLoginEnabled && (
          <>
            <form onSubmit={submit}>
              <div className="field stacked">
                <label>Email</label>
                <input
                  type="email"
                  name="email"
                  autoComplete="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                />
              </div>
              <div className="field stacked">
                <label>Password</label>
                <input
                  type="password"
                  name="password"
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  minLength={mode === "register" ? 8 : undefined}
                />
              </div>
              <Button fullWidth type="submit" loading={mutation.isPending}>
                {mode === "login" ? "Log In" : "Create Account"}
              </Button>
            </form>

            {(registrationEnabled || mode === "register") && (
              <button className="auth-switch" type="button" onClick={() => setMode(mode === "login" ? "register" : "login")}>
                {mode === "login" ? "Need an account? Create one" : "Already have an account? Log in"}
              </button>
            )}
          </>
        )}

        {passwordLoginEnabled && authentikEnabled && (
          <div className="auth-divider">
            <span />
            <b>or</b>
            <span />
          </div>
        )}

        {authentikEnabled && (
          <ButtonLink
            className="auth-sso"
            fullWidth
            href={authentikLoginUrl}
            icon={ssoPending ? <Spinner /> : <ShieldCheck size={16} />}
            onClick={() => setSsoPending(true)}
            aria-disabled={ssoPending}
          >
            {ssoPending ? "Redirecting..." : authentikButtonText}
          </ButtonLink>
        )}

        {!passwordLoginEnabled && !authentikEnabled && (
          <div className="auth-empty">Sign-in is not configured.</div>
        )}
      </section>
    </main>
  );
}

function DashboardPage({ notify }: { notify: (message: string, category?: Category) => void }) {
  const queryClient = useQueryClient();
  const [reorderMode, setReorderMode] = useState(false);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [dragFrame, setDragFrame] = useState<DragFrame | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const dragPointerIdRef = useRef<number | null>(null);
  const dragOriginalIdsRef = useRef<number[]>([]);
  const dragOriginalDashboardRef = useRef<Dashboard | null>(null);

  const dashboardQuery = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard
  });

  const handleAction: ActionHandler = (action, showToast = false) => {
    queryClient.setQueryData<Dashboard>(["dashboard"], (current) => applyActionToDashboard(current, action));
    if (showToast && action.message) notify(action.message, action.category);
  };

  const checkAll = useMutation({
    mutationFn: api.checkAll,
    onSuccess: (action) => handleAction(action, true),
    onError: () => undefined
  });

  const reorder = useMutation({
    mutationFn: api.reorder,
    onSuccess: (action) => {
      dragOriginalIdsRef.current = [];
      dragOriginalDashboardRef.current = null;
      handleAction(action);
    },
    onError: (error) => {
      if (dragOriginalDashboardRef.current) {
        queryClient.setQueryData(["dashboard"], dragOriginalDashboardRef.current);
      }
      dragOriginalIdsRef.current = [];
      dragOriginalDashboardRef.current = null;
    }
  });

  const setWatchOrder = (watches: Watch[]) => {
    queryClient.setQueryData<Dashboard>(["dashboard"], (current) => {
      if (!current) return current;
      return {
        ...current,
        watches,
        stats: deriveStats(watches, current.stats.interval, current.stats.weekly_new_jobs)
      };
    });
  };

  const moveDraggingWatch = (clientY: number) => {
    const current = queryClient.getQueryData<Dashboard>(["dashboard"]);
    const list = listRef.current;
    if (!current || !list || draggingId === null) return;

    const dragged = current.watches.find((watch) => watch.id === draggingId);
    if (!dragged) return;

    const staticCards = Array.from(list.querySelectorAll<HTMLElement>("[data-watch-fragment]:not(.dragging)"));
    const afterElement = staticCards.reduce<{ offset: number; element: HTMLElement | null }>(
      (closest, item) => {
        const box = item.getBoundingClientRect();
        const offset = clientY - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) return { offset, element: item };
        return closest;
      },
      { offset: Number.NEGATIVE_INFINITY, element: null }
    ).element;

    const remaining = current.watches.filter((watch) => watch.id !== draggingId);
    const afterId = afterElement ? Number(afterElement.dataset.watchId) : null;
    const insertAt = afterId ? Math.max(0, remaining.findIndex((watch) => watch.id === afterId)) : remaining.length;
    const nextWatches = [...remaining];
    nextWatches.splice(insertAt, 0, dragged);
    if (nextWatches.map((watch) => watch.id).join(",") === current.watches.map((watch) => watch.id).join(",")) return;
    setWatchOrder(nextWatches);
  };

  const finishDraggingWatch = () => {
    const current = queryClient.getQueryData<Dashboard>(["dashboard"]);
    const originalIds = dragOriginalIdsRef.current;
    const nextIds = current?.watches.map((watch) => watch.id) || [];

    setDragFrame(null);
    setDraggingId(null);
    dragPointerIdRef.current = null;

    if (originalIds.length && nextIds.length && originalIds.join(",") !== nextIds.join(",")) {
      reorder.mutate(nextIds);
    } else {
      dragOriginalIdsRef.current = [];
      dragOriginalDashboardRef.current = null;
    }
  };

  useEffect(() => {
    if (draggingId === null) return;

    const handleMove = (event: globalThis.PointerEvent) => {
      if (dragPointerIdRef.current !== event.pointerId) return;
      event.preventDefault();
      setDragFrame((frame) => {
        if (!frame || frame.pointerId !== event.pointerId) return frame;
        return {
          ...frame,
          left: event.clientX - frame.offsetX,
          top: event.clientY - frame.offsetY
        };
      });
      moveDraggingWatch(event.clientY);
    };

    const handleEnd = (event: globalThis.PointerEvent) => {
      if (dragPointerIdRef.current !== event.pointerId) return;
      finishDraggingWatch();
    };

    window.addEventListener("pointermove", handleMove, { passive: false });
    window.addEventListener("pointerup", handleEnd);
    window.addEventListener("pointercancel", handleEnd);

    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleEnd);
      window.removeEventListener("pointercancel", handleEnd);
    };
  }, [draggingId]);

  const beginDraggingWatch = (watchId: number, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!reorderMode) return;
    const current = queryClient.getQueryData<Dashboard>(["dashboard"]);
    if (!current || current.watches.length < 2 || reorder.isPending) return;

    event.preventDefault();
    const fragment = event.currentTarget.closest<HTMLElement>("[data-watch-fragment]");
    if (!fragment) return;

    const rect = fragment.getBoundingClientRect();
    dragPointerIdRef.current = event.pointerId;
    dragOriginalIdsRef.current = current.watches.map((watch) => watch.id);
    dragOriginalDashboardRef.current = current;
    setDragFrame({
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
      left: rect.left,
      top: rect.top
    });
    setDraggingId(watchId);

    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture can fail if the browser already released it.
    }
  };

  if (dashboardQuery.isLoading) return <PanelLoading label="Loading dashboard" />;
  if (dashboardQuery.isError) return <EmptyState title="Dashboard unavailable" detail={messageFromError(dashboardQuery.error)} />;

  const dashboard = dashboardQuery.data;
  if (!dashboard) return <EmptyState title="Dashboard unavailable" detail="No dashboard data was returned." />;

  return (
    <>
      <header className="page-header">
        <div className="page-label">// dashboard</div>
        <h1 className="page-title">Your Job Alerts</h1>
        <p className="page-sub">Alerts check every {dashboard.stats.interval} hours. New matches are emailed to your account address.</p>
      </header>

      <StatsRow stats={dashboard.stats} />
      <CreateAlert notify={notify} onAction={handleAction} />

      <div className="section-title-row" id="alerts-section">
        <span>Current Job Alerts ({dashboard.stats.alerts})</span>
        <div className="section-actions">
          <Button variant="ghost" size="sm" icon={<ChevronsUpDown size={15} />} onClick={() => setReorderMode(!reorderMode)} disabled={reorder.isPending}>
            {reorderMode ? "Done" : "Reorder"}
          </Button>
          <Button variant="ghost" size="sm" loading={checkAll.isPending} icon={<RefreshCcw size={15} />} onClick={() => checkAll.mutate()}>
            Check All Now
          </Button>
        </div>
      </div>

      <div className={cx("watch-list", reorderMode && "reordering")} ref={listRef}>
        {dashboard.watches.length === 0 ? (
          <EmptyState title="No alerts yet" detail="Add a company careers page to start watching for new roles." />
        ) : (
          dashboard.watches.map((watch) => {
            const isDragging = draggingId === watch.id;
            const dragStyle: CSSProperties | undefined = isDragging && dragFrame ? {
              left: dragFrame.left,
              top: dragFrame.top,
              width: dragFrame.width
            } : undefined;

            return (
              <Fragment key={watch.id}>
                {isDragging && dragFrame && <div className="watch-drop-placeholder" style={{ height: dragFrame.height }} />}
                <WatchCard
                  watch={watch}
                  reorderMode={reorderMode}
                  isDragging={isDragging}
                  dragStyle={dragStyle}
                  onDragStart={beginDraggingWatch}
                  onAction={handleAction}
                  notify={notify}
                />
              </Fragment>
            );
          })
        )}
      </div>
    </>
  );
}

function StatsRow({ stats }: { stats: Stats }) {
  return (
    <div className="stats-row">
      <div className="stat">
        <div className="stat-value">{stats.alerts}</div>
        <div className="stat-label">Active Alerts</div>
      </div>
      <div className="stat">
        <div className="stat-value">{stats.jobs}</div>
        <div className="stat-label">Current Listings</div>
      </div>
    </div>
  );
}

function CreateAlert({ notify, onAction }: { notify: (message: string, category?: Category) => void; onAction: ActionHandler }) {
  const [input, setInput] = useState<WatchInput>(emptyWatchInput);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);

  const create = useMutation({
    mutationFn: api.createWatch,
    onSuccess: (action) => {
      onAction(action, true);
      setInput(emptyWatchInput);
      setPreview(null);
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const previewMutation = useMutation({
    mutationFn: api.preview,
    onSuccess: setPreview,
    onError: () => undefined
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate(input);
  };

  return (
    <>
      <div className="section-title" id="add-alert">Create a New Job Alert</div>
      <section className="card card-accent">
        <form onSubmit={submit}>
          <WatchFields input={input} onChange={setInput} />
          <div className="form-actions">
            <Button type="submit" loading={create.isPending} icon={<Plus size={16} />} aria-label="Add alert and check">
              <span className="label-desktop">Add Alert & Check</span>
              <span className="label-mobile">Add & Check</span>
            </Button>
            <Button variant="ghost" loading={previewMutation.isPending} icon={<Eye size={16} />} onClick={() => previewMutation.mutate(input)} aria-label="Preview results">
              <span className="label-desktop">Preview Results</span>
              <span className="label-mobile">Preview</span>
            </Button>
          </div>
        </form>
      </section>
      <PreviewModal preview={preview} onClose={() => setPreview(null)} />
    </>
  );
}

function WatchFields({ input, onChange }: { input: WatchInput; onChange: (input: WatchInput) => void }) {
  return (
    <div className="form-grid">
      <div className="field">
        <label>Company Name</label>
        <input value={input.company_name} onChange={(event) => onChange({ ...input, company_name: event.target.value })} placeholder="e.g. Medela" required />
      </div>
      <div className="field">
        <label>Careers Page URL</label>
        <input value={input.careers_url} onChange={(event) => onChange({ ...input, careers_url: event.target.value })} placeholder="https://www.medela.com/careers" type="url" required />
        <div className="field-hint">Paste the direct URL of the page that lists open positions.</div>
      </div>
      <div className="field form-full">
        <label>Keywords <span>(optional)</span></label>
        <input value={input.keywords} onChange={(event) => onChange({ ...input, keywords: event.target.value })} placeholder="engineer, mechanical, -intern" />
        <div className="field-hint">Comma-separated. Use -keyword to exclude titles. Leave blank to see all open roles.</div>
      </div>
    </div>
  );
}

function WatchCard({
  watch,
  reorderMode,
  isDragging,
  dragStyle,
  onDragStart,
  onAction,
  notify
}: {
  watch: Watch;
  reorderMode: boolean;
  isDragging: boolean;
  dragStyle?: CSSProperties;
  onDragStart: (watchId: number, event: ReactPointerEvent<HTMLButtonElement>) => void;
  onAction: ActionHandler;
  notify: (message: string, category?: Category) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [notifications, setNotifications] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const menuPresence = usePresence(menuOpen);

  const closeMenu = () => setMenuOpen(false);
  const toggleMenu = () => {
    setMenuOpen((open) => (open || menuPresence.present ? false : true));
  };

  const check = useMutation({
    mutationFn: () => api.checkWatch(watch.id),
    onSuccess: (action) => {
      setMenuOpen(false);
      onAction(action, true);
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteWatch(watch.id),
    onSuccess: (action) => {
      setConfirmDelete(false);
      onAction(action);
    },
    onError: () => undefined
  });

  const iconLabel = watch.company_name.slice(0, 1).toUpperCase() || "J";

  return (
    <article className={cx("watch-fragment", isDragging && "dragging")} id={`watch-${watch.id}`} data-watch-fragment data-watch-id={watch.id} style={dragStyle}>
      <div className="watch-card">
        {reorderMode && (
          <div className="watch-drag">
            <IconButton className="drag-handle" onPointerDown={(event) => onDragStart(watch.id, event)} aria-label={`Drag ${watch.company_name} alert to reorder`}>
              <GripVertical size={18} />
            </IconButton>
          </div>
        )}

        <div className="watch-icon" aria-hidden="true">
          {iconLabel}
        </div>

        <div className="watch-summary">
          <div className="watch-company">{watch.company_name}</div>
          <div className="watch-created">
            Created {formatDate(watch.created_at).slice(0, 10)}
            {watch.last_checked ? <> - Last checked {formatDate(watch.last_checked)}</> : null}
          </div>
          <div className="watch-meta">
            {watch.careers_url && (
              <a className="chip link-chip" href={watch.careers_url} target="_blank" rel="noreferrer">
                {watch.careers_url.length > 55 ? `${watch.careers_url.slice(0, 55)}...` : watch.careers_url} <ExternalLink size={12} />
              </a>
            )}
            {watch.keywords ? <span className="chip kw">Search: {watch.keywords}</span> : <span className="chip">all roles</span>}
            {!watch.email_enabled && <span className="chip warn">email paused</span>}
            {watch.push_enabled && <span className="chip kw">push enabled</span>}
            {watch.last_error && <span className="chip warn">last scan failed</span>}
          </div>
          {watch.diagnostic && (
            <div className="watch-error">
              <strong>{watch.diagnostic.title}:</strong> {watch.diagnostic.detail}
            </div>
          )}
        </div>

        <div className="watch-body">
          {watch.jobs.length ? (
            <div className="watch-jobs">
              {watch.jobs.map((job) => (
                <JobRow key={job.job_id} job={job} />
              ))}
              {watch.job_count > 5 && <div className="more-jobs">+{watch.job_count - 5} more / view all jobs</div>}
            </div>
          ) : (
            <div className="watch-empty">No matching listings found yet. Run a check to scan now.</div>
          )}
        </div>

        <div className="watch-actions">
          <IconButton
            className={cx("menu-trigger", menuOpen && "open")}
            onClick={toggleMenu}
            aria-label={`Alert settings for ${watch.company_name}`}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
          >
            <MoreVertical size={18} />
          </IconButton>
          <DropdownMenu open={menuOpen} present={menuPresence.present} closing={menuPresence.closing} onClose={closeMenu}>
            <MenuButton icon={<RefreshCcw size={16} />} label={check.isPending ? "Checking..." : "Check"} loading={check.isPending} onClick={() => check.mutate()} />
            <MenuButton icon={<Bell size={16} />} label="Notifications" onClick={() => { setNotifications(true); closeMenu(); }} />
            <MenuButton icon={<FileText size={16} />} label="Notes" onClick={() => { setNotesOpen(true); closeMenu(); }} />
            <MenuButton icon={<Edit3 size={16} />} label="Edit" onClick={() => { setEditing(true); closeMenu(); }} />
            <MenuButton danger icon={<Trash2 size={16} />} label="Delete" onClick={() => { setConfirmDelete(true); closeMenu(); }} />
          </DropdownMenu>
        </div>
      </div>

      <EditWatchModal watch={watch} open={editing} onClose={() => setEditing(false)} onAction={onAction} notify={notify} />
      <NotificationsModal watch={watch} open={notifications} onClose={() => setNotifications(false)} onAction={onAction} notify={notify} />
      <JobNotesModal watch={watch} open={notesOpen} onClose={() => setNotesOpen(false)} notify={notify} />
      <ConfirmModal
        open={confirmDelete}
        title={`Delete ${watch.company_name}?`}
        detail="This removes the alert and hides its saved listings from your dashboard."
        confirmLabel="Delete"
        danger
        pending={deleteMutation.isPending}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => deleteMutation.mutate()}
      />
    </article>
  );
}

function DropdownMenu({
  open,
  present,
  closing,
  onClose,
  children
}: {
  open: boolean;
  present: boolean;
  closing: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      onClose();
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, onClose]);

  if (!present) return null;

  return (
    <div className={cx("dropdown-panel", "menu-list", closing && "closing")} ref={menuRef} role="menu">
      {children}
    </div>
  );
}

function MenuButton({
  icon,
  label,
  danger,
  loading,
  disabled,
  onClick
}: {
  icon: ReactNode;
  label: string;
  danger?: boolean;
  loading?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button className={cx("menu-item", danger && "menu-item-danger")} type="button" disabled={disabled || loading} onClick={onClick} role="menuitem">
      {loading ? <Spinner /> : icon}
      <span>{label}</span>
    </button>
  );
}

function JobRow({ job }: { job: Job }) {
  const statusLabel = formatJobStatus(job.status);

  return (
    <div className={cx("job-row", job.status === "ignored" && "job-row-muted")}>
      <div className="job-dot" />
      <div className="job-title">
        {job.title}
        {job.location ? <span className="job-location-inline"> - {job.location}</span> : null}
      </div>
      {statusLabel && <span className={cx("job-status-chip", `status-${job.status}`)}>{statusLabel}</span>}
      {job.url && (
        <a className="job-link" href={job.url} target="_blank" rel="noreferrer">
          Apply <ExternalLink size={13} />
        </a>
      )}
    </div>
  );
}

function EditWatchModal({
  watch,
  open,
  onClose,
  onAction,
  notify
}: {
  watch: Watch;
  open: boolean;
  onClose: () => void;
  onAction: ActionHandler;
  notify: (message: string, category?: Category) => void;
}) {
  const [input, setInput] = useState<WatchInput>(() => ({
    company_name: watch.company_name,
    careers_url: watch.careers_url,
    keywords: watch.keywords
  }));
  const [preview, setPreview] = useState<PreviewResponse | null>(null);

  useEffect(() => {
    if (open) {
      setInput({ company_name: watch.company_name, careers_url: watch.careers_url, keywords: watch.keywords });
    }
  }, [open, watch]);

  const update = useMutation({
    mutationFn: () => api.updateWatch(watch.id, input),
    onSuccess: (action) => {
      onAction(action, true);
      onClose();
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const previewMutation = useMutation({
    mutationFn: () => api.preview(input),
    onSuccess: setPreview,
    onError: () => undefined
  });

  return (
    <>
      <Modal open={open} onClose={onClose} title={`Edit ${watch.company_name}`} subTitle="Save changes to run a fresh check.">
        <form onSubmit={(event) => { event.preventDefault(); update.mutate(); }}>
          <WatchFields input={input} onChange={setInput} />
          <div className="modal-actions">
            <Button variant="ghost" loading={previewMutation.isPending} icon={<Eye size={16} />} onClick={() => previewMutation.mutate()}>
              Preview
            </Button>
            <Button type="submit" loading={update.isPending} icon={<Check size={16} />}>
              Save & Check
            </Button>
          </div>
        </form>
      </Modal>
      <PreviewModal preview={preview} onClose={() => setPreview(null)} />
    </>
  );
}

function NotificationsModal({
  watch,
  open,
  onClose,
  onAction,
  notify
}: {
  watch: Watch;
  open: boolean;
  onClose: () => void;
  onAction: ActionHandler;
  notify: (message: string, category?: Category) => void;
}) {
  const [emailEnabled, setEmailEnabled] = useState(watch.email_enabled);
  const [pushEnabled, setPushEnabled] = useState(watch.push_enabled);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (open) {
      setEmailEnabled(watch.email_enabled);
      setPushEnabled(watch.push_enabled);
      setSaved(false);
    }
  }, [open, watch.email_enabled, watch.push_enabled]);

  const pushConfigQuery = useQuery({
    queryKey: ["push-config"],
    queryFn: api.pushConfig,
    enabled: open,
    staleTime: 5 * 60 * 1000
  });

  const mutation = useMutation({
    mutationFn: async () => {
      if (pushEnabled) {
        const config = pushConfigQuery.data || await api.pushConfig();
        await ensurePushSubscription(config);
      }
      return api.updateNotifications(watch.id, emailEnabled, pushEnabled);
    },
    onSuccess: (action) => {
      onAction(action);
      setSaved(true);
      window.setTimeout(onClose, 650);
    },
    onError: () => setSaved(false)
  });

  const pushUnsupported = pushSupportMessage();
  const pushConfig = pushConfigQuery.data;
  const pushUnavailable = Boolean(pushUnsupported || pushConfigQuery.isError || (pushConfig && !pushConfig.enabled));
  const pushCopy = pushUnsupported ||
    (pushConfigQuery.isError
      ? "Could not check browser push configuration."
      : pushConfig?.enabled
        ? "Send browser notifications to this device for new matches."
        : "Push delivery needs VAPID keys in the server environment.");

  return (
    <Modal open={open} onClose={onClose} title="Notifications" subTitle={watch.company_name}>
      <div className="notification-options">
        <ToggleOption
          title="Email alerts"
          detail="Send new matches to your account email."
          checked={emailEnabled}
          disabled={mutation.isPending}
          onChange={(checked) => {
            setSaved(false);
            setEmailEnabled(checked);
          }}
        />
        <ToggleOption
          title="Browser push"
          detail={pushConfigQuery.isLoading ? "Checking browser push support..." : pushCopy}
          checked={pushEnabled}
          disabled={pushConfigQuery.isLoading || pushUnavailable || mutation.isPending}
          muted={pushUnavailable}
          onChange={(checked) => {
            setSaved(false);
            setPushEnabled(checked);
          }}
        />
      </div>
      <div className="modal-actions">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button loading={mutation.isPending} icon={<Check size={16} />} onClick={() => mutation.mutate()}>
          {saved ? "Saved" : "Save Notifications"}
        </Button>
      </div>
    </Modal>
  );
}

function JobNotesModal({
  watch,
  open,
  onClose,
  notify
}: {
  watch: Watch;
  open: boolean;
  onClose: () => void;
  notify: (message: string, category?: Category) => void;
}) {
  const jobsQuery = useQuery({
    queryKey: ["watch-jobs", watch.id],
    queryFn: () => api.watchJobs(watch.id),
    enabled: open
  });

  return (
    <Modal open={open} onClose={onClose} title="Notes" subTitle={watch.company_name}>
      {jobsQuery.isLoading ? (
        <PanelLoading label="Loading jobs" />
      ) : jobsQuery.isError ? (
        <EmptyState title="Could not load jobs" detail={messageFromError(jobsQuery.error)} />
      ) : jobsQuery.data && jobsQuery.data.length > 0 ? (
        <div className="job-notes-list">
          {jobsQuery.data.map((job) => (
            <JobNoteEditor key={job.id || job.job_id} job={job} notify={notify} />
          ))}
        </div>
      ) : (
        <EmptyState title="No active jobs" detail="Run a check to discover jobs before adding notes." />
      )}
    </Modal>
  );
}

function JobNoteEditor({ job, notify }: { job: Job; notify: (message: string, category?: Category) => void }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<JobStatus>(editableJobStatus(job.status));
  const [notes, setNotes] = useState(job.notes || "");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setStatus(editableJobStatus(job.status));
    setNotes(job.notes || "");
    setSaved(false);
  }, [job.id, job.status, job.notes]);

  const save = useMutation({
    mutationFn: () => {
      if (!job.id) throw new Error("This job cannot be updated yet.");
      return api.updateJobMeta(job.id, status, notes);
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<Job[]>(["watch-jobs", updated.watch_id || job.watch_id], (current) =>
        current?.map((item) => (item.id === updated.id ? updated : item)) || current
      );
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1600);
    },
    onError: () => setSaved(false)
  });

  return (
    <div className="job-note-card">
      <div className="job-note-header">
        <div>
          <div className="job-note-title">{job.title}</div>
          <div className="job-table-meta">{job.location || "Location not listed"}</div>
        </div>
        {job.url && (
          <ButtonLink size="sm" href={job.url} target="_blank" rel="noreferrer" icon={<ExternalLink size={14} />}>
            Apply
          </ButtonLink>
        )}
      </div>
      <SegmentedControl
        label={`Status for ${job.title}`}
        value={status}
        options={jobStatusOptions}
        onChange={(value) => {
          setSaved(false);
          setStatus(value);
        }}
      />
      <textarea
        className="notes-input"
        value={notes}
        maxLength={4000}
        onChange={(event) => {
          setSaved(false);
          setNotes(event.target.value);
        }}
        placeholder="Notes, contacts, next steps..."
      />
      <div className="modal-actions compact-actions">
        <Button size="sm" loading={save.isPending} icon={<Check size={15} />} onClick={() => save.mutate()}>
          {saved ? "Saved" : "Save Notes"}
        </Button>
      </div>
    </div>
  );
}

function SettingsPage({ notify }: { notify: (message: string, category?: Category) => void }) {
  const queryClient = useQueryClient();
  const restoreInputRef = useRef<HTMLInputElement | null>(null);
  const saveRequestIdRef = useRef(0);
  const [appearance, setAppearance] = useState<Appearance>("system");
  const [defaultEmailEnabled, setDefaultEmailEnabled] = useState(true);
  const [defaultPushEnabled, setDefaultPushEnabled] = useState(false);
  const [checkIntervalHours, setCheckIntervalHours] = useState("4");
  const [restorePayload, setRestorePayload] = useState<Record<string, unknown> | null>(null);
  const [autosaveMessage, setAutosaveMessage] = useState("Changes save automatically.");
  const resolvedAppearance = useResolvedAppearance(appearance);

  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: api.settings
  });

  const pushConfigQuery = useQuery({
    queryKey: ["push-config"],
    queryFn: api.pushConfig,
    staleTime: 5 * 60 * 1000
  });

  const applySettingsLocally = (settings: UserSettings) => {
    setAppearance(settings.appearance);
    setDefaultEmailEnabled(settings.default_email_enabled);
    setDefaultPushEnabled(settings.default_push_enabled);
    setCheckIntervalHours(String(settings.check_interval_hours));
    document.documentElement.dataset.theme = settings.appearance;
  };

  const applySettingsToCaches = (settings: UserSettings) => {
    queryClient.setQueryData(["settings"], settings);
    queryClient.setQueryData<Session>(["session"], (current) =>
      current ? { ...current, check_interval: settings.check_interval_hours } : current
    );
    queryClient.setQueryData<Dashboard>(["dashboard"], (current) =>
      current ? { ...current, stats: { ...current.stats, interval: settings.check_interval_hours } } : current
    );
  };

  const currentSettings = (patch: Partial<UserSettings> = {}): UserSettings => {
    const next = {
      appearance,
      default_email_enabled: defaultEmailEnabled,
      default_push_enabled: defaultPushEnabled,
      check_interval_hours: normalizeCheckInterval(checkIntervalHours || 4),
      ...patch
    };
    return {
      ...next,
      check_interval_hours: normalizeCheckInterval(next.check_interval_hours)
    };
  };

  const save = useMutation({
    mutationFn: async ({ settings, requirePushSubscription }: SettingsSaveRequest) => {
      if (settings.default_push_enabled && requirePushSubscription) {
        const config = pushConfigQuery.data || await api.pushConfig();
        await ensurePushSubscription(config);
      }
      return api.updateSettings(settings);
    },
    onMutate: async ({ settings }) => {
      await queryClient.cancelQueries({ queryKey: ["settings"] });
      const previousSettings = queryClient.getQueryData<UserSettings>(["settings"]);
      applySettingsLocally(settings);
      applySettingsToCaches(settings);
      setAutosaveMessage("Saving changes...");
      return { previousSettings };
    },
    onSuccess: (settings, request) => {
      if (request.requestId !== saveRequestIdRef.current) return;
      applySettingsLocally(settings);
      applySettingsToCaches(settings);
      setAutosaveMessage("Saved.");
      window.setTimeout(() => {
        setAutosaveMessage((current) => current === "Saved." ? "Changes save automatically." : current);
      }, 1800);
    },
    onError: (error, request, context) => {
      if (request.requestId === saveRequestIdRef.current && context?.previousSettings) {
        applySettingsLocally(context.previousSettings);
        applySettingsToCaches(context.previousSettings);
      }
      setAutosaveMessage(messageFromError(error));
    }
  });

  useEffect(() => {
    if (!settingsQuery.data || save.isPending) return;
    applySettingsLocally(settingsQuery.data);
  }, [settingsQuery.data, save.isPending]);

  const testNotification = useMutation({
    mutationFn: async () => {
      const config = pushConfigQuery.data || await api.pushConfig();
      await ensurePushSubscription(config);
      return api.testNotification();
    },
    onSuccess: () => setAutosaveMessage("Test notification sent."),
    onError: (error) => setAutosaveMessage(messageFromError(error))
  });

  const exportData = useMutation({
    mutationFn: api.exportData,
    onSuccess: (data) => {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const date = new Date().toISOString().slice(0, 10);
      link.href = url;
      link.download = `job-tracker-backup-${date}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setAutosaveMessage("Export ready.");
    },
    onError: (error) => setAutosaveMessage(messageFromError(error))
  });

  const restoreData = useMutation({
    mutationFn: async () => {
      if (!restorePayload) throw new Error("Choose a backup file first.");
      return api.restoreData(restorePayload);
    },
    onSuccess: (action) => {
      setRestorePayload(null);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["session"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      setAutosaveMessage(action.message);
    },
    onError: (error) => setAutosaveMessage(messageFromError(error))
  });

  const chooseRestoreFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Backup file must contain a JSON object.");
      }
      setRestorePayload(parsed as Record<string, unknown>);
    } catch (error) {
      setAutosaveMessage(messageFromError(error));
    } finally {
      if (restoreInputRef.current) restoreInputRef.current.value = "";
    }
  };

  const persistSettings = (patch: Partial<UserSettings>, requirePushSubscription = false) => {
    const settings = currentSettings(patch);
    const requestId = saveRequestIdRef.current + 1;
    saveRequestIdRef.current = requestId;
    save.mutate({ settings, requestId, requirePushSubscription });
  };

  const chooseAppearance = (value: Appearance) => persistSettings({ appearance: value });

  const appearanceStatus = appearance === "system"
    ? `System is using ${formatAppearance(resolvedAppearance)} mode on this device.`
    : `${formatAppearance(appearance)} mode selected.`;

  const pushUnsupported = pushSupportMessage();
  const pushConfig = pushConfigQuery.data;
  const pushUnavailable = Boolean(pushUnsupported || pushConfigQuery.isError || (pushConfig && !pushConfig.enabled));
  const pushCopy = pushUnsupported ||
    (pushConfigQuery.isError
      ? "Could not check browser push configuration."
      : pushConfig?.enabled
        ? "Use browser push for new alerts by default."
        : "Push delivery needs VAPID keys in the server environment.");

  if (settingsQuery.isLoading) return <PanelLoading label="Loading settings" />;
  if (settingsQuery.isError) return <EmptyState title="Settings unavailable" detail={messageFromError(settingsQuery.error)} />;

  return (
    <>
      <header className="page-header">
        <div className="page-label">// settings</div>
        <h1 className="page-title">Settings</h1>
        <p className="page-sub">Account-wide defaults for this Job Tracker workspace.</p>
      </header>

      <section className="card settings-card">
        <div className="settings-section">
          <div className="settings-heading">
            <SettingsIcon size={18} />
            <span>Appearance</span>
          </div>
          <SegmentedControl
            label="Appearance"
            value={appearance}
            options={appearanceOptions}
            onChange={chooseAppearance}
          />
          <div className="appearance-status">{appearanceStatus}</div>
        </div>

        <div className="settings-section">
          <div className="settings-heading">
            <Bell size={18} />
            <span>Notification Defaults</span>
          </div>
          <div className="notification-options">
            <ToggleOption
              title="Email alerts"
              detail="Enable email for new alerts."
              checked={defaultEmailEnabled}
              onChange={(checked) => persistSettings({ default_email_enabled: checked })}
            />
            <ToggleOption
              title="Browser push"
              detail={pushConfigQuery.isLoading ? "Checking browser push support..." : pushCopy}
              checked={defaultPushEnabled}
              disabled={pushConfigQuery.isLoading}
              muted={pushUnavailable && !defaultPushEnabled}
              onChange={(checked) => {
                if (checked && pushUnavailable) {
                  setAutosaveMessage(pushCopy);
                  return;
                }
                persistSettings({ default_push_enabled: checked }, checked && !defaultPushEnabled);
              }}
            />
          </div>
          <div className="notification-actions">
            <Button
              variant="ghost"
              loading={testNotification.isPending}
              icon={<Bell size={16} />}
              onClick={() => testNotification.mutate()}
            >
              Test notification
            </Button>
          </div>
        </div>

        <div className="settings-section">
          <div className="settings-heading">
            <RefreshCcw size={18} />
            <span>Check Interval</span>
          </div>
          <div className="field settings-number-field">
            <label>Hours between scheduled checks</label>
            <input
              type="number"
              min={1}
              max={168}
              step={1}
              value={checkIntervalHours}
              onChange={(event) => {
                const rawValue = event.target.value;
                setCheckIntervalHours(rawValue);
                if (rawValue !== "") {
                  persistSettings({ check_interval_hours: Number(rawValue) });
                }
              }}
              onBlur={() => {
                if (checkIntervalHours === "") {
                  persistSettings({ check_interval_hours: 4 });
                }
              }}
              placeholder="4"
            />
            <div className="field-hint">Use 1-168 hours. Leave blank to use 4 hours. Manual checks still run immediately.</div>
          </div>
        </div>

        <div className="settings-section">
          <div className="settings-heading">
            <Download size={18} />
            <span>Export / Restore Data</span>
          </div>
          <div className="data-actions">
            <Button
              variant="ghost"
              loading={exportData.isPending}
              icon={<Download size={16} />}
              onClick={() => exportData.mutate()}
            >
              Export Data
            </Button>
            <Button
              variant="ghost"
              icon={<Upload size={16} />}
              onClick={() => restoreInputRef.current?.click()}
            >
              Restore Data
            </Button>
            <input
              ref={restoreInputRef}
              className="visually-hidden"
              type="file"
              accept="application/json,.json"
              onChange={(event) => chooseRestoreFile(event.target.files?.[0])}
            />
          </div>
          <div className="field-hint">Restore replaces this account's alerts, jobs, notes, and defaults with the backup file.</div>
        </div>

        <div className="settings-autosave" aria-live="polite">{autosaveMessage}</div>
      </section>
      <section className="card app-info-card">
        <div className="app-info-main">
          <span className="app-info-icon">
            <img src={APP_ICON_SRC} alt="" />
          </span>
          <div>
            <div className="app-info-title">Job Tracker</div>
            <div className="app-info-sub">{PRODUCT_TAGLINE}</div>
          </div>
        </div>
        <div className="app-info-grid">
          <div className="app-info-item">
            <Info size={16} />
            <span>Developer</span>
            <strong>{APP_DEVELOPER}</strong>
          </div>
          <div className="app-info-item">
            <Info size={16} />
            <span>Build</span>
            <strong>{APP_BUILD}</strong>
          </div>
        </div>
        <Button variant="ghost" icon={<Heart size={16} />} onClick={() => setAutosaveMessage("Donation support is coming later.")}>
          Donate
        </Button>
      </section>
      <ConfirmModal
        open={Boolean(restorePayload)}
        title="Restore backup?"
        detail="This will replace this account's current alerts, saved jobs, statuses, notes, and defaults."
        confirmLabel="Restore"
        pending={restoreData.isPending}
        onClose={() => setRestorePayload(null)}
        onConfirm={() => restoreData.mutate()}
      />
    </>
  );
}

function PreviewModal({ preview, onClose }: { preview: PreviewResponse | null; onClose: () => void }) {
  return (
    <Modal open={Boolean(preview)} onClose={onClose} title="Preview Results" subTitle={preview?.company_name || ""}>
      {preview?.diagnostic && (
        <div className="watch-error block">
          <strong>{preview.diagnostic.title}:</strong> {preview.diagnostic.detail}
        </div>
      )}
      {preview && preview.jobs.length > 0 ? (
        <div className="watch-jobs preview-list">
          {preview.jobs.map((job) => <JobRow key={job.job_id} job={job} />)}
        </div>
      ) : (
        <EmptyState title="No matches in preview" detail="The app can still keep checking this page on a schedule." />
      )}
    </Modal>
  );
}

function ConfirmModal({
  open,
  title,
  detail,
  confirmLabel,
  danger,
  pending,
  onClose,
  onConfirm
}: {
  open: boolean;
  title: string;
  detail: string;
  confirmLabel: string;
  danger?: boolean;
  pending?: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const handleClose = () => {
    if (!pending) onClose();
  };

  return (
    <Modal open={open} onClose={handleClose} title={title}>
      <p className="confirm-detail">{detail}</p>
      <div className="modal-actions">
        <Button variant="ghost" disabled={pending} onClick={handleClose}>Cancel</Button>
        <Button variant={danger ? "danger" : "primary"} loading={pending} onClick={onConfirm}>{confirmLabel}</Button>
      </div>
    </Modal>
  );
}

function Modal({
  open,
  title,
  subTitle,
  children,
  onClose
}: {
  open: boolean;
  title: string;
  subTitle?: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const { present, closing } = usePresence(open);

  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!present) return null;

  return (
    <div className={cx("modal-backdrop", closing && "closing")} onMouseDown={onClose}>
      <section className={cx("app-modal", closing && "closing")} onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <div className="modal-title">{title}</div>
            {subTitle && <div className="modal-sub">{subTitle}</div>}
          </div>
          <button className="modal-close" type="button" aria-label="Close dialog" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

function JobsPage({ notify }: { notify: (message: string, category?: Category) => void }) {
  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs
  });

  return (
    <>
      <header className="page-header">
        <div className="page-label">// listings</div>
        <h1 className="page-title">Current Job Listings</h1>
        <p className="page-sub">Active roles discovered across your watched careers pages.</p>
      </header>
      <section className="card">
        {jobsQuery.isLoading ? (
          <PanelLoading label="Loading jobs" />
        ) : jobsQuery.data && jobsQuery.data.length > 0 ? (
          <div className="jobs-table">
            {jobsQuery.data.map((job) => (
              <div className="jobs-table-row" key={`${job.watch_id}-${job.job_id}`}>
                <div>
                  <div className="job-table-title">{job.title}</div>
                  <div className="job-table-meta">
                    {job.company_name} / {job.location || "Location not listed"} / Found {formatDate(job.found_at || null)}
                    {formatJobStatus(job.status) ? ` / ${formatJobStatus(job.status)}` : ""}
                  </div>
                </div>
                {job.url && (
                  <ButtonLink size="sm" href={job.url} target="_blank" rel="noreferrer" icon={<ExternalLink size={14} />}>
                    Apply
                  </ButtonLink>
                )}
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="No active listings" detail="Run a check or add an alert to discover matching roles." />
        )}
      </section>
    </>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <BriefcaseBusiness size={22} />
      <div>
        <div className="empty-title">{title}</div>
        <div className="empty-detail">{detail}</div>
      </div>
    </div>
  );
}

function PanelLoading({ label }: { label: string }) {
  return (
    <div className="panel-loading">
      <Spinner />
      {label}
    </div>
  );
}

function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

function ToastStack({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  if (!toasts.length) return null;
  return (
    <div className="flash-stack has-flashes" aria-live="polite">
      {toasts.map((toast) => (
        <div className={cx("flash", toast.category, toast.leaving && "leaving")} key={toast.id}>
          <span>{toast.message}</span>
          <button className="flash-close" type="button" aria-label="Dismiss notification" onClick={() => onDismiss(toast.id)}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
