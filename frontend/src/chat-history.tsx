import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { api } from "./api";
import { useNotice } from "./notices";
import { directoryName } from "./wechat-directory";
import { userMessage } from "./user-messages";
import { ChatText, copyChatText } from "./chat-content";
import { ChatImage, type HistoryImage } from "./chat-image";
import { ChatVoice, useVoicePlayback, type HistoryVoice } from "./chat-voice";
import { ClearChatHistory, type HistoryDeletion } from "./chat-history-clear";
import { ReplyContextDialog } from "./reply-context";
import "./chat-history.css";

type Message = {
  id: string;
  sender_name: string;
  timestamp: number;
  direction: string;
  source: string;
  msg_type: string;
  type_label: string;
  content: string;
  revoked: boolean;
  image?: HistoryImage | null;
  voice?: HistoryVoice | null;
};
type Result = {
  items: Message[];
  cursor: string | null;
  watermark: string;
  has_more: boolean;
  history_revision?: string;
  clearing?: HistoryDeletion | null;
};
type View = Result & { ready: boolean };
type Mode = "initial" | "older" | "newer";
type Anchor = { id?: string; offset?: number; top?: number; bottom?: boolean };
const empty: View = {
  items: [],
  cursor: null,
  watermark: "0",
  has_more: false,
  ready: false,
};
const clock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function merge(...groups: Message[][]) {
  const values = new Map(groups.flat().map((item) => [item.id, item]));
  return [...values.values()].sort(
    (a, b) =>
      a.timestamp - b.timestamp ||
      (BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0),
  );
}

function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return text;
  const pattern = new RegExp(
    `(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`,
    "gi",
  );
  return text
    .split(pattern)
    .map((part, index) => (index % 2 ? <mark key={index}>{part}</mark> : part));
}

export function ChatHistoryDialog({
  aid,
  contact,
  onClose,
}: {
  aid: string;
  contact: Record<string, any>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const { setHost, reportFailure, recovered } = useNotice();
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [composing, setComposing] = useState(false);
  const [view, setView] = useState<View>(empty);
  const current = useRef(view);
  const normal = useRef<{ view: View; top: number } | null>(null);
  const restore = useRef<Anchor | null>(null);
  const load = useRef<(mode: Mode) => void>(() => {});
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearVersion, setClearVersion] = useState(0);
  const [contextMessage, setContextMessage] = useState<{
    id: string;
    trigger: HTMLElement;
  } | null>(null);
  const endpoint = `/accounts/${aid}/conversations/${contact.id}/history`;
  const source = `chat-history:${aid}:${contact.id}`;
  const playback = useVoicePlayback(`${source}:${query}`);
  useEffect(() => {
    setContextMessage(null);
  }, [aid, contact.id, search, view.history_revision, clearing]);

  const anchor = (follow = false): Anchor => {
    const element = list.current;
    if (!element) return {};
    if (
      follow &&
      element.scrollHeight - element.clientHeight - element.scrollTop < 40
    )
      return { bottom: true };
    const top = element.getBoundingClientRect().top;
    const first = [
      ...element.querySelectorAll<HTMLElement>("[data-message-id]"),
    ].find((node) => node.getBoundingClientRect().bottom > top);
    return first
      ? {
          id: first.dataset.messageId,
          offset: first.getBoundingClientRect().top - top,
        }
      : { top: element.scrollTop };
  };
  const apply = (next: View, position: Anchor) => {
    restore.current = position;
    current.current = next;
    setView(next);
  };

  useEffect(() => {
    dialog.current?.showModal();
    setHost(dialog.current);
    return () => setHost(null);
  }, [setHost]);
  useEffect(() => {
    if (composing) return;
    const timer = window.setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search, composing]);
  useLayoutEffect(() => {
    const element = list.current,
      position = restore.current;
    if (!element || !position) return;
    restore.current = null;
    if (position.bottom) element.scrollTop = element.scrollHeight;
    else if (position.id) {
      const node = element.querySelector<HTMLElement>(
        `[data-message-id="${position.id}"]`,
      );
      if (node)
        element.scrollTop +=
          node.getBoundingClientRect().top -
          element.getBoundingClientRect().top -
          (position.offset || 0);
    } else element.scrollTop = position.top || 0;
  }, [view]);
  useEffect(() => {
    if (clearing) {
      normal.current = null;
      apply(empty, { top: 0 });
      setLoading(false);
      return;
    }
    let live = true,
      running = false,
      timer = 0;
    let controller: AbortController | undefined;
    let retry: Mode | null = null;
    setFailed(false);
    if (!query && normal.current) {
      apply(normal.current.view, { top: normal.current.top });
      setLoading(false);
    } else {
      apply(empty, { top: 0 });
      setLoading(true);
    }
    const schedule = () => {
      clearTimeout(timer);
      if (live && !document.hidden && (retry || !query))
        timer = window.setTimeout(
          () =>
            void read(retry || (current.current.ready ? "newer" : "initial")),
          5000,
        );
    };
    const read = async (requested: Mode) => {
      if (!live || running || document.hidden) return;
      let mode = current.current.ready ? requested : "initial";
      if (mode === "older" && !current.current.has_more) return;
      if (mode === "newer" && query) return;
      clearTimeout(timer);
      running = true;
      setLoading(true);
      controller = new AbortController();
      const signal = controller.signal;
      const original = current.current;
      try {
        let params = new URLSearchParams({ q: query, limit: "50" });
        if (mode === "older" && original.cursor)
          params.set("cursor", original.cursor);
        if (mode === "newer") params.set("after_id", original.watermark);
        let result: Result = await api(
          `${endpoint}?${params}`,
          "GET",
          undefined,
          signal,
        );
        if (
          original.ready &&
          result.history_revision !== original.history_revision
        ) {
          mode = "initial";
          normal.current = null;
          result = await api(
            `${endpoint}?${new URLSearchParams({ q: query, limit: "50" })}`,
            "GET",
            undefined,
            signal,
          );
        }
        let items = result.items;
        let snapshotRevision = result.history_revision;
        // 搜索额度及新增消息均有续页；必须继续读完当前批次，不能把空批次当成搜索结束。
        while (result.has_more && (mode === "newer" || items.length < 50)) {
          if (!live || signal.aborted) return;
          params = new URLSearchParams({
            q: query,
            limit:
              mode === "newer" ? "50" : String(Math.max(1, 50 - items.length)),
          });
          if (mode === "newer") params.set("after_id", result.watermark);
          else if (result.cursor) params.set("cursor", result.cursor);
          else break;
          result = await api(`${endpoint}?${params}`, "GET", undefined, signal);
          if (result.history_revision !== snapshotRevision) {
            normal.current = null;
            mode = "initial";
            result = await api(
              `${endpoint}?${new URLSearchParams({ q: query, limit: "50" })}`,
              "GET",
              undefined,
              signal,
            );
            items = result.items;
            snapshotRevision = result.history_revision;
            continue;
          }
          items = merge(items, result.items);
        }
        if (!live || signal.aborted) return;
        let next: View;
        if (mode === "newer") {
          // 同时更新最近记录的撤回和已完成转写，不覆盖正在阅读的历史页。
          const latest: Result = await api(
            `${endpoint}?limit=50`,
            "GET",
            undefined,
            signal,
          );
          if (!live || signal.aborted) return;
          if (latest.history_revision !== original.history_revision)
            normal.current = null;
          next =
            latest.history_revision !== original.history_revision
              ? { ...latest, ready: true }
              : {
                  ...original,
                  items: merge(original.items, items, latest.items),
                  watermark: result.watermark,
                  clearing: latest.clearing,
                };
        } else
          next = {
            ...result,
            items: mode === "older" ? merge(original.items, items) : items,
            ready: true,
          };
        apply(
          next,
          mode === "initial" ? { bottom: true } : anchor(mode === "newer"),
        );
        retry = null;
        setFailed(false);
        recovered(source);
      } catch (error) {
        if (live && !signal.aborted) {
          retry = mode;
          setFailed(true);
          reportFailure(
            source,
            userMessage(error, "聊天记录暂时无法读取，请稍后重试"),
          );
        }
      } finally {
        running = false;
        if (live) {
          setLoading(false);
          schedule();
        }
      }
    };
    load.current = (mode) => {
      void read(mode);
    };
    const visibility = () => {
      clearTimeout(timer);
      if (document.hidden) controller?.abort();
      else if (!running)
        void read(retry || (current.current.ready ? "newer" : "initial"));
    };
    const focus = () => {
      if (!document.hidden) visibility();
    };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("focus", focus);
    void read(current.current.ready && !query ? "newer" : "initial");
    return () => {
      if (!query && current.current.ready)
        normal.current = {
          view: current.current,
          top: list.current?.scrollTop || 0,
        };
      live = false;
      clearTimeout(timer);
      controller?.abort();
      load.current = () => {};
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("focus", focus);
    };
  }, [
    endpoint,
    query,
    source,
    recovered,
    reportFailure,
    clearing,
    clearVersion,
  ]);
  useEffect(() => {
    const element = list.current;
    if (
      view.ready &&
      view.has_more &&
      !loading &&
      !failed &&
      element &&
      element.scrollHeight <= element.clientHeight
    )
      load.current("older");
  }, [view, loading, failed]);
  const close = () => {
    playback.stop();
    dialog.current?.close();
    onClose();
  };
  return (
    <dialog
      ref={dialog}
      className="account-dialog chat-history-dialog"
      aria-labelledby="chat-history-title"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === dialog.current) close();
      }}
    >
      <header>
        <h2 id="chat-history-title" title={directoryName(contact)}>
          {directoryName(contact)} · 聊天记录
        </h2>
        <span className="chat-history-state" role="status">
          {clearing
            ? "清空中"
            : failed
              ? "读取失败，正在重试"
              : loading
                ? query
                  ? "搜索中"
                  : "读取中"
                : ""}
        </span>
        <button type="button" aria-label="关闭聊天记录" onClick={close}>
          <X size={19} />
        </button>
      </header>
      <div className="chat-history-tools">
        <label className="reply-search">
          <Search size={17} aria-hidden="true" />
          <input
            type="search"
            aria-label="搜索聊天记录"
            placeholder="搜索聊天内容"
            value={search}
            disabled={clearing}
            onChange={(event) => setSearch(event.target.value)}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={(event) => {
              setSearch(event.currentTarget.value);
              setComposing(false);
            }}
          />
        </label>
        <ClearChatHistory
          endpoint={endpoint}
          name={directoryName(contact)}
          detected={view.clearing}
          onBusy={(busy) => {
            if (busy) playback.stop();
            setClearing(busy);
          }}
          onCleared={() => {
            normal.current = null;
            apply(empty, { bottom: true });
            setSearch("");
            setQuery("");
            setClearing(false);
            setClearVersion((version) => version + 1);
          }}
        />
      </div>
      <div
        ref={list}
        className="chat-history-list"
        role="region"
        aria-label="聊天记录正文"
        aria-busy={loading}
        tabIndex={0}
        onCopy={copyChatText}
        onScroll={() => {
          if (
            list.current &&
            list.current.scrollTop < 80 &&
            !loading &&
            !failed
          )
            load.current("older");
        }}
      >
        {view.ready && !view.has_more && view.items.length > 0 && (
          <p className="chat-history-boundary">
            已显示全部{query ? "匹配" : ""}记录
          </p>
        )}
        {view.items.map((item) => (
          <article
            className={`chat-history-message ${item.direction === "outbound" ? "outbound" : "inbound"}`}
            key={item.id}
            data-message-id={item.id}
          >
            <div className="chat-history-meta">
              <strong>{item.sender_name}</strong>
              {item.source === "auto" && <span>助手</span>}
              <time dateTime={new Date(item.timestamp * 1000).toISOString()}>
                {clock.format(new Date(item.timestamp * 1000))}
              </time>
              {item.revoked && (
                <span className="chat-history-revoked">已撤回</span>
              )}
            </div>
            <div
              className={`chat-history-bubble${item.voice ? " chat-voice-bubble" : ""}`}
              role={item.source === "auto" ? "button" : undefined}
              tabIndex={item.source === "auto" ? 0 : undefined}
              aria-label={
                item.source === "auto" ? "查看 AI 回复上下文" : undefined
              }
              title={
                item.source === "auto"
                  ? "点击查看本条回复的 AI 上下文"
                  : undefined
              }
              onClick={
                item.source === "auto"
                  ? (event) => {
                      if (
                        window.getSelection()?.toString() ||
                        (event.target as HTMLElement).closest("button, a")
                      )
                        return;
                      setContextMessage({
                        id: item.id,
                        trigger: event.currentTarget,
                      });
                    }
                  : undefined
              }
              onKeyDown={
                item.source === "auto"
                  ? (event) => {
                      if (
                        event.target === event.currentTarget &&
                        (event.key === "Enter" || event.key === " ")
                      ) {
                        event.preventDefault();
                        setContextMessage({
                          id: item.id,
                          trigger: event.currentTarget,
                        });
                      }
                    }
                  : undefined
              }
            >
              {item.voice && (
                <ChatVoice
                  voice={item.voice}
                  playback={playback}
                  query={query}
                />
              )}
              {item.image && (
                <ChatImage key={item.image.url} image={item.image} />
              )}
              {item.msg_type !== "1" && !item.image && !item.voice && (
                <span className="chat-history-type">
                  <Highlight text={`[${item.type_label}]`} query={query} />
                </span>
              )}
              {(item.voice ? item.voice.transcript : item.content) && (
                <div className="chat-history-text">
                  <ChatText
                    text={item.voice ? item.voice.transcript : item.content}
                    query={query}
                  />
                </div>
              )}
            </div>
          </article>
        ))}
        {!view.items.length && (
          <p className="chat-history-empty">
            {clearing
              ? "聊天记录清空中"
              : loading
                ? query
                  ? "聊天记录搜索中"
                  : "聊天记录读取中"
                : failed
                  ? "聊天记录暂时无法读取"
                  : query
                    ? "没有匹配的聊天记录"
                    : "暂无已保存的聊天记录"}
          </p>
        )}
      </div>
      <audio ref={playback.element} preload="none" hidden />
      {contextMessage && (
        <ReplyContextDialog
          key={`${source}:${contextMessage.id}:${view.history_revision}`}
          endpoint={`${endpoint}/${contextMessage.id}/context`}
          trigger={contextMessage.trigger}
          onClose={() => setContextMessage(null)}
        />
      )}
    </dialog>
  );
}
