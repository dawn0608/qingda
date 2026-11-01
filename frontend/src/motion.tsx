import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import "./motion.css";

type Kind = "content" | "modal" | "drawer" | "menu" | "toast" | "fade";
const running = new WeakMap<Element, Animation>();
const interrupted = new WeakMap<Element, Keyframe>();
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)");

function cancelTransition(element: HTMLElement, animation: Animation | null) {
  if (
    animation &&
    running.get(element) === animation &&
    animation.playState === "running"
  ) {
    const style = getComputedStyle(element);
    interrupted.set(element, {
      opacity: style.opacity,
      translate: style.translate,
      scale: style.scale,
    });
  }
  animation?.cancel();
}

// Individual translate/scale properties preserve positioning transforms (e.g. toast centering).
export function playMotion(element: HTMLElement, kind: Kind, exit = false) {
  const previous = running.get(element);
  const current = previous ? getComputedStyle(element) : null;
  const start =
    interrupted.get(element) ||
    (current
      ? {
          opacity: current.opacity,
          translate: current.translate,
          scale: current.scale,
        }
      : null);
  interrupted.delete(element);
  previous?.cancel();
  if (reduced().matches || !element.animate) return null;
  const offset =
    kind === "drawer"
      ? "24px 0"
      : kind === "toast"
        ? "0 -6px"
        : kind === "menu" && element.style.bottom
          ? "0 4px"
          : kind === "menu"
            ? "0 -4px"
            : "0 6px";
  const hidden = {
    opacity: 0,
    translate: kind === "fade" ? "0 0" : offset,
    scale: kind === "modal" ? ".98" : "1",
  };
  const visible = { opacity: 1, translate: "0 0", scale: "1" };
  return animate(
    element,
    [start || (exit ? visible : hidden), exit ? hidden : visible],
    exit
      ? 160
      : kind === "modal" || kind === "drawer"
        ? 240
        : kind === "menu"
          ? 160
          : 200,
  );
}

function animate(element: HTMLElement, frames: Keyframe[], duration: number) {
  if (reduced().matches || !element.animate) return null;
  const animation = element.animate(frames, {
    duration,
    easing: "cubic-bezier(.2,.8,.2,1)",
    fill: "both",
  });
  running.set(element, animation);
  const preference = reduced();
  const finish = () => {
    if (preference.matches) animation.finish();
  };
  preference.addEventListener("change", finish);
  void animation.finished
    .then(
      () => {},
      () => {},
    )
    .finally(() => {
      preference.removeEventListener("change", finish);
      if (running.get(element) === animation) running.delete(element);
      animation.cancel();
    });
  return animation;
}

export function useContentMotion<T extends HTMLElement>(
  key: unknown,
  selector?: string,
  enabled = true,
) {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const root = ref.current;
    if (!root) return;
    if (!enabled) return;
    const elements = selector
      ? Array.from(root.querySelectorAll<HTMLElement>(selector))
      : [root];
    const animations = elements.map((element) =>
      playMotion(element, "content"),
    );
    return () =>
      animations.forEach((animation, index) =>
        cancelTransition(elements[index], animation),
      );
  }, [key, selector]);
  return ref;
}

// Controlled presence retains only the departing UI, not a second business page.
export function usePresence<T extends HTMLElement>(
  open: boolean,
  kind: Kind,
  identity: unknown = open,
) {
  const ref = useRef<T>(null);
  const [present, setPresent] = useState(open);
  useLayoutEffect(() => {
    if (open && !present) {
      setPresent(true);
      return;
    }
    if (!present || !ref.current) return;
    const element = ref.current;
    element.inert = !open;
    const animation = playMotion(element, kind, !open);
    let live = true;
    if (!open) {
      if (!animation) setPresent(false);
      else
        void animation.finished.then(
          () => {
            if (live) setPresent(false);
          },
          () => {},
        );
    }
    return () => {
      live = false;
      cancelTransition(element, animation);
    };
  }, [open, present, kind, identity]);
  return { ref, present };
}

export function useDismissMotion<T extends HTMLElement>(
  ref: RefObject<T | null>,
  done: () => void,
  kind: Kind,
) {
  const callback = useRef(done);
  callback.current = done;
  const closing = useRef(false);
  const alive = useRef(true);
  useLayoutEffect(() => {
    alive.current = true;
    closing.current = false;
    const element = ref.current;
    const backdrop = element?.closest<HTMLElement>(".modal-backdrop");
    const animation = element && playMotion(element, kind);
    const shade = backdrop && playMotion(backdrop, "fade");
    return () => {
      alive.current = false;
      animation?.cancel();
      shade?.cancel();
      if (element) running.get(element)?.cancel();
      if (backdrop) running.get(backdrop)?.cancel();
    };
  }, [ref, kind]);
  return useCallback(() => {
    if (closing.current) return;
    closing.current = true;
    const element = ref.current;
    if (element) {
      element.inert = true;
      element.dataset.motionClosing = "true";
      const backdrop = element.closest<HTMLElement>(".modal-backdrop");
      if (backdrop) playMotion(backdrop, "fade", true);
    }
    const animation = element && playMotion(element, kind, true);
    if (!animation) callback.current();
    else
      void animation.finished.then(
        () => {
          if (alive.current) callback.current();
        },
        () => {},
      );
  }, [ref, kind]);
}

export function useSizeMotion<T extends HTMLElement>(key: unknown) {
  const ref = useRef<T>(null);
  const height = useRef(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (!running.has(element))
        height.current = element.getBoundingClientRect().height;
    });
    height.current = element.getBoundingClientRect().height;
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const from = height.current;
    running.get(element)?.cancel();
    const to = element.getBoundingClientRect().height;
    height.current = to;
    if (!from || from === to) return;
    element.style.overflow = "clip";
    const animation = animate(
      element,
      [{ height: `${from}px` }, { height: `${to}px` }],
      200,
    );
    let live = true;
    const restore = () => {
      if (live) element.style.overflow = "";
    };
    if (animation) void animation.finished.then(restore, restore);
    else restore();
    return () => {
      live = false;
      animation?.cancel();
      element.style.overflow = "";
    };
  }, [key]);
  return ref;
}

export function AnimatedDetails({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  useLayoutEffect(() => {
    const element = ref.current!;
    const summary = element.querySelector("summary")!;
    let desired = element.open;
    let animation: Animation | null = null;
    const toggle = (event: MouseEvent) => {
      event.preventDefault();
      const from = element.getBoundingClientRect().height;
      animation?.cancel();
      desired = !desired;
      element.open = true;
      const to = desired
        ? element.getBoundingClientRect().height
        : summary.getBoundingClientRect().height +
          parseFloat(getComputedStyle(element).paddingTop) +
          parseFloat(getComputedStyle(element).paddingBottom) +
          parseFloat(getComputedStyle(element).borderTopWidth) +
          parseFloat(getComputedStyle(element).borderBottomWidth);
      element.style.overflow = "clip";
      const target = desired;
      animation = animate(
        element,
        [{ height: `${from}px` }, { height: `${to}px` }],
        200,
      );
      const finish = () => {
        element.open = target;
        element.style.overflow = "";
      };
      if (animation) void animation.finished.then(finish, () => {});
      else finish();
    };
    summary.addEventListener("click", toggle);
    return () => {
      animation?.cancel();
      summary.removeEventListener("click", toggle);
    };
  }, []);
  return (
    <details ref={ref} className={className}>
      {children}
    </details>
  );
}
