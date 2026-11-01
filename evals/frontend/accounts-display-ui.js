async function checkAccountDisplayUI(page) {
  if (!["127.0.0.1:17953", "127.0.0.1:17954", "127.0.0.1:17955"].includes(new URL(page.url()).host))
    throw Error("只能在隔离验收服务运行");
  const checks = [];
  const verify = (ok, name) => {
    if (!ok) throw Error(name);
    checks.push(name);
  };
  const longAppId = "wx-app-" + "0123456789abcdef".repeat(12);
  const longSignature = "认真生活，及时回应。".repeat(30) + "<img src=x onerror=alert(1)>";
  let accounts = [
    {
      id: 1001,
      last_successful_refresh_at: 1791590400,
      nickname: "本地演示账号",
      wxid: "wxid_demo_1001",
      current_app_id: "wx-app-demo-1001",
      online: 1,
      contacts: { friend_count: 37, chatroom_count: 1, updated_at: 1791590400, state: "idle" },
      login_state: "logged_in",
      login_config: { device_type: "mac", region_id: "320000" },
      profile: {
        mobile: "13800000000",
        alias: "linzhou_notes",
        country: "CN",
        province: "重庆",
        city: "重庆",
        signature: "认真生活，及时回应。",
        sex: 1,
        smallHeadImgUrl: "https://wx.qlogo.cn/isolated-broken-avatar",
      },
    },
    {
      id: 1002,
      nickname: "协作账号",
      wxid: "wxid_demo_1002",
      online: 0,
      contacts: { friend_count: 12, chatroom_count: 0, updated_at: 1791590400, state: "failed" },
      login_state: "offline",
      login_config: { device_type: "ipad" },
      profile: { sex: 2 },
    },
    {
      id: 1003,
      nickname: "用于验证完整展示边界的超长微信昵称与团队项目沟通",
      wxid: "wxid_demo_1003_abcdefghijklmnopqrstuvwxyz",
      current_app_id: longAppId,
      online: 1,
      login_state: "logged_in",
      contacts: { friend_count: null, chatroom_count: null, updated_at: null, state: "queued" },
      login_config: {},
      profile: { sex: 0, signature: longSignature },
    },
    {
      id: 1004,
      nickname: "待核实账号",
      wxid: "wxid_demo_1004",
      online: null,
      login_state: "logged_out",
      profile: {},
    },
    {
      id: 1005,
      nickname: "登录中账号",
      wxid: "wxid_demo_1005",
      online: 1,
      login_state: "waiting_confirm",
      profile: {},
    },
  ];
  const row = (name) => page.locator(".account-row").filter({ hasText: name });
  const json = (route, data) => route.fulfill({ json: data });
  let listRequests = 0;
  let onlineRequests = 0;
  const listRoute = (route) => {
    listRequests++;
    return json(route, accounts);
  };
  const onlineRoute = (route) => {
    onlineRequests++;
    return json(route, {});
  };
  const pagination = page.getByRole("navigation", { name: "微信账号分页" });
  const previous = pagination.getByRole("button", { name: "上一页" });
  const next = pagination.getByRole("button", { name: "下一页" });
  let refreshReentered = false;
  const refresh = async () => {
    const button = page.getByRole("button", { name: "刷新", exact: true });
    if (await button.count()) {
      refreshReentered = false;
      let responses = 0;
      const response = page.waitForResponse((r) => r.url().endsWith("/accounts") && ++responses === 2);
      await button.click();
      await response;
      await button.click({ trial: true });
    } else {
      refreshReentered = true;
      let responses = 0;
      const finished = page.waitForResponse((r) => r.url().endsWith("/accounts") && ++responses === 2);
      await page.getByRole("button", { name: "消息管理", exact: true }).click();
      await page.getByRole("button", { name: "微信管理", exact: true }).click();
      await finished;
      await page.waitForTimeout(100);
    }
  };
  let authorizationRequests = 0;
  const settle = () => page.waitForLoadState("networkidle");
  const authorizationRoute = (route) => {
    authorizationRequests++;
    return route.fulfill({ status: 404, json: { detail: "接口已删除" } });
  };
  const avatarRoute = (route) => route.abort();
  await page.route("**/wechat/api/v1/accounts", listRoute);
  await page.route("**/wechat/api/v1/accounts/*/online", onlineRoute);
  await page.route("**/wechat/api/v1/authorization", authorizationRoute);
  await page.route("https://wx.qlogo.cn/isolated-broken-avatar", avatarRoute);
  try {
    await page.clock.install();
    await page.clock.pauseAt(new Date());
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.reload();
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await row("本地演示账号").waitFor();
    await page.waitForTimeout(100);
    verify(
      (await page.getByRole("columnheader").allTextContents()).join(",") ===
        "头像,昵称,微信号,手机号,性别,个性签名,登录设备,登录状态,最近更新时间",
      "九列表头和账号信息完整展示",
    );
    verify(
      (await page
        .locator(
          ".account-row button, .accounts-page input, .accounts-page select",
        )
        .count()) === 0,
      "账号区域没有行内操作按钮",
    );
    verify(
      !(await page.locator(".accounts-page").innerText()).match(
        /AppID|Token|回调地址|账号地区|登录时长|最近登录/,
      ),
      "不展示无可靠来源或无关字段",
    );
    verify(
      (await row("协作账号")
        .locator(".account-signature .account-value")
        .innerText()) === "—" &&
        (await row("协作账号").innerText()).includes("wxid_demo_1002"),
      "缺失资料占位且微信号回退wxid",
    );
    verify(
      (await row("待核实账号").innerText()).includes("状态待确认") &&
        (await row("登录中账号").innerText()).includes("登录中"),
      "未知及登录中状态准确展示",
    );
    verify((await row("本地演示账号").locator(".account-gender .account-value").innerText()) === "男" &&
      (await row("协作账号").locator(".account-gender .account-value").innerText()) === "女" &&
      (await row("用于验证完整展示边界").locator(".account-gender .account-value").innerText()) === "—",
      "性别男女性别未知正确显示");
    verify((await row("本地演示账号").locator(".account-refreshed-at .account-value").innerText()) === "2026-10-10 08:00:00" &&
      (await row("协作账号").locator(".account-refreshed-at .account-value").innerText()) === "—",
      "成功更新时间按北京时间展示且缺失占位");
    verify((await row("用于验证完整展示边界").locator(".account-signature img").count()) === 0,
      "签名按纯文本展示不执行HTML");
    verify((await row("本地演示账号").locator(".account-mobile .account-value").innerText()) === "13800000000" &&
      (await row("协作账号").locator(".account-mobile .account-value").innerText()) === "—", "手机号完整展示及缺失占位");
    verify((await row("本地演示账号").locator(".account-device .account-value").innerText()) === "Mac" &&
      (await row("协作账号").locator(".account-device .account-value").innerText()) === "iPad" &&
      (await row("用于验证完整展示边界").locator(".account-device .account-value").innerText()) === "—",
      "登录设备使用保存的平台类型且缺失占位");
    verify(await page.locator(".account-friends,.account-chatrooms").count() === 0,
      "账号列表移除通讯录统计列");
    verify(await page.locator(".accounts-total strong").evaluate((el) =>
      el.textContent === "5" && Number(getComputedStyle(el).fontWeight) >= 600),
      "底部总数仅数字加粗");
    await page.waitForFunction(
      () => document.querySelector(".account-avatar img")?.hidden === true,
    );
    verify(
      (await row("本地演示账号").locator(".account-avatar").innerText()) ===
        "本",
      "头像失败使用昵称占位",
    );
    for (const selector of [
      "strong",
      ".account-wechat-id small",
      ".account-signature .account-value",
    ])
      verify(
        await row("用于验证完整展示边界")
          .locator(selector)
          .evaluate(
            (e) => e.scrollWidth > e.clientWidth && e.title === e.textContent,
          ),
        "长文本省略且悬停全文：" + selector,
      );
    for (const width of [1440, 1920]) {
      await page.setViewportSize({ width, height: 1000 });
      verify(
        await page.locator(".accounts-page").evaluate((el) => {
          const headers = [...el.querySelectorAll('[role="columnheader"]')];
          return (
            headers.every((h) => getComputedStyle(h).textAlign === "center") &&
            [...el.querySelectorAll(".account-row")].every((row) =>
              [...row.querySelectorAll('[role="cell"]')].every((cell, i) => {
                const r = cell.getBoundingClientRect(),
                  h = headers[i].getBoundingClientRect(),
                  rr = row.getBoundingClientRect();
                return (
                  Math.abs(r.left - h.left) < 1 &&
                  Math.abs(r.width - h.width) < 1 &&
                  Math.abs(r.top + r.height / 2 - rr.top - rr.height / 2) < 1 &&
                  getComputedStyle(cell).textAlign === "center"
                );
              }),
            )
          );
        }),
        width + "px所有表头内容居中且列宽一致",
      );
      verify(
        await page
          .locator(".account-row")
          .evaluateAll((rows) =>
            rows.every((r) => r.getBoundingClientRect().height === 56),
          ),
        width + "px账号行高56px",
      );
      verify(
        await page.locator(".accounts-page").evaluate(
          (el) =>
            el.querySelector(".accounts-header").getBoundingClientRect()
              .height === 40 &&
            [...el.querySelectorAll(".account-identity")].every((group) => {
              const cells = [...group.children].map((cell) => cell.getBoundingClientRect());
              const avatar = group.querySelector(".account-avatar").getBoundingClientRect();
              return cells[0].width === 56 && cells[1].width === 96 && cells[2].width === 132 &&
                avatar.width === 36 && avatar.height === 36 &&
                Math.abs(avatar.left + avatar.width / 2 - cells[0].left - cells[0].width / 2) < 1;
            }),
        ),
        width + "px头像昵称微信号独立三列，头像36px且首列居中",
      );
      verify(await page.locator(".accounts-page").evaluate((el) => {
        const total = el.querySelector(".accounts-total").getBoundingClientRect();
        const avatar = el.querySelector(".account-avatar").getBoundingClientRect();
        const pagination = el.querySelector(".accounts-pagination").getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(el.querySelector(".account-refreshed-at time[datetime]"));
        return Math.abs(total.left - avatar.left) < 1 &&
          Math.abs(pagination.right - range.getBoundingClientRect().right - 4) < 1;
      }), width + "px底栏总数与头像左边缘对齐、分页相对时间文字向右微调4px");
      verify(
        await page
          .locator(".accounts-pagination")
          .evaluate(
            (el) =>
              getComputedStyle(el).gap === "6px" &&
              getComputedStyle(el.querySelector("span")).minWidth === "40px",
          ),
        width + "px分页间距6px页码40px",
      );
      verify(
        await page
          .locator(".accounts-footer")
          .evaluate((el) => el.getBoundingClientRect().height === 48),
        width + "px底栏48px",
      );
      verify(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        width + "px无横向溢出",
      );
      await page.screenshot({
        path: "output/playwright/wechat-management-identity-" + width + ".png",
        fullPage: true,
      });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    verify(await row("本地演示账号").locator(".account-identity").evaluate((group) => {
      const avatar = group.querySelector(".account-avatar").getBoundingClientRect();
      const nickname = group.querySelector(".account-nickname").getBoundingClientRect();
      const identifier = group.querySelector(".account-wechat-id").getBoundingClientRect();
      return getComputedStyle(group).display === "grid" && avatar.right < nickname.left &&
        Math.abs(nickname.left - identifier.left) < 1 && nickname.bottom <= identifier.top;
    }), "390px头像与昵称微信号恢复紧凑组合");
    for (const selector of [
      ".account-signature",
      ".account-refreshed-at",
    ])
      verify(
        await row("用于验证完整展示边界")
          .locator(selector + " .account-value")
          .evaluate(
            (e) =>
              getComputedStyle(e).whiteSpace === "normal" &&
              e.scrollWidth <= e.clientWidth,
          ),
        "390px长值完整换行：" + selector,
      );
    verify(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      "390px无横向溢出",
    );
    verify(
      await next.evaluate((e) => e.getBoundingClientRect().height === 44),
      "390px分页点击区域44px",
    );
    await page.screenshot({
      path: "output/playwright/wechat-management-identity-390.png",
      fullPage: true,
    });
    accounts = accounts.slice(0, 1);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await refresh();
    verify(
      (await page.locator(".account-row").count()) === 1 &&
        (await pagination.getByRole("status").innerText()) === "1 / 1" &&
        !(await previous.isEnabled()) &&
        !(await next.isEnabled()),
      "单账号页码及边界正确",
    );
    verify(authorizationRequests === 0, "微信管理不请求授权配置接口");
    verify((await page.locator(".account-token, .account-callback, .account-app-id").count()) === 0,
      "已删除列及其悬停信息全部移除");
    accounts = [];
    await refresh();
    await page.getByText("还没有接入的微信", { exact: true }).waitFor();
    verify(
      (await pagination.getByRole("status").innerText()) === "0 / 0" &&
        !(await previous.isEnabled()) &&
        !(await next.isEnabled()) &&
        (await page.locator(".accounts-total").innerText()) === "共 0 条",
      "空态总数和分页正确",
    );
    const manyAccounts = Array.from({ length: 25 }, (_, i) => ({
      id: 2001 + i,
      nickname: "分页演示" + String(i + 1).padStart(2, "0"),
      wxid: "wxid_pagination_" + (i + 1),
      current_app_id: "wx-app-pagination-" + (i + 1),
      online: 1, login_state: "logged_in", profile: {},
    }));
    const names = () => page.locator(".account-row strong").allTextContents();
    const capacityAt = (height) => ({ 600: 7, 768: 10, 900: 12, 1080: 12 })[height];
    const waitNames = async (expected) => {
      await page.waitForFunction((expected) =>
        [...document.querySelectorAll(".account-row strong")].map(e => e.textContent).join(",") === expected.join(","), expected);
    };
    const expectedNames = (start, end) => accounts.slice(start, end).map(a => a.nickname);
    const checkFit = async (full, label) => {
      const geometry = await page.locator(".accounts-page").evaluate((el) => {
        const table = el.querySelector(".panel").getBoundingClientRect();
        const topbar = document.querySelector(".topbar").getBoundingClientRect();
        return {
          top: table.top - topbar.bottom, bottom: innerHeight - table.bottom,
          heights: [...el.querySelectorAll(".account-row")].map(e => e.getBoundingClientRect().height),
          noOverflow: document.documentElement.scrollHeight <= innerHeight &&
            document.documentElement.scrollWidth <= innerWidth &&
            [...el.querySelectorAll('.panel, [role="table"], [role="rowgroup"]')].every(e =>
              e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth + 1),
        };
      });
      verify(geometry.noOverflow && Math.abs(geometry.top - 24) < 1 &&
        (full ? Math.abs(geometry.bottom - 24) < 1 && geometry.heights.every(h => h >= 56 && Math.abs(h - geometry.heights[0]) < 1)
          : geometry.heights.every(h => h === 56)), label + (full ? "满页上下24px且均分行高无溢出" : "不足一页保持56px紧凑且无溢出"));
    };
    for (const viewport of [
      { width: 1440, height: 900 }, { width: 1920, height: 1080 },
      { width: 1440, height: 768 }, { width: 1440, height: 600 },
    ]) {
      await page.setViewportSize(viewport);
      const capacity = capacityAt(viewport.height);
      for (const count of [0, 1, 11, 12, 13, 24, 25]) {
        accounts = manyAccounts.slice(0, count);
        await refresh();
        while (await previous.isEnabled()) await previous.click();
        await waitNames(expectedNames(0, capacity));
        await settle();
        const pages = Math.ceil(count / capacity);
        verify((await pagination.getByRole("status").innerText()) === (count ? "1 / " + pages : "0 / 0") &&
          !(await previous.isEnabled()) && (await next.isEnabled()) === (pages > 1),
          viewport.width + "×" + viewport.height + "数量" + count + "容量" + capacity + "及首尾禁用正确");
        await checkFit(count >= capacity, viewport.height + "px数量" + count);
        const seen = await names();
        const before = [listRequests, onlineRequests, authorizationRequests];
        for (let p = 2; p <= pages; p++) {
          await next.click();
          await waitNames(expectedNames((p - 1) * capacity, p * capacity));
          seen.push(...await names());
          await checkFit(count - (p - 1) * capacity >= capacity, viewport.height + "px第" + p + "页");
        }
        verify(seen.join(",") === accounts.map(a => a.nickname).join(",") && !(await next.isEnabled()) &&
          before.join(",") === [listRequests, onlineRequests, authorizationRequests].join(","),
          viewport.height + "px数量" + count + "逐页无重复遗漏且不请求接口 ");
        if (count === 24) {
          // 回第一页留存满页测试截图。
          while (await previous.isEnabled()) await previous.click();
          await page.screenshot({ path: "output/playwright/wechat-management-adaptive-" + viewport.width + "x" + viewport.height + ".png", fullPage: true });
        }
      }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    accounts = manyAccounts;
    await refresh();
    while (await previous.isEnabled()) await previous.click();
    await page.locator(".top-actions .official-platform-link").focus();
    await page.keyboard.press("Tab");
    verify(await next.evaluate(e => e === document.activeElement && getComputedStyle(e).outlineStyle !== "none"), "分页按钮保留清晰键盘焦点");
    await next.click();
    await waitNames(expectedNames(12, 24));
    await settle();
    const beforeResize = [listRequests, onlineRequests];
    await page.setViewportSize({ width: 1440, height: 600 });
    await waitNames(expectedNames(7, 14));
    verify((await pagination.getByRole("status").innerText()) === "2 / 4", "窗口缩小保留原首条所在页而非跳首页");
    await page.setViewportSize({ width: 1440, height: 900 });
    await waitNames(expectedNames(0, 12));
    verify(beforeResize.join(",") === [listRequests, onlineRequests].join(","), "调整窗口不触发账号加载或在线核实");
    await next.click();
    await page.setViewportSize({ width: 390, height: 844 });
    await waitNames(expectedNames(10, 20));
    verify((await pagination.getByRole("status").innerText()) === "2 / 3" &&
      await page.locator(".account-identity").first().evaluate(e => getComputedStyle(e).display === "grid"),
      "切换卡片固定10条且保留首条所在页");
    verify(await page.locator(".accounts-page").evaluate((content) => {
      content.scrollTop = content.scrollHeight;
      return document.documentElement.scrollWidth <= innerWidth &&
        document.documentElement.scrollHeight <= innerHeight &&
        content.scrollHeight > content.clientHeight && content.scrollTop > 0 &&
        content.scrollHeight - content.clientHeight - content.scrollTop < 1;
    }), "390px卡片内容区自然纵向滚动到底部且无横向溢出");
    await page.screenshot({ path: "output/playwright/wechat-management-adaptive-390.png", fullPage: true });
    await page.setViewportSize({ width: 1440, height: 768 });
    await waitNames(expectedNames(10, 20));
    await page.setViewportSize({ width: 1440, height: 900 });
    await waitNames(expectedNames(0, 12));
    verify((await pagination.getByRole("status").innerText()) === "1 / 3", "相同容量模式切换与扩大窗口按原首条计算新页码");
    // 浏览器缩放改变 CSS 视口，和窗口调整走同一测量路径。
    await page.setViewportSize({ width: 1152, height: 720 });
    await waitNames(expectedNames(0, 10));
    await page.setViewportSize({ width: 1440, height: 900 });
    await waitNames(expectedNames(0, 12));
    verify(beforeResize.join(",") === [listRequests, onlineRequests].join(","), "模拟125%缩放及恢复不触发在线查询");
    await next.click();
    await page.evaluate(() => document.querySelector(".shell").style.setProperty("--sidebar-width", "248px"));
    await waitNames(expectedNames(10, 20));
    await page.evaluate(() => document.querySelector(".shell").style.removeProperty("--sidebar-width"));
    await waitNames(expectedNames(0, 12));
    verify((await pagination.getByRole("status").innerText()) === "1 / 3", "仅容器变窄或恢复也能切换卡片与容量");
    await next.click();
    await refresh();
    await waitNames(expectedNames(refreshReentered ? 0 : 12, refreshReentered ? 12 : 24));
    verify((await pagination.getByRole("status").innerText()) === (refreshReentered ? "1 / 3" : "2 / 3"),
      "刷新保留页码，显式重新进入从第一页开始");
    while (await next.isEnabled()) await next.click();
    accounts = manyAccounts.slice(0, 13);
    await refresh();
    await waitNames(expectedNames(refreshReentered ? 0 : 12, refreshReentered ? 12 : 13));
    verify((await pagination.getByRole("status").innerText()) === (refreshReentered ? "1 / 2" : "2 / 2"),
      "数量减少纠正越界，重新进入重置首屏");
    accounts = [];
    await refresh();
    await waitNames([]);
    verify((await pagination.getByRole("status").innerText()) === "0 / 0", "末页清空账号正确回到空态");
  } finally {
    await page.clock.resume();
    await page.unroute("**/wechat/api/v1/accounts", listRoute);
    await page.unroute("**/wechat/api/v1/accounts/*/online", onlineRoute);
    await page.unroute("**/wechat/api/v1/authorization", authorizationRoute);
    await page.unroute(
      "https://wx.qlogo.cn/isolated-broken-avatar",
      avatarRoute,
    );
  }
  return checks;
}
