# 全站界面字体

所有网页采用 Inter 显示英文及数字，Noto Sans SC 显示中文。正文400、按钮及关键标签500、标题及强调600；公共主题通过 `--font-ui` 统一登录、管理台、弹窗、提示、编辑器。代码及源码使用 `--font-mono`，生僻字和Emoji可回退到系统字体。

字体随站点提供，不请求外部字体服务。`fonts.css` 按互不重叠的 `unicode-range` 加载字符分片，并使用 `font-display: swap`：

| 文件 | 覆盖范围 | 大小 |
| --- | --- | --- |
| `inter-latin.woff2` | 基本及扩展拉丁字母、数字、常用标点和货币符号 | 77,056 bytes |
| `noto-sans-sc-ui.woff2` | 全站前端源码及默认助手设定中的固定用字、ASCII及常用连接标点 | 158,212 bytes |
| `noto-sans-sc-extended.woff2` | GB2312、CJK标点和全角字符中未进入界面分片的字符 | 1,687,792 bytes |

新增文案中已被扩展分片覆盖的字符会自动按需加载；超出覆盖范围时使用系统回退字体。

原始字体均采用 SIL Open Font License 1.1，完整许可为本目录的 `OFL-Inter.txt` 和 `OFL-NotoSansSC.txt`。原始文件来源、版本、SHA256和各发布分片哈希记录于 `sources.json`。

- Noto Sans SC 2.004：[Google Fonts 固定提交](https://github.com/google/fonts/tree/a85815a42757630ce188fdad368c2dfc444d4773/ofl/notosanssc)。
- Inter 4.001：[官方字体文件](https://rsms.me/inter/font-files/InterVariable.woff2)。

分片使用 FontTools `subset.Subsetter` 保留指定 Unicode 字符，再以 `varLib.instancer.instantiateVariableFont` 将 `wght` 限定至400–600，输出 WOFF2；保留 Inter 的原有光学字号轴。来源更新或重新分片时同步核对许可、字符范围、CSS与 `sources.json`。

新增固定界面文案后，使用已记录版本重新生成中文分片：

```powershell
uv run --no-project --with "fonttools[woff]==4.65.0" --python 3.10 python scripts/build_font_assets.py
```

脚本按 `sources.json` 的固定来源及原件SHA256校验，不自动更新字体版本；可用 `--source <原字体路径>` 复用已下载原件。先裁剪字符、再限制字重，更新生成器版本、输入文件、分片及哈希；Inter分片保持原样。
