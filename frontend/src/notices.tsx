import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Info } from "lucide-react";
import { usePresence } from "./motion";
import type { NoticeKind } from "./user-messages";
import "./notices.css";

type Notice = {
  id: number;
  text: string;
  kind: NoticeKind;
  background: boolean;
};
type Notices = {
  show: (text: string, kind?: NoticeKind) => void;
  clear: () => void;
  reportFailure: (source: string, text: string, manual?: boolean) => void;
  recovered: (source: string) => void;
  setHost: (host: HTMLElement | null) => void;
};
const Context = createContext<Notices | null>(null);

export function NoticeProvider({ children }: { children: ReactNode }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const active = useRef<Notice | null>(null);
  const sequence = useRef(0);
  const failures = useRef(new Set<string>());
  const publish = useCallback(
    (text: string, kind: NoticeKind, background = false) => {
      text = text.trim().replace(/[。．.]+\s*$/u, "");
      if (!text || (background && active.current && !active.current.background))
        return;
      const next = { id: ++sequence.current, text, kind, background };
      active.current = next;
      setNotice(next);
    },
    [],
  );
  const show = useCallback(
    (text: string, kind: NoticeKind = "info") => publish(text, kind),
    [publish],
  );
  const clear = useCallback(() => {
    active.current = null;
    failures.current.clear();
    setNotice(null);
  }, []);
  const reportFailure = useCallback(
    (source: string, text: string, manual = false) => {
      if (!text || (!manual && failures.current.has(source))) return;
      failures.current.add(source);
      publish(text, "error", !manual);
    },
    [publish],
  );
  const recovered = useCallback((source: string) => {
    failures.current.delete(source);
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => {
      if (active.current?.id !== notice.id) return;
      active.current = null;
      setNotice(null);
    }, 6500);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    let first:
      HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null = null;
    const invalid = (event: Event) => {
      const input = event.target;
      if (!(
        input instanceof HTMLInputElement ||
        input instanceof HTMLTextAreaElement ||
        input instanceof HTMLSelectElement
      ))
        return;
      event.preventDefault();
      if (first) return;
      first = input;
      queueMicrotask(() => {
        const field = first!;
        first = null;
        if (!field.isConnected) return;
        const label =
          field.getAttribute("aria-label") ||
          field.labels?.[0]?.textContent?.trim().split(/\s/)[0] ||
          "";
        const message = field.validity.valueMissing
          ? `请填写${label || "必填项"}`
          : field.validity.typeMismatch &&
              field instanceof HTMLInputElement &&
              field.type === "email"
            ? "请输入有效的邮箱"
            : field.validity.patternMismatch && /验证码/.test(label)
              ? "请输入6位验证码"
              : "请检查填写内容";
        field.focus();
        show(message, "error");
      });
    };
    document.addEventListener("invalid", invalid, true);
    return () => document.removeEventListener("invalid", invalid, true);
  }, [show]);
  const presence = usePresence<HTMLDivElement>(!!notice, "toast", notice?.id);
  const last = useRef(notice);
  if (notice) last.current = notice;
  const value = useMemo(
    () => ({ show, clear, reportFailure, recovered, setHost }),
    [show, clear, reportFailure, recovered],
  );
  return (
    <Context.Provider value={value}>
      {children}
      {presence.present &&
        last.current &&
        createPortal(
          <div
            ref={presence.ref}
            className={`toast toast-${last.current.kind}`}
            role={last.current.kind === "error" ? "alert" : "status"}
            aria-atomic="true"
            key={last.current.id}
          >
            <Info size={18} aria-hidden="true" />
            {last.current.text}
          </div>,
          host || document.body,
        )}
    </Context.Provider>
  );
}

export function useNotice() {
  const value = useContext(Context);
  if (!value) throw new Error("NoticeProvider is required");
  return value;
}
