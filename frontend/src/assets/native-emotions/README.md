# 微信内置文字表情资源

仅用于聊天记录中的内置文字表情展示，别名与标准名称始终来自后端 `native_emotions.json`。

素材取自 [airinghost/wechat-emoji](https://github.com/airinghost/wechat-emoji/tree/2896eb55348041f5c5a66e4eccfb4882ab9cda0c) 保留的微信官网原始PNG，固定提交为 `2896eb55348041f5c5a66e4eccfb4882ab9cda0c`。只使用 `web.wechat.com/uncompressed` 和 `mp.weixin.qq.com/uncompressed`，未使用第三方重绘或压缩版本；图案版权归微信原权利人。该归档的来源说明见其README。

`manifest.json` 逐项登记标准名称、本站文件名、SHA-256、原官网与固定归档地址。构建检查统一词表的完整覆盖和每个文件哈希，将图片输出至本站 `/wechat/assets/native-emotions/`；页面不请求归档站或微信CDN。新增或调整词表时须同步核实图标，不能用近似表情代替。
