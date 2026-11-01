import { defineConfig } from "vite";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { createHash } from "node:crypto";

const require = createRequire(import.meta.url);
const vditorDist = dirname(require.resolve("vditor"));
const emotionRoot = new URL("./src/assets/native-emotions/", import.meta.url);

export default defineConfig({
  base: "/wechat/",
  plugins: [
    {
      name: "native-emotion-resources",
      buildStart() {
        const aliases = JSON.parse(
          readFileSync(
            new URL(
              "../backend/wechat_manager/native_emotions.json",
              import.meta.url,
            ),
            "utf8",
          ),
        );
        const resources = JSON.parse(
          readFileSync(new URL("manifest.json", emotionRoot), "utf8"),
        );
        const names = new Set(Object.values(aliases));
        if (
          Object.keys(resources).length !== names.size ||
          Object.keys(resources).some((name) => !names.has(name))
        )
          this.error("微信内置表情图标与统一词表不一致");
        for (const name of names) {
          const resource = resources[name as string];
          if (!resource || !/^emotion-\d{3}\.png$/.test(resource.file))
            this.error(`微信表情缺少图标：${name}`);
          const body = readFileSync(new URL(resource.file, emotionRoot));
          if (
            createHash("sha256").update(body).digest("hex") !== resource.sha256
          )
            this.error(`微信表情图标哈希不符：${name}`);
          this.emitFile({
            type: "asset",
            fileName: `assets/native-emotions/${resource.file}`,
            source: body,
          });
        }
      },
    },
    {
      name: "prompt-editor-resources",
      generateBundle() {
        for (const name of [
          "js/lute/lute.min.js",
          "js/i18n/zh_CN.js",
          "js/icons/ant.js",
          "js/highlight.js/styles/github.min.css",
        ]) {
          this.emitFile({
            type: "asset",
            fileName: `assets/vendor/vditor/4.0.0/dist/${name}`,
            source: readFileSync(join(vditorDist, name)),
          });
        }
      },
    },
  ],
  server: { proxy: { "/wechat/api": "http://127.0.0.1:17950" } },
});
