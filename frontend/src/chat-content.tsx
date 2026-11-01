import { useState, type ClipboardEvent, type ReactNode } from "react";
import catalog from "../../backend/wechat_manager/native_emotions.json";
import resources from "./assets/native-emotions/manifest.json";

const aliases: Record<string, string> = catalog;
const pictures: Record<string, { file: string }> = resources;
const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const emotions = new RegExp(
  Object.keys(aliases)
    .sort((a, b) => b.length - a.length)
    .map(escape)
    .join("|"),
  "g",
);

function Emotion({ original }: { original: string }) {
  const [failed, setFailed] = useState(false);
  const resource = pictures[aliases[original]];
  if (failed || !resource) return original;
  return (
    <img
      className="chat-emotion"
      src={`${import.meta.env.BASE_URL}assets/native-emotions/${resource.file}`}
      alt={original}
      title={original}
      data-emotion={original}
      draggable={false}
      onError={() => setFailed(true)}
    />
  );
}

export function ChatText({ text, query }: { text: string; query: string }) {
  const matches = query
    ? [...text.matchAll(new RegExp(escape(query), "gi"))].map((match) => [
        match.index!,
        match.index! + match[0].length,
      ])
    : [];
  const nodes: ReactNode[] = [];
  const plain = (start: number, end: number) => {
    let at = start;
    for (const [left, right] of matches) {
      if (right <= start || left >= end) continue;
      const begin = Math.max(start, left),
        finish = Math.min(end, right);
      if (at < begin) nodes.push(text.slice(at, begin));
      nodes.push(
        <mark key={`text:${begin}`}>{text.slice(begin, finish)}</mark>,
      );
      at = finish;
    }
    if (at < end) nodes.push(text.slice(at, end));
  };
  let at = 0;
  for (const match of text.matchAll(emotions)) {
    const start = match.index!,
      end = start + match[0].length;
    plain(at, start);
    const icon = <Emotion original={match[0]} />;
    nodes.push(
      matches.some(([left, right]) => left < end && right > start) ? (
        <mark key={start}>{icon}</mark>
      ) : (
        <span key={start}>{icon}</span>
      ),
    );
    at = end;
  }
  plain(at, text.length);
  return nodes;
}

export function copyChatText(event: ClipboardEvent<HTMLElement>) {
  const selection = window.getSelection();
  if (!selection?.rangeCount || selection.isCollapsed) return;
  const range = selection.getRangeAt(0);
  if (!event.currentTarget.contains(range.commonAncestorContainer)) return;
  const fragment = range.cloneContents();
  const icons =
    fragment.querySelectorAll<HTMLImageElement>("img[data-emotion]");
  if (!icons.length) return;
  icons.forEach((icon) =>
    icon.replaceWith(document.createTextNode(icon.dataset.emotion!)),
  );
  const serialize = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
    const body = [...node.childNodes].map(serialize).join("");
    return node instanceof HTMLElement &&
      /^(DIV|ARTICLE|P|BR)$/.test(node.tagName)
      ? body + "\n"
      : body;
  };
  event.clipboardData.setData("text/plain", serialize(fragment));
  event.preventDefault();
}
