import { FilingNotice } from "./filing-notice";
import { useEffect, useRef, useState } from "react";
import { ShieldCheck, Smartphone } from "lucide-react";
import { api, ApiError } from "./api";
import { userMessage, type NoticeKind } from "./user-messages";
import brandMark from "./assets/qingda-logo.png";
import "./authentication.css";

export type AuthSession = {
  phone: string;
  email: string;
  user_id: number;
  channel: "email" | "phone";
  identifier: string;
  username: string;
  avatar_url: string | null;
  email_available: boolean;
  csrf: string;
  sms_available: boolean;
};

export function codeCooldownKey(channel: string, identifier: string) {
  return `auth-code-cooldown:${channel}:${identifier.trim().toLowerCase()}`;
}

function savedUntil(key?: string) {
  if (!key) return 0;
  try {
    const value = Number(sessionStorage.getItem(key));
    return Number.isFinite(value) && value > Date.now() ? value : 0;
  } catch {
    return 0;
  }
}

export function useCountdown(storageKey?: string) {
  const [until, setUntil] = useState(() => savedUntil(storageKey));
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setUntil(savedUntil(storageKey));
  }, [storageKey]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return {
    seconds: Math.max(0, Math.ceil((until - now) / 1000)),
    start: (seconds: number) => {
      const deadline = Math.max(
        savedUntil(storageKey),
        Date.now() + seconds * 1000,
      );
      setNow(Date.now());
      setUntil(deadline);
      if (storageKey) {
        try {
          sessionStorage.setItem(storageKey, String(deadline));
        } catch {
          /* 服务端仍强制限流。 */
        }
      }
    },
    now,
  };
}

export function useVerificationCode(identifier: string, purpose: "login") {
  const [challenge, setChallenge] = useState("");
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const pending = useRef(false);
  const channel = identifier.includes("@") ? "email" : "phone";
  const cooldown = useCountdown(codeCooldownKey(channel, identifier));
  useEffect(() => {
    setChallenge("");
    setCode("");
  }, [identifier, purpose]);
  return {
    challenge,
    code,
    setCode,
    sending,
    seconds: cooldown.seconds,
    async send() {
      if (pending.current || cooldown.seconds > 0) return;
      pending.current = true;
      setSending(true);
      try {
        const data = await api("/auth/code/send", "POST", {
          channel,
          identifier,
          purpose,
        });
        setChallenge(data.challenge_id);
        setCode("");
        cooldown.start(data.retry_after);
      } catch (error) {
        if (error instanceof ApiError && error.retryAfter)
          cooldown.start(error.retryAfter);
        if (!(error instanceof ApiError) || error.status !== 429) {
          setChallenge("");
          setCode("");
        }
        throw error;
      } finally {
        pending.current = false;
        setSending(false);
      }
    },
  };
}

type CodeState = ReturnType<typeof useVerificationCode>;

function LoginArtwork() {
  return (
    <aside className="login-visual" aria-label="青答 微信智能助手">
      <div className="login-brand">
        <img src={brandMark} alt="" width="32" height="32" />
        <span>青答</span>
      </div>
      <div className="login-visual-center">
        <svg
          className="login-artwork"
          viewBox="0 0 480 380"
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <linearGradient
              id="login-bubble-green"
              x1="0"
              y1="0"
              x2="0.7"
              y2="1"
            >
              <stop stopColor="var(--illustration-light)" />
              <stop offset="0.5" stopColor="var(--illustration-main)" />
              <stop offset="1" stopColor="var(--illustration-dark)" />
            </linearGradient>
            <linearGradient
              id="login-bubble-white"
              x1="0"
              y1="0"
              x2="0.6"
              y2="1"
            >
              <stop stopColor="var(--surface)" />
              <stop offset="0.65" stopColor="var(--surface)" />
              <stop offset="1" stopColor="var(--surface-active)" />
            </linearGradient>
            <filter
              id="login-bubble-shadow"
              x="-40%"
              y="-40%"
              width="180%"
              height="200%"
            >
              <feDropShadow
                dx="0"
                dy="18"
                stdDeviation="14"
                floodColor="var(--brand)"
                floodOpacity="0.14"
              />
              <feDropShadow
                dx="0"
                dy="3"
                stdDeviation="3"
                floodColor="var(--brand)"
                floodOpacity="0.06"
              />
            </filter>
            <radialGradient id="login-ground-shadow">
              <stop stopColor="var(--brand)" stopOpacity="0.14" />
              <stop offset="1" stopColor="var(--brand)" stopOpacity="0" />
            </radialGradient>
          </defs>
          <ellipse
            cx="242"
            cy="337"
            rx="167"
            ry="24"
            fill="url(#login-ground-shadow)"
          />
          <g className="login-bubble login-bubble-back">
            <path
              d="M201 49c-76 0-137 45-137 102 0 32 20 61 52 80l-14 42 50-25c15 4 32 6 49 6 76 0 137-46 137-103S277 49 201 49Z"
              fill="url(#login-bubble-green)"
              stroke="var(--illustration-light)"
              strokeWidth="1.5"
              filter="url(#login-bubble-shadow)"
            />
            <path
              d="M96 114c19-29 57-48 101-49"
              fill="none"
              stroke="var(--illustration-light)"
              strokeOpacity="0.55"
              strokeWidth="3"
              strokeLinecap="round"
            />
            <g fill="var(--brand)">
              <circle cx="153" cy="137" r="10" />
              <circle cx="243" cy="137" r="10" />
            </g>
          </g>
          <g className="login-bubble login-bubble-front">
            <path
              d="M297 144v28"
              stroke="var(--surface)"
              strokeWidth="6"
              strokeLinecap="round"
            />
            <circle
              className="login-agent-signal"
              cx="297"
              cy="140"
              r="13"
              fill="var(--surface)"
              opacity="0.5"
            />
            <circle cx="297" cy="140" r="6" fill="var(--surface)" />
            <path
              d="M297 159c-65 0-117 36-117 82s52 84 117 84c14 0 28-2 41-6l40 23-10-36c28-16 46-39 46-65 0-46-52-82-117-82Z"
              fill="url(#login-bubble-white)"
              stroke="var(--surface)"
              strokeWidth="1.5"
              filter="url(#login-bubble-shadow)"
            />
            <rect
              x="236"
              y="208"
              width="122"
              height="60"
              rx="24"
              fill="var(--surface-active)"
            />
            <g
              stroke="var(--brand)"
              strokeWidth="6"
              strokeLinecap="round"
              fill="none"
            >
              <path d="M263 228v7M331 228v7" />
              <path d="M283 249q14 10 28 0" strokeWidth="4" />
            </g>
            <path
              d="M271 284h52"
              stroke="var(--brand-border)"
              strokeWidth="5"
              strokeLinecap="round"
            />
            <path
              d="m382 149 5 14 14 5-14 5-5 14-5-14-14-5 14-5Z"
              fill="var(--surface)"
              stroke="var(--brand-border)"
              strokeWidth="1.5"
            />
          </g>
        </svg>
        <div className="login-visual-copy">
          <h1>让回应，更合心意</h1>
          <p>读懂对话脉络，延续你的表达</p>
        </div>
      </div>
    </aside>
  );
}

function CodeFields({
  sms,
  disabled,
  send,
  login = false,
  required = true,
  label = "验证码",
  destination,
}: {
  sms: CodeState;
  disabled: boolean;
  send: () => void;
  login?: boolean;
  required?: boolean;
  label?: string;
  destination?: string;
}) {
  const input = (
    <input
      aria-label={label}
      value={sms.code}
      onChange={(e) => sms.setCode(e.target.value)}
      inputMode="numeric"
      autoComplete="one-time-code"
      pattern="[0-9]{6}"
      maxLength={6}
      required={required}
      disabled={disabled}
      placeholder={login ? "请输入 6 位验证码" : `6 位${label}`}
    />
  );
  const row = (
    <div className="auth-code-row">
      {login ? (
        <div className="login-input">
          <ShieldCheck aria-hidden="true" />
          {input}
        </div>
      ) : (
        input
      )}
      <button
        type="button"
        disabled={disabled || sms.sending || sms.seconds > 0}
        aria-busy={sms.sending}
        onClick={send}
      >
        {sms.sending
          ? "发送中"
          : sms.seconds > 0
            ? `${sms.seconds} 秒后重发`
            : "获取验证码"}
      </button>
    </div>
  );
  if (login) return row;
  return (
    <label>
      {destination ? (
        <span className="account-code-label">
          <span>{label}</span>
          <span className="account-code-destination" title={destination}>
            {destination}
          </span>
        </span>
      ) : (
        label
      )}
      {row}
    </label>
  );
}

export function Login({
  onLogin,
  onNotice,
}: {
  onLogin: (session: AuthSession) => void;
  onNotice: (message: string, kind?: NoticeKind) => void;
}) {
  const [identifier, setIdentifier] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const pending = useRef(false);
  const [options, setOptions] = useState({
    sms_available: false,
    email_available: false,
    registration_enabled: false,
  });
  const channel = identifier.includes("@") ? "email" : "phone";
  const normalized = identifier.trim().toLowerCase();
  const valid =
    channel === "phone"
      ? /^1[0-9]{10}$/.test(normalized)
      : /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized);
  const available =
    channel === "phone" ? options.sms_available : options.email_available;
  const sms = useVerificationCode(normalized, "login");
  const cooldown = useCountdown();
  useEffect(() => {
    api("/auth/options")
      .then(setOptions)
      .catch((e) => onNotice(userMessage(e)));
  }, [onNotice]);
  const run = async (action: () => Promise<void>, submission = true) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setSubmitting(submission);
    onNotice("");
    try {
      await action();
    } catch (error) {
      onNotice(userMessage(error));
      if (submission && error instanceof ApiError && error.retryAfter)
        cooldown.start(error.retryAfter);
    } finally {
      pending.current = false;
      setBusy(false);
      setSubmitting(false);
    }
  };
  const canSubmit =
    valid &&
    !busy &&
    cooldown.seconds === 0 &&
    available &&
    Boolean(sms.challenge) &&
    /^[0-9]{6}$/.test(sms.code);
  return (
    <main className="login-page">
      <div className="login-layout">
        <LoginArtwork />
        <section className="login-form-panel" aria-labelledby="login-title">
          <div className="login-form-content">
            <h2 id="login-title">登录青答</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!canSubmit) return;
                void run(async () => {
                  const session = await api("/auth/login", "POST", {
                    channel,
                    identifier: normalized,
                    method: "code",
                    challenge_id: sms.challenge,
                    code: sms.code,
                  });
                  onLogin(session);
                });
              }}
            >
              <div className="login-input">
                <Smartphone aria-hidden="true" />
                <input
                  aria-label="邮箱或手机号"
                  autoComplete="username"
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  placeholder="请输入邮箱或手机号"
                  maxLength={254}
                  required
                  disabled={busy}
                />
              </div>
              {
                <CodeFields
                  login
                  sms={sms}
                  disabled={busy || !valid || !available}
                  send={() =>
                    void run(async () => {
                      await sms.send();
                      onNotice("验证码已发送，5分钟内有效", "success");
                    }, false)
                  }
                />
              }
              <button
                className="primary full"
                disabled={!canSubmit}
                aria-busy={submitting}
              >
                {submitting
                  ? "登录中"
                  : valid && !available
                    ? channel === "email"
                      ? "邮箱验证码暂不可用"
                      : "短信验证码暂不可用"
                    : "登录"}
              </button>
              <p className="login-auto-register-hint">
                {options.registration_enabled
                  ? "未注册账号验证后自动注册"
                  : "当前仅支持已有账号登录"}
              </p>
            </form>
          </div>
        </section>
      </div>
      <FilingNotice />
    </main>
  );
}
