import { Users } from "lucide-react";
import { useState } from "react";

type Row = Record<string, any>;
export const directoryName = (item?: Row) =>
  item?.display_name || item?.name || item?.peer_id || "微信用户";

export function WechatSelectValue({ item, name }: { item: Row; name: string }) {
  return (
    <span className="wechat-select-value">
      <WechatAvatar item={{ ...item, display_name: name }} />
      <span className="wechat-select-meta">
        <strong title={name}>{name}</strong>
      </span>
    </span>
  );
}

function AvatarImage({ sources }: { sources: string[] }) {
  const [index, setIndex] = useState(0);
  const source = sources[index];
  if (!source) return null;
  return (
    <img
      key={source}
      src={source}
      alt=""
      referrerPolicy="no-referrer"
      onError={() =>
        setIndex((current) =>
          sources[current] === source ? current + 1 : current,
        )
      }
    />
  );
}

export function WechatAvatar({
  item,
  className = "",
}: {
  item?: Row;
  className?: string;
}) {
  const profile = item?.profile || {};
  const sources = [
    ...new Set(
      [profile.bigHeadImgUrl, profile.smallHeadImgUrl]
        .filter((url): url is string => typeof url === "string" && !!url.trim())
        .map((url) => url.trim()),
    ),
  ];
  return (
    <span
      aria-hidden="true"
      className={
        "avatar wechat-avatar" +
        (item?.kind === "group" ? " group" : "") +
        (className ? " " + className : "")
      }
    >
      {item?.kind === "group" ? (
        <Users size={20} />
      ) : (
        directoryName(item).slice(0, 1)
      )}
      <AvatarImage key={JSON.stringify(sources)} sources={sources} />
    </span>
  );
}
