/** 在生产页面直接验证已加载构建中的公共错误处理函数，不执行真实请求。 */
async function checkNoticeContract(page, expectedOrigin) {
  if (!expectedOrigin || new URL(page.url()).origin !== expectedOrigin)
    throw Error("只允许验证生产构建");
  return page.evaluate(async () => {
    const entry = document.querySelector('script[type="module"][src]')?.src;
    const bundle = await import(entry);
    const functions = Object.values(bundle).filter(
      (value) => typeof value === "function",
    );
    const request = functions.find(
      (fn) =>
        String(fn).includes("fetch(") && String(fn).includes("JSON.parse("),
    );
    const message = functions.find(
      (fn) => String(fn).includes(".message") && String(fn).includes(".has("),
    );
    if (!request || !message)
      throw Error("生产公共模块未找到，请根据当前构建调整定位");
    const checks = [];
    const verify = (ok, name) => {
      if (!ok) throw Error(name);
      checks.push(name);
    };
    const realFetch = window.fetch;
    const failure = async (response, method = "GET", options = {}) => {
      window.fetch = async () => response;
      try {
        await request(
          "/injected-only",
          { method },
          { scope: "manager", ...options },
        );
        throw Error("预期请求失败");
      } catch (error) {
        return error;
      }
    };
    try {
      for (const input of [
        undefined,
        null,
        1,
        "SECRET_TRACE",
        { message: "SECRET_TRACE" },
        new Error("SyntaxError SECRET_TRACE"),
      ])
        verify(
          message(input) === "服务暂时不可用，请稍后再试",
          "未知异常转换为中文兜底：" + typeof input,
        );
      let unauthorized = 0;
      let error = await failure(
        new Response("<html>SECRET_TRACE</html>", {
          status: 401,
          headers: { "Retry-After": "4" },
        }),
        "GET",
        { onUnauthorized: () => unauthorized++ },
      );
      verify(
        unauthorized === 1 && error.status === 401 && error.retryAfter === 4,
        "非JSON401保留鉴权和限流信息",
      );
      verify(message(error) === "登录已过期，请重新登录", "非JSON401文案正确");
      error = await failure(
        new Response("", {
          status: 429,
          headers: { "Retry-After": new Date(Date.now() + 6000).toUTCString() },
        }),
      );
      verify(
        error.retryAfter > 0 &&
          error.retryAfter <= 6 &&
          message(error) === "操作太频繁，请稍后再试",
        "HTTP日期限流等待正确",
      );
      error = await failure(
        new Response(
          '{"reason":"platform_response_unknown","uncertain":true}',
          { status: 502 },
        ),
        "POST",
      );
      verify(
        error.uncertain && message(error) === "操作结果暂未确认，请刷新查看",
        "不确定结果保持真实语义",
      );
      for (const body of [
        "",
        "null",
        "123",
        '"SECRET_TRACE"',
        "[]",
        "{broken",
      ]) {
        error = await failure(new Response(body, { status: 200 }));
        verify(
          error.status === 200 &&
            message(error) === "服务暂时不可用，请稍后再试",
          "异常成功响应类型受控：" + body,
        );
      }
      error = await failure(new Response("{}", { status: 200 }), "GET", {
        array: true,
      });
      verify(
        message(error) === "服务暂时不可用，请稍后再试",
        "列表接口拒绝错误响应类型",
      );
      window.fetch = async () => {
        throw new TypeError("Failed to fetch SECRET_TRACE");
      };
      for (const method of ["GET", "POST"]) {
        try {
          await request("/injected-only", { method }, { scope: "manager" });
        } catch (error) {
          verify(
            message(error) ===
              (method === "GET"
                ? "网络连接失败，请检查网络"
                : "操作结果暂未确认，请刷新查看"),
            method + "网络失败文案准确",
          );
        }
      }
      window.fetch = (_url, init) =>
        new Promise((_resolve, reject) =>
          init.signal.addEventListener("abort", () =>
            reject(new DOMException("SECRET_TRACE", "AbortError")),
          ),
        );
      const controller = new AbortController();
      const canceled = request(
        "/injected-only",
        { signal: controller.signal },
        { scope: "manager" },
      );
      controller.abort();
      try {
        await canceled;
        throw Error("取消请求必须拒绝");
      } catch (error) {
        verify(
          error.name === "AbortError" && message(error) === "",
          "主动取消不产生错误提示",
        );
      }
      return {
        checks,
        entry,
        scope:
          "直接验证生产构建的公共模块；请求全部由浏览器内存替代，无真实业务请求",
      };
    } finally {
      window.fetch = realFetch;
    }
  });
}
