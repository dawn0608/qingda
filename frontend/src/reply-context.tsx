import { useEffect, useRef, useState } from "react";
import { Copy, X } from "lucide-react";
import { api } from "./api";
import { ChatImage } from "./chat-image";
import { useNotice } from "./notices";
import "./reply-context.css";

type Value = Record<string, any>;
type Call = {
  call_id: string;
  kind: string;
  stage: string;
  name?: string;
  model?: string;
  available?: boolean;
  legacy?: boolean;
  tool?: Value;
  status?: number;
};
type Snapshot = {
  status: "complete" | "partial" | "unavailable";
  missing: string[];
  input: {
    system: string[];
    data: Value;
    images: Value[];
    size?: Value;
  } | null;
  calls: Call[];
  cursor: string | null;
  has_more: boolean;
};
const dates = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const labels: Record<string, string> = {
  conversation_history: "相关历史对话",
  group_background: "群聊背景",
  current_messages: "当前消息",
  pending_messages: "候选消息",
  interaction_signals: "互动信号",
  participant_profiles: "参与者资料",
  wait_source: "等待材料来源",
  legacy_context: "已有上下文清单",
  content: "正文",
  original_content: "原文",
  normalized_content: "表情释义",
  sender: "发送人",
  nickname: "昵称",
  speaker_type: "发言身份",
  reference: "引用",
  recent_images: "近期图片来源",
  recent_media: "近期媒体来源",
  input_scope: "输入范围",
  context_scope: "历史范围",
  reply_target: "回复对象",
  conversation_kind: "会话类型",
  message_id: "消息编号",
  timestamp: "发送时间",
  processing_state: "处理状态",
  requester: "提问者",
  owner: "号主",
  assistant: "助手",
  markdown: "资料正文",
  configured: "已配置",
  profile: "资料",
  media_status: "媒体状态",
  received_at: "接收时间",
  reply_to_sender: "回复对象",
  history_round: "历史轮次",
  message_kind: "消息类型",
};
const reasons: Record<string, string> = {
  round_limit: "轮次上限",
  message_limit: "消息数量上限",
  token_limit: "上下文容量上限",
  duplicate_round: "重复轮次",
  background: "群聊背景容量",
  history: "历史容量",
  optional_material: "可选资料容量",
};

function display(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function Content({ value }: { value: unknown }) {
  if (value == null) return <span className="reply-context-muted">未提供</span>;
  if (Array.isArray(value))
    return value.length ? (
      <ol className="reply-context-items">
        {value.map((item, index) => (
          <li key={index}>
            <Content value={item} />
          </li>
        ))}
      </ol>
    ) : (
      <span className="reply-context-muted">未加载</span>
    );
  if (typeof value !== "object")
    return (
      <div className="reply-context-text">
        {typeof value === "boolean" ? (value ? "是" : "否") : String(value)}
      </div>
    );
  return (
    <dl className="reply-context-fields">
      {Object.entries(value).map(([key, item]) => (
        <div key={key}>
          <dt>{labels[key] || key}</dt>
          <dd>
            {key === "timestamp" && typeof item === "number" ? (
              dates.format(new Date(item * 1000))
            ) : (
              <Content value={item} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Raw({ title, value }: { title: string; value: unknown }) {
  const { show } = useNotice();
  return (
    <details className="reply-context-raw">
      <summary>{title}</summary>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard.writeText(display(value)).then(
            () => show("已复制"),
            () => show("复制失败，请选择文字复制", "error"),
          );
        }}
      >
        <Copy size={14} aria-hidden="true" />
        复制
      </button>
      <pre>{display(value)}</pre>
    </details>
  );
}

export function ReplyContextDialog({
  endpoint,
  trigger,
  onClose,
}: {
  endpoint: string;
  trigger: HTMLElement | null;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const { setHost } = useNotice();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("context");
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Value | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);
  const [moreLoading, setMoreLoading] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);
  const requests = useRef(new Set<AbortController>());
  const live = useRef(true);
  const close = () => {
    dialog.current?.close();
    onClose();
  };
  useEffect(() => {
    live.current = true;
    const previous = trigger?.closest("dialog") || null;
    dialog.current?.showModal();
    setHost(dialog.current);
    return () => {
      live.current = false;
      for (const request of requests.current) request.abort();
      setHost(previous);
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    };
  }, [trigger, setHost]);
  useEffect(() => {
    const controller = new AbortController();
    requests.current.add(controller);
    setLoading(true);
    setFailed(false);
    setSnapshot(null);
    setSelected(null);
    setDetail(null);
    void api(endpoint, "GET", undefined, controller.signal)
      .then((result: Snapshot) => {
        if (!controller.signal.aborted && live.current) setSnapshot(result);
      })
      .catch(() => {
        if (!controller.signal.aborted && live.current) setFailed(true);
      })
      .finally(() => {
        requests.current.delete(controller);
        if (!controller.signal.aborted && live.current) setLoading(false);
      });
    return () => controller.abort();
  }, [endpoint, retry]);
  useEffect(() => {
    setDetail(null);
    setDetailFailed(false);
    if (!selected) return;
    const legacy = snapshot?.calls.find(
      (call) => call.call_id === selected && call.legacy,
    );
    if (legacy) {
      setDetail({ tool: legacy.tool });
      return;
    }
    const controller = new AbortController();
    requests.current.add(controller);
    void api(
      `${endpoint}?${new URLSearchParams({ call_id: selected })}`,
      "GET",
      undefined,
      controller.signal,
    )
      .then((result: Value) => {
        if (!controller.signal.aborted && live.current) setDetail(result);
      })
      .catch(() => {
        if (!controller.signal.aborted && live.current) setDetailFailed(true);
      })
      .finally(() => requests.current.delete(controller));
    return () => controller.abort();
  }, [endpoint, selected]);
  const more = async () => {
    if (!snapshot?.cursor || moreLoading) return;
    const controller = new AbortController();
    requests.current.add(controller);
    setMoreLoading(true);
    setMoreFailed(false);
    try {
      const next: Snapshot = await api(
        `${endpoint}?${new URLSearchParams({ cursor: snapshot.cursor })}`,
        "GET",
        undefined,
        controller.signal,
      );
      if (!controller.signal.aborted && live.current)
        setSnapshot((previous) =>
          previous
            ? { ...next, calls: [...previous.calls, ...next.calls] }
            : previous,
        );
    } catch {
      if (!controller.signal.aborted && live.current) setMoreFailed(true);
    } finally {
      requests.current.delete(controller);
      if (live.current) setMoreLoading(false);
    }
  };
  const input = snapshot?.input;
  const data = input?.data || {};
  const coverage = data.history_coverage || {};
  const history = [
    ...(data.conversation_history || []),
    ...(data.group_background || []),
  ];
  const historyCount = data.legacy_context
    ? (coverage.selected_messages ??
      data.legacy_context.filter((item: Value) => !item.current).length)
    : history.length;
  return (
    <dialog
      ref={dialog}
      className="account-dialog reply-context-dialog"
      aria-labelledby="reply-context-title"
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        close();
      }}
      onClick={(event) => {
        event.stopPropagation();
        if (event.target === dialog.current) close();
      }}
    >
      <header>
        <h2 id="reply-context-title">AI 回复上下文</h2>
        <button type="button" aria-label="关闭 AI 回复上下文" onClick={close}>
          <X size={19} />
        </button>
      </header>
      <nav className="reply-context-tabs" aria-label="上下文视图">
        <button
          type="button"
          aria-pressed={tab === "context"}
          onClick={() => setTab("context")}
        >
          上下文
        </button>
        <button
          type="button"
          aria-pressed={tab === "process"}
          onClick={() => setTab("process")}
        >
          生成过程
        </button>
      </nav>
      <div className="reply-context-body" aria-busy={loading}>
        {loading && <p role="status">上下文读取中</p>}
        {failed && (
          <p role="alert">
            上下文读取失败{" "}
            <button
              type="button"
              onClick={() => setRetry((value) => value + 1)}
            >
              重试
            </button>
          </p>
        )}
        {snapshot && (
          <>
            <div className="reply-context-coverage" role="status">
              <strong>
                {snapshot.status === "complete"
                  ? "生成记录完整"
                  : snapshot.status === "partial"
                    ? "生成记录不完整"
                    : "上下文记录不可用"}
              </strong>
              {snapshot.missing.map((reason) => (
                <p key={reason}>{reason}</p>
              ))}
              {snapshot.status !== "complete" && (
                <button
                  type="button"
                  onClick={() => setRetry((value) => value + 1)}
                >
                  重新读取
                </button>
              )}
            </div>
            {tab === "context" && input && (
              <>
                <div className="reply-context-coverage">
                  <p>
                    {coverage.scope === "current_member"
                      ? "群聊当前成员"
                      : "当前会话"}{" "}
                    · {data.legacy_context ? "清单中" : "已加载"} {historyCount}{" "}
                    条历史
                    {input.size?.tokens != null
                      ? ` · 输入约 ${input.size.tokens.toLocaleString()} tokens`
                      : ""}
                  </p>
                  {coverage.oldest_timestamp && (
                    <p>
                      {dates.format(new Date(coverage.oldest_timestamp * 1000))}{" "}
                      —{" "}
                      {dates.format(
                        new Date(
                          (coverage.newest_timestamp ||
                            coverage.oldest_timestamp) * 1000,
                        ),
                      )}
                    </p>
                  )}
                  {coverage.has_more && <p>还有未加载的历史原文</p>}
                  {Object.entries(coverage.trim_reasons || {}).map(
                    ([key, value]) => (
                      <p key={key}>
                        删减原因：{reasons[key] || key}（{display(value)}）
                      </p>
                    ),
                  )}
                </div>
                <section>
                  <details className="reply-context-system">
                    <summary>
                      系统提示词与助手、号主资料（
                      {input.system.join("").length.toLocaleString()} 字）
                    </summary>
                    {input.system.length ? (
                      input.system.map((text, index) => (
                        <div className="reply-context-text" key={index}>
                          {text}
                        </div>
                      ))
                    ) : (
                      <p>当时的系统提示词未记录</p>
                    )}
                  </details>
                </section>
                {Object.entries(data)
                  .filter(([key]) => key !== "history_coverage")
                  .map(([key, value]) => (
                    <section key={key}>
                      <h3>{labels[key] || key}</h3>
                      <Content value={value} />
                    </section>
                  ))}
                {!!input.images.length && (
                  <section>
                    <h3>实际注入图片</h3>
                    <div className="reply-context-images">
                      {input.images.map((image, index) => (
                        <figure key={index}>
                          {image.sha256 && image.width && image.height ? (
                            <ChatImage
                              image={{
                                url: `/wechat/api/v1${endpoint}/images/${image.sha256}`,
                                width: image.width,
                                height: image.height,
                              }}
                            />
                          ) : (
                            <p>图片记录不完整</p>
                          )}
                          <figcaption>
                            消息 {image.message_id || "未知"} ·{" "}
                            {image.source_type === "quote"
                              ? "引用"
                              : image.source_type === "recent"
                                ? "近期候选"
                                : image.source_type === "history"
                                  ? "历史"
                                  : "本轮"}
                            <br />
                            {image.image_location}
                          </figcaption>
                        </figure>
                      ))}
                    </div>
                  </section>
                )}
              </>
            )}
            {tab === "process" && (
              <>
                <p className="reply-context-muted">
                  按实际执行顺序排列；不展示凭据、媒体编码字节和思考正文。
                </p>
                <ol className="reply-context-calls">
                  {snapshot.calls.map((call, index) => (
                    <li key={call.call_id}>
                      <button
                        type="button"
                        aria-expanded={selected === call.call_id}
                        onClick={() => {
                          setSelected((previous) =>
                            previous === call.call_id ? null : call.call_id,
                          );
                          setDetail(call.legacy ? { tool: call.tool } : null);
                        }}
                      >
                        {call.kind === "model"
                          ? `模型请求 ${index + 1}`
                          : `工具 · ${call.name || "记录缺失"}`}
                      </button>
                      <span className="reply-context-muted">
                        {call.stage === "input_control"
                          ? "等待补充判断"
                          : "回复生成"}
                        {call.model ? ` · ${call.model}` : ""}
                        {call.available === false ? " · 记录缺失" : ""}
                      </span>
                      {selected === call.call_id && (
                        <div className="reply-context-detail">
                          {!detail && !detailFailed && (
                            <p role="status">调用详情读取中</p>
                          )}
                          {detailFailed && (
                            <p role="alert">详情读取失败，请重新展开</p>
                          )}
                          {detail && (
                            <>
                              {detail.kind === "model" &&
                                detail.request_available === false && (
                                  <p>该次模型请求记录缺失</p>
                                )}
                              {detail.kind === "model" &&
                                detail.response_available === false && (
                                  <p>该次模型响应记录缺失</p>
                                )}
                              {detail.tool && <Content value={detail.tool} />}
                              {detail.request && (
                                <Raw
                                  title="脱敏原始请求"
                                  value={detail.request}
                                />
                              )}
                              {detail.response && (
                                <Raw
                                  title="模型响应与用量"
                                  value={detail.response}
                                />
                              )}
                              {!detail.tool &&
                                !detail.request &&
                                !detail.response && <p>该次调用记录缺失</p>}
                            </>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
                {snapshot.has_more && (
                  <button
                    type="button"
                    disabled={moreLoading}
                    onClick={() => void more()}
                  >
                    {moreLoading ? "读取中" : "加载更多调用"}
                  </button>
                )}
                {moreFailed && <p role="alert">更多调用读取失败，可重新加载</p>}
              </>
            )}
          </>
        )}
      </div>
    </dialog>
  );
}
