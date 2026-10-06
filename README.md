# hotnews · 热点新闻检索

网页形式呈现的国内 / 国际热点新闻图库，支持搜索、分类、筛选、实时刷新。

单文件 exe，双击即用，无安装、无控制台窗口。

## 使用

下载 [最新 Release](https://gitee.com/kele551/hotnews/releases) 里的 `hotnews-v1.2.0.exe`，双击运行，自动打开浏览器。

- 自动选端口（8000 起，被占则顺延）
- 启动时自动检查更新（可关闭，见 config.json）
- 页面右上角「检查更新」手动触发

> 国内用户请从 Gitee 下载（速度快）。本仓库（GitHub）为镜像。

## 数据源

| 通道 | 源 |
|---|---|
| 国内 | 中新网（6 频道）+ 界面新闻（财经）+ IT之家（科技） |
| 国际 | France24（3 频道） |

国内综合类 RSS 生态已基本停更，现役可用源如上，详见 `说明.txt`。

## 本地开发

```bash
pip install -r requirements.txt
python app.py            # 或 python -m uvicorn server:app --port 8000
```

## 打包

```bash
pyinstaller build_exe.spec --clean --noconfirm   # 产出 dist/hotnews.exe
```

## 自动更新机制

- 程序启动时后台静默检查 `version.json`（Gitee，国内稳定）
- 发现新版 → 自动下载 exe → 校验 sha256 → 批处理延迟替换
- 替换在退出程序时执行（exe 运行中不能覆盖自己）
- 也可点页面「检查更新」按钮手动触发
- 更新源：`https://raw.giteeusercontent.com/kele551/hotnews/raw/master/version.json`
