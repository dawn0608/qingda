import { usePresence } from "./motion";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, MoreHorizontal } from "lucide-react";

type Option = {
  value: string;
  label: string;
  disabled?: boolean;
};

export function Toggle({
  checked,
  onChange,
  disabled = false,
  pending = false,
  label = "开关",
}: {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  pending?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      className={"toggle " + (checked ? "on" : "")}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={pending ? label : undefined}
      disabled={disabled}
      aria-disabled={disabled || pending}
      aria-busy={pending}
      onClick={() => {
        if (!pending) onChange();
      }}
    >
      <span />
    </button>
  );
}

// 浮层保留在所属对话框中，避免 aria-modal 隔离菜单，也不受面板 overflow 裁剪。
function usePopup(count: number, menu = false) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({});
  const [container, setContainer] = useState<Element | null>(null);
  const presence = usePresence<HTMLDivElement>(open && !!container, "menu");
  const popup = presence.ref;
  useLayoutEffect(() => {
    if (!open || !trigger.current) return;
    setContainer(trigger.current.closest('[role="dialog"]') || document.body);
    const position = () => {
      const rect = trigger.current!.getBoundingClientRect();
      if (
        rect.bottom <= 0 ||
        rect.top >= innerHeight ||
        rect.right <= 0 ||
        rect.left >= innerWidth
      ) {
        setOpen(false);
        return;
      }
      const width = Math.min(
        Math.max(menu ? 176 : rect.width, 160),
        innerWidth - 24,
      );
      const wanted = Math.min(count * 38 + 12, 300);
      const below = innerHeight - rect.bottom - 20;
      const above = rect.top - 20;
      const up = below < wanted && above > below;
      const maxHeight = Math.max(0, Math.min(300, up ? above : below));
      const next: CSSProperties = {
        width,
        maxHeight,
        left: Math.max(
          12,
          Math.min(
            menu ? rect.right - width : rect.left,
            innerWidth - width - 12,
          ),
        ),
        ...(up
          ? { bottom: innerHeight - rect.top + 6 }
          : { top: rect.bottom + 6 }),
      };
      setStyle(next);
    };
    position();
    const outside = (event: PointerEvent) => {
      if (
        !trigger.current?.contains(event.target as Node) &&
        !popup.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    const resize = new ResizeObserver(position);
    resize.observe(trigger.current);
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
      resize.disconnect();
    };
  }, [open, count, menu]);
  const close = (restore = false) => {
    setOpen(false);
    if (restore) trigger.current?.focus();
  };
  return {
    trigger,
    popup,
    present: presence.present,
    open,
    setOpen,
    close,
    style,
    container,
  };
}

export function Select({
  value,
  options,
  onValueChange,
  label,
  disabled = false,
  pending = false,
  triggerContent,
  className = "",
}: {
  value: string;
  options: Option[];
  onValueChange: (value: string) => void;
  label: string;
  disabled?: boolean;
  pending?: boolean;
  triggerContent?: ReactNode;
  className?: string;
}) {
  const id = useId();
  const p = usePopup(options.length);
  const [active, setActive] = useState(0);
  const selected = options.findIndex((o) => o.value === value);
  useEffect(() => {
    p.close();
  }, [disabled, pending, options.length]);
  useEffect(() => {
    if (p.open)
      document
        .getElementById(`${id}-${active}`)
        ?.scrollIntoView({ block: "nearest" });
  }, [p.open, p.present, p.container, active]);
  const choose = (index: number) => {
    const option = options[index];
    if (disabled || pending || !option || option.disabled) return;
    onValueChange(option.value);
    p.close(true);
  };
  const move = (direction: number, edge?: number) => {
    let next = edge ?? active + direction;
    while (next >= 0 && next < options.length && options[next].disabled)
      next += direction;
    if (next >= 0 && next < options.length) {
      setActive(next);
    }
  };
  const expand = () => {
    if (disabled || pending || !options.length) return;

    setActive(
      selected >= 0 && !options[selected].disabled
        ? selected
        : Math.max(
            0,
            options.findIndex((o) => !o.disabled),
          ),
    );
    p.setOpen(true);
  };
  const key = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (pending) {
      if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(e.key))
        e.preventDefault();
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(e.key)) {
      e.preventDefault();
      if (!p.open) {
        expand();
        return;
      }
      if (e.key === "ArrowDown") move(1);
      else if (e.key === "ArrowUp") move(-1);
      else if (e.key === "Home") move(1, 0);
      else if (e.key === "End") move(-1, options.length - 1);
      else choose(active);
    } else if (e.key === "Escape" && p.open) {
      e.preventDefault();
      e.stopPropagation();
      p.close(true);
    } else if (e.key === "Tab") p.close();
  };
  const optionList = options.map((option, i) => (
    <div
      id={`${id}-${i}`}
      key={option.value}
      role="option"
      aria-selected={option.value === value}
      aria-disabled={option.disabled || undefined}
      className={
        "select-option " +
        (active === i ? "active " : "") +
        (option.value === value ? "chosen" : "")
      }
      onPointerMove={() => {
        if (!option.disabled) {
          setActive(i);
        }
      }}
      onPointerDown={(e) => e.preventDefault()}
      onClick={() => choose(i)}
    >
      <span className="select-option-content">{option.label}</span>
      {option.value === value && <Check size={16} />}
    </div>
  ));
  return (
    <div className={`select ${className}`}>
      <button
        type="button"
        ref={p.trigger}
        role="combobox"
        aria-label={label}
        aria-expanded={p.open}
        aria-controls={p.open ? id : undefined}
        aria-haspopup="listbox"
        aria-activedescendant={
          p.open && options[active] ? `${id}-${active}` : undefined
        }
        disabled={disabled || !options.length}
        aria-disabled={disabled || !options.length || pending}
        aria-busy={pending}
        className={"select-trigger " + (p.open ? "expanded" : "")}
        onClick={() => (p.open ? p.close() : expand())}
        onKeyDown={key}
      >
        <span>{triggerContent ?? (options[selected]?.label || "请选择")}</span>
        <ChevronDown size={16} />
      </button>
      {p.present &&
        p.container &&
        createPortal(
          <div
            ref={p.popup}
            id={id}
            role="listbox"
            aria-label={label}
            className="popup select-options"
            style={p.style}
          >
            {optionList}
          </div>,
          p.container,
        )}
    </div>
  );
}

type MenuAction = {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
};
export function ActionMenu({
  label,
  triggerLabel = "更多",
  actions,
}: {
  label: string;
  triggerLabel?: string;
  actions: MenuAction[];
}) {
  const id = useId();
  const p = usePopup(actions.length, true);
  const [active, setActive] = useState(0);
  useEffect(() => {
    if (p.open && p.container) popupFocus(active);
  }, [p.open, p.present, p.container, active]);
  const popupFocus = (index: number) =>
    document.getElementById(`${id}-${index}`)?.focus();
  const expand = (last = false) => {
    const available = actions
      .map((a, i) => (!a.disabled ? i : -1))
      .filter((i) => i >= 0);
    setActive((last ? available.at(-1) : available[0]) ?? 0);
    p.setOpen(true);
  };
  const key = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      p.close(true);
    }
    if (e.key === "Tab") {
      p.close(true);
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      const available = actions
        .map((a, i) => (!a.disabled ? i : -1))
        .filter((i) => i >= 0);
      if (!available.length) return;
      const pos = available.indexOf(active);
      setActive(
        e.key === "Home"
          ? available[0]
          : e.key === "End"
            ? available.at(-1)!
            : available[
                (pos + (e.key === "ArrowDown" ? 1 : -1) + available.length) %
                  available.length
              ],
      );
    }
  };
  return (
    <div className="action-menu">
      <button
        type="button"
        ref={p.trigger}
        className="secondary menu-trigger"
        disabled={actions.every((action) => action.disabled)}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={p.open}
        aria-controls={p.open ? id : undefined}
        onClick={() => (p.open ? p.close() : expand())}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            expand(e.key === "ArrowUp");
          } else if (p.open) key(e);
        }}
      >
        <MoreHorizontal size={17} />
        <span>{triggerLabel}</span>
      </button>
      {p.present &&
        p.container &&
        createPortal(
          <div
            ref={p.popup}
            id={id}
            role="menu"
            aria-label={label}
            className="popup action-options"
            style={p.style}
            onKeyDown={key}
          >
            {actions.map((action, i) => (
              <button
                key={action.label}
                id={`${id}-${i}`}
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={action.disabled}
                className={action.danger ? "menu-danger" : ""}
                onClick={() => {
                  p.close(true);
                  action.onSelect();
                }}
              >
                {action.label}
              </button>
            ))}
          </div>,
          p.container,
        )}
    </div>
  );
}
