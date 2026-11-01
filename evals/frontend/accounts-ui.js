async function checkAccountUI(page) {
  if (!["127.0.0.1:17953", "127.0.0.1:17954", "127.0.0.1:17955"].includes(new URL(page.url()).host))
    throw Error("只能在隔离验收服务运行");
  const checks = [];
  const verify = (ok, name) => {
    if (!ok) throw Error(name);
    checks.push(name);
  };
  const requests = [];
  const observe = (request) => requests.push(new URL(request.url()).pathname);
  const login = async () => {
    await page.locator(".login-page, .shell").waitFor();
    if (await page.getByLabel("手机号", { exact: true }).count()) {
    await page.getByRole("button", { name: "密码登录", exact: true }).click();
    await page.getByLabel("手机号", { exact: true }).fill("13900000000");
      await page.getByLabel("密码", { exact: true }).fill("isolated-browser-only-passphrase");
      await page.getByRole("button", { name: "登录", exact: true }).click();
    }
    await page.locator(".shell").waitFor();
  };
  page.on("request", observe);
  try {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => sessionStorage.removeItem("wechat-page"));
    await page.reload();
    await login();
    verify((await page.locator(".topbar h1").innerText()) === "微信管理", "首次进入默认打开微信管理");
    verify((await page.locator(".sidebar nav button").allTextContents()).join(",") ===
      "微信管理,消息管理,自动回复,任务记录,系统设置", "侧栏名称和顺序统一");
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await page
      .locator(".account-row")
      .filter({ hasText: "本地验收账号" })
      .getByText("在线", { exact: true })
      .waitFor();
    verify(
      (await page.locator(".account-row").count()) === 2,
      "进入微信管理核实已接入账号状态",
    );
    verify(
      (await page.locator(".account-row button").count()) === 0,
      "所有账号状态均无登录退出操作",
    );
    for (const name of [
      "扫码登录新账号",
      "扫码登录新微信",
      "重新登录",
      "退出登录",
      "获取二维码",
    ])
      verify(
        (await page.getByRole("button", { name, exact: true }).count()) === 0,
        "无操作入口：" + name,
      );
    verify(
      (await page.getByRole("columnheader").allTextContents()).join(",") ===
        "头像,昵称,微信号,手机号,性别,个性签名,登录设备,登录状态,最近更新时间",
      "九列只读账号信息顺序正确",
    );
    verify((await page.locator(".account-token, .account-callback, .account-app-id").count()) === 0,
      "已删除AppID、Token和回调地址列");
    verify(!requests.some((path) => path.endsWith("/authorization")), "微信管理不读取授权配置");
    verify((await page.locator(".account-refreshed-at time").first().innerText()) !== "—",
      "真实隔离后端成功刷新后返回更新时间");
    const platformLink = page.locator(".topbar").getByRole("link", { name: "官方平台", exact: true });
    verify(
      (await platformLink.count()) === 1 &&
      (await platformLink.getAttribute("href")) === "https://newmanager.wechatapi.net/dashboard/" &&
      (await platformLink.getAttribute("target")) === "_blank" &&
      (await platformLink.getAttribute("rel")) === "noopener noreferrer",
      "顶部官方平台按钮新窗口跳转 dashboard 且不携带凭据",
    );
    verify((await page.locator(".accounts-page .page-heading, .accounts-page .official-platform-link").count()) === 0,
      "账号页旧链接及占位区域已移除");
    verify(
      (await page.getByRole("button", { name: "接口授权", exact: true }).count()) === 0,
      "接口授权导航已删除",
    );
    verify((await page.locator(".authorization-page").count()) === 0, "接口授权页面已删除");
    await page.getByRole("button", { name: "系统设置", exact: true }).click();
    verify((await page.getByLabel("访问 Token", { exact: true }).count()) === 0, "设置不提供Token输入框");
    verify((await page.getByRole("button", { name: "保存 Token", exact: true }).count()) === 0, "设置不提供Token保存按钮");
    verify((await page.getByRole("button", { name: "保存模型设置", exact: true }).count()) === 1, "模型配置功能保留");
    for (const width of [1440, 1920, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      verify(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), width + "px设置页面无横向溢出");
      verify((await page.locator(".top-actions .admin").count()) === 0,
        width + "px右上角管理员退出按钮已移除");
      const logout = page.getByRole("button", { name: "退出管理台", exact: true });
      verify((await logout.count()) === 1 && await logout.isVisible(), width + "px只保留可见的侧栏退出入口");
      if (width <= 780) {
        verify(await logout.evaluate((button) => {
          const rect = button.getBoundingClientRect();
          const nav = button.closest(".sidebar").querySelector("nav").getBoundingClientRect();
          return rect.width >= 44 && rect.height >= 44 && rect.left >= nav.right &&
            rect.right <= innerWidth && rect.bottom <= innerHeight &&
            getComputedStyle(button.querySelector("span")).display === "none";
        }), "窄屏退出图标在底部最右侧且触控区域至少44px");
      }
      verify(await platformLink.evaluate((link) => {
        const rect = link.getBoundingClientRect();
        const header = link.closest(".topbar").getBoundingClientRect();
        const title = link.closest(".topbar").querySelector("h1").getBoundingClientRect();
        return rect.left >= title.right && rect.right <= header.right &&
          rect.top >= header.top && rect.bottom <= header.bottom &&
          rect.width > 0 && rect.height >= (innerWidth <= 1150 ? 44 : 36);
      }), width + "px顶部按钮与标题对齐且触控尺寸足够");
      await page.screenshot({ path: "output/playwright/wechat-settings-env-" + width + ".png", fullPage: true });
    }
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await page.locator(".account-refreshed-at time").first().waitFor();
    verify(
      !requests.some((path) =>
        /\/login\/|\/logout$|\/callback\/(bind|check)$/.test(path),
      ),
      "只读页面不请求登录、退出、绑定或连通检测接口",
    );
    const control = await (
      await page.request.get(
        new URL("/wechat/__acceptance/control", page.url()).href,
      )
    ).json();
    verify(
      control.platform_paths.length > 0 && control.platform_paths.every((path) =>
        ["/login/checkOnline", "/personal/getProfile"].includes(path)),
      "账号页面上游仅调用在线及资料查询",
    );
    verify(control.historical_sends === 1, "历史发送记录保持完整");
    for (const name of ["微信管理", "消息管理", "自动回复", "任务记录", "系统设置"]) {
      verify(
        (await page.getByRole("button", { name, exact: true }).count()) === 1,
        "保留业务页面：" + name,
      );
      await page.getByRole("button", { name, exact: true }).click();
      verify((await page.locator(".topbar h1").innerText()) === name &&
        (await page.locator(".sidebar nav button.active").innerText()) === name,
        name + "页头标题与选中导航一致");
      verify(await platformLink.isVisible(), name + "页面保留顶部官方平台按钮");
    }
    for (const [saved, title] of [["messages", "消息管理"], ["accounts", "微信管理"], ["invalid-page", "微信管理"]]) {
      await page.evaluate((saved) => sessionStorage.setItem("wechat-page", saved), saved);
      await page.reload();
      await page.locator(".shell").waitFor();
      verify((await page.locator(".topbar h1").innerText()) === title, "会话页面恢复：" + saved);
    }
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      const before = requests.filter((path) => path.endsWith("/auth/logout")).length;
      const response = page.waitForResponse((r) => r.url().endsWith("/auth/logout") && r.request().method() === "POST");
      await page.getByRole("button", { name: "退出管理台", exact: true }).click();
      verify((await response).ok(), width + "px退出接口成功");
      await page.getByRole("heading", { name: "登录管理台", exact: true }).waitFor();
      verify(requests.filter((path) => path.endsWith("/auth/logout")).length === before + 1,
        width + "px单次点击只提交一次退出请求");
      await page.reload();
      await page.getByRole("heading", { name: "登录管理台", exact: true }).waitFor();
      verify((await page.request.get(new URL("/wechat/api/v1/auth/session", page.url()).href)).status() === 401,
        width + "px退出后刷新仍需登录");
      await login();
    }
    return checks;
  } finally {
    page.off("request", observe);
  }
}
