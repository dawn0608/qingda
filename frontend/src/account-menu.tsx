import { useEffect, useRef, useState } from "react";
import { Camera, ChevronUp, LogOut, Settings, X } from "lucide-react";
import { api, uploadAvatar } from "./api";
import {
  AccountSettingsSaveError,
  saveProfileSettings,
} from "./account-settings-submit";
import { useNotice } from "./notices";
import { userMessage } from "./user-messages";
import { ContactSettings } from "./contact-settings";
import { PanelHeading } from "./panel-heading";
import { useAutoRefresh } from "./use-auto-refresh";

type User = Record<string, any>;
export function AccountMenu({
  user,
  onUser,
  onLogout,
  disabled,
  loggingOut,
}: {
  user: User;
  onUser: (user: User) => void;
  onLogout: () => void;
  disabled: boolean;
  loggingOut: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    setImageFailed(false);
  }, [user.avatar_url]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (!container.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  const avatar = (url: string | null, failed = false) =>
    url && !failed ? (
      <img src={url} alt="" onError={() => setImageFailed(true)} />
    ) : (
      <span>{(user.username || "用户").slice(0, 1)}</span>
    );
  return (
    <>
      <div ref={container} className="account-menu">
        <button
          ref={trigger}
          className="account-trigger"
          type="button"
          disabled={disabled}
          aria-label={`账户：${user.username || "用户"}`}
          aria-expanded={open}
          aria-haspopup="true"
          onClick={() => setOpen(!open)}
        >
          <span className="user-avatar">
            {avatar(user.avatar_url, imageFailed)}
          </span>
          <span className="account-name">{user.username || "用户"}</span>
        </button>
        {open && (
          <div className="account-popover" aria-label="账户菜单">
            <button
              type="button"
              disabled={disabled}
              onClick={() => {
                setOpen(false);
                setSettings(true);
              }}
            >
              <Settings size={16} />
              账户设置
            </button>
            <button
              type="button"
              className="account-logout"
              disabled={disabled}
              aria-busy={loggingOut}
              onClick={() => {
                onLogout();
              }}
            >
              <LogOut size={16} />
              {loggingOut ? "退出中" : "退出登录"}
            </button>
          </div>
        )}
      </div>
      {settings && (
        <AccountSettingsDialog
          user={user}
          onUser={onUser}
          onClose={() => {
            setSettings(false);
            trigger.current?.focus();
          }}
        />
      )}
    </>
  );
}

function AccountSettingsDialog({
  user,
  onUser,
  onClose,
}: {
  user: User;
  onUser: (user: User) => void;
  onClose: () => void;
}) {
  const { show, setHost } = useNotice();
  const dialog = useRef<HTMLDialogElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const saveGuard = useRef(false);
  const contactGuard = useRef(false);
  const [name, setName] = useState(user.username || "");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
  const [failedImage, setFailedImage] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingAvatar, setSavingAvatar] = useState(false);
  const [contactBusy, setContactBusy] = useState(false);
  const [expanded, setExpanded] = useState<
    "profile" | "phone" | "email" | null
  >(null);
  const busy = savingProfile || savingAvatar || contactBusy;
  const image = preview || user.avatar_url;
  useEffect(() => {
    dialog.current?.showModal();
    setHost(dialog.current);
    return () => setHost(null);
  }, [setHost]);
  useEffect(() => {
    if (!file) {
      setPreview("");
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const close = () => {
    if (saveGuard.current || contactGuard.current || busy) return;
    dialog.current?.close();
    onClose();
  };
  return (
    <dialog
      ref={dialog}
      className="account-dialog"
      role="dialog"
      aria-labelledby="account-settings-title"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      onClick={(e) => {
        if (e.target === dialog.current) close();
      }}
    >
      <header>
        <h2 id="account-settings-title">账户设置</h2>
        <button
          type="button"
          aria-label="关闭账户设置"
          disabled={busy}
          onClick={close}
        >
          <X size={19} />
        </button>
      </header>
      <div className="account-dialog-body">
        <section className="account-profile" aria-label="个人资料">
          <div className="account-profile-summary">
            <button
              type="button"
              className="account-avatar-button"
              aria-label={savingAvatar ? "头像更新中" : "更换头像"}
              aria-busy={savingAvatar}
              title={savingAvatar ? "头像更新中" : "更换头像"}
              disabled={busy}
              onClick={() => {
                if (saveGuard.current || contactGuard.current) return;
                fileInput.current?.click();
              }}
            >
              <span className="user-avatar large">
                {image && failedImage !== image ? (
                  <img
                    src={image}
                    alt=""
                    onError={() => setFailedImage(image)}
                  />
                ) : (
                  <span>{(user.username || "用户").slice(0, 1)}</span>
                )}
              </span>
              <span className="account-avatar-camera" aria-hidden="true">
                <Camera size={13} />
              </span>
            </button>
            <input
              ref={fileInput}
              className="profile-avatar-input"
              type="file"
              aria-label="选择头像图片"
              accept="image/png,image/jpeg,image/webp,image/gif"
              tabIndex={-1}
              disabled={busy}
              onChange={async (e) => {
                const selected = e.target.files?.[0];
                e.target.value = "";
                if (!selected || saveGuard.current || contactGuard.current)
                  return;
                if (
                  ![
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                    "image/gif",
                  ].includes(selected.type)
                ) {
                  show("请选择 PNG、JPEG、WebP 或 GIF 图片", "error");
                  return;
                }
                if (selected.size > 5 * 1024 * 1024) {
                  show("头像不能超过5MiB", "error");
                  return;
                }
                saveGuard.current = true;
                setSavingAvatar(true);
                setFile(selected);
                try {
                  const result = await uploadAvatar(selected);
                  onUser({ ...user, avatar_url: result.avatar_url });
                  show("头像已更新", "success");
                } catch (error) {
                  show(userMessage(error), "error");
                } finally {
                  setFile(null);
                  saveGuard.current = false;
                  setSavingAvatar(false);
                }
              }}
            />
            <strong title={user.username}>{user.username}</strong>
            <button
              type="button"
              className="account-inline-action"
              aria-label={expanded === "profile" ? "收起昵称编辑" : "修改昵称"}
              aria-expanded={expanded === "profile"}
              aria-controls="account-profile-editor"
              disabled={busy}
              onClick={() => {
                if (saveGuard.current || contactGuard.current) return;
                setExpanded(expanded === "profile" ? null : "profile");
              }}
            >
              {expanded === "profile" ? "收起" : "修改昵称"}
              {expanded === "profile" && (
                <ChevronUp size={16} aria-hidden="true" />
              )}
            </button>
          </div>
          <div
            id="account-profile-editor"
            className="account-profile-editor"
            hidden={expanded !== "profile"}
          >
            <form
              className="account-profile-form"
              aria-labelledby="account-profile-title"
              onSubmit={async (e) => {
                e.preventDefault();
                if (saveGuard.current || contactGuard.current || busy) return;
                saveGuard.current = true;
                setSavingProfile(true);
                const username =
                  name.trim() !== user.username ? name.trim() : undefined;
                try {
                  await saveProfileSettings(
                    { username, avatar: null },
                    {
                      updateUsername: (username) =>
                        api("/profile", "PATCH", { username }),
                      uploadAvatar,
                      avatarSaved: () => {},
                      readUser: () => api("/auth/session"),
                      onUser,
                    },
                  );
                  show(
                    username !== undefined ? "昵称已保存" : "昵称未更改",
                    username !== undefined ? "success" : "info",
                  );
                } catch (e) {
                  if (e instanceof AccountSettingsSaveError) {
                    const detail = userMessage(e.reason);
                    show(
                      e.profileSaved
                        ? e.stage === "readback"
                          ? "昵称已保存，请刷新页面"
                          : `昵称已部分保存：${detail}`
                        : detail,
                      e.profileSaved && e.stage === "readback"
                        ? "info"
                        : "error",
                    );
                  } else show(userMessage(e), "error");
                } finally {
                  saveGuard.current = false;
                  setSavingProfile(false);
                }
              }}
            >
              <h3 id="account-profile-title" className="visually-hidden">
                个人资料
              </h3>
              <label>
                昵称
                <input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                  }}
                  maxLength={32}
                  required
                  disabled={busy}
                  autoComplete="nickname"
                  placeholder="请输入昵称（1～32 个字符）"
                />
              </label>
              <button
                className="primary"
                disabled={busy || !name.trim()}
                aria-busy={savingProfile}
              >
                {savingProfile ? "保存中" : "保存昵称"}
              </button>
            </form>
          </div>
        </section>
        <ContactSettings
          user={user}
          channel={
            expanded === "phone" || expanded === "email" ? expanded : null
          }
          onChannelChange={setExpanded}
          disabled={savingProfile || savingAvatar}
          onBusy={(value) => {
            contactGuard.current = value;
            setContactBusy(value);
          }}
          onUser={onUser}
        />
      </div>
    </dialog>
  );
}

export function WechatSettings({
  busy,
  show,
}: {
  busy: boolean;
  show: (text: string, kind?: any) => void;
}) {
  const [settings, setSettings] = useState<User | null>(null);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const { reportFailure, recovered } = useNotice();
  const loadRequest = useRef(0);
  const mounted = useRef(true);
  const dirtyToken = useRef(false);
  const savingToken = useRef(false);
  const load = async (signal: AbortSignal) => {
    if (savingToken.current) return;
    const current = ++loadRequest.current;
    try {
      const value = await api("/wechat-config", "GET", undefined, signal);
      if (signal.aborted || !mounted.current || current !== loadRequest.current)
        return;
      setSettings((previous) =>
        dirtyToken.current && previous
          ? { ...value, revision: previous.revision }
          : value,
      );
      if (!dirtyToken.current) setToken(value.token);
      setError("");
      recovered("wechat-config");
    } catch (e) {
      if (signal.aborted || !mounted.current || current !== loadRequest.current)
        return;
      const text = userMessage(e);
      setError(text);
      reportFailure("wechat-config", text);
    }
  };
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ++loadRequest.current;
    };
  }, []);
  useAutoRefresh(load, { enabled: !saving });
  const state: Record<string, string> = {
    configured: "已绑定",
    unconfigured: "未绑定",
    binding: "绑定中",
    uncertain: "结果待确认",
    failed: "绑定失败",
  };
  return (
    <section className="panel wechat-settings" aria-label="微信接入">
      <PanelHeading title="微信接入">
        <span
          className={`badge ${settings?.state === "configured" ? "accepted" : "queued"}`}
        >
          {error ? "读取失败" : settings ? state[settings.state] : "读取中"}
        </span>
      </PanelHeading>
      {settings && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (savingToken.current) return;
            savingToken.current = true;
            ++loadRequest.current;
            setConfirming(settings.state === "uncertain");
            setSaving(true);
            setError("");
            try {
              const value = await api("/wechat-config", "PUT", {
                token,
                revision: settings.revision,
              });
              dirtyToken.current = false;
              setSettings(value);
              setToken(value.token);
              show("绑定成功", "success");
            } catch (e) {
              show(userMessage(e), "error");
              setSettings(await api("/wechat-config").catch(() => settings));
            } finally {
              savingToken.current = false;
              setSaving(false);
            }
          }}
        >
          <label className="wechat-token-field">
            <span>接入 Token</span>
            <input
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={token}
              onChange={(e) => {
                dirtyToken.current = true;
                setToken(e.target.value);
              }}
              maxLength={1000}
              placeholder="请输入平台 Token"
              disabled={busy}
              readOnly={saving}
              aria-busy={saving}
            />
          </label>
          <div className="wechat-config-actions">
            <button
              className="primary"
              disabled={busy || settings.state === "binding"}
              aria-disabled={saving || busy || settings.state === "binding"}
              aria-busy={saving}
            >
              {saving
                ? confirming
                  ? "确认中"
                  : "绑定中"
                : settings.state === "binding"
                  ? "绑定中"
                  : settings.state === "uncertain"
                    ? "重新确认绑定"
                    : "保存并绑定"}
            </button>
          </div>
          <label className="wechat-callback-field">
            <span>消息接收地址</span>
            <input
              type="text"
              spellCheck={false}
              value={settings.callback_url}
              readOnly
              title="点击复制消息接收地址"
              className="callback-copy"
              aria-label="消息接收地址（系统自动生成，点击复制）"
              onClick={(event) => {
                const input = event.currentTarget;
                Promise.resolve()
                  .then(() => navigator.clipboard.writeText(input.value))
                  .then(() => show("消息接收地址已复制", "success"))
                  .catch(() => {
                    input.select();
                    show("复制失败，请手动选择地址复制", "error");
                  });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  event.currentTarget.click();
                }
              }}
            />
            <small>
              地址由系统自动生成，保存时自动绑定，用于接收微信消息。
            </small>
          </label>
        </form>
      )}
    </section>
  );
}
