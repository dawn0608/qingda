async function checkAccountEntryUI(page) {
  if (new URL(page.url()).host !== "127.0.0.1:17953")
    throw Error("只能在隔离验收服务运行");
  const checks = [];
  const verify = (ok, name) => {
    if (!ok) throw Error(name);
    checks.push(name);
  };
  const until = async (condition, name) => {
    for (let i = 0; i < 100 && !(await condition()); i++)
      await page.waitForTimeout(20);
    if (!(await condition())) throw Error(name);
    await page.waitForTimeout(50);
  };
  if (await page.getByLabel("手机号", { exact: true }).count()) {
    await page.getByRole("button", { name: "密码登录", exact: true }).click();
    await page.getByLabel("手机号", { exact: true }).fill("13900000000");
    await page
      .getByLabel("密码", { exact: true })
      .fill("isolated-browser-only-passphrase");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).waitFor();
  }
  let requests = 0,
    completed = 0,
    awaitingFinal = false,
    pending = 0,
    listRequests = 0,
    failures = 0;
  let failAccount = false,
    failList = false,
    expire = false;
  let nextName = "进入时查询",
    release;
  let gate = new Promise((resolve) => {
    release = resolve;
  });
  let rows = Array.from({ length: 21 }, (_, i) => ({
    id: 9001 + i,
    nickname: i === 0 ? nextName : "并行账号" + i,
    wxid: "wxid_entry_fixture_" + i,
    online: 1,
    profile: {},
  }));
  const list = (route) => {
    listRequests++;
    if (failList) {
      failures++;
      return route.fulfill({ status: 503, json: { detail: "隔离列表失败" } });
    }
    if (awaitingFinal) {
      awaitingFinal = false;
      completed++;
    } else if (!rows.length) awaitingFinal = true;
    return route.fulfill({ json: rows });
  };
  const online = async (route) => {
    requests++;
    pending++;
    awaitingFinal = true;
    const id = Number(route.request().url().split("/").at(-2));
    const a = rows.find((row) => row.id === id),
      name = nextName;
    try {
      if (gate) await gate;
      if (expire)
        return await route.fulfill({
          status: 401,
          json: { detail: "会话失效" },
        });
      if (failAccount && id === 9001) {
        a.online = null;
        return await route.fulfill({
          status: 503,
          json: { detail: "隔离查询失败" },
        });
      }
      a.online = 1;
      if (id === 9001) {
        a.nickname = name;
        a.profile.smallHeadImgUrl = "https://avatar.example.test/entry.svg";
      }
      await route.fulfill({ json: {} });
    } finally {
      pending--;
    }
  };
  const avatar = (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36"><rect width="36" height="36" fill="green"/></svg>',
    });
  const visible = (value) =>
    page.evaluate((value) => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        get: () => !value,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    }, value);
  const reenter = async () => {
    const before = completed;
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(
      () => completed > before && pending === 0,
      "重新进入应完成查询",
    );
  };
  await page.route("**/wechat/api/v1/accounts", list);
  await page.route("**/wechat/api/v1/accounts/*/online", online);
  await page.route("https://avatar.example.test/*", avatar);
  await page.clock.install();
  await page.clock.pauseAt(
    new Date((await page.evaluate(() => Date.now())) + 1000),
  );
  try {
    await page.reload();
    if ((await page.locator(".topbar h1").innerText()) !== "微信管理")
      await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(
      () => requests === 21 && pending === 21,
      "21个账号应全部同时开始查询",
    );
    verify(requests === 21, "进入时21个账号全部并行，不设置固定并发上限");
    verify(
      await page.getByRole("button", { name: "退出管理台", exact: true }).isEnabled(),
      "查询不锁定全局操作",
    );
    await page.clock.fastForward(300001);
    verify(requests === 21, "慢查询超过五分钟不追加批次");
    release();
    gate = null;
    await until(
      () => completed >= 1 && pending === 0,
      "进入页面应完成一次查询",
    );
    verify(
      (await page.locator(".account-row").count()) === 10,
      "一次查询完成后按原分页展示",
    );
    await page.clock.fastForward(300001);
    await page.waitForTimeout(100);
    verify(requests === 21, "停留超过五分钟不追加账号查询");
    await visible(false);
    await page.clock.fastForward(300001);
    await visible(true);
    await page.clock.fastForward(1);
    await page.waitForTimeout(50);
    verify(requests === 21, "隐藏后恢复可见不追加查询");
    const beforePaging = [requests, listRequests];
    const pager = page.getByRole("navigation", { name: "微信账号分页" });
    await pager.getByRole("button", { name: "下一页" }).click();
    verify(
      requests === beforePaging[0] && listRequests === beforePaging[1],
      "翻页和重渲染不触发查询",
    );
    nextName = "重新进入后资料";
    await reenter();
    verify(
      requests === 42 &&
        (await pager.getByRole("status").innerText()) === "1 / 3",
      "重新进入只查询一次并回到第一页",
    );
    verify(
      (await page.getByText(nextName, { exact: true }).count()) === 1 &&
        (await page
          .locator(".account-avatar img")
          .first()
          .getAttribute("src")) === "https://avatar.example.test/entry.svg",
      "重新进入更新昵称和头像",
    );
    const beforeReload = completed,
      oldRequests = requests;
    await page.reload();
    await until(
      () => completed > beforeReload && pending === 0,
      "刷新网页应完成查询",
    );
    verify(requests === oldRequests + 21, "刷新网页立即核实一次");
    failAccount = true;
    await reenter();
    verify(
      (await page.locator(".account-row").first().innerText()).includes(
        "状态待确认",
      ) &&
        (await page.locator(".account-row").nth(1).innerText()).includes(
          "在线",
        ),
      "单账号失败继续核实其他账号",
    );
    const failedRequests = requests;
    await page.clock.fastForward(300001);
    verify(requests === failedRequests, "查询失败不定时重试");
    failAccount = false;
    await reenter();
    verify(
      (await page.locator(".account-row").first().innerText()).includes("在线"),
      "下次进入可恢复失败账号",
    );
    failList = true;
    const beforeRows = await page.locator(".account-row").allTextContents();
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(() => failures > 0, "应模拟列表读取失败");
    verify(
      JSON.stringify(await page.locator(".account-row").allTextContents()) ===
        JSON.stringify(beforeRows),
      "列表失败保留已有内容",
    );
    const failedLists = listRequests;
    await page.clock.fastForward(300001);
    verify(listRequests === failedLists, "列表失败不自动重试");
    failList = false;
    await reenter();
    verify(
      (await page.locator(".account-row").count()) === 10,
      "列表失败后重新进入恢复",
    );
    gate = new Promise((resolve) => {
      release = resolve;
    });
    const beforeSlow = requests;
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(() => requests === beforeSlow + 21, "离页前应开始查询");
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    const leaveLists = listRequests;
    release();
    gate = null;
    await until(() => pending === 0, "离页后已有请求结束");
    await page.clock.fastForward(300001);
    verify(listRequests === leaveLists, "离页忽略迟到响应且不继续读取账号");
    awaitingFinal = false;
    await reenter();
    nextName = "旧页面迟到资料";
    gate = new Promise((resolve) => {
      release = resolve;
    });
    const oldRelease = release,
      beforeSwitch = requests;
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(() => requests === beforeSwitch + 21, "旧页面查询应开始");
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    nextName = "快速重进的新资料";
    gate = new Promise((resolve) => {
      release = resolve;
    });
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(() => requests === beforeSwitch + 42, "新页面独立执行一次查询");
    const switchingLists = listRequests,
      switchingCompleted = completed;
    oldRelease();
    await until(() => pending === 21, "旧页面请求应结束");
    verify(
      listRequests === switchingLists,
      "快速重进时旧页面迟到响应不读取或覆盖当前列表",
    );
    release();
    gate = null;
    await until(
      () => completed > switchingCompleted && pending === 0,
      "新页面查询应完成",
    );
    verify(
      (await page.getByText(nextName, { exact: true }).count()) === 1 &&
        requests === beforeSwitch + 42,
      "快速重进仅各查询一次且展示本轮资料",
    );
    rows = [];
    await reenter();
    const emptyRequests = requests;
    verify(
      (await page.locator(".accounts-total").innerText()) === "共 0 条",
      "空列表一次加载后正确展示",
    );
    await page.clock.fastForward(300001);
    verify(requests === emptyRequests, "空列表不启动定时查询");
    rows = [
      {
        id: 9001,
        nickname: "新增核实账号",
        wxid: "wxid_entry_new",
        online: 1,
        profile: {},
      },
    ];
    nextName = "新增核实账号";
    await reenter();
    verify(
      (await page.getByText(nextName, { exact: true }).count()) === 1,
      "新账号在下次进入时展示",
    );
    expire = true;
    await page.getByRole("button", { name: "消息管理", exact: true }).click();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await until(
      async () =>
        (await page.getByRole("heading", { name: "登录管理台" }).count()) === 1,
      "会话失效应返回登录页",
    );
    const expiredRequests = requests;
    await page.clock.fastForward(300001);
    verify(requests === expiredRequests, "会话失效后无后续查询");
    return checks;
  } finally {
    release();
    await page.clock.resume();
    await page.unroute("**/wechat/api/v1/accounts", list);
    await page.unroute("**/wechat/api/v1/accounts/*/online", online);
    await page.unroute("https://avatar.example.test/*", avatar);
    await page.evaluate(() => {
      delete document.hidden;
    });
  }
}
