import assert from "node:assert/strict";

export async function checkAssistantUI(page, output) {
  assert.equal(
    new URL(page.url()).host,
    "127.0.0.1:17962",
    "只能操作服务器隔离夹具",
  );
  const checks = [],
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const check = (ok, name) => {
    assert.ok(ok, name);
    checks.push(name);
  };
  const choose = async (name, value) => {
    await page.getByRole("combobox", { name, exact: true }).click();
    await page.getByRole("option", { name: value, exact: true }).click();
  };
  const scopeData = async () =>
    (await page.request.get("/wechat/api/v1/accounts/1/reply-scope")).json();
  await page.getByRole("button", { name: "密码登录", exact: true }).click();
  await page.getByLabel("手机号", { exact: true }).fill("13900000000");
  await page
    .getByLabel("密码", { exact: true })
    .fill("isolated-assistant-only-passphrase");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.locator(".assistant-toolbar").waitFor();
  check(
    (await page.locator("#assistant-panel-prompt").isVisible()) &&
      (await page.locator("#assistant-panel-scope").isVisible()) &&
      (await page.getByRole("tab").count()) === 0,
    "助手设定和回复范围同时展示，不使用标签切换",
  );
  await page
    .locator(".prompt-live-editor:not(.is-loading)")
    .waitFor({ timeout: 60000 });
  const textbox = page.getByRole("textbox", { name: "助手设定内容" });
  await textbox.click();
  await page.keyboard.press("Control+End");
  await page.keyboard.press("Enter");
  await page.keyboard.insertText("隔离草稿保护验收");
  await page.getByText("有未保存的修改", { exact: true }).waitFor();
  await page.getByRole("button", { name: "群聊", exact: true }).click();
  await page.getByText("共 1 个群聊", { exact: true }).waitFor();
  check(
    await textbox.innerText().then((text) => text.includes("隔离草稿保护验收")),
    "操作右侧回复范围保留左侧设定草稿",
  );
  await choose("选择账号", "第二账号");
  await page.getByRole("dialog").waitFor();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "继续编辑", exact: true })
    .click();
  check(
    await textbox.innerText().then((text) => text.includes("隔离草稿保护验收")),
    "切账号继续编辑保留草稿",
  );
  await choose("选择账号", "第二账号");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "放弃修改", exact: true })
    .click();
  await page.getByText("共 3 位好友", { exact: true }).waitFor();
  await choose("选择账号", "WeMind");
  await page.getByText("共 527 位好友", { exact: true }).waitFor();
  check(
    (await page.locator(".reply-scope-row").count()) === 10,
    "默认每页十条且总数超过500",
  );
  check(
    (await page
      .locator(".reply-pause-badge")
      .evaluate((el) => getComputedStyle(el).borderRadius)) === "4px",
    "人工暂停标签4px圆角",
  );
  check(
    await page.locator(".reply-resume").evaluate((el) => {
      const r = el.getBoundingClientRect();
      return r.width === 72 && r.height === 26;
    }),
    "恢复按钮72x26px",
  );
  check(
    (await page
      .locator(".reply-scope-row")
      .filter({ hasText: "唐欣" })
      .getByRole("switch")
      .count()) === 0,
    "暂停行只保留恢复操作",
  );
  await page.screenshot({ path: output + "/isolated-pause-1440.png" });
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await page
    .locator(".reply-scope-row")
    .first()
    .getByText("0010 好友 0010", { exact: true })
    .waitFor();
  checks.push("服务端翻页返回下一组联系人");
  const search = page.getByRole("textbox", {
    name: "搜索回复范围",
    exact: true,
  });
  await search.fill("跨页原始昵称");
  await page.getByText("共 1 位好友", { exact: true }).waitFor();
  check(
    (await page.locator(".reply-contact-name strong").innerText()) ===
      "跨页备注目标",
    "搜索原始昵称命中第500条之后的备注显示名",
  );
  await choose("回复状态筛选", "已关闭");
  await page.getByText("共 0 位好友", { exact: true }).waitFor();
  check(
    await page.getByText("没有符合条件的联系人", { exact: true }).isVisible(),
    "搜索与保存选择组合筛选",
  );
  await choose("回复状态筛选", "全部状态");
  await search.fill("");
  await page.getByText("共 527 位好友", { exact: true }).waitFor();
  const requests = [];
  page.on("request", (request) => {
    if (request.url().includes("/reply-scope?"))
      requests.push(new URL(request.url()).searchParams.get("q"));
  });
  await search.dispatchEvent("compositionstart");
  await search.fill("组词尚未完成");
  await page.waitForTimeout(420);
  check(!requests.includes("组词尚未完成"), "输入法组词期间不发出中间搜索");
  await search.dispatchEvent("compositionend");
  await page.getByText("共 0 位好友", { exact: true }).waitFor();
  checks.push("输入法完成后查询");
  await search.fill("");
  await choose("回复状态筛选", "人工暂停");
  await page.getByText("共 1 位好友", { exact: true }).waitFor();
  await page.getByRole("button", { name: /恢复.*唐欣.*回复/ }).click();
  await page.getByText("共 0 位好友", { exact: true }).waitFor();
  check(
    (await scopeData()).items.find((r) => r.name.includes("唐欣"))
      ?.pause_until === 0,
    "恢复操作真实持久化且刷新筛选总数",
  );
  await choose("回复状态筛选", "全部状态");
  await page.getByText("共 527 位好友", { exact: true }).waitFor();
  await choose("每页条数", "50 条/页");
  await page.waitForFunction(
    () => document.querySelectorAll(".reply-scope-row").length === 50,
  );
  checks.push("每页50条由服务端返回");
  await choose("每页条数", "10 条/页");
  await page.waitForFunction(
    () => document.querySelectorAll(".reply-scope-row").length === 10,
  );
  const before = await page
    .locator(".reply-contact-name strong")
    .allTextContents();
  await page.route("**/reply-scope?**", (route) =>
    route.fulfill({ status: 503, body: "" }),
  );
  await page.locator(".reply-scope-error").waitFor();
  check(
    JSON.stringify(before) ===
      JSON.stringify(
        await page.locator(".reply-contact-name strong").allTextContents(),
      ),
    "读取失败保留上次成功结果",
  );
  await page.unroute("**/reply-scope?**");
  await page.locator(".reply-scope-error").waitFor({ state: "hidden" });
  await page.route("**/reply-scope?**", async (route) => {
    if (new URL(route.request().url()).searchParams.get("q") === "好友 0010")
      await new Promise((r) => setTimeout(r, 1000));
    await route.continue().catch(() => {});
  });
  await search.fill("好友 0010");
  await page.waitForTimeout(400);
  await search.fill("跨页原始昵称");
  await page.getByText("共 1 位好友", { exact: true }).waitFor();
  await page.waitForTimeout(1200);
  check(
    (await page.locator(".reply-contact-name strong").innerText()) ===
      "跨页备注目标",
    "快速查询忽略旧响应",
  );
  await page.unroute("**/reply-scope?**");
  await choose("选择账号", "第二账号");
  await page.getByText("共 3 位好友", { exact: true }).waitFor();
  check(
    (await page
      .getByRole("textbox", { name: "搜索回复范围", exact: true })
      .inputValue()) === "",
    "切换账号重置查询且无跨账号结果",
  );
  await choose("选择账号", "WeMind");
  await page.getByText("共 527 位好友", { exact: true }).waitFor();
  let releaseGroup, arrivedGroup;
  const groupGate = new Promise((resolve) => {
    releaseGroup = resolve;
  });
  const groupSeen = new Promise((resolve) => {
    arrivedGroup = resolve;
  });
  await page.route("**/reply-scope?**", async (route) => {
    if (new URL(route.request().url()).searchParams.get("kind") === "group") {
      arrivedGroup();
      await groupGate;
    }
    await route.continue().catch(() => {});
  });
  await page.getByRole("button", { name: "群聊", exact: true }).click();
  await groupSeen;
  check(
    (await page.locator(".reply-scope-row").count()) === 0,
    "分类请求未完成时不展示上一分类对象",
  );
  releaseGroup();
  await page.getByText("共 1 个群聊", { exact: true }).waitFor();
  await page.unroute("**/reply-scope?**");
  checks.push("好友群聊独立查询");
  await page.route("**/reply-scope?**", (route) =>
    route.fulfill({ status: 503, body: "" }),
  );
  await page.getByRole("button", { name: "好友", exact: true }).click();
  await page.locator(".reply-scope-error").waitFor();
  check(
    await page
      .locator(".reply-scope-row button")
      .evaluateAll(
        (buttons) =>
          buttons.length > 0 && buttons.every((button) => button.disabled),
      ),
    "新查询失败时缓存旧对象不可误操作",
  );
  await page.unroute("**/reply-scope?**");
  await page.getByText("共 527 位好友", { exact: true }).waitFor();
  await page.locator(".prompt-live-editor:not(.is-loading)").waitFor();
  await page.getByRole("textbox", { name: "助手设定内容" }).click();
  await page.keyboard.press("Control+End");
  await page.keyboard.insertText("\n隔离保存验收");
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await page.locator(".prompt-save-bar").waitFor({ state: "hidden" });
  check(
    (
      await (await page.request.get("/wechat/api/v1/accounts")).json()
    )[0].prompt.includes("隔离保存验收"),
    "设定通过现有接口保存并回读",
  );
  for (const [width, height] of [
    [1366, 768],
    [1440, 1024],
    [1920, 1080],
    [390, 844],
    [320, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.waitForFunction(() => !document.querySelector(".toast"));
    check(
      await page.locator(".assistant-enable").evaluate((el) => {
        const text = el.querySelector("span").getBoundingClientRect(),
          toggle = el.querySelector("button").getBoundingClientRect();
        return (
          Math.abs(text.y + text.height / 2 - toggle.y - toggle.height / 2) < 1
        );
      }),
      `${width}px启用助手文字与开关同行`,
    );
    check(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `${width}px助手设定无横向溢出`,
    );
    check(
      await page.evaluate(() => {
        const prompt = document
          .querySelector("#assistant-panel-prompt")
          .getBoundingClientRect();
        const scope = document
          .querySelector("#assistant-panel-scope")
          .getBoundingClientRect();
        const controls = document
          .querySelector(".assistant-account-controls")
          .getBoundingClientRect();
        return (
          controls.bottom <= prompt.top &&
          (innerWidth > 1100
            ? Math.abs(prompt.top - scope.top) < 1 && prompt.right < scope.left
            : prompt.bottom <= scope.top)
        );
      }),
      `${width}px账号在顶部，双栏或上下排列正确`,
    );
    await page.screenshot({ path: `${output}/isolated-prompt-${width}.png` });
    await page.getByText("共 527 位好友", { exact: true }).waitFor();
    check(
      await page.locator(".reply-search").evaluate((el) => {
        const icon = el.querySelector("svg").getBoundingClientRect(),
          input = el.querySelector("input").getBoundingClientRect();
        return (
          Math.abs(icon.y + icon.height / 2 - input.y - input.height / 2) < 1
        );
      }),
      `${width}px搜索图标与输入框同行`,
    );
    check(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `${width}px回复范围无横向溢出`,
    );
    await page.screenshot({ path: `${output}/isolated-scope-${width}.png` });
    await page.locator(".prompt-live-editor:not(.is-loading)").waitFor();
  }
  await page.reload();
  await page.locator(".assistant-columns").waitFor();
  check(
    (await page.locator("#assistant-panel-prompt").isVisible()) &&
      (await page.locator("#assistant-panel-scope").isVisible()),
    "刷新后两栏内容仍同时显示",
  );
  check(errors.length === 0, "浏览器无脚本异常");
  return { checks, errors };
}
