import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type Vditor from "vditor";
import { changeReply } from "./auto-reply";
import { api } from "./api";
import {
  PromptDraftSession,
  type PromptEditorAPI,
} from "./prompt-draft-session";
import "vditor/dist/index.css";
import { useNotice } from "./notices";
import { ActionMenu } from "./ui";
import defaultAssistantTemplate from "../../prompts/assistant_default.md?raw";

const defaultAssistantPrompt = defaultAssistantTemplate
  .replace(/^<!--[^]*?-->\s*/, "")
  .replaceAll("{assistant_name}", "WeMind")
  .trim();

type Account = {
  id: number | string;
  wxid?: string;
  prompt?: string;
  owner_profile?: string;
};
type EditorAPI = PromptEditorAPI;
const editorMarkdown = (editor: Vditor) =>
  editor.getCurrentMode() === "sv"
    ? editor.vditor.sv!.element.value
    : editor.getValue().replace(/\n$/, "");
const editorComposing = (editor: Vditor) =>
  Boolean(editor.vditor[editor.getCurrentMode()]?.composingLock);
const editorAPI = (editor: Vditor): EditorAPI => ({
  read: () => editorMarkdown(editor),
  focus: () => editor.focus(),
  composing: () => editorComposing(editor),
  normalize: (value) =>
    editor.vditor.lute
      .VditorIRDOM2Md(editor.vditor.lute.Md2VditorIRDOM(value))
      .replace(/\n$/, ""),
});
const activateEditorButton = (button?: Element | null) =>
  button?.dispatchEvent(
    new Event(/iPhone/.test(navigator.userAgent) ? "touchstart" : "click", {
      bubbles: true,
      cancelable: true,
    }),
  );

export function usePromptSession(
  userId: number | string | undefined,
  onSaved: (row: Record<string, any>) => void,
  documentKind: "prompt" | "owner-profile" = "prompt",
) {
  const [noticeRevision, render] = useState(0);
  const { reportFailure, recovered } = useNotice();
  const manualNotice = useRef(false);
  const [manualAction, setManualAction] = useState<{
    id: number | string;
    kind: "retry" | "resolve";
  } | null>(null);
  const controller = useRef<PromptDraftSession | null>(null);
  const identity = useRef("");
  const owner = useRef(userId);
  const saved = useRef(onSaved);
  saved.current = onSaved;
  const mounted = useRef(true);
  const tabId = useRef("");
  if (!tabId.current) {
    tabId.current =
      sessionStorage.getItem("wechat-prompt-tab") || crypto.randomUUID();
    sessionStorage.setItem("wechat-prompt-tab", tabId.current);
  }
  const notify = useCallback(() => {
    if (mounted.current) render((n) => n + 1);
  }, []);
  const sync = useCallback(
    (account?: Account) => {
      if (!account || userId === undefined) return;
      const binding = `${account.id}:${documentKind === "owner-profile" ? account.wxid : ""}`;
      if (controller.current && identity.current === binding) {
        controller.current.observeActive(
          (documentKind === "owner-profile"
            ? account.owner_profile
            : account.prompt) || "",
        );
        return;
      }
      controller.current?.dispose();
      owner.current = userId;
      identity.current = binding;
      const path = `/accounts/${account.id}/${documentKind}-draft`;
      const query =
        documentKind === "owner-profile"
          ? `?owner_wxid=${encodeURIComponent(account.wxid || "")}`
          : "";
      controller.current = new PromptDraftSession(
        String(account.id),
        String(userId),
        {
          load: () => api(path + query),
          save: (body, keepalive) =>
            api(path + query, "PUT", body, undefined, keepalive),
          apply: (body) =>
            changeReply(path + "/apply" + query, body, () => {}, "POST"),
          resolve: (body) => api(path + "/resolve" + query, "POST", body),
        },
        localStorage,
        notify,
        (row) => saved.current(row),
        documentKind === "owner-profile"
          ? `${account.wxid}:${tabId.current}`
          : tabId.current,
        documentKind,
      );
      notify();
    },
    [userId, notify, documentKind],
  );
  const bind = useCallback(
    (value: EditorAPI | null) => controller.current?.bind(value),
    [],
  );
  const change = useCallback(
    (value: string) => controller.current?.change(value),
    [],
  );
  const finish = useCallback(
    () => controller.current?.finish() ?? Promise.resolve(true),
    [],
  );
  const requestLeave = useCallback(
    async (action: () => void | Promise<void>) => {
      const current = controller.current;
      if (!current) {
        await action();
        return true;
      }
      const ok = await current.finish();
      if (!ok || controller.current !== current) return false;
      await action();
      return true;
    },
    [],
  );
  const clear = useCallback(() => {
    controller.current?.dispose();
    controller.current = null;
    if (userId !== undefined) {
      const prefix = `wechat-${documentKind}-recovery:${userId}:`;
      try {
        Object.keys(localStorage)
          .filter((key) => key.startsWith(prefix))
          .forEach((key) => localStorage.removeItem(key));
      } catch {
        /* 账户切换仍以服务端认证用户为准。 */
      }
    }
    notify();
  }, [userId, notify, documentKind]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.dispose();
    };
  }, []);
  useEffect(() => {
    if (owner.current !== userId) {
      controller.current?.dispose();
      controller.current = null;
      owner.current = userId;
      notify();
    }
  }, [userId, notify]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (controller.current?.emergencySave()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    const hidden = () => {
      if (document.hidden) controller.current?.emergencySave();
    };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("pagehide", hidden);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("pagehide", hidden);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, []);
  const state = controller.current?.state;
  useEffect(() => {
    if (!state || manualNotice.current) return;
    const source = `${documentKind}-draft:${state.id}`;
    if (state.error) reportFailure(source, state.error);
    else recovered(source);
  }, [
    state?.id,
    state?.error,
    noticeRevision,
    reportFailure,
    recovered,
    documentKind,
  ]);
  const runManual = async (
    kind: "retry" | "resolve",
    action: (current: PromptDraftSession) => Promise<unknown>,
  ) => {
    const current = controller.current;
    if (!current || manualNotice.current) return;
    manualNotice.current = true;
    setManualAction({ id: current.state.id, kind });
    try {
      await action(current);
    } finally {
      manualNotice.current = false;
      if (mounted.current) setManualAction(null);
      if (controller.current === current) {
        const source = `${documentKind}-draft:${current.state.id}`;
        if (current.state.error)
          reportFailure(source, current.state.error, true);
        else recovered(source);
      }
    }
  };
  return {
    label: documentKind === "owner-profile" ? "号主资料" : "助手设定",
    draft: state ? { ...state } : null,
    ready: state?.ready ?? false,
    manualAction: manualAction?.id === state?.id ? manualAction?.kind : null,
    sync,
    bind,
    change,
    finish,
    requestLeave,
    clear,
    focus: () => controller.current?.focus(),
    compositionStart: () => controller.current?.compositionStart(),
    compositionEnd: () => controller.current?.compositionEnd(),
    retry: () => runManual("retry", (current) => current.retry()),
    resolve: (keepLocal: boolean) =>
      runManual("resolve", (current) => current.resolve(keepLocal)),
  };
}
export type PromptSession = ReturnType<typeof usePromptSession>;

export function PromptStatus({ session }: { session: PromptSession }) {
  const draft = session.draft;
  return (
    <div className={"prompt-draft-status" + (draft?.error ? " is-error" : "")}>
      {session.manualAction ? (
        <span role="status">
          {session.manualAction === "retry" ? "重试中" : "解决中"}
        </span>
      ) : draft?.error ? (
        draft.conflict ? (
          <ActionMenu
            label={
              session.label === "号主资料" ? "处理号主资料冲突" : "处理设定冲突"
            }
            triggerLabel="处理冲突"
            actions={[
              {
                label: "保留本页内容",
                onSelect: () => void session.resolve(true),
              },
              {
                label:
                  session.label === "号主资料"
                    ? "采用生效资料"
                    : "采用生效设定",
                onSelect: () => void session.resolve(false),
              },
            ]}
          />
        ) : (
          <button
            type="button"
            title={draft.error}
            onClick={() => void session.retry()}
          >
            草稿待重试
          </button>
        )
      ) : (
        <span role="status">
          {draft
            ? (
                {
                  loading: "读取中",
                  saving: "保存中",
                  applying: "生效中",
                } as Record<string, string>
              )[draft.status]
            : ""}
        </span>
      )}
    </div>
  );
}

const safeURL = (value: string, image = false) => {
  const checked = defaultUrlTransform(value.trim());
  if (!checked || /[\u0000-\u0020\u007f]/.test(checked)) return "";
  try {
    const url = new URL(checked, window.location.href);
    return url.protocol === "http:" ||
      url.protocol === "https:" ||
      (!image && url.protocol === "mailto:")
      ? checked
      : "";
  } catch {
    return "";
  }
};
const escapeHTML = (text: string) =>
  text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const vendorRoot = `${import.meta.env.BASE_URL}assets/vendor/vditor/4.0.0`;
let resources: Promise<void> | undefined;
function loadScript(path: string, id: string) {
  return new Promise<void>((resolve, reject) => {
    if (document.getElementById(id)) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    script.src = path;
    const timer = window.setTimeout(() => {
      script.remove();
      reject(new Error("editor_resource_timeout"));
    }, 60000);
    script.onload = () => {
      window.clearTimeout(timer);
      script.id = id;
      resolve();
    };
    script.onerror = () => {
      window.clearTimeout(timer);
      script.remove();
      reject(new Error("editor_resource_unavailable"));
    };
    document.head.appendChild(script);
  });
}
function loadResources() {
  return (resources ||= Promise.all([
    loadScript(`${vendorRoot}/dist/js/lute/lute.min.js`, "vditorLuteScript"),
    loadScript(`${vendorRoot}/dist/js/i18n/zh_CN.js`, "vditorI18nScriptzh_CN"),
    loadScript(`${vendorRoot}/dist/js/icons/ant.js`, "vditorIconScript"),
  ])
    .then(() => {})
    .catch((error) => {
      resources = undefined;
      throw error;
    }));
}

// 保留 IR 的源码节点结构，让 HTML 和公式作为可编辑文本往返，而非执行预览。
function configureLute(editor: Vditor) {
  const lute = editor.vditor.lute;
  const LuteClass = (window as unknown as { Lute: typeof Lute }).Lute;
  const renderers = {
    renderHTMLBlock: (node: ILuteNode, entering: boolean): [string, number] =>
      entering
        ? [
            `<div data-block="0" data-type="html-block" class="vditor-ir__node"><pre class="vditor-ir__marker--pre vditor-ir__marker"><code data-type="html-block">${escapeHTML(node.TokensStr())}</code></pre><pre class="vditor-ir__preview" data-render="1">${escapeHTML(node.TokensStr())}</pre></div>`,
            LuteClass.WalkSkipChildren,
          ]
        : ["", LuteClass.WalkContinue],
    renderInlineHTML: (node: ILuteNode, entering: boolean): [string, number] =>
      entering
        ? [
            `<span data-type="html-inline" class="vditor-ir__node prompt-html-literal"><code class="vditor-ir__marker">${escapeHTML(node.TokensStr())}</code></span>`,
            LuteClass.WalkSkipChildren,
          ]
        : ["", LuteClass.WalkContinue],
  };
  (
    lute as typeof lute & { SetInlineMath: (enabled: boolean) => void }
  ).SetInlineMath(false);
  lute.SetSanitize(false);
  lute.SetJSRenderers({
    renderers: { Md2VditorIRDOM: renderers, HTML2VditorIRDOM: renderers },
  });
  // URI 过滤发生在接入 DOM 前，不删除源码节点中的原始链接文字。
  for (const name of ["Md2VditorIRDOM", "SpinVditorIRDOM"] as const) {
    const original = lute[name].bind(lute);
    lute[name] = (source: string) => {
      const template = document.createElement("template");
      template.innerHTML = original(source);
      template.content
        .querySelectorAll(
          "script,iframe,object,embed,style,link,meta,video,audio",
        )
        .forEach((node) => node.remove());
      template.content.querySelectorAll("*").forEach((node) => {
        for (const attribute of [...node.attributes]) {
          if (/^on/i.test(attribute.name) || attribute.name === "srcdoc")
            node.removeAttribute(attribute.name);
        }
      });
      template.content.querySelectorAll("img").forEach((node) => {
        const src = safeURL(node.getAttribute("src") || "", true);
        if (!src) node.removeAttribute("src");
        node.referrerPolicy = "no-referrer";
      });
      template.content.querySelectorAll("a").forEach((node) => {
        const href = safeURL(node.getAttribute("href") || "");
        if (!href) node.removeAttribute("href");
      });
      return template.innerHTML;
    };
  }
}

const fallbackComponents: Components = {
  a: ({ node: _node, href, children }) =>
    href ? (
      <a
        href={href}
        {...(href.startsWith("#")
          ? {}
          : { target: "_blank", rel: "noopener noreferrer" })}
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  table: ({ children }) => (
    <div className="prompt-markdown-table">
      <table>{children}</table>
    </div>
  ),
};

export function PromptEditorCard({
  account,
  session,
  onLoadError,
  label = "助手设定",
  placeholder = "直接输入 Markdown 设定…",
}: {
  account?: Account;
  session: PromptSession;
  onLoadError: () => void;
  label?: string;
  placeholder?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Vditor | null>(null);
  const latest = useRef(session);
  latest.current = session;
  const loadErrorCallback = useRef(onLoadError);
  loadErrorCallback.current = onLoadError;
  const [loaded, setLoaded] = useState(false);
  const [keyboardFocus, setKeyboardFocus] = useState(false);
  const [mode, setMode] = useState<"ir" | "sv">("ir");
  const appliedRevision = useRef(-1);
  const switchingMode = useRef(false);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Tab") setKeyboardFocus(true);
    };
    const onPointerDown = () => setKeyboardFocus(false);
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, []);
  useLayoutEffect(() => {
    session.sync(account);
  }, [
    account?.id,
    account?.wxid,
    account?.prompt,
    account?.owner_profile,
    session.sync,
  ]);
  useEffect(() => {
    if (!account || !host.current) return;
    let live = true;
    let failed = false;
    setLoaded(false);
    setMode("ir");
    const reportFailure = () => {
      if (!live || failed) return;
      failed = true;
      latest.current.bind(null);
      setLoaded(false);
      loadErrorCallback.current();
    };
    const start = async () => {
      try {
        const [{ default: Editor }] = await Promise.all([
          import("vditor"),
          loadResources(),
        ]);
        if (!live || !host.current) return;
        const editor = new Editor(host.current, {
          mode: "ir",
          value: "",
          cdn: vendorRoot,
          i18n: window.VditorI18n,
          height: "auto",
          minHeight: 120,
          width: "100%",
          typewriterMode: false,
          toolbar: [
            "undo",
            "redo",
            "|",
            "headings",
            "bold",
            "italic",
            "strike",
            "|",
            "list",
            "ordered-list",
            "check",
            "outdent",
            "indent",
            "quote",
            "code",
            "link",
            "table",
            "line",
            "|",
            "edit-mode",
          ],
          toolbarConfig: { hide: false, pin: false },
          outline: { enable: false, position: "left" },
          cache: { enable: false },
          resize: { enable: false },
          upload: {},
          hint: { parse: false, emoji: {}, delay: 0 },
          image: { isPreview: false },
          link: { isOpen: false },
          placeholder,
          undoDelay: 100,
          preview: {
            mode: "editor",
            maxWidth: 1000000,
            actions: [],
            hljs: { enable: false },
            theme: { current: "", path: "" },
            render: { media: { enable: false } },
            markdown: {
              autoSpace: false,
              fixTermTypo: false,
              callout: false,
              toc: false,
              footnotes: true,
              gfmAutoLink: true,
              codeBlockPreview: false,
              mathBlockPreview: false,
              sanitize: true,
            },
          },
          input: () => {
            if (live && !editorComposing(editor))
              latest.current.change(editorMarkdown(editor));
          },
          after: () => {
            if (!live) {
              editor.destroy();
              return;
            }
            try {
              configureLute(editor);
              const draft = latest.current.draft;
              editor.setValue(draft?.markdown || "", true);
              appliedRevision.current = draft?.revision ?? -1;
              const editable = editor.vditor.ir?.element;
              if (!editable) throw new Error("editor_not_ready");
              editable.setAttribute("role", "textbox");
              editable.setAttribute("aria-label", `${label}内容`);
              editable.setAttribute("aria-multiline", "true");
              const source = editor.vditor.sv!.element;
              source.setAttribute("aria-label", `${label} Markdown 源码`);
              const modeMenu = editor.vditor.toolbar!.elements!["edit-mode"];
              modeMenu.querySelector('[data-mode="wysiwyg"]')?.remove();
              modeMenu.querySelector('[data-mode="ir"]')!.textContent =
                "即时预览";
              modeMenu.querySelector('[data-mode="sv"]')!.textContent =
                "Markdown 源码";
              modeMenu.firstElementChild?.setAttribute(
                "aria-label",
                "切换编辑模式（Ctrl+/）",
              );
              modeMenu
                .querySelector(".vditor-hint")
                ?.setAttribute("role", "group");
              modeMenu
                .querySelector(".vditor-hint")
                ?.setAttribute("aria-label", "编辑模式");
              modeMenu
                .querySelector('[data-mode="ir"]')
                ?.setAttribute("aria-pressed", "true");
              modeMenu
                .querySelector('[data-mode="sv"]')
                ?.setAttribute("aria-pressed", "false");
              latest.current.bind(editorAPI(editor));
              setLoaded(true);
            } catch {
              reportFailure();
              editor.destroy();
              if (instance.current === editor) instance.current = null;
            }
          },
        });
        instance.current = editor;
      } catch {
        reportFailure();
      }
    };
    void start();
    return () => {
      live = false;
      latest.current.bind(null);
      instance.current?.destroy();
      instance.current = null;
    };
  }, [account?.id, account?.wxid, label, placeholder]);
  useEffect(() => {
    const editor = instance.current;
    const draft = session.draft;
    if (!loaded || !editor || !draft || draft.id !== String(account?.id))
      return;
    if (appliedRevision.current !== draft.revision) {
      appliedRevision.current = draft.revision;
      editor.setValue(draft.markdown, true);
      session.bind(editorAPI(editor));
    }
  }, [session.draft?.revision, loaded, account?.id, session.bind]);
  useEffect(() => {
    if (!loaded || !instance.current) return;
    if (!session.ready) instance.current.disabled();
    else instance.current.enable();
  }, [loaded, session.ready]);
  const draft = session.draft;
  const onPaste = (event: React.ClipboardEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (!session.ready) return;
    instance.current?.insertMD(event.clipboardData.getData("text/plain"));
    if (instance.current) session.change(editorMarkdown(instance.current));
  };
  const onClick = (event: React.MouseEvent) => {
    if (!session.ready) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const target = event.target as HTMLElement;
    const mode = target.closest<HTMLButtonElement>("button[data-mode]");
    const editor = instance.current;
    if (mode && editor) {
      if (switchingMode.current) return;
      if (event.type !== "touchstart") event.preventDefault();
      event.stopPropagation();
      if (editorComposing(editor)) {
        return;
      }
      const markdown = editorMarkdown(editor);
      const previousMode = editor.getCurrentMode();
      if (mode.dataset.mode === previousMode) return;
      window.clearTimeout(
        previousMode === "ir"
          ? editor.vditor.ir?.processTimeoutId
          : editor.vditor.sv?.processTimeoutId,
      );
      editor.vditor.undo?.addToUndoStack(editor.vditor);
      // 同步调用原生模式按钮。真实鼠标/触摸事件在监听器之间可能执行
      // microtask，不能用 queueMicrotask 猜测原生切换已经完成。
      switchingMode.current = true;
      try {
        activateEditorButton(mode);
      } finally {
        switchingMode.current = false;
      }
      // 原生模式切换传入的是带序列化终止符的 Markdown。源码区只去掉
      // 编辑器添加的终止符，不能让纯查看制造脏状态或逐次增加空行。
      if (editor.getCurrentMode() === "sv")
        editor.vditor.sv!.element.value = markdown;
      setMode(editor.getCurrentMode() === "sv" ? "sv" : "ir");
      // 立即记入切换后的基线，快速输入也能撤销；不等待防抖计时器。
      editor.vditor.undo?.addToUndoStack(editor.vditor);
      mode.parentElement
        ?.querySelectorAll<HTMLButtonElement>("[data-mode]")
        .forEach((button) =>
          button.setAttribute(
            "aria-pressed",
            String(button.dataset.mode === editor.getCurrentMode()),
          ),
        );
      return;
    }
    const footnote = target.closest<HTMLElement>("[data-type='footnotes-ref']");
    if (footnote) {
      const label = footnote.dataset.footnotesLabel?.replace(/^\^/, "");
      const definition = [
        ...(host.current?.querySelectorAll<HTMLElement>(
          "[data-type='footnotes-def']",
        ) || []),
      ].find((node) => node.textContent?.startsWith(`[^${label}]:`));
      definition?.scrollIntoView({ block: "center" });
    }
    const link = target.closest<HTMLElement>("[data-type='a'],a");
    if (!link) return;
    if (link.tagName !== "A" && !event.ctrlKey && !event.metaKey) return;
    const raw =
      link.getAttribute("href") ||
      link.querySelector(".vditor-ir__marker--link")?.textContent ||
      "";
    const href = safeURL(raw);
    event.preventDefault();
    event.stopPropagation();
    if (!href) return;
    if (href.startsWith("#"))
      document
        .getElementById(href.slice(1))
        ?.scrollIntoView({ block: "center" });
    else if (event.ctrlKey || event.metaKey)
      window.open(href, "_blank", "noopener,noreferrer");
  };
  return (
    <section
      className="panel prompt-composer"
      aria-label={label}
      onFocusCapture={() => session.focus()}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          void session.finish();
      }}
    >
      {account ? (
        <>
          {!loaded && (
            <div className="prompt-editor-fallback">
              <div className="prompt-reading-content">
                <Markdown
                  remarkPlugins={[remarkGfm]}
                  components={fallbackComponents}
                >
                  {draft?.markdown ??
                    (label === "号主资料"
                      ? account.owner_profile
                      : account.prompt) ??
                    ""}
                </Markdown>
              </div>
            </div>
          )}
          <div
            className={
              "prompt-live-editor" +
              (!loaded ? " is-loading" : "") +
              (mode === "sv" ? " is-source" : "") +
              (keyboardFocus ? " is-keyboard-focus" : "")
            }
            onPasteCapture={onPaste}
            onDropCapture={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onClickCapture={onClick}
            onTouchStartCapture={(event) => {
              if (
                /iPhone/.test(navigator.userAgent) &&
                (event.target as HTMLElement).closest("button[data-mode]")
              )
                onClick(event as unknown as React.MouseEvent);
            }}
            onInput={() =>
              queueMicrotask(() => {
                if (instance.current && !editorComposing(instance.current)) {
                  if (host.current?.contains(document.activeElement))
                    session.focus();
                  session.change(editorMarkdown(instance.current));
                }
              })
            }
            onCompositionStartCapture={() => session.compositionStart()}
            onCompositionEnd={() =>
              queueMicrotask(() => session.compositionEnd())
            }
            onKeyDownCapture={(event) => {
              if (
                (event.ctrlKey || event.metaKey) &&
                event.key.toLowerCase() === "s"
              ) {
                event.preventDefault();
                event.stopPropagation();
                if (
                  !event.nativeEvent.isComposing &&
                  instance.current &&
                  !editorComposing(instance.current)
                )
                  void session.finish();
                return;
              }
              if (
                event.nativeEvent.isComposing ||
                (instance.current && editorComposing(instance.current))
              )
                return;
              const editor = instance.current;
              if (
                event.key === "Tab" &&
                editor?.getCurrentMode() === "ir" &&
                !event.ctrlKey &&
                !event.metaKey &&
                !event.altKey
              ) {
                const selection = window.getSelection();
                const range = selection?.rangeCount
                  ? selection.getRangeAt(0)
                  : null;
                const start = range?.startContainer;
                const element =
                  start?.nodeType === Node.ELEMENT_NODE
                    ? (start as Element)
                    : start?.parentElement;
                const item = element?.closest("li");
                if (
                  item &&
                  range &&
                  item.contains(range.endContainer) &&
                  editor.vditor.ir?.element.contains(item)
                ) {
                  event.preventDefault();
                  event.stopPropagation();
                  activateEditorButton(
                    editor.vditor.toolbar?.elements?.[
                      event.shiftKey ? "outdent" : "indent"
                    ]?.querySelector("button"),
                  );
                  return;
                }
              }
              if (
                (event.ctrlKey || event.metaKey) &&
                !event.altKey &&
                (event.key === "/" || event.code === "Slash")
              ) {
                event.preventDefault();
                event.stopPropagation();
                if (!editor || !session.ready) return;
                const mode = editor.getCurrentMode() === "ir" ? "sv" : "ir";
                activateEditorButton(
                  editor.vditor.toolbar?.elements?.["edit-mode"].querySelector(
                    `[data-mode="${mode}"]`,
                  ),
                );
              }
              if (
                (event.ctrlKey || event.metaKey) &&
                event.shiftKey &&
                event.key.toLowerCase() === "z"
              ) {
                event.preventDefault();
                event.stopPropagation();
                instance.current?.vditor.toolbar?.elements?.redo
                  .querySelector<HTMLButtonElement>("button")
                  ?.click();
              }
              if (
                (event.ctrlKey || event.metaKey) &&
                event.altKey &&
                ["Digit7", "Digit8", "Digit9"].includes(event.code)
              ) {
                event.preventDefault();
                event.stopPropagation();
                if (event.code !== "Digit7" && editor && session.ready)
                  activateEditorButton(
                    editor.vditor.toolbar?.elements?.[
                      "edit-mode"
                    ].querySelector(
                      `[data-mode="${event.code === "Digit8" ? "ir" : "sv"}"]`,
                    ),
                  );
              }
            }}
          >
            {/* 此节点的 class、style 和正文由 Vditor 独占，React 只维护外层。 */}
            <div ref={host} />
          </div>
        </>
      ) : (
        <div
          className="prompt-editor-fallback"
          aria-label={label === "号主资料" ? "号主资料内容" : "默认助手设定"}
        >
          <div className="prompt-reading-content">
            <Markdown
              remarkPlugins={[remarkGfm]}
              components={fallbackComponents}
            >
              {label === "号主资料" ? "" : defaultAssistantPrompt}
            </Markdown>
          </div>
        </div>
      )}
    </section>
  );
}
