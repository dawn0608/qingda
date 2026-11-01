import { useEffect, useState } from "react";
import "./filing-notice.css";

type Filing = {
  icp_number: string;
  icp_url: string;
  public_security_number: string;
  public_security_url: string;
};

export function FilingNotice() {
  const [filing, setFiling] = useState<Filing | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/wechat/api/v1/auth/options", {
      signal: controller.signal,
      credentials: "same-origin",
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => setFiling(body?.filing ?? null))
      .catch(() => {});
    return () => controller.abort();
  }, []);
  if (!filing?.icp_number && !filing?.public_security_number) return null;
  return (
    <footer className="filing-notice" aria-label="网站备案信息">
      {filing.icp_number && (
        <a href={filing.icp_url} target="_blank" rel="noopener noreferrer">
          {filing.icp_number}
        </a>
      )}
      {filing.public_security_number && filing.public_security_url && (
        <a
          href={filing.public_security_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {filing.public_security_number}
        </a>
      )}
    </footer>
  );
}
