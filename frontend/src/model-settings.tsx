import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { api } from "./api";
import { useNotice } from "./notices";
import { PanelHeading } from "./panel-heading";
import { useAutoRefresh } from "./use-auto-refresh";
import { userMessage } from "./user-messages";

type Row = Record<string, any>;

function ApiKeyLabel({
  inputId,
  provider,
  href,
}: {
  inputId: string;
  provider: string;
  href: string;
}) {
  return (
    <div className="model-key-heading">
      <label htmlFor={inputId}>API 密钥</label>
      <a
        className="model-key-link"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`获取 ${provider} API Key（新标签页打开）`}
      >
        获取 API Key <ArrowUpRight size={14} aria-hidden="true" />
      </a>
    </div>
  );
}

export function ModelSettings({ busy }: { busy: boolean }) {
  const { show, reportFailure, recovered } = useNotice();
  const [settings, setSettings] = useState<Row>({});
  const [key, setKey] = useState("");
  const [asrKey, setAsrKey] = useState("");
  const [weatherHost, setWeatherHost] = useState("");
  const [weatherKey, setWeatherKey] = useState("");
  const weatherDirty = useRef(false);
  const [saving, setSaving] = useState<"model" | "asr" | "weather" | null>(
    null,
  );
  const pending = useRef(false);
  const revision = useRef(0);
  const request = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ++request.current;
    };
  }, []);
  const apply = (value: Row) => {
    if ((value.model_revision ?? 0) < revision.current) return;
    revision.current = value.model_revision ?? 0;
    setSettings(value);
    if (!weatherDirty.current) setWeatherHost(value.weather?.api_host ?? "");
    recovered("manager-settings");
  };
  const refresh = useAutoRefresh(async (signal) => {
    if (pending.current) return;
    const current = ++request.current;
    try {
      const value = await api("/settings", "GET", undefined, signal);
      if (!signal.aborted && mounted.current && current === request.current)
        apply(value);
    } catch (error) {
      if (!signal.aborted && mounted.current && current === request.current)
        reportFailure("manager-settings", userMessage(error));
    }
  });

  const disabled = busy || settings.model_revision === undefined;
  const saveWeather = async () => {
    if (disabled || pending.current) return;
    const host = weatherHost.trim();
    if (!host || (!weatherKey.trim() && !settings.weather?.key_configured)) {
      show(
        !host ? "请填写和风天气 API Host" : "请填写和风天气 API 密钥",
        "error",
      );
      return;
    }
    pending.current = true;
    ++request.current;
    setSaving("weather");
    try {
      const result = await api("/settings", "PATCH", {
        weather: { api_host: host, api_key: weatherKey.trim() },
      });
      if (mounted.current) {
        weatherDirty.current = false;
        apply(result);
        setWeatherKey("");
      }
      show("天气配置已保存", "success");
    } catch (error) {
      show(userMessage(error), "error");
      pending.current = false;
      refresh();
    } finally {
      pending.current = false;
      if (mounted.current) setSaving(null);
    }
  };
  const saveKey = async (provider: "model" | "asr") => {
    if (disabled || pending.current) return;
    const value = (provider === "model" ? key : asrKey).trim();
    if (!value) {
      show(
        provider === "model"
          ? "请填写 DeepSeek API 密钥"
          : "请填写阿里云百炼 API 密钥",
        "error",
      );
      return;
    }
    pending.current = true;
    ++request.current;
    setSaving(provider);
    try {
      const result = await api("/settings", "PATCH", {
        [provider === "model" ? "model_key" : "asr_key"]: value,
      });
      if (mounted.current) {
        apply(result);
        if (provider === "model") setKey("");
        else setAsrKey("");
      }
      show(
        provider === "model" ? "DeepSeek 密钥已保存" : "语音密钥已保存",
        "success",
      );
    } catch (error) {
      show(userMessage(error), "error");
      pending.current = false;
      refresh();
    } finally {
      pending.current = false;
      if (mounted.current) setSaving(null);
    }
  };
  return (
    <section className="panel model-settings" aria-label="AI 模型设置">
      <PanelHeading title="AI 模型设置" />
      <div className="model-settings-form">
        <form
          className="model-provider-section"
          aria-label="DeepSeek 密钥设置"
          onSubmit={(event) => {
            event.preventDefault();
            void saveKey("model");
          }}
        >
          <div className="model-provider-heading">
            <h3>
              智能回复 ·{" "}
              <a
                className="provider-source-link"
                href="https://www.deepseek.com"
                target="_blank"
                rel="noopener noreferrer"
              >
                DeepSeek
              </a>
            </h3>
            <span
              className={
                "badge " +
                (settings.model_key_configured ? "accepted" : "queued")
              }
            >
              {settings.model_key_configured ? "已配置" : "未配置"}
            </span>
          </div>
          <div className="form-columns model-fields">
            <label>
              模型名称
              <input
                className="model-name"
                aria-label="DeepSeek 模型名称"
                value="deepseek-flash"
                readOnly
              />
            </label>
            <div className="model-key-field">
              <ApiKeyLabel
                inputId="deepseek-api-key"
                provider="DeepSeek"
                href="https://platform.deepseek.com/api_keys"
              />
              <input
                id="deepseek-api-key"
                aria-label="DeepSeek API Key"
                type="password"
                autoComplete="off"
                value={key}
                disabled={disabled}
                readOnly={saving === "model"}
                aria-busy={saving === "model"}
                placeholder={
                  settings.model_key_configured
                    ? settings.model_key_preview || "留空保留原密钥"
                    : "请输入 DeepSeek API 密钥"
                }
                onChange={(event) => setKey(event.target.value)}
              />
            </div>
          </div>
          <button
            className="primary"
            aria-label="保存 DeepSeek 密钥"
            disabled={disabled}
            aria-disabled={disabled || !!saving}
            aria-busy={saving === "model"}
          >
            {saving === "model" ? "保存中" : "保存密钥"}
          </button>
        </form>
        <form
          className="model-provider-section asr-provider-section"
          aria-label="语音密钥设置"
          onSubmit={(event) => {
            event.preventDefault();
            void saveKey("asr");
          }}
        >
          <div className="model-provider-heading">
            <h3>
              语音转写 ·{" "}
              <a
                className="provider-source-link"
                href="https://www.aliyun.com/product/bailian"
                target="_blank"
                rel="noopener noreferrer"
              >
                阿里云百炼
              </a>
            </h3>
            <span
              className={
                "badge " +
                (settings.asr?.key_configured ? "accepted" : "queued")
              }
            >
              {settings.asr?.key_configured ? "已配置" : "未配置"}
            </span>
          </div>
          <div className="form-columns model-fields">
            <label>
              模型名称
              <input
                className="model-name"
                aria-label="语音模型名称"
                value={settings.asr?.model ?? "qwen-audio-3.1-asr-flash"}
                readOnly
              />
            </label>
            <div className="model-key-field">
              <ApiKeyLabel
                inputId="bailian-api-key"
                provider="阿里云百炼"
                href="https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key"
              />
              <input
                id="bailian-api-key"
                type="password"
                autoComplete="off"
                aria-label="阿里云百炼 API Key"
                value={asrKey}
                disabled={disabled}
                readOnly={saving === "asr"}
                aria-busy={saving === "asr"}
                placeholder={
                  settings.asr?.key_configured
                    ? settings.asr.key_preview || "留空保留原密钥"
                    : "请输入阿里云百炼 API 密钥"
                }
                onChange={(event) => setAsrKey(event.target.value)}
              />
            </div>
          </div>
          <button
            className="primary"
            aria-label="保存语音密钥"
            disabled={disabled}
            aria-disabled={disabled || !!saving}
            aria-busy={saving === "asr"}
          >
            {saving === "asr" ? "保存中" : "保存密钥"}
          </button>
        </form>
        <form
          className="model-provider-section weather-provider-section"
          aria-label="天气配置"
          onSubmit={(event) => {
            event.preventDefault();
            void saveWeather();
          }}
        >
          <div className="model-provider-heading">
            <h3>
              天气查询 ·{" "}
              <a
                className="provider-source-link"
                href="https://www.qweather.com"
                target="_blank"
                rel="noopener noreferrer"
              >
                和风天气
              </a>
            </h3>
            <span
              className={
                "badge " +
                (settings.weather?.key_configured ? "accepted" : "queued")
              }
            >
              {settings.weather?.key_configured ? "已配置" : "未配置"}
            </span>
          </div>
          <div className="form-columns model-fields">
            <label>
              API Host
              <input
                aria-label="和风天气 API Host"
                autoComplete="off"
                value={weatherHost}
                disabled={disabled}
                readOnly={saving === "weather"}
                maxLength={253}
                placeholder="请输入和风天气域名"
                onChange={(event) => {
                  weatherDirty.current = true;
                  setWeatherHost(event.target.value);
                }}
              />
            </label>
            <div className="model-key-field">
              <ApiKeyLabel
                inputId="qweather-api-key"
                provider="和风天气"
                href="https://console.qweather.com/project"
              />
              <input
                id="qweather-api-key"
                aria-label="和风天气 API Key"
                type="password"
                autoComplete="off"
                value={weatherKey}
                disabled={disabled}
                readOnly={saving === "weather"}
                placeholder={
                  settings.weather?.key_configured
                    ? settings.weather.key_preview || "留空保留原密钥"
                    : "请输入和风天气 API 密钥"
                }
                onChange={(event) => setWeatherKey(event.target.value)}
              />
            </div>
          </div>
          <button
            className="primary"
            disabled={disabled}
            aria-disabled={disabled || !!saving}
            aria-busy={saving === "weather"}
          >
            {saving === "weather" ? "保存中" : "保存配置"}
          </button>
        </form>
      </div>
    </section>
  );
}
