import { FilingNotice } from "./filing-notice";
import { useContentMotion } from "./motion";
import { NoticeProvider, useNotice } from "./notices";
import { Login } from "./authentication";
import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import { usePromptSession, type PromptSession } from "./prompt-editor";
import {
  Users,
  Sparkles,
  Settings,
  ArrowUpRight,
  LoaderCircle,
  Info,
} from "lucide-react";
import { api, ApiError, setCsrf, sessionChangeKey } from "./api";
import { userMessage, type NoticeKind } from "./user-messages";
import {
  changeReply,
  replyVersion,
  replyPending,
  useReplyPolling,
} from "./auto-reply";
import { AssistantPage } from "./assistant-page";
import { fitTableRows, Pagination } from "./pagination";
import { Select } from "./ui";
import { PanelHeading } from "./panel-heading";
import { ModelSettings } from "./model-settings";
import { WechatAvatar } from "./wechat-directory";
import { AccountMenu, WechatSettings } from "./account-menu";
import brandMark from "./assets/qingda-logo.png";
import "./style.css";

type Row = Record<string, any>;
const nav = [
  ["accounts", "微信管理", Users],
  ["automation", "智能助手", Sparkles],
  ["settings", "系统设置", Settings],
] as const;
const accountRefreshTime = (value: unknown) => {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0)
    return "—";
  const date = new Date(value * 1000);
  if (!Number.isFinite(date.getTime())) return "—";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(date);
  const part = (name: string) => parts.find((p) => p.type === name)?.value;
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
};
const title = (a?: Row) =>
  a?.remark || a?.nickname || (a ? `账号 ${a.id}` : "全部账号");

function Empty({ text, description }: { text: string; description?: string }) {
  return (
    <div className="empty">
      <Info size={30} />
      <h3>{text}</h3>
      {description && <p>{description}</p>}
    </div>
  );
}

function App() {
  const [session, setSession] = useState<Row | null | undefined>(undefined);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const [page, setPage] = useState(() => {
    const savedPage = sessionStorage.getItem("wechat-page");
    return nav.some(([id]) => id === savedPage) ? savedPage! : "accounts";
  });
  const [accounts, setAccounts] = useState<Row[]>([]);
  const { show, clear: clearNotice } = useNotice();
  const showLoginNotice = useCallback(
    (text: string, kind: NoticeKind = "error") => {
      if (text) show(text, kind);
      else clearNotice();
    },
    [show, clearNotice],
  );
  const [busy, setBusy] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const logoutGuard = useRef(false);
  const [revision, setRevision] = useState(0);
  const contentMotion = useContentMotion<HTMLElement>(
    `${page}|${Boolean(session)}`,
    ":scope > :not(.topbar)",
  );
  const pageRef = useRef(page);
  pageRef.current = page;
  const promptSession = usePromptSession(session?.user_id, (row) => {
    window.dispatchEvent(
      new CustomEvent("auto-reply-account", { detail: row }),
    );
  });
  const ownerProfileSession = usePromptSession(
    session?.user_id,
    (row) => {
      window.dispatchEvent(
        new CustomEvent("auto-reply-account", { detail: row }),
      );
    },
    "owner-profile",
  );
  const requestAssistantLeave = async (action: () => void | Promise<void>) => {
    let left = false;
    await promptSession.requestLeave(async () => {
      left = await ownerProfileSession.requestLeave(action);
    });
    return left;
  };
  const refresh = async () => {
    const owner = sessionRef.current?.user_id;
    const stamp = replyVersion();
    const a = await api("/accounts");
    if (owner !== sessionRef.current?.user_id) return;
    // 微信页自行核实后发布结果，避免全局初始化的旧快照覆盖新资料。
    if (
      pageRef.current !== "accounts" &&
      stamp === replyVersion() &&
      !replyPending()
    )
      setAccounts(a);
    setRevision((r) => r + 1);
  };
  useReplyPolling(
    () => api("/accounts"),
    setAccounts,
    [page, session],
    undefined,
    Boolean(session) && page === "automation",
  );
  useEffect(() => {
    const update = (event: Event) => {
      const row = (event as CustomEvent).detail;
      setAccounts((old) =>
        old.map((a) => (a.id === row.id ? { ...a, ...row } : a)),
      );
    };
    window.addEventListener("auto-reply-account", update);
    return () => window.removeEventListener("auto-reply-account", update);
  }, []);
  const act = async (fn: () => Promise<any>, success?: string) => {
    setBusy(true);
    try {
      const r = await fn();
      if (success) show(success, "success");
      try {
        if (sessionRef.current === null) return r;
        await refresh();
      } catch {
        // 操作已经成功，后续读取失败不能改变提交结果或引导重复提交。
        show("操作已完成，请刷新页面");
      }
      return r;
    } catch (e) {
      show(userMessage(e), "error");
      await refresh().catch(() => {});
      return null;
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    sessionStorage.setItem("wechat-page", page);
  }, [page]);
  useEffect(() => {
    api("/auth/session")
      .then((s) => {
        setCsrf(s.csrf);
        setSession(s);
      })
      .catch((error: unknown) => {
        setSession(null);
        if (!(error instanceof ApiError && error.status === 401))
          showLoginNotice(userMessage(error));
      });
    const expired = () => {
      clearNotice();
      if (sessionRef.current) showLoginNotice("登录已过期，请重新登录");
      sessionRef.current = null;
      setSession(null);
      setAccounts([]);
      setCsrf("");
    };
    const changed = (event: StorageEvent) => {
      if (event.key !== sessionChangeKey) return;
      // 重建组件树，避免另一账号复用旧页面的读取与表单状态。
      location.reload();
    };
    window.addEventListener("session-expired", expired);
    window.addEventListener("storage", changed);
    return () => {
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("storage", changed);
    };
  }, []);
  useEffect(() => {
    if (session) refresh().catch((e) => show(userMessage(e), "error"));
  }, [session]);
  if (session === undefined)
    return (
      <div className="boot">
        <LoaderCircle className="spin" />
        管理台加载中
      </div>
    );
  if (!session)
    return (
      <>
        <Login
          onNotice={showLoginNotice}
          onLogin={(s) => {
            showLoginNotice("");
            clearNotice();
            setCsrf(s.csrf);
            setSession(s);
          }}
        />
      </>
    );
  const props = {
    accounts,
    busy,
    act,
    show,
    revision,
    promptSession,
    ownerProfileSession,
    requestAssistantLeave,
  };
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src={brandMark} alt="" width="36" height="36" />
          <div>
            青答<small>微信智能助手</small>
          </div>
        </div>
        <nav>
          {nav.map(([id, text, Icon]) => (
            <button
              key={id}
              className={page === id ? "active" : ""}
              onClick={() => {
                if (page !== id) requestAssistantLeave(() => setPage(id));
              }}
            >
              <Icon size={20} />
              <span>{text}</span>
            </button>
          ))}
        </nav>
        <AccountMenu
          disabled={busy}
          loggingOut={loggingOut}
          user={session || {}}
          onUser={setSession}
          onLogout={async () => {
            if (logoutGuard.current) return;
            logoutGuard.current = true;
            try {
              await requestAssistantLeave(async () => {
                setLoggingOut(true);
                await act(async () => {
                  await api("/auth/logout", "POST", {});
                  promptSession.clear();
                  ownerProfileSession.clear();
                  setCsrf("");
                  sessionRef.current = null;
                  clearNotice();
                  setSession(null);
                });
              });
            } finally {
              logoutGuard.current = false;
              setLoggingOut(false);
            }
          }}
        />
      </aside>
      <main ref={contentMotion}>
        <header className="topbar">
          <div>
            <h1>{nav.find((n) => n[0] === page)?.[1]}</h1>
          </div>
          <div className="top-actions">
            <a
              className="official-platform-link"
              href="https://newmanager.wechatapi.net/dashboard/"
              target="_blank"
              rel="noopener noreferrer"
            >
              官方平台
              <ArrowUpRight size={15} />
            </a>
          </div>
        </header>
        {page === "accounts" && (
          <Accounts {...props} onAccountsChange={setAccounts} />
        )}
        {page === "automation" && (
          <div className="automation-scroll">
            <Automation {...props} />
          </div>
        )}
        {page === "settings" && <SettingsPage {...props} />}
        <FilingNotice />
      </main>
    </div>
  );
}

type Props = {
  promptSession: PromptSession;
  ownerProfileSession: PromptSession;
  requestAssistantLeave: (
    action: () => void | Promise<void>,
  ) => Promise<boolean>;
  accounts: Row[];
  busy: boolean;
  act: (f: () => Promise<any>, s?: string) => Promise<any>;
  show: (s: string, kind?: NoticeKind) => void;
  revision: number;
};

function Accounts({
  accounts,
  show,
  onAccountsChange,
}: Props & {
  onAccountsChange: (update: Row[] | ((current: Row[]) => Row[])) => void;
}) {
  const showRef = useRef(show);
  showRef.current = show;
  const pageRef = useRef<HTMLDivElement>(null);
  const [pagination, setPagination] = useState({
    page: 1,
    pageSize: 10,
    rowHeight: 56,
    desktop: false,
  });
  const { pageSize, rowHeight, desktop } = pagination;
  const pageCount = Math.ceil(accounts.length / pageSize);
  const currentPage = Math.min(pagination.page, Math.max(1, pageCount));
  const visibleAccounts = accounts.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );
  useEffect(() => {
    setPagination((previous) =>
      previous.page === currentPage
        ? previous
        : {
            ...previous,
            page: currentPage,
          },
    );
  }, [pageCount]);
  useLayoutEffect(() => {
    const page = pageRef.current!;
    const panel = page.querySelector<HTMLElement>(".panel")!;
    const measure = () => {
      // 与卡片样式共用 CSS 标记，空列表也能识别布局，不维护第二套断点。
      const desktop =
        getComputedStyle(panel)
          .getPropertyValue("--account-table-layout")
          .trim() === "1";
      const style = getComputedStyle(page);
      const border = getComputedStyle(panel);
      const timestamp = panel.querySelector(
        ".account-refreshed-at time[datetime]",
      );
      let footerEnd = 26;
      if (desktop && timestamp) {
        // 按实际日期文字边缘对齐，兼容字体、窗口缩放及时间刷新。
        const range = document.createRange();
        range.selectNodeContents(timestamp);
        footerEnd =
          panel.getBoundingClientRect().right -
          parseFloat(border.borderRightWidth) -
          range.getBoundingClientRect().right;
      }
      page.style.setProperty(
        "--accounts-footer-end",
        `${Math.max(16, footerEnd)}px`,
      );
      const available =
        document.documentElement.clientHeight -
        (page.getBoundingClientRect().top + window.scrollY) -
        parseFloat(style.paddingTop) -
        parseFloat(style.paddingBottom) -
        parseFloat(border.borderTopWidth) -
        parseFloat(border.borderBottomWidth) -
        40 -
        48;
      const { pageSize, rowHeight } = fitTableRows(available, desktop);
      setPagination((previous) => {
        if (
          previous.pageSize === pageSize &&
          previous.rowHeight === rowHeight &&
          previous.desktop === desktop
        )
          return previous;
        const first = (previous.page - 1) * previous.pageSize;
        return {
          page: Math.floor(first / pageSize) + 1,
          pageSize,
          rowHeight,
          desktop,
        };
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(page);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [accounts, currentPage]);
  useEffect(() => {
    let live = true;
    const load = async () => {
      let error: Error | undefined;
      try {
        const rows: Row[] = await api("/accounts");
        if (!live) return;
        onAccountsChange(rows);
        await Promise.all(
          rows.map(async (a) => {
            if (!live) return;
            try {
              await api("/accounts/" + a.id + "/online", "POST", {});
            } catch (e) {
              if (e instanceof ApiError && e.status === 401) live = false;
              error ??= e as Error;
            }
          }),
        );
        if (!live) return;
        const updated: Row[] = await api("/accounts");
        if (live) onAccountsChange(updated);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          live = false;
          return;
        }
        error ??= e as Error;
      }
      if (live && error) showRef.current(userMessage(error), "error");
    };
    void load();
    return () => {
      live = false;
    };
  }, [onAccountsChange]);
  const nickname = (a: Row) => a.nickname || a.profile?.nickName || "微信用户";
  const loginState = (a: Row) => {
    if (
      [
        "qr_ready",
        "waiting_scan",
        "waiting_confirm",
        "verification_required",
        "verifying",
        "starting",
      ].includes(a.login_state)
    )
      return "登录中";
    if (a.login_state === "logout_unknown") return "状态待确认";
    return a.online === 1 ? "在线" : a.online === 0 ? "离线" : "状态待确认";
  };
  return (
    <div
      ref={pageRef}
      className={`page accounts-page${desktop ? " accounts-table-mode" : ""}`}
      style={
        {
          "--account-row-height": `${desktop && visibleAccounts.length === pageSize ? rowHeight : 56}px`,
        } as React.CSSProperties
      }
    >
      <section className="panel" aria-label="微信列表">
        {accounts.length ? (
          <div role="table" aria-label="微信账号列表">
            <div role="rowgroup" className="accounts-head">
              <div role="row" className="accounts-header">
                {[
                  "头像",
                  "昵称",
                  "微信号",
                  "手机号",
                  "性别",
                  "个性签名",
                  "登录状态",
                  "最近更新时间",
                ].map((name) => (
                  <span
                    role="columnheader"
                    key={name}
                    title={
                      name === "最近更新时间"
                        ? "最近一次成功查询并保存账号在线或离线状态的时间（北京时间）"
                        : undefined
                    }
                  >
                    {name}
                  </span>
                ))}
              </div>
            </div>
            <div role="rowgroup">
              {visibleAccounts.map((a) => {
                const state = loginState(a);
                const sex = a.profile?.sex;
                const gender =
                  sex === 1 || sex === "1"
                    ? "男"
                    : sex === 2 || sex === "2"
                      ? "女"
                      : "—";
                const signature =
                  typeof a.profile?.signature === "string" &&
                  a.profile.signature.trim()
                    ? a.profile.signature.trim()
                    : "—";
                const refreshedAt = accountRefreshTime(
                  a.last_successful_refresh_at,
                );
                const mobile =
                  typeof a.profile?.mobile === "string" &&
                  a.profile.mobile.trim()
                    ? a.profile.mobile
                        .trim()
                        .replace(/^(\d{3})\d{4}(\d{4})$/, "$1****$2")
                    : "—";
                return (
                  <div role="row" className="account-row" key={a.id}>
                    <div role="presentation" className="account-identity">
                      <div role="cell" className="account-avatar-cell">
                        <WechatAvatar
                          item={{ ...a, display_name: nickname(a) }}
                          className="account-avatar"
                        />
                      </div>
                      <div role="cell" className="account-nickname">
                        <strong title={nickname(a)}>{nickname(a)}</strong>
                      </div>
                      <div role="cell" className="account-wechat-id">
                        <small
                          title={a.profile?.alias || a.wxid}
                          data-empty={!a.profile?.alias && !a.wxid}
                        >
                          {a.profile?.alias || a.wxid || "—"}
                        </small>
                      </div>
                    </div>
                    <div role="cell" className="account-detail account-mobile">
                      <span className="account-cell-label" aria-hidden="true">
                        手机号
                      </span>
                      <span
                        className="account-value"
                        data-empty={mobile === "—"}
                      >
                        {mobile}
                      </span>
                    </div>
                    <div role="cell" className="account-detail account-gender">
                      <span className="account-cell-label" aria-hidden="true">
                        性别
                      </span>
                      <span
                        className="account-value"
                        data-empty={gender === "—"}
                      >
                        {gender}
                      </span>
                    </div>
                    <div
                      role="cell"
                      className="account-detail account-signature"
                    >
                      <span className="account-cell-label" aria-hidden="true">
                        个性签名
                      </span>
                      <span
                        className="account-value"
                        title={signature}
                        data-empty={signature === "—"}
                      >
                        {signature}
                      </span>
                    </div>
                    <div role="cell" className="account-state">
                      <span className="account-cell-label" aria-hidden="true">
                        登录状态
                      </span>
                      <span
                        className={
                          "badge " +
                          (state === "在线"
                            ? "accepted"
                            : state === "离线"
                              ? "canceled"
                              : "unknown")
                        }
                      >
                        <span
                          className="account-status-dot"
                          aria-hidden="true"
                        />
                        {state}
                      </span>
                    </div>
                    <div
                      role="cell"
                      className="account-detail account-refreshed-at"
                    >
                      <span className="account-cell-label" aria-hidden="true">
                        最近更新时间
                      </span>
                      <time
                        className="account-value"
                        data-empty={refreshedAt === "—"}
                        dateTime={
                          refreshedAt === "—"
                            ? undefined
                            : new Date(
                                a.last_successful_refresh_at * 1000,
                              ).toISOString()
                        }
                      >
                        {refreshedAt}
                      </time>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <Empty
            text="还没有接入的微信"
            description="请在官方平台完成登录，并将消息回调配置到本平台。"
          />
        )}
        <footer className="accounts-footer">
          <span className="accounts-total">
            共 <strong>{accounts.length}</strong> 条
          </span>
          <Pagination
            label="微信账号分页"
            page={currentPage}
            pageCount={pageCount}
            onPageChange={(page) =>
              setPagination((previous) => ({ ...previous, page }))
            }
          />
        </footer>
      </section>
    </div>
  );
}

function Automation(props: Props) {
  return <AssistantPage {...props} />;
}

function SettingsPage({ busy, show }: Props) {
  return (
    <div className="page settings-page">
      <div className="settings-grid">
        <WechatSettings busy={busy} show={show} />
        <ModelSettings busy={busy} />
      </div>
    </div>
  );
}

const root = createRoot(document.getElementById("root")!);
root.render(
  <NoticeProvider>
    <App />
  </NoticeProvider>,
);
