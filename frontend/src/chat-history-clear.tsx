import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api } from "./api";
import { useNotice } from "./notices";
import { userMessage } from "./user-messages";

export type HistoryDeletion = {
  id: string;
  state: "waiting" | "files" | "complete";
  error: string;
  message_count: number;
};

export function ClearChatHistory({
  endpoint,
  name,
  detected,
  onBusy,
  onCleared,
}: {
  endpoint: string;
  name: string;
  detected?: HistoryDeletion | null;
  onBusy: (busy: boolean) => void;
  onCleared: () => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const callbacks = useRef({ onBusy, onCleared });
  callbacks.current = { onBusy, onCleared };
  const current = useRef<HistoryDeletion | null>(null);
  const finished = useRef("");
  const nonce = useRef("");
  const guard = useRef(false);
  const [operation, setOperation] = useState<HistoryDeletion | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const { show, setHost, reportFailure, recovered } = useNotice();
  const source = `history-deletion:${endpoint}`;
  const close = () => {
    if (guard.current) return;
    dialog.current?.close();
    setConfirming(false);
    trigger.current?.focus({ preventScroll: true });
  };
  const accept = (next: HistoryDeletion | null) => {
    if (!next) return;
    if (next.state === "complete") {
      current.current = null;
      setOperation(null);
      callbacks.current.onBusy(false);
      recovered(source);
      if (finished.current !== next.id) {
        finished.current = next.id;
        nonce.current = "";
        callbacks.current.onCleared();
        show("聊天记录已清空", "success");
      }
    } else {
      current.current = next;
      setOperation(next);
      callbacks.current.onBusy(true);
      if (next.error) reportFailure(source, "部分数据尚未清理，系统将继续重试");
      else recovered(source);
    }
  };
  useEffect(() => {
    if (!confirming || !dialog.current) return;
    const previous = trigger.current?.closest("dialog") || null;
    dialog.current.showModal();
    setHost(dialog.current);
    cancel.current?.focus();
    return () => setHost(previous?.isConnected ? previous : null);
  }, [confirming, setHost]);
  useEffect(() => {
    let live = true,
      timer = 0;
    const controller = new AbortController();
    if (detected) accept(detected);
    const read = async () => {
      try {
        const id = current.current?.id;
        const result = await api(
          `${endpoint}/deletion${id ? `?request_id=${id}` : ""}`,
          "GET",
          undefined,
          controller.signal,
        );
        if (live) accept(result.operation);
      } catch (error) {
        if (live && !controller.signal.aborted)
          reportFailure(
            source,
            userMessage(error, "清空进度暂时无法读取，请稍后重试"),
          );
      } finally {
        if (live && current.current) timer = window.setTimeout(read, 1000);
      }
    };
    void read();
    return () => {
      live = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [endpoint, detected?.id, revision, reportFailure, recovered]);
  const clear = async () => {
    if (guard.current) return;
    guard.current = true;
    setSubmitting(true);
    callbacks.current.onBusy(true);
    nonce.current ||= crypto.randomUUID();
    try {
      const next: HistoryDeletion = await api(endpoint, "DELETE", {
        request_id: nonce.current,
        confirmed: true,
      });
      guard.current = false;
      close();
      accept(next);
      if (next.state !== "complete") setRevision((value) => value + 1);
    } catch (error) {
      // 网络中断不代表事务没有提交；先查同一请求，不能另起一次清空误删新消息。
      try {
        const result = await api(
          `${endpoint}/deletion?request_id=${nonce.current}`,
        );
        if (result.operation) {
          guard.current = false;
          close();
          accept(result.operation);
          setRevision((value) => value + 1);
          return;
        }
      } catch {
        /* 保留同一请求编号，允许用户在原确认框重试。 */
      }
      callbacks.current.onBusy(false);
      show(userMessage(error, "清空聊天记录失败，请稍后重试"), "error");
    } finally {
      guard.current = false;
      setSubmitting(false);
    }
  };
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="chat-history-clear"
        disabled={!!operation || submitting}
        aria-label="清空聊天记录"
        onClick={() => setConfirming(true)}
      >
        {operation
          ? operation.error
            ? "清空待重试"
            : "清空中"
          : "清空聊天记录"}
      </button>
      {confirming && (
        <dialog
          ref={dialog}
          className="account-dialog chat-history-confirm"
          aria-labelledby="chat-history-clear-title"
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
            <h2 id="chat-history-clear-title">清空聊天记录</h2>
            <button
              type="button"
              aria-label="关闭清空确认"
              disabled={submitting}
              onClick={close}
            >
              <X size={19} />
            </button>
          </header>
          <div className="chat-history-confirm-body">
            <p>确定清空与「{name}」的全部聊天记录？</p>
            <p>包括图片、语音及其缓存。删除后无法在本系统恢复。</p>
          </div>
          <footer>
            <button
              ref={cancel}
              type="button"
              disabled={submitting}
              onClick={close}
            >
              取消
            </button>
            <button
              type="button"
              className="chat-history-confirm-delete"
              disabled={submitting}
              onClick={() => void clear()}
            >
              {submitting ? "清空中" : "确认清空"}
            </button>
          </footer>
        </dialog>
      )}
    </>
  );
}
