import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { useNotice } from "./notices";

export type HistoryImage = { url: string; width: number; height: number };

export function ChatImage({ image }: { image: HistoryImage }) {
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const { setHost, show } = useNotice();
  const close = () => {
    dialog.current?.close();
    setPreview(false);
    trigger.current?.focus({ preventScroll: true });
  };
  useEffect(() => {
    setFailed(false);
  }, [image.url]);
  useEffect(() => {
    if (!preview || !dialog.current) return;
    const previous = trigger.current?.closest("dialog") || null;
    dialog.current.showModal();
    setHost(dialog.current);
    return () => setHost(previous);
  }, [preview, setHost]);
  const ratio = Math.min(1, 240 / image.width, 200 / image.height);
  if (failed)
    return (
      <span
        className="chat-image-placeholder"
        style={{
          width: image.width * ratio,
          aspectRatio: `${image.width} / ${image.height}`,
        }}
      >
        [图片暂不可用]
      </span>
    );
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="chat-image-button"
        aria-label="查看大图"
        style={{
          width: image.width * ratio,
          aspectRatio: `${image.width} / ${image.height}`,
        }}
        onClick={() => setPreview(true)}
      >
        <img
          src={image.url}
          width={image.width}
          height={image.height}
          loading="lazy"
          alt="图片"
          onError={() => {
            if (preview) {
              close();
              show("图片暂不可用", "error");
            }
            setFailed(true);
          }}
        />
      </button>
      {preview && (
        <dialog
          ref={dialog}
          className="account-dialog chat-image-preview"
          aria-label="图片预览"
          onCancel={(event) => {
            event.preventDefault();
            event.stopPropagation();
            close();
          }}
          onClick={(event) => {
            event.stopPropagation();
            if (event.target === dialog.current) close();
          }}
        >
          <header>
            <h2>图片预览</h2>
            <button type="button" aria-label="关闭图片预览" onClick={close}>
              <X size={19} />
            </button>
          </header>
          <div className="chat-image-stage">
            <img
              src={image.url}
              width={image.width}
              height={image.height}
              alt="聊天图片大图"
              onError={() => {
                close();
                setFailed(true);
                show("图片暂不可用", "error");
              }}
            />
          </div>
        </dialog>
      )}
    </>
  );
}
