/** 系统提示只展示本地维护的文案，接口原文不能作为用户提示。 */
export type NoticeKind = "info" | "success" | "error";

const unavailable = "服务暂时不可用，请稍后再试";
const uncertain = "操作结果暂未确认，请刷新查看";
const frequent = "操作太频繁，请稍后再试";
const messages: Record<string, string> = {
  请输入微信平台Token: "请输入平台 Token",
  该Token已绑定其他用户: "该 Token 已绑定其他用户",
  回调地址必须为当前用户的本站接收地址:
    "消息接收地址必须为当前用户的系统生成地址",
  "回调绑定结果待确认，请核对后重试": "绑定结果待确认，请核对后重试",
  "官方回调绑定失败，请检查Token": "绑定失败，请重试",
  "请先填写 API 密钥": "请先填写 API 密钥",
  "DeepSeek API 密钥无效或没有权限": "DeepSeek API 密钥无效或没有权限",
  "模型列表获取失败，请稍后重试": "模型列表获取失败，请稍后重试",
  请选择当前可用的模型: "请选择当前可用的模型",
  模型开关必须为布尔值: "模型开关必须为布尔值",
  wechat_binding_pending: "微信配置尚未确认，请在系统设置中完成绑定",
  missing_wechat_token: "微信服务尚未配置，请联系管理员",
  platform_http_401: "微信服务授权已失效，请联系管理员",
  platform_http_403: "微信服务暂无访问权限，请联系管理员",
  platform_http_429: frequent,
  platform_response_unknown: uncertain,
  profile_unavailable: "暂时无法获取微信资料，请检查账号是否在线",
  login_identity_mismatch: "微信账号不一致，请核对扫码账号",
  invalid_online_response: "微信在线状态暂未确认，请稍后查看",
  platform_check_rejected: "无法确认微信状态，请到官方平台检查账号",
  "手机号或凭据不正确，请检查后重试": "手机号或验证码不正确，请检查后重试",
  请登录管理台: "登录已过期，请重新登录",
  请重新登录: "登录已过期，请重新登录",
  "会话校验失败，请重新登录": "页面已失效，请重新登录",
  请求来源不符: "页面校验失败，请刷新后再试",
  "短信登录暂不可用，请等待管理员完成短信配置":
    "短信验证码暂不可用，请稍后再试",
  "短信发送未确认，请稍后重新获取": "验证码发送结果未确认，请稍后重新获取",
  "短信核验未确认，请重新获取验证码": "验证码验证未完成，请重新获取",
  "验证码已失效，请重新获取": "验证码已过期，请重新获取",
  "验证码不正确或已失效，请重新获取": "验证码不正确或已过期，请重新获取",
  "验证码不正确或已失效，请检查后重试": "验证码不正确或已过期，请检查后重试",
  "该邮箱或手机号已绑定，请使用其他联系方式":
    "该邮箱或手机号已注册或绑定，请使用其他联系方式",
  联系方式未更改: "请输入新的手机号或邮箱",
  "验证码服务暂不可用，请稍后再试": "验证码服务暂不可用，请稍后再试",
  "验证码发送未确认，请稍后重新获取": "验证码发送结果未确认，请稍后重新获取",
  "验证码核验未确认，请重新获取验证码": "验证码验证未完成，请重新获取",
  "尝试次数过多，请稍后重试": frequent,
  "输入格式不正确，请检查必填项": "请检查填写内容",
  账号不存在: "账号不存在，请刷新页面",
  会话不存在: "会话不存在，请刷新页面",
  "当前会话正在发送，请稍后清空": "当前会话正在发送消息，请稍后清空",
  清空请求与会话不一致: "清空请求已失效，请重新打开聊天记录",
  会话身份缺失: "暂时无法确认会话，请刷新后再试",
  "appId 格式无效": "微信账号信息不正确，请到官方平台检查",
  平台没有返回可靠的微信身份: "暂时无法确认微信账号，请稍后再试",
  "设备对应的微信身份发生变化，请核对后使用原账号重新扫码":
    "微信账号已变化，请到官方平台重新登录",
  "首版使用已确认的 DeepSeek 官方接口": "请选择支持的模型服务",
  模型名称不能为空: "请填写模型名称",
  "timeout 超出允许范围 5–300": "请求等待时间应为 5 至 300 秒",
  "max_tokens 超出允许范围 128–393216": "输出长度应为 128 至 393216",
  密钥保存不可用: "暂时无法保存密钥，请联系管理员",
  启用前请填写账号提示词: "请先填写助手设定",
  "请先保存模型 Key": "请先在系统设置中保存模型密钥",
  "账号已移除，请重新扫码登录后恢复": "账号已移除，请到官方平台重新登录",
  账号未启用: "请先启用微信账号",
  "阿里云百炼 API Key 格式不正确": "阿里云百炼 API Key 格式不正确",
  文件超过大小限制: "文件过大，暂时无法处理",
  文件不存在或授权已过期: "文件不可用，请刷新后重新下载",
  文件尚未上传或不可用: "文件暂不可用，请稍后再试",
  文件不存在或无法读取: "文件不存在或无法读取",
};
const safeMessages = new Set([
  ...Object.values(messages),
  "验证码服务暂不可用，请稍后再试",
  "请输入有效的邮箱或手机号",
  "暂不支持新账号首次登录",
  "联系方式状态已更改，请重新获取验证码",
  "请输入用户名",
  "该登录标识不可用于本次操作",
  "该邮箱或手机号已注册",
  "该邮箱或手机号已绑定，请使用其他联系方式",
  "联系方式未更改",
  "请通过联系方式设置获取验证码",
  "邮箱、手机号或凭据不正确，请检查后重试",
  "验证码发送未确认，请稍后重新获取",
  "验证码核验未确认，请重新获取验证码",
  "请选择有效的头像图片",
  "头像不能超过5MiB",
  "回调地址必须为当前用户的本站接收地址",
  "配置已变化或正在绑定，请刷新后重试",
  "当前仍有任务运行，请等待完成后修改微信配置",
  "请输入微信平台Token",
  "该Token已绑定其他用户",
  "配置已变化，请刷新后查看",
  "回调绑定结果待确认，请核对后重试",
  "官方回调绑定失败，请检查Token",

  unavailable,
  uncertain,
  frequent,
  "网络连接失败，请检查网络",
  "请求超时，请稍后再试",
  "暂无权限进行此操作",
  "内容不存在，请刷新查看",
  "内容已更新，请刷新后再试",
  "请检查填写内容",
]);

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public retryAfter = 0,
    public uncertain = false,
  ) {
    super(safeMessages.has(message) ? message : unavailable);
    this.name = "ApiError";
  }
}

export function isCanceled(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

export function userMessage(error: unknown, fallback = unavailable): string {
  if (isCanceled(error)) return "";
  return error instanceof ApiError && safeMessages.has(error.message)
    ? error.message
    : fallback;
}

function knownMessage(value: unknown): string | undefined {
  return typeof value === "string" && Object.hasOwn(messages, value)
    ? messages[value]
    : undefined;
}

function statusMessage(status: number) {
  if (status === 401) return "登录已过期，请重新登录";
  if (status === 403) return "暂无权限进行此操作";
  if (status === 404 || status === 410) return "内容不存在，请刷新查看";
  if (status === 409) return "内容已更新，请刷新后再试";
  if (status === 400 || status === 422) return "请检查填写内容";
  if (status === 413) return messages["文件超过大小限制"];
  if (status === 429) return frequent;
  if (status === 408 || status === 504) return "请求超时，请稍后再试";
  return unavailable;
}

function retryAfter(response: Response) {
  const value = response.headers.get("Retry-After");
  if (!value) return 0;
  const seconds = Number(value);
  const wait = Number.isFinite(seconds)
    ? seconds
    : (Date.parse(value) - Date.now()) / 1000;
  return Number.isFinite(wait) ? Math.max(0, Math.ceil(wait)) : 0;
}

export async function requestJson(
  url: string,
  init: RequestInit,
  options: { array?: boolean; onUnauthorized?: () => void },
): Promise<any> {
  const writing = !["GET", "HEAD"].includes(
    (init.method || "GET").toUpperCase(),
  );
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    if (init.signal?.aborted || isCanceled(error))
      throw new DOMException("", "AbortError");
    throw new ApiError(
      writing ? uncertain : "网络连接失败，请检查网络",
      0,
      0,
      writing,
    );
  }
  // 鉴权和限流先读取，不能因网关返回 HTML 或空响应而丢失。
  if (response.status === 401) options.onUnauthorized?.();
  const wait = retryAfter(response);
  let data: any;
  try {
    data = JSON.parse(await response.text());
  } catch (error) {
    if (init.signal?.aborted || isCanceled(error))
      throw new DOMException("", "AbortError");
  }
  if (!response.ok) {
    const message =
      knownMessage(data?.reason) ||
      knownMessage(data?.error) ||
      knownMessage(data?.detail);
    const unconfirmed =
      data?.uncertain === true ||
      (!message && writing && response.status >= 500);
    throw new ApiError(
      unconfirmed ? uncertain : message || statusMessage(response.status),
      response.status,
      wait,
      unconfirmed,
    );
  }
  const valid = options.array
    ? Array.isArray(data) &&
      data.every((row) => row && typeof row === "object" && !Array.isArray(row))
    : data !== null && typeof data === "object" && !Array.isArray(data);
  if (!valid)
    throw new ApiError(
      writing ? uncertain : unavailable,
      response.status,
      wait,
      writing,
    );
  return data;
}
