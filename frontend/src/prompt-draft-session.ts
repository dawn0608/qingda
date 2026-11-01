export type DraftReceipt = {
  version: string | null;
  markdown: string;
  base_sha256: string;
  active_prompt: string;
  active_sha256: string;
  dirty: boolean;
  conflict: boolean;
};
type ApplyReceipt = {
  draft: DraftReceipt;
  account: Record<string, any>;
  changed: boolean;
};
export type DraftTransport = {
  load: () => Promise<DraftReceipt>;
  save: (
    body: { markdown: string; version: string | null; base_sha256: string },
    keepalive?: boolean,
  ) => Promise<DraftReceipt>;
  apply: (body: {
    version: string;
    base_sha256: string;
  }) => Promise<ApplyReceipt>;
  resolve: (body: {
    markdown: string;
    version: string | null;
    base_sha256: string;
  }) => Promise<DraftReceipt>;
};
export type PromptEditorAPI = {
  read: () => string;
  focus: () => void;
  composing: () => boolean;
  normalize: (value: string) => string;
};
type LocalDraft = {
  markdown: string;
  base_sha256: string;
  version: string | null;
};

// 网络回执只推进已持久化水位，绝不回填正在编辑的正文。
export class PromptDraftSession {
  state = {
    id: "",
    original: "",
    markdown: "",
    baseline: "",
    persisted: "",
    version: null as string | null,
    baseSha256: "",
    dirty: false,
    revision: 0,
    loaded: false,
    ready: false,
    status: "loading",
    error: "",
    conflict: false,
  };
  private api: PromptEditorAPI | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private lastInput = 0;
  private sequence = 0;
  private focusGeneration = 0;
  private focused = false;
  private composing = false;
  private compositionWaiters: Array<() => void> = [];
  private pendingDraft = false;
  private saveFlight: Promise<boolean> | null = null;
  private applyFlight: Promise<boolean> | null = null;
  private finishFlight: Promise<boolean> | null = null;
  private disposed = false;
  private cacheFailed = false;
  private cacheKey: string;
  private storage: Storage;
  private transport: DraftTransport;
  private notify: () => void;
  private applied: (row: Record<string, any>) => void;
  private initialization: Promise<void>;

  constructor(
    id: string,
    userId: string,
    transport: DraftTransport,
    storage: Storage,
    notify: () => void,
    applied: (row: Record<string, any>) => void,
    tabId = "",
    namespace = "prompt",
  ) {
    this.state.id = id;
    this.cacheKey = `wechat-${namespace}-recovery:${userId}:${id}:${tabId}`;
    this.transport = transport;
    this.storage = storage;
    this.notify = notify;
    this.applied = applied;
    this.initialization = this.load();
  }
  private emit() {
    this.state.dirty =
      this.pendingDraft || this.state.markdown !== this.state.baseline;
    if (!this.disposed) this.notify();
  }
  private cache() {
    if (!this.state.loaded || this.disposed) return;
    try {
      if (this.state.markdown === this.state.persisted) {
        const previous = JSON.parse(
          this.storage.getItem(this.cacheKey) || "null",
        );
        if (!previous || previous.markdown === this.state.markdown)
          this.storage.removeItem(this.cacheKey);
      } else
        this.storage.setItem(
          this.cacheKey,
          JSON.stringify({
            markdown: this.state.markdown,
            version: this.state.version,
            base_sha256: this.state.baseSha256,
          }),
        );
      this.cacheFailed = false;
    } catch {
      this.cacheFailed = true;
    }
  }
  private async load(useLocal = true) {
    this.state.status = "loading";
    this.emit();
    try {
      const row = await this.transport.load();
      if (this.disposed) return;
      let local: LocalDraft | null = null;
      try {
        const candidate = JSON.parse(
          this.storage.getItem(this.cacheKey) || "null",
        );
        if (
          useLocal &&
          candidate &&
          typeof candidate.markdown === "string" &&
          typeof candidate.base_sha256 === "string"
        )
          local = candidate;
      } catch {
        /* 失效的浏览器备份不能覆盖服务器正文。 */
      }
      this.accept(row);
      this.state.original = row.active_prompt;
      this.state.baseline = row.active_prompt;
      this.state.markdown = local?.markdown ?? row.markdown;
      this.state.loaded = true;
      this.state.ready = false;
      this.state.revision++;
      const safeLocal =
        !local ||
        local.markdown === row.markdown ||
        (local.base_sha256 === row.active_sha256 &&
          (local.version === row.version || !row.dirty));
      this.state.conflict = row.conflict || !safeLocal;
      this.state.status = this.state.conflict ? "error" : "idle";
      this.state.error = this.state.conflict
        ? "另一处已修改设定，当前内容已保留，请核对后继续"
        : "";
      this.emit();
    } catch {
      this.state.status = "error";
      this.state.error = "草稿暂时无法读取，请重试";
      this.emit();
    }
  }
  private accept(row: DraftReceipt) {
    this.state.version = row.version;
    this.state.baseSha256 = row.base_sha256;
    this.state.persisted = row.markdown;
    this.pendingDraft = row.dirty;
  }
  bind(api: PromptEditorAPI | null) {
    this.api = api;
    if (!api || !this.state.loaded) return;
    if (!this.state.ready) {
      this.state.baseline = api.normalize(this.state.original);
      this.state.persisted = api.normalize(this.state.persisted);
      this.state.markdown = api.read();
      this.state.ready = true;
      this.cache();
      this.emit();
      if (this.state.markdown !== this.state.persisted && !this.state.conflict)
        this.schedule();
    }
  }
  change(markdown: string) {
    if (
      !this.state.ready ||
      this.composing ||
      this.api?.composing() ||
      markdown === this.state.markdown
    )
      return;
    this.state.markdown = markdown;
    this.sequence++;
    this.lastInput = Date.now();
    if (!this.state.conflict) {
      this.state.error =
        markdown.length > 50000
          ? "设定不能超过 50000 个字符，请精简后继续"
          : "";
      if (this.state.error) this.state.status = "error";
      else if (this.state.status === "error") this.state.status = "idle";
    }
    this.cache();
    this.emit();
    this.schedule();
  }
  focus() {
    if (this.focused) return;
    this.focused = true;
    this.focusGeneration++;
  }
  compositionStart() {
    this.composing = true;
    clearTimeout(this.timer);
  }
  compositionEnd() {
    this.composing = false;
    if (this.api) this.change(this.api.read());
    this.compositionWaiters.splice(0).forEach((resolve) => resolve());
    this.schedule();
  }
  private schedule() {
    clearTimeout(this.timer);
    if (
      this.disposed ||
      this.composing ||
      this.state.conflict ||
      !this.state.ready
    )
      return;
    if (this.state.markdown === this.state.persisted) return;
    this.timer = setTimeout(
      () => void this.persist(),
      Math.max(0, 1500 - (Date.now() - this.lastInput)),
    );
  }
  async persist(keepalive = false): Promise<boolean> {
    clearTimeout(this.timer);
    await this.initialization;
    if (this.applyFlight) {
      await this.applyFlight;
      return this.persist(keepalive);
    }
    if (
      !this.state.ready ||
      this.composing ||
      this.api?.composing() ||
      this.state.conflict
    )
      return false;
    if (this.saveFlight) return this.saveFlight;
    if (this.state.markdown === this.state.persisted) return true;
    if (this.state.markdown.length > 50000) {
      this.state.status = "error";
      this.state.error = "设定不能超过 50000 个字符，请精简后继续";
      this.emit();
      return false;
    }
    const markdown = this.state.markdown;
    const sequence = this.sequence;
    const body = {
      markdown,
      version: this.state.version,
      base_sha256: this.state.baseSha256,
    };
    this.state.status = "saving";
    this.emit();
    this.saveFlight = (async () => {
      try {
        const row = await this.transport.save(
          body,
          keepalive && new Blob([JSON.stringify(body)]).size < 60000,
        );
        this.accept(row);
        this.state.status = "idle";
        this.state.error = "";
        this.cache();
        return true;
      } catch (error) {
        if ((error as { status?: number })?.status !== 409) {
          try {
            const known = await this.transport.load();
            if (
              known.markdown === markdown &&
              known.active_sha256 === body.base_sha256
            ) {
              this.accept(known);
              this.state.status = "idle";
              this.state.error = "";
              this.cache();
              return true;
            }
          } catch {
            /* 仅做一次只读核对，不盲目重复写入。 */
          }
        }
        this.state.conflict = (error as { status?: number })?.status === 409;
        this.state.status = "error";
        this.state.error = this.state.conflict
          ? "另一处已修改设定，当前内容已保留，请核对后继续"
          : "草稿保存暂未完成，内容已保留，请重试";
        return false;
      } finally {
        this.saveFlight = null;
        this.emit();
        if (this.sequence !== sequence) this.schedule();
      }
    })();
    return this.saveFlight;
  }
  finish(): Promise<boolean> {
    this.focused = false;
    if (this.finishFlight) return this.finishFlight;
    const sequence = this.sequence;
    this.finishFlight = this.finishNow().finally(() => {
      this.finishFlight = null;
      if (
        this.sequence !== sequence &&
        !this.focused &&
        !this.state.error &&
        !this.disposed
      )
        queueMicrotask(() => void this.finish());
    });
    return this.finishFlight;
  }
  private async finishNow(): Promise<boolean> {
    const generation = this.focusGeneration;
    if (this.applyFlight) return this.applyFlight;
    await this.initialization;
    if (this.composing || this.api?.composing())
      await new Promise<void>((resolve) =>
        this.compositionWaiters.push(resolve),
      );
    await Promise.resolve();
    if (!this.state.ready || this.state.conflict) return false;
    while (this.state.markdown !== this.state.persisted || this.saveFlight) {
      if (!(await this.persist())) return false;
      if (this.focused || this.focusGeneration !== generation) return false;
    }
    if (this.focused || this.focusGeneration !== generation) return false;
    if (!this.state.dirty || !this.state.version) return true;
    const markdown = this.state.markdown;
    const sequence = this.sequence;
    const body = {
      version: this.state.version,
      base_sha256: this.state.baseSha256,
    };
    this.state.status = "applying";
    this.emit();
    this.applyFlight = (async () => {
      try {
        const row = await this.transport.apply(body);
        this.accept(row.draft);
        this.state.original = row.draft.active_prompt;
        this.state.baseline = markdown;
        this.state.status = "idle";
        this.state.error = "";
        this.cache();
        if (!this.disposed) this.applied(row.account);
        return this.sequence === sequence && !this.focused;
      } catch (error) {
        if ((error as { status?: number })?.status !== 409) {
          try {
            const known = await this.transport.load();
            if (
              known.version === body.version &&
              !known.dirty &&
              !known.conflict
            ) {
              const receipt = await this.transport.apply(body);
              this.accept(receipt.draft);
              this.state.original = receipt.draft.active_prompt;
              this.state.baseline = markdown;
              this.state.status = "idle";
              this.state.error = "";
              this.cache();
              if (!this.disposed) this.applied(receipt.account);
              return this.sequence === sequence && !this.focused;
            }
          } catch {
            /* 已确认同一版本生效才做幂等回执读取，不重放未知版本。 */
          }
        }
        this.state.conflict = (error as { status?: number })?.status === 409;
        this.state.status = "error";
        this.state.error = this.state.conflict
          ? "另一处已修改设定，当前内容已保留，请核对后继续"
          : "草稿已保留，设定暂未生效，请重试";
        return false;
      } finally {
        this.applyFlight = null;
        this.emit();
        this.schedule();
      }
    })();
    return this.applyFlight;
  }
  async retry() {
    if (!this.state.loaded) {
      this.initialization = this.load();
      return this.initialization;
    }
    if (this.focused) await this.persist();
    else await this.finish();
  }
  async resolve(keepLocal: boolean) {
    try {
      const row = await this.transport.load();
      if (!keepLocal) {
        await this.transport.resolve({
          markdown: row.active_prompt,
          version: row.version,
          base_sha256: row.active_sha256,
        });
        try {
          this.storage.removeItem(this.cacheKey);
        } catch {
          /* 服务器草稿仍可读取。 */
        }
        await this.load(false);
        return;
      }
      const result = await this.transport.resolve({
        markdown: this.state.markdown,
        version: row.version,
        base_sha256: row.active_sha256,
      });
      this.accept(result);
      this.state.original = result.active_prompt;
      this.state.baseline =
        this.api?.normalize(result.active_prompt) ?? result.active_prompt;
      this.state.conflict = false;
      this.state.error = "";
      this.state.status = "idle";
      this.cache();
      this.emit();
      if (!this.focused) await this.finish();
    } catch {
      this.state.status = "error";
      this.state.error = "冲突处理暂未完成，当前内容已保留，请重试";
      this.emit();
    }
  }
  emergencySave() {
    this.cache();
    if (!this.composing) void this.persist(true);
    return this.cacheFailed && this.state.markdown !== this.state.persisted;
  }
  observeActive(prompt: string) {
    if (
      !this.state.loaded ||
      this.applyFlight ||
      prompt === this.state.original
    )
      return;
    // 轮询不能在输入中回填正文；生效时由服务端再次校验基线。
    if (this.state.dirty || this.focused) {
      this.state.conflict = true;
      this.state.error = "另一处已修改设定，当前内容已保留，请核对后继续";
      this.state.status = "error";
      this.emit();
    } else this.initialization = this.load();
  }
  dispose() {
    this.cache();
    this.disposed = true;
    clearTimeout(this.timer);
  }
}
