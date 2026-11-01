import { useCallback, useEffect, useRef } from "react";

// 只刷新可见页面；一次读取结束后再计时，避免慢请求重叠。
export function useAutoRefresh(
  read: (signal: AbortSignal) => Promise<void>,
  { enabled = true, resource = "", interval = 5000 } = {},
) {
  const latest = useRef(read);
  const trigger = useRef<() => void>(() => {});
  latest.current = read;
  useEffect(() => {
    if (!enabled) return;
    let live = true,
      running = false,
      requested = false,
      timer = 0;
    let controller: AbortController | undefined;
    const refresh = async () => {
      if (!live || running) return;
      clearTimeout(timer);
      if (!document.hidden) {
        running = true;
        controller = new AbortController();
        try {
          await latest.current(controller.signal);
        } finally {
          running = false;
          if (live) {
            timer = window.setTimeout(refresh, requested ? 0 : interval);
            requested = false;
          }
        }
      } else timer = window.setTimeout(refresh, interval);
    };
    const resume = () => {
      if (!running) void refresh();
    };
    // 写后回读与轮询共用一个入口；进行中的旧读取结束后最多补读一次。
    const requestRefresh = () => {
      if (running) requested = true;
      else void refresh();
    };
    trigger.current = requestRefresh;
    void refresh();
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      live = false;
      clearTimeout(timer);
      controller?.abort();
      if (trigger.current === requestRefresh) trigger.current = () => {};
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [enabled, resource, interval]);
  return useCallback(() => trigger.current(), []);
}
