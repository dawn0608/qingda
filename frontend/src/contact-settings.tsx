import { useEffect, useRef, useState } from "react";
import { ChevronUp, Mail, ShieldCheck, Smartphone } from "lucide-react";
import { api, ApiError, setCsrf } from "./api";
import {
  codeCooldownKey,
  useCountdown,
  type AuthSession,
} from "./authentication";
import { useNotice } from "./notices";
import { userMessage } from "./user-messages";
import { maskedContact, type ContactChannel } from "./contact-display";

export function ContactSettings({
  user,
  channel,
  onChannelChange,
  disabled,
  onUser,
  onBusy,
}: {
  user: Record<string, any>;
  channel: ContactChannel | null;
  onChannelChange: (channel: ContactChannel | null) => void;
  disabled: boolean;
  onUser: (value: AuthSession) => void;
  onBusy: (value: boolean) => void;
}) {
  const [target, setTarget] = useState("");
  const [challenge, setChallenge] = useState("");
  const [code, setCode] = useState("");
  const [pending, setPending] = useState<"sending" | "submitting" | null>(null);
  const busy = pending !== null;
  const { show } = useNotice();
  const guard = useRef(false);
  const targetInput = useRef<HTMLInputElement>(null);
  const cooldown = useCountdown(
    channel ? codeCooldownKey(channel, target) : undefined,
  );
  const available =
    channel === "phone" ? user.sms_available : user.email_available;
  const label = channel === "phone" ? "手机号" : "邮箱";
  const codeLabel = channel === "phone" ? "短信验证码" : "邮箱验证码";
  const ContactIcon = channel === "phone" ? Smartphone : Mail;
  const bound = channel ? Boolean(user[channel]) : false;
  const validTarget =
    channel === "phone"
      ? /^1[0-9]{10}$/.test(target.trim())
      : /^[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+$/.test(target.trim());
  useEffect(() => {
    setChallenge("");
    setCode("");
  }, [target, channel]);
  useEffect(() => {
    setTarget("");
    if (channel) {
      targetInput.current?.focus();
      if (!available) show(`${codeLabel}服务暂不可用，请稍后再试`, "error");
    }
  }, [channel]);
  const reset = () => {
    onChannelChange(null);
    setTarget("");
    setChallenge("");
    setCode("");
  };
  const act = async (
    operation: "sending" | "submitting",
    action: () => Promise<void>,
  ) => {
    if (guard.current || disabled) return;
    guard.current = true;
    onBusy(true);
    setPending(operation);
    try {
      await action();
    } catch (e) {
      show(userMessage(e), "error");
      if (e instanceof ApiError && e.retryAfter) cooldown.start(e.retryAfter);
    } finally {
      guard.current = false;
      setPending(null);
      onBusy(false);
    }
  };
  const complete = (value: AuthSession) => {
    setCsrf(value.csrf);
    onUser(value);
    reset();
    show(`${label}已${bound ? "更换" : "绑定"}`, "success");
  };
  const editor = channel && (
    <form
      className="account-contact-edit"
      aria-label={`${bound ? "更换" : "绑定"}${label}`}
      onSubmit={(e) => {
        e.preventDefault();
        void act("submitting", async () => {
          let result;
          try {
            result = await api("/auth/contacts", "PUT", {
              channel,
              identifier: target.trim(),
              challenge_id: challenge,
              code,
            });
          } catch (failure) {
            // 写入返回中断时先回读；不能引导用户重放已完成的绑定操作。
            const current = await api("/auth/session").catch(() => null);
            if (
              current?.user_id === user.user_id &&
              current[channel] === target.trim().toLowerCase()
            ) {
              complete(current);
              return;
            }
            throw failure;
          }
          complete(result);
        });
      }}
    >
      <div className="account-contact-input">
        <ContactIcon size={18} aria-hidden="true" />
        <input
          ref={targetInput}
          aria-label={`${bound ? "新" : ""}${label}`}
          type={channel === "email" ? "email" : "tel"}
          inputMode={channel === "email" ? "email" : "tel"}
          autoComplete={channel === "email" ? "email" : "tel-national"}
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          required
          maxLength={channel === "phone" ? 11 : 254}
          placeholder={`请输入${bound ? "新" : ""}${label}`}
          disabled={busy || disabled}
        />
      </div>
      <div className="auth-code-row">
        <div className="account-contact-input">
          <ShieldCheck size={18} aria-hidden="true" />
          <input
            aria-label={codeLabel}
            placeholder="6 位验证码"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
            required
            title={!available ? `${codeLabel}服务暂不可用` : undefined}
            disabled={busy || disabled || !available}
          />
        </div>
        <button
          type="button"
          className="secondary"
          aria-busy={pending === "sending"}
          disabled={
            busy ||
            disabled ||
            !available ||
            !validTarget ||
            cooldown.seconds > 0
          }
          onClick={() =>
            void act("sending", async () => {
              const result = await api("/auth/contacts/code/send", "POST", {
                channel,
                identifier: target.trim(),
              });
              setChallenge(result.challenge_id);
              setCode("");
              cooldown.start(result.retry_after);
              show("验证码已发送，5分钟内有效", "success");
            })
          }
        >
          {pending === "sending"
            ? "发送中"
            : cooldown.seconds > 0
              ? `${cooldown.seconds} 秒后重发`
              : "获取验证码"}
        </button>
      </div>
      <div className="account-contact-actions">
        <button
          className="primary"
          aria-busy={pending === "submitting"}
          disabled={
            busy ||
            disabled ||
            !available ||
            !validTarget ||
            !challenge ||
            !/^[0-9]{6}$/.test(code)
          }
        >
          {pending === "submitting"
            ? `${bound ? "更换" : "绑定"}中`
            : `确认${bound ? "更换" : "绑定"}`}
        </button>
      </div>
    </form>
  );
  return (
    <section
      className="account-contacts"
      aria-labelledby="account-contacts-title"
    >
      <h3 id="account-contacts-title" className="account-section-title">
        联系方式
      </h3>
      <div className="account-contact-list">
        {(["phone", "email"] as const).map((kind) => {
          const Icon = kind === "phone" ? Smartphone : Mail;
          const name = kind === "phone" ? "手机号" : "邮箱";
          const expanded = channel === kind;
          return (
            <div className="account-contact-item" key={kind}>
              <div className="account-contact-row">
                <span className="account-contact-icon">
                  <Icon size={22} aria-hidden="true" />
                </span>
                <div className="account-contact-copy">
                  <strong>{name}</strong>
                  <span className={!user[kind] ? "unbound" : ""}>
                    {maskedContact(user[kind], kind)}
                  </span>
                </div>
                <button
                  type="button"
                  className="account-inline-action"
                  aria-label={
                    expanded
                      ? `收起${name}`
                      : `${user[kind] ? "更换" : "绑定"}${name}`
                  }
                  aria-expanded={expanded}
                  aria-controls={`account-contact-editor-${kind}`}
                  disabled={busy || disabled}
                  onClick={() => {
                    if (guard.current || disabled) return;
                    reset();
                    if (!expanded) onChannelChange(kind);
                  }}
                >
                  {expanded ? "收起" : user[kind] ? "更换" : "绑定"}
                  {expanded && <ChevronUp size={16} aria-hidden="true" />}
                </button>
              </div>
              <div id={`account-contact-editor-${kind}`} hidden={!expanded}>
                {expanded && editor}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
