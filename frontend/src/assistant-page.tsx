import "./assistant-page.css";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { MessageCircleMore, Search, UserRound } from "lucide-react";
import { api } from "./api";
import {
  changeReply,
  replyBlocked,
  replyChanged,
  replyPending,
  replyVersion,
} from "./auto-reply";
import {
  PromptEditorCard,
  PromptStatus,
  type PromptSession,
} from "./prompt-editor";
import { relationshipBadge } from "./relationship-view";
import { Select, Toggle } from "./ui";
import {
  directoryName,
  WechatAvatar,
  WechatSelectValue,
} from "./wechat-directory";
import { userMessage, type NoticeKind } from "./user-messages";
import { useNotice } from "./notices";
import { Pagination } from "./pagination";
import { PanelHeading } from "./panel-heading";
import { ChatHistoryDialog } from "./chat-history";

type Row = Record<string, any>;
type Props = {
  accounts: Row[];
  busy: boolean;
  act: (f: () => Promise<any>, success?: string) => Promise<any>;
  show: (message: string, kind?: NoticeKind) => void;
  promptSession: PromptSession;
  ownerProfileSession: PromptSession;
  requestAssistantLeave: (
    action: () => void | Promise<void>,
  ) => Promise<boolean>;
};
type Query = {
  kind: "private" | "group";
  q: string;
  status: string;
  page: number;
  page_size: 10;
};
type Result = {
  items: Row[];
  total: number;
  page: number;
  page_size: number;
  server_time: number;
};
const initialQuery: Query = {
  kind: "private",
  q: "",
  status: "all",
  page: 1,
  page_size: 10,
};
const accountName = (a: Row) => a.remark || a.nickname || `账号 ${a.id}`;

export function AssistantPage({
  accounts,
  busy,
  act,
  show,
  promptSession,
  ownerProfileSession,
  requestAssistantLeave,
}: Props) {
  const [aid, setAid] = useState(promptSession.draft?.id || "");
  const [pendingAccount, setPendingAccount] = useState<{
    id: string;
    enabled: boolean;
  } | null>(null);
  const accountGuard = useRef(false);
  const account = accounts.find((a) => String(a.id) === aid);
  useLayoutEffect(() => {
    promptSession.sync(account as { id: number; prompt?: string } | undefined);
  }, [account?.id, account?.prompt, promptSession.sync]);
  useEffect(() => {
    if (!accounts.some((a) => String(a.id) === aid))
      setAid(accounts[0] ? String(accounts[0].id) : "");
  }, [accounts, aid]);
  const notice =
    account &&
    (!account.enabled
      ? "账号已停用"
      : account.online === 0
        ? "账号离线，暂时无法回复"
        : "");
  return (
    <div className="page assistant-page">
      <div className="assistant-toolbar">
        <div className="assistant-account-controls">
          <div className="assistant-account-select wechat-identity-select">
            {accounts.length ? (
              <Select
                label="选择账号"
                value={aid}
                triggerContent={
                  account ? (
                    <WechatSelectValue
                      item={account}
                      name={accountName(account)}
                    />
                  ) : undefined
                }
                options={accounts.map((a) => ({
                  value: String(a.id),
                  label: accountName(a) + (!a.enabled ? "（未启用）" : ""),
                }))}
                onValueChange={(next) =>
                  requestAssistantLeave(() => setAid(next))
                }
              />
            ) : (
              <div
                className="assistant-account-empty wechat-select-value"
                role="status"
              >
                <span className="avatar" aria-hidden="true">
                  <UserRound size={20} />
                </span>
                <span className="wechat-select-meta">
                  <strong>暂无微信账号</strong>
                </span>
              </div>
            )}
          </div>
          <label className="assistant-enable">
            {notice && (
              <span className="assistant-account-state" title={notice}>
                {!account?.enabled ? "已停用" : "离线"}
              </span>
            )}
            <span>
              {pendingAccount?.id === aid
                ? pendingAccount.enabled
                  ? "启用中"
                  : "停用中"
                : "启用助手"}
            </span>
            <Toggle
              label={
                pendingAccount?.id === aid
                  ? `当前账号助手${pendingAccount.enabled ? "启用中" : "停用中"}`
                  : "启用当前账号助手"
              }
              pending={pendingAccount?.id === aid}
              checked={!!account?.ai_enabled}
              disabled={busy || !account}
              onChange={() =>
                requestAssistantLeave(async () => {
                  if (accountGuard.current) return;
                  accountGuard.current = true;
                  const enabled = !account?.ai_enabled;
                  setPendingAccount({ id: aid, enabled });
                  try {
                    await act(
                      () =>
                        changeReply(
                          `/accounts/${aid}/automation`,
                          { ai_enabled: !account?.ai_enabled },
                          (row) =>
                            window.dispatchEvent(
                              new CustomEvent("auto-reply-account", {
                                detail: row,
                              }),
                            ),
                        ),
                      account?.ai_enabled
                        ? "当前账号助手已停用"
                        : "当前账号助手已启用",
                    );
                  } finally {
                    accountGuard.current = false;
                    setPendingAccount(null);
                  }
                })
              }
            />
          </label>
        </div>
      </div>
      <div className="assistant-columns">
        <div className="assistant-editors">
          <section
            className="assistant-column"
            id="assistant-panel-prompt"
            aria-labelledby="assistant-prompt-heading"
          >
            <PanelHeading
              title="助手设定"
              id="assistant-prompt-heading"
              className="assistant-column-heading"
            >
              <PromptStatus session={promptSession} />
            </PanelHeading>
            <PromptEditorCard
              account={account as { id: number; prompt?: string } | undefined}
              session={promptSession}
              onLoadError={() =>
                show("编辑器暂时无法加载，请刷新页面重试", "error")
              }
            />
          </section>
          <section
            className="assistant-column"
            id="assistant-panel-owner-profile"
            aria-labelledby="assistant-owner-profile-heading"
          >
            <PanelHeading
              title="号主资料"
              id="assistant-owner-profile-heading"
              className="assistant-column-heading"
            >
              <PromptStatus session={ownerProfileSession} />
            </PanelHeading>
            <PromptEditorCard
              account={
                account as
                  | { id: number; wxid?: string; owner_profile?: string }
                  | undefined
              }
              session={ownerProfileSession}
              label="号主资料"
              placeholder="填写允许向好友和群聊公开的号主资料"
              onLoadError={() =>
                show("编辑器暂时无法加载，请刷新页面重试", "error")
              }
            />
          </section>
        </div>
        <section
          className="assistant-column"
          id="assistant-panel-scope"
          aria-labelledby="assistant-scope-heading"
        >
          {account ? (
            <ReplyScope
              key={aid}
              aid={aid}
              busy={busy}
              act={act}
              sync={account.contacts}
            />
          ) : (
            <>
              <PanelHeading
                className="assistant-column-heading"
                id="assistant-scope-heading"
                title="回复范围"
              />
              <p className="assistant-empty">暂无可管理的微信账号</p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function ReplyScope({
  aid,
  busy,
  act,
  sync,
}: {
  aid: string;
  busy: boolean;
  act: Props["act"];
  sync?: { state?: string; updated_at?: number | null };
}) {
  const { reportFailure, recovered } = useNotice();
  const source = `reply-scope:${aid}`;
  const syncing =
    !!sync &&
    (sync.state === "queued" ||
      sync.state === "running" ||
      (!sync.updated_at && sync.state !== "failed"));
  const [query, setQuery] = useState<Query>(initialQuery);
  const [historyContact, setHistoryContact] = useState<Row | null>(null);
  const [search, setSearch] = useState("");
  const [composing, setComposing] = useState(false);
  const [cached, setCached] = useState<{ query: Query; result: Result } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [pendingChange, setPendingChange] = useState<{
    id: number;
    enabled: boolean;
  } | null>(null);
  const changeGuard = useRef(false);
  const [tick, setTick] = useState(Date.now());
  const received = useRef(Date.now());
  useEffect(() => {
    if (composing) return;
    const timer = window.setTimeout(
      () =>
        setQuery((old) =>
          old.q === search.trim() ? old : { ...old, q: search.trim(), page: 1 },
        ),
      300,
    );
    return () => clearTimeout(timer);
  }, [search, composing]);
  useEffect(() => {
    let live = true,
      running = false,
      timer = 0;
    let controller: AbortController | undefined;
    const schedule = () => {
      clearTimeout(timer);
      if (live) timer = window.setTimeout(read, 1000);
    };
    const read = async () => {
      if (!live || running) return;
      clearTimeout(timer);
      if (document.hidden || replyPending()) {
        schedule();
        return;
      }
      running = true;
      controller = new AbortController();
      const version = replyVersion();
      try {
        const params = new URLSearchParams(
          Object.entries(query).map(([key, value]) => [key, String(value)]),
        );
        const value: Result = await api(
          `/accounts/${aid}/reply-scope?${params}`,
          "GET",
          undefined,
          controller.signal,
        );
        if (!live || version !== replyVersion() || replyPending()) return;
        received.current = Date.now();
        setTick(received.current);
        setCached({ query: { ...query, page: value.page }, result: value });
        setError("");
        recovered(source);
        if (value.page !== query.page)
          setQuery((old) => ({ ...old, page: value.page }));
      } catch (failure) {
        if (
          live &&
          !controller.signal.aborted &&
          version === replyVersion() &&
          !replyPending()
        ) {
          const text = userMessage(failure, "回复范围暂时无法读取，请稍后重试");
          setError(text);
          reportFailure(source, text);
        }
      } finally {
        running = false;
        if (live) {
          setLoading(false);
          schedule();
        }
      }
    };
    setLoading(true);
    void read();
    const refresh = () => {
      void read();
    };
    window.addEventListener(replyChanged, refresh);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    const clock = window.setInterval(() => setTick(Date.now()), 1000);
    return () => {
      live = false;
      clearTimeout(timer);
      clearInterval(clock);
      controller?.abort();
      window.removeEventListener(replyChanged, refresh);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [aid, query]);
  const update = (patch: Partial<Query>) =>
    setQuery((old) => ({ ...old, ...patch, page: patch.page ?? 1 }));
  const current =
    !!cached && JSON.stringify(cached.query) === JSON.stringify(query);
  // 缓存用于读取失败时回看；新查询等待期间不能把旧对象放进新分类供操作。
  const result = current || error ? (cached?.result ?? null) : null;
  const resultQuery = result ? cached!.query : query;
  const pages = result ? Math.ceil(result.total / result.page_size) : 0;
  const unit = resultQuery.kind === "private" ? "位好友" : "个群聊";
  const toggle = async (row: Row, enabled: boolean, paused = false) => {
    if (changeGuard.current) return;
    changeGuard.current = true;
    setPendingChange({ id: row.id, enabled });
    try {
      await act(
        () =>
          changeReply(
            `/conversations/${row.id}/auto-reply`,
            { enabled },
            () => {},
          ),
        paused ? "回复已恢复" : undefined,
      );
    } finally {
      changeGuard.current = false;
      setPendingChange(null);
    }
  };
  return (
    <>
      <PanelHeading
        title="回复范围"
        id="assistant-scope-heading"
        className="assistant-column-heading"
      >
        {(syncing || sync?.state === "failed") && (
          <span role="status">
            {syncing ? "通讯录同步中" : "通讯录更新暂不可用"}
          </span>
        )}
      </PanelHeading>
      <section
        className="reply-scope"
        aria-label="回复范围"
        aria-busy={loading}
      >
        <div className="reply-scope-tools">
          <div className="reply-kind-tabs" role="group" aria-label="联系人分类">
            {[
              ["private", "好友"],
              ["group", "群聊"],
            ].map(([kind, name]) => (
              <button
                key={kind}
                aria-pressed={query.kind === kind}
                onClick={() => update({ kind: kind as Query["kind"] })}
              >
                {name}
              </button>
            ))}
          </div>
          <label className="reply-search">
            <Search size={17} aria-hidden="true" />
            <input
              aria-label="搜索回复范围"
              placeholder={`搜索${query.kind === "private" ? "好友" : "群聊"}昵称或备注`}
              maxLength={2000}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onCompositionStart={() => setComposing(true)}
              onCompositionEnd={(e) => {
                setComposing(false);
                setSearch(e.currentTarget.value);
              }}
            />
          </label>
          <Select
            label="回复状态筛选"
            value={query.status}
            onValueChange={(status) => update({ status })}
            options={[
              { value: "all", label: "全部状态" },
              { value: "enabled", label: "回复开启" },
              { value: "disabled", label: "回复关闭" },
              { value: "paused", label: "回复暂停" },
              {
                value: "removed",
                label: query.kind === "private" ? "好友已删除" : "已不在群聊",
              },
            ]}
          />
        </div>
        <div
          className="reply-scope-list"
          style={{ "--reply-page-size": query.page_size } as CSSProperties}
          role="region"
          aria-label="回复范围联系人"
          tabIndex={0}
        >
          {result?.items.map((row) => {
            const paused =
              !!row.allowed &&
              row.pause_until > result.server_time &&
              row.removed_at == null;
            const seconds = Math.max(
              0,
              row.pause_until -
                result.server_time -
                (tick - received.current) / 1000,
            );
            const badge = relationshipBadge(row);
            return (
              <div className="reply-scope-row" key={row.id}>
                <WechatAvatar item={row} />
                <div className="reply-contact-info">
                  <div className="reply-contact-name">
                    <strong title={directoryName(row)}>
                      {directoryName(row)}
                    </strong>
                    {paused && (
                      <span className="reply-pause-badge">回复暂停</span>
                    )}
                    {badge && (
                      <span className="relationship-badge">{badge}</span>
                    )}
                  </div>
                  {paused && (
                    <small>
                      {seconds > 0
                        ? `${Math.ceil(seconds / 60)} 分钟后自动恢复`
                        : "恢复状态确认中"}
                    </small>
                  )}
                </div>
                <div className="reply-contact-actions">
                  <button
                    type="button"
                    className="reply-history"
                    aria-label={`查看 ${directoryName(row)} 的聊天记录`}
                    onClick={() => setHistoryContact(row)}
                  >
                    <MessageCircleMore size={16} aria-hidden="true" />
                    聊天记录
                  </button>
                  <div className="reply-contact-action">
                    {paused ? (
                      <button
                        className="reply-resume"
                        disabled={
                          !current ||
                          !!error ||
                          busy ||
                          replyBlocked(row) ||
                          seconds <= 0
                        }
                        aria-label={
                          pendingChange?.id === row.id
                            ? `${directoryName(row)} 的回复恢复中`
                            : `恢复 ${directoryName(row)} 的回复`
                        }
                        aria-busy={pendingChange?.id === row.id}
                        onClick={() => toggle(row, true, true)}
                      >
                        {pendingChange?.id === row.id ? "恢复中" : "恢复回复"}
                      </button>
                    ) : (
                      <Toggle
                        checked={!!row.allowed}
                        label={
                          pendingChange?.id === row.id
                            ? `${directoryName(row)} 的回复${pendingChange?.enabled ? "开启中" : "关闭中"}`
                            : `允许 ${directoryName(row)}`
                        }
                        pending={pendingChange?.id === row.id}
                        disabled={
                          !current ||
                          !!error ||
                          busy ||
                          row.removed_at != null ||
                          replyBlocked(row)
                        }
                        onChange={() => toggle(row, !row.allowed)}
                      />
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          {!result && !error && (
            <p className="assistant-empty" role="status">
              回复范围读取中
            </p>
          )}
          {result && !result.items.length && (
            <p className="assistant-empty">
              {resultQuery.q || resultQuery.status !== "all"
                ? "没有符合条件的联系人"
                : syncing
                  ? "通讯录同步中"
                  : sync?.state === "failed"
                    ? "通讯录更新暂不可用，稍后自动重试"
                    : resultQuery.kind === "private"
                      ? "暂无好友"
                      : "暂无群聊"}
            </p>
          )}
        </div>
        <footer className="accounts-footer reply-scope-footer">
          <span className="accounts-total" role="status" aria-live="polite">
            {result && (
              <>
                共 <strong>{result.total}</strong> {unit}
              </>
            )}
          </span>
          <Pagination
            label="回复范围分页"
            page={result?.page ?? query.page}
            pageCount={pages}
            disabled={!current || !!error || !result}
            onPageChange={(page) => update({ page })}
          />
        </footer>
      </section>
      {historyContact && (
        <ChatHistoryDialog
          aid={aid}
          contact={historyContact}
          onClose={() => setHistoryContact(null)}
        />
      )}
    </>
  );
}
