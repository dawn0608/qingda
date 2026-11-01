import { useEffect, useRef } from "react";
import { api } from "./api";
import { useNotice } from "./notices";
import { userMessage } from "./user-messages";

type Row = Record<string, any>;
let version = 0;
let pending = 0;
export const replyVersion = () => version;
export const replyPending = () => pending > 0;
export const replyChanged = "auto-reply-changed";

export async function changeReply(
  path: string,
  body: unknown,
  apply: (row: Row) => void,
  method = "PATCH",
) {
  version++;
  pending++;
  try {
    const row = await api(path, method, body);
    apply(row);
    return row;
  } finally {
    pending--;
    version++;
    window.dispatchEvent(new Event(replyChanged));
  }
}

export const replyEnabled = (row: Row) => row.auto_reply_status === "enabled";
export const replyBlocked = (row: Row) =>
  [
    "ai_disabled",
    "account_disabled",
    "account_offline",
    "relationship_unverified",
    "contact_removed",
  ].includes(row.auto_reply_status);

// 时间基准来自服务端；到期重新查询，不在浏览器内自行宣告恢复。
export function useReplyPolling(
  load: () => Promise<any>,
  apply: (value: any) => void,
  deps: unknown[],
  expiry?: { until: number; now: number },
  enabled = true,
) {
  const { reportFailure, recovered } = useNotice();
  const applyRef = useRef(apply);
  applyRef.current = apply;
  useEffect(() => {
    if (!enabled) return;
    let live = true,
      running = false;
    const refresh = async () => {
      if (!live || running || document.hidden || replyPending()) return;
      running = true;
      const stamp = replyVersion();
      try {
        const value = await load();
        if (live && stamp === replyVersion() && !replyPending()) {
          applyRef.current(value);
          recovered("assistant-accounts");
        }
      } catch (error) {
        if (live && stamp === replyVersion())
          reportFailure("assistant-accounts", userMessage(error));
      } finally {
        running = false;
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 1000);
    window.addEventListener(replyChanged, refresh);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      live = false;
      clearInterval(timer);
      window.removeEventListener(replyChanged, refresh);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [...deps, enabled]);
  useEffect(() => {
    if (!enabled || !expiry?.until || expiry.until <= expiry.now) return;
    const timer = window.setTimeout(
      () => window.dispatchEvent(new Event(replyChanged)),
      Math.max(0, (expiry.until - expiry.now) * 1000) + 30,
    );
    return () => clearTimeout(timer);
  }, [enabled, expiry?.until, expiry?.now]);
}
