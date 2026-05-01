import { Fragment, useEffect, useRef, useState } from "react";
import type { AnchorHTMLAttributes, ButtonHTMLAttributes, CSSProperties, FormEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  BriefcaseBusiness,
  Check,
  ChevronsUpDown,
  Edit3,
  ExternalLink,
  Eye,
  GripVertical,
  MoreVertical,
  Plus,
  RefreshCcw,
  ShieldCheck,
  Trash2,
  X
} from "lucide-react";
import { api, ApiError, setCsrfToken } from "./api";
import type { ActionResponse, Category, Dashboard, Job, PreviewResponse, Session, Stats, Watch, WatchInput } from "./types";

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

const UI_EXIT_MS = 180;

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

function deriveStats(watches: Watch[], interval: number): Stats {
  return {
    alerts: watches.length,
    jobs: watches.reduce((sum, watch) => sum + watch.job_count, 0),
    interval
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
      stats: action.stats || deriveStats(action.watches, current.stats.interval)
    };
  }
  if (action.watch) {
    const watches = updateWatchList(current.watches, action.watch);
    return {
      watches,
      stats: action.stats || deriveStats(watches, current.stats.interval)
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

function domainFromUrl(value: string) {
  try {
    return new URL(value).hostname;
  } catch {
    return "";
  }
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

export default function App() {
  const queryClient = useQueryClient();
  const { path, navigate } = useRoute();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmLogoutOpen, setConfirmLogoutOpen] = useState(false);

  const notify = (message: string, category: Category = "info") => {
    const id = Date.now() + Math.random();
    setToasts((items) => [{ id, message, category }, ...items]);
    window.setTimeout(() => {
      setToasts((items) => items.map((toast) => (toast.id === id ? { ...toast, leaving: true } : toast)));
      window.setTimeout(() => {
        setToasts((items) => items.filter((toast) => toast.id !== id));
      }, UI_EXIT_MS);
    }, 5000);
  };

  const closeToast = (id: number) => {
    setToasts((items) => items.map((toast) => (toast.id === id ? { ...toast, leaving: true } : toast)));
    window.setTimeout(() => {
      setToasts((items) => items.filter((toast) => toast.id !== id));
    }, UI_EXIT_MS);
  };

  const logoutMutation = useMutation({
    mutationFn: async () => {
      const logout = await api.logout();
      if (logout.logout_url) return { logout, session: null };
      return { logout, session: await api.session() };
    },
    onSuccess: ({ logout, session: fresh }) => {
      queryClient.removeQueries({ queryKey: ["dashboard"] });
      queryClient.removeQueries({ queryKey: ["jobs"] });
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
    onError: (error) => notify(messageFromError(error), "error")
  });

  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: api.session,
    staleTime: 5 * 60 * 1000
  });

  useEffect(() => {
    if (sessionQuery.data?.csrf_token) setCsrfToken(sessionQuery.data.csrf_token);
  }, [sessionQuery.data?.csrf_token]);

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
        ) : (
          <DashboardPage notify={notify} interval={session.check_interval} />
        )}
      </main>
      <footer>job-tracker / jobs.overbay.app / checks every {session.check_interval}h</footer>
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
      <img src="/static/logo.png" alt="" />
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
            <img src="/static/logo.png" alt="" />
          </span>
          <span>
            <span className="nav-logo-text">Job Tracker</span>
            <span className="nav-logo-sub">jobs.overbay.app</span>
          </span>
        </button>

        <div className="nav-right">
          <span className="nav-user">{session.user?.email}</span>
          {navLink("Dashboard", "/")}
          {navLink("Jobs", "/jobs")}
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
    onError: (error) => notify(messageFromError(error), "error")
  });

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authError = params.get("auth_error");
    if (!authError) return;

    notify(authError, "error");
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
  }, [mode, passwordLoginEnabled]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!passwordLoginEnabled) return;
    mutation.mutate();
  };

  return (
    <main className="auth-wrap">
      <section className="auth-card">
        <div className="auth-logo">
          <span className="auth-logo-mark">
            <img src="/static/logo.png" alt="" />
          </span>
          <span>
            <span className="auth-title">Job Tracker</span>
            <span className="auth-sub">Watch careers pages without babysitting tabs.</span>
          </span>
        </div>

        {passwordLoginEnabled && (
          <>
            <form onSubmit={submit}>
              <div className="field stacked">
                <label>Email</label>
                <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
              </div>
              <div className="field stacked">
                <label>Password</label>
                <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={mode === "register" ? 8 : undefined} />
              </div>
              <Button fullWidth type="submit" loading={mutation.isPending}>
                {mode === "login" ? "Log In" : "Create Account"}
              </Button>
            </form>

            <button className="auth-switch" type="button" onClick={() => setMode(mode === "login" ? "register" : "login")}>
              {mode === "login" ? "Need an account? Register" : "Already have an account? Log in"}
            </button>
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

function DashboardPage({ notify, interval }: { notify: (message: string, category?: Category) => void; interval: number }) {
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

  const handleAction = (action: ActionResponse) => {
    queryClient.setQueryData<Dashboard>(["dashboard"], (current) => applyActionToDashboard(current, action));
    if (action.message) notify(action.message, action.category);
  };

  const checkAll = useMutation({
    mutationFn: api.checkAll,
    onSuccess: handleAction,
    onError: (error) => notify(messageFromError(error), "error")
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
      notify(messageFromError(error), "error");
    }
  });

  const setWatchOrder = (watches: Watch[]) => {
    queryClient.setQueryData<Dashboard>(["dashboard"], (current) => {
      if (!current) return current;
      return {
        ...current,
        watches,
        stats: deriveStats(watches, current.stats.interval)
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
        <p className="page-sub">Alerts check every {interval} hours. New matches are emailed to your account address.</p>
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
      <div className="stat">
        <div className="stat-value">{stats.interval}h</div>
        <div className="stat-label">Check Interval</div>
      </div>
    </div>
  );
}

function CreateAlert({ notify, onAction }: { notify: (message: string, category?: Category) => void; onAction: (action: ActionResponse) => void }) {
  const [input, setInput] = useState<WatchInput>(emptyWatchInput);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);

  const create = useMutation({
    mutationFn: api.createWatch,
    onSuccess: (action) => {
      onAction(action);
      setInput(emptyWatchInput);
      setPreview(null);
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const previewMutation = useMutation({
    mutationFn: api.preview,
    onSuccess: setPreview,
    onError: (error) => notify(messageFromError(error), "error")
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
        <input value={input.keywords} onChange={(event) => onChange({ ...input, keywords: event.target.value })} placeholder="engineer, mechanical, product designer" />
        <div className="field-hint">Comma-separated. Matches any keyword in the job title. Leave blank to see all open roles.</div>
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
  onAction: (action: ActionResponse) => void;
  notify: (message: string, category?: Category) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [notifications, setNotifications] = useState(false);
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
      onAction(action);
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteWatch(watch.id),
    onSuccess: (action) => {
      setConfirmDelete(false);
      onAction(action);
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const faviconHost = domainFromUrl(watch.careers_url);

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

        <div className="watch-icon">
          {faviconHost ? (
            <img src={`https://www.google.com/s2/favicons?domain=${faviconHost}&sz=32`} alt="" />
          ) : (
            watch.company_name.slice(0, 1).toUpperCase()
          )}
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
          <IconButton onClick={toggleMenu} aria-label={`Alert settings for ${watch.company_name}`} aria-expanded={menuOpen} aria-haspopup="menu">
            <MoreVertical size={18} />
          </IconButton>
          <DropdownMenu open={menuOpen} present={menuPresence.present} closing={menuPresence.closing} onClose={closeMenu}>
            <MenuButton icon={<RefreshCcw size={16} />} label={check.isPending ? "Checking..." : "Check"} loading={check.isPending} onClick={() => check.mutate()} />
            <MenuButton icon={<Bell size={16} />} label="Notifications" onClick={() => { setNotifications(true); closeMenu(); }} />
            <MenuButton icon={<Edit3 size={16} />} label="Edit" onClick={() => { setEditing(true); closeMenu(); }} />
            <MenuButton danger icon={<Trash2 size={16} />} label="Delete" onClick={() => { setConfirmDelete(true); closeMenu(); }} />
          </DropdownMenu>
        </div>
      </div>

      <EditWatchModal watch={watch} open={editing} onClose={() => setEditing(false)} onAction={onAction} notify={notify} />
      <NotificationsModal watch={watch} open={notifications} onClose={() => setNotifications(false)} onAction={onAction} notify={notify} />
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
  return (
    <div className="job-row">
      <div className="job-dot" />
      <div className="job-title">
        {job.title}
        {job.location ? <span className="job-location-inline"> - {job.location}</span> : null}
      </div>
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
  onAction: (action: ActionResponse) => void;
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
      onAction(action);
      onClose();
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  const previewMutation = useMutation({
    mutationFn: () => api.preview(input),
    onSuccess: setPreview,
    onError: (error) => notify(messageFromError(error), "error")
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
  onAction: (action: ActionResponse) => void;
  notify: (message: string, category?: Category) => void;
}) {
  const [emailEnabled, setEmailEnabled] = useState(watch.email_enabled);

  useEffect(() => {
    if (open) setEmailEnabled(watch.email_enabled);
  }, [open, watch.email_enabled]);

  const mutation = useMutation({
    mutationFn: () => api.updateNotifications(watch.id, emailEnabled, watch.push_enabled),
    onSuccess: (action) => {
      onAction(action);
      onClose();
    },
    onError: (error) => notify(messageFromError(error), "error")
  });

  return (
    <Modal open={open} onClose={onClose} title="Notifications" subTitle={watch.company_name}>
      <div className="notification-options">
        <label className="notification-option">
          <span>
            <span className="notification-title">Email alerts</span>
            <span className="notification-copy">Send new matches to your account email.</span>
          </span>
          <input type="checkbox" checked={emailEnabled} onChange={(event) => setEmailEnabled(event.target.checked)} />
        </label>
        <label className="notification-option disabled">
          <span>
            <span className="notification-title">Browser push</span>
            <span className="notification-copy">Push delivery will use this device after subscription setup is added.</span>
          </span>
          <input type="checkbox" checked={watch.push_enabled} disabled readOnly />
        </label>
      </div>
      <div className="modal-actions">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button loading={mutation.isPending} icon={<Check size={16} />} onClick={() => mutation.mutate()}>
          Save Notifications
        </Button>
      </div>
    </Modal>
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

  useEffect(() => {
    if (jobsQuery.isError) notify(messageFromError(jobsQuery.error), "error");
  }, [jobsQuery.isError]);

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
                  <div className="job-table-meta">{job.company_name} / {job.location || "Location not listed"} / Found {formatDate(job.found_at || null)}</div>
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
