import { useEffect, useRef, useState } from "react";
import { useNotice } from "./notices";

export type HistoryVoice = {
  url: string;
  duration_ms: number;
  transcript: string;
};

export function useVoicePlayback(scope: string) {
  const element = useRef<HTMLAudioElement>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const sequence = useRef(0);
  const [active, setActive] = useState("");
  const [state, setState] = useState<"idle" | "loading" | "playing">("idle");
  const { show } = useNotice();

  const stop = () => {
    sequence.current++;
    const player = audio.current;
    audio.current = null;
    if (player) {
      player.pause();
      player.removeAttribute("src");
      player.load();
    }
    setActive("");
    setState("idle");
  };
  useEffect(() => {
    stop();
    const hidden = () => {
      if (document.hidden) stop();
    };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [scope]);

  const toggle = (voice: HistoryVoice) => {
    const same = audio.current?.getAttribute("src") === voice.url;
    if (same && active) {
      if (state === "loading") stop();
      else {
        audio.current!.pause();
        setActive("");
        setState("idle");
      }
      return;
    }
    if (!same) stop();
    const player = element.current;
    if (!player) return;
    const attempt = ++sequence.current;
    audio.current = player;
    setActive(voice.url);
    setState("loading");
    player.onplaying = () => {
      if (sequence.current === attempt) setState("playing");
    };
    player.onwaiting = () => {
      if (sequence.current === attempt) setState("loading");
    };
    player.onended = () => {
      if (sequence.current === attempt) stop();
    };
    const failure = () => {
      if (sequence.current !== attempt) return;
      stop();
      show("语音暂时无法播放，请稍后重试", "error");
    };
    player.onerror = failure;
    if (!same) player.src = voice.url;
    void player.play().catch(failure);
  };
  return { element, active, state, toggle, stop };
}

export function ChatVoice({
  voice,
  playback,
  query,
}: {
  voice: HistoryVoice;
  playback: ReturnType<typeof useVoicePlayback>;
  query: string;
}) {
  const seconds = Math.ceil(voice.duration_ms / 1000);
  const active = playback.active === voice.url;
  const loading = active && playback.state === "loading";
  const playing = active && playback.state === "playing";
  const matched =
    query && `[语音]\n${seconds}秒`.toLowerCase().includes(query.toLowerCase());
  return (
    <button
      type="button"
      className={`chat-voice-button${playing ? " playing" : ""}`}
      style={{ width: Math.min(240, 88 + Math.max(0, seconds) * 3) }}
      aria-label={`${active ? (loading ? "取消加载" : "暂停") : "播放"}语音${seconds > 0 ? `，${seconds}秒` : ""}`}
      aria-pressed={active}
      aria-busy={loading}
      onClick={() => playback.toggle(voice)}
    >
      <svg className="chat-voice-wave" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="5" cy="12" r="1.8" fill="currentColor" />
        <path className="voice-wave-middle" d="M10 7a8 8 0 0 1 0 10" />
        <path className="voice-wave-outer" d="M15 3a14 14 0 0 1 0 18" />
      </svg>
      <span className="chat-voice-duration" aria-hidden="true">
        {matched ? (
          <mark>{seconds > 0 ? `${seconds}″` : "语音"}</mark>
        ) : loading ? (
          "加载中"
        ) : seconds > 0 ? (
          `${seconds}″`
        ) : (
          "语音"
        )}
      </span>
    </button>
  );
}
