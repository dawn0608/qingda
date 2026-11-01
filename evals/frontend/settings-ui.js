async function checkSettingsUI(page) {
  if (!["127.0.0.1", "localhost"].includes(new URL(page.url()).hostname))
    throw Error("仅允许服务器隔离验收入口");
  const checks = [];
  const verify = (ok, name) => {
    if (!ok) throw Error(name);
    checks.push(name);
  };
  await page.getByRole("button", { name: "系统设置", exact: true }).click();
  await page.getByRole("textbox", { name: "接入 Token", exact: true }).waitFor();
  for (const [width, height] of [[1864, 893], [1440, 640], [1280, 720], [1024, 768], [390, 844], [320, 568]]) {
    await page.setViewportSize({ width, height });
    const geometry = await page.locator(".settings-grid").evaluate(el => {
      const panels = [...el.children];
      const [wechat, model] = panels.map(p => p.getBoundingClientRect());
      return { count: panels.length, top: Math.abs(wechat.top - model.top),
        equal: Math.abs(wechat.width - model.width), stacked: model.top >= wechat.bottom + 15,
        overflow: document.documentElement.scrollWidth > innerWidth };
    });
    verify(geometry.count === 2 && !geometry.overflow, width + "px两个面板且无横向溢出");
    verify(width >= 1280 ? geometry.top < 1 && geometry.equal < 1 : geometry.stacked,
      width + "px桌面双列、窄屏单列");
    verify(await page.locator(".model-provider-section").count() === 2, "保留两个模型供应商");
    const callback = page.locator(".callback-copy");
    verify(await callback.evaluate(e => e.readOnly && e.tagName === "INPUT"), "回调只读单行展示");
  }
  return checks;
}
