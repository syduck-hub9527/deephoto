deephoto 前端内置第三方库(离线可用,无构建链,经 /vendor 静态挂载直接提供)
来源均为 npm registry,版本钉死;升级时同步更新本文件与对应 LICENSE。

katex/         (既有)KaTeX,公式排版。见 katex/README.txt。

preact/        preact 11.0.0,MIT
               https://www.npmjs.com/package/preact/v/11.0.0
               preact.mjs       = package/dist/preact.mjs        (11,802 B)
               hooks.mjs        = package/hooks/dist/hooks.mjs   ( 3,667 B,含裸导入 "preact",依赖 import map)
htm/           htm 3.1.1,Apache-2.0
               https://www.npmjs.com/package/htm/v/3.1.1
               htm.module.js    = package/dist/htm.module.js     ( 1,207 B,默认导出)
markdown-it/   markdown-it 15.0.2,MIT
               https://www.npmjs.com/package/markdown-it/v/15.0.2
               markdown-it.esm.min.mjs = package/dist/browser/markdown-it.esm.min.mjs (138,276 B,默认导出,无外部导入)
               markdown-it.umd.min.js  = package/dist/browser/markdown-it.umd.min.js  (115,080 B,备选:
                                         若 ESM+import map 在目标浏览器不可用,改用经典 <script> 加载得到 window.markdownit)

import map(见 M0 验证页 probe.html,实施后见 index.html):
  "preact":        "/vendor/preact/preact.mjs"
  "preact/hooks":  "/vendor/preact/hooks.mjs"
  "htm":           "/vendor/htm/htm.module.js"
  "markdown-it":   "/vendor/markdown-it/markdown-it.esm.min.mjs"

2026-09-30 内置(对应开发文档 md文档/deephoto_frontend_redesign_dev.md §5.1 / M0)。
