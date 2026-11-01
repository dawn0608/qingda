async function checkNavigationUI(page) {
  if (!["127.0.0.1:17953", "127.0.0.1:17954", "127.0.0.1:17955", "127.0.0.1:17959", "127.0.0.1:17961"].includes(new URL(page.url()).host))
    throw Error("只能在隔离验收服务运行");
  const checks = [];
  const verify = (ok, name) => {
    if (!ok) throw Error(name);
    checks.push(name);
  };
  await page.locator(".login-page, .shell").waitFor();
  if (await page.getByLabel("手机号", { exact: true }).count()) {
    await page.getByRole("button", { name: "密码登录", exact: true }).click();
    await page.getByLabel("手机号", { exact: true }).fill("13900000000");
    await page.getByLabel("密码", { exact: true }).fill("isolated-browser-only-passphrase");
    await page.getByRole("button", { name: "登录", exact: true }).click();
  }
  await page.locator(".shell").waitFor();
  for (const [width, height] of [[1366, 768], [1440, 900], [1920, 1080], [1994, 966], [1440, 600], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.getByRole("button", { name: "微信管理", exact: true }).click();
    await page.locator(".account-row").first().waitFor();
    await page.evaluate(() => {
      window.scrollTo(0, 0);
      const original = document.querySelector(".official-platform-link");
      const frames = [];
      let running = true;
      const read = () => {
        const link = document.querySelector(".official-platform-link");
        const rect = link?.getBoundingClientRect(), style = link && getComputedStyle(link);
        const content = document.querySelector(".shell > main > .page");
        const header = link?.closest(".topbar").getBoundingClientRect();
        frames.push({
          same: link === original,
          visible: !!rect && rect.width > 0 && rect.height > 0 && style.opacity === "1" &&
            style.visibility === "visible" && style.display !== "none",
          x: rect?.x, y: rect?.y, width: rect?.width, height: rect?.height,
          color: style?.color, background: style?.backgroundColor,
          scrollable: !!content && content.scrollHeight > content.clientHeight,
          rootScrollable: document.documentElement.scrollHeight > innerHeight,
          rightGap: header ? innerWidth - header.right : null,
        });
        if (running) requestAnimationFrame(read);
      };
      window.__navigationCheck = { frames, stop: () => { running = false; } };
      read();
    });
    let frames;
    try {
      for (const name of ["系统设置", "微信管理", "系统设置", "智能助手", "微信管理"]) {
        await page.getByRole("button", { name, exact: true }).click();
        await page.waitForTimeout(100);
        verify(await page.locator(".topbar h1").innerText() === name, width + "px切页标题：" + name);
      }
    } finally {
      frames = await page.evaluate(() => {
        const check = window.__navigationCheck;
        check.stop();
        delete window.__navigationCheck;
        return check.frames;
      });
    }
    verify(frames.length > 7 && frames.every((frame) => frame.same && frame.visible),
      width + "px逐帧按钮持续可见且未重建");
    for (const key of ["x", "y", "width", "height"])
      verify(Math.max(...frames.map((frame) => frame[key])) - Math.min(...frames.map((frame) => frame[key])) < 1,
        width + "px逐帧按钮" + key + "保持稳定");
    verify(frames.every((frame) => frame.color === frames[0].color && frame.background === frames[0].background),
      width + "px切页不改变按钮颜色");
    verify(frames.every((frame) => !frame.rootScrollable && Math.abs(frame.rightGap) < 1),
      width + "px页面铺满右侧且根页面不产生额外滚动条");
    await page.getByRole("button", { name: "系统设置", exact: true }).click();
    verify(await page.locator('.sidebar nav button').count() === 3, "仅保留三个导航入口");
    verify(await page.locator(".settings-grid > .panel").count() === 2, "系统设置仅两个面板");
    await page.getByRole("textbox", { name: "接入 Token", exact: true }).waitFor();
    if (width >= 1366 && height >= 768) {
      const layout = await page.locator('.settings-page').evaluate(el => {
        const cards = [...el.querySelectorAll('.settings-grid > .panel')];
        const [security, model] = cards.map(card => card.getBoundingClientRect());
        const main = el.parentElement.getBoundingClientRect();
        return el.scrollHeight <= el.clientHeight + 1 && cards.every(card => card.scrollHeight <= card.clientHeight + 1) &&
          Math.abs(security.top - model.top) < 1 && Math.abs(security.width - model.width) < 1 &&
          security.left - main.left <= 24 && main.right - model.right <= 24;
      });
      verify(layout, width + 'x' + height + '设置两列等宽、内容完整且无纵向滚动条');
    }
    const scroll = await page.locator(".shell > main > .page").evaluate((content) => {
      const root = document.documentElement;
      const header = document.querySelector(".topbar").getBoundingClientRect().top;
      content.scrollTop = content.scrollHeight;
      return {
        needed: content.scrollHeight > content.clientHeight,
        moved: content.scrollTop > 0,
        bottomVisible: content.scrollHeight - content.clientHeight - content.scrollTop < 1,
        headerStable: document.querySelector(".topbar").getBoundingClientRect().top === header,
        rootStayed: root.scrollTop === 0,
      };
    });
    verify((!scroll.needed || (scroll.moved && scroll.bottomVisible)) && scroll.headerStable && scroll.rootStayed,
      width + "px长页面内容可滚动到底部且顶栏固定");
    verify(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      width + "px切页无横向溢出");
    await page.screenshot({ path: "output/playwright/navigation-stable-" + width + "x" + height + ".png" });
  }
  await page.evaluate(() => sessionStorage.setItem('wechat-page', 'settings'));
  await page.reload();
  await page.locator('.settings-page').waitFor();
  verify(await page.locator('.topbar h1').innerText() === '系统设置', '刷新恢复系统设置');
  await page.reload();
  await page.locator('.settings-page').waitFor();
  verify(await page.evaluate(() => sessionStorage.getItem('wechat-page')) === 'settings', '刷新保留系统设置');
  return checks;
}
