# 第三方资源与权利声明

根 `LICENSE` 中的 MIT 许可适用于本项目自有代码，不覆盖第三方独立资源、商标及外部服务。以下材料保留原权利人的版权及适用条款，来源说明本身不构成额外授权。

## 界面字体

Inter 4.001 与 Noto Sans SC 2.004 采用 SIL Open Font License 1.1。仓库包含原始完整许可及生成分片的来源、版本和哈希记录：

- [Inter OFL](frontend/src/assets/fonts/OFL-Inter.txt)
- [Noto Sans SC OFL](frontend/src/assets/fonts/OFL-NotoSansSC.txt)
- [字体说明](frontend/src/assets/fonts/README.md)及[来源清单](frontend/src/assets/fonts/sources.json)

## 微信内置文字表情

`frontend/src/assets/native-emotions/` 的 PNG 来源于微信官网原始素材的固定归档，图案版权归微信原权利人，未改以本项目 MIT 许可授权。

来源为 [airinghost/wechat-emoji 固定提交](https://github.com/airinghost/wechat-emoji/tree/2896eb55348041f5c5a66e4eccfb4882ab9cda0c)；原官网地址、固定归档地址及逐文件哈希见[资源清单](frontend/src/assets/native-emotions/manifest.json)，用途与来源见[资源说明](frontend/src/assets/native-emotions/README.md)。归档仓库的声明不替代图案权利人的授权。

## DeepSeek 分词资源

`backend/wechat_manager/tokenizer/tokenizer.json` 来自 DeepSeek 官方发布的分词资源，来源及哈希见[分词资源说明](backend/wechat_manager/tokenizer/README.md)。本项目不拥有该资源的版权，也不将其重新授权为 MIT；其使用和再分发须遵循上游适用条款。本仓库的来源记录不代表已取得额外的再许可权。

## 外部来源与依赖

评测中的外部网页链接和来源元数据用于说明历史案例；公开版新闻搜索夹具的正文已改为自行编写的简短说明，并明确标注，不作为原网页完整响应或新闻事实依据。原网站内容权利归相应权利人。

Python 和 npm 依赖由 `uv.lock`、`frontend/package-lock.json` 锁定，各自使用其上游许可证；安装或构建后再分发依赖时，须同时保留依赖自带的许可声明。WechatApi、微信、DeepSeek、阿里云百炼及和风天气等服务名称与商标归各自权利人，本项目不代表这些服务提供方。
