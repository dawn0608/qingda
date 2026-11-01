import { requestJson } from "./user-messages";
export { ApiError } from "./user-messages";

export const sessionChangeKey = "wechat-account-session-change";
let csrf = "";
export const setCsrf = (value: string) => {
  csrf = value;
};
export async function uploadAvatar(file: File) {
  return requestJson(
    "/wechat/api/v1/profile/avatar",
    {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-CSRF-Token": csrf,
      },
      body: file,
    },
    {
      onUnauthorized: () => window.dispatchEvent(new Event("session-expired")),
    },
  );
}
export async function api(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
  keepalive = false,
): Promise<any> {
  const result = await requestJson(
    "/wechat/api/v1" + path,
    {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(method !== "GET" ? { "X-CSRF-Token": csrf } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      keepalive,
    },
    {
      array: ["/accounts", "/conversations"].includes(path.split("?")[0]),
      onUnauthorized: () => {
        if (!["/auth/login", "/auth/code/send"].includes(path))
          window.dispatchEvent(new Event("session-expired"));
      },
    },
  );
  if (path === "/auth/login" || path === "/auth/logout") {
    setCsrf(path === "/auth/login" ? result.csrf : "");
    try {
      localStorage.setItem(sessionChangeKey, crypto.randomUUID());
    } catch {
      // 不支持共享存储时，各页面仍由认证 API 校验会话。
    }
  }
  return result;
}
