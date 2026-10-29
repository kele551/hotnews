# 项目结构与上手路线（热点新闻 hotnews）

> 给想改代码的人：**15 分钟看懂这个项目从哪进、改哪儿、改完怎么验。**
> 作者：海风（kele551） · 仓库：https://gitcode.com/kele551/hotnews

---

## 一、一句话架构

**一个本机网页应用**：`app.py` 选端口、起服务、开浏览器 → `server.py`（FastAPI，约 7000 行）
负责抓取、去重、国内/国际分流、翻译、图片代理与页面接口 → 页面在 `static/` 下 →
`updater.py` 负责在线升级（校验 sha256 + Ed25519 签名）。打包成单文件 `热点新闻.exe`。

```
热点新闻.exe (app.py)
   ├─ 已有实例在跑 → 直接开浏览器；否则选端口（8000 起）→ uvicorn 起 server:app → 开浏览器
   ├─ server.py：抓取来源 → 去重合并 → 国内/国际分流 → 翻译（带磁盘缓存）→ 图片代理 → api_news()
   ├─ static/index.html、static/intl.html：国内页 / 国际页（含右下角气泡提醒）
   └─ updater.py：读 GitCode version.json（兜底 GitHub）→ 校验 sha256 + Ed25519 签名 → 下载 → 退出时替换并重启
```

**合规底线**：只聚合**公开的标题与链接**，不存储、不转载正文，每条保留原文跳转；
不做新闻采编发布；用户数据不采集不上传；数据源默认只用境内权威媒体
（**新闻内容源**只有两家境外娱乐媒体这一个例外；另有一栏非新闻的「开源热榜」走 GitHub 官方接口，
两处都登记在 `docs/COMPLIANCE.md` 第 7 节（7.1 / 7.3），各有可一键关闭的总开关 `INTL_ENT_OVERSEAS` / `OSS_ENABLED`）。

---

## 二、目录与文件职责

| 文件 | 职责 | 改它的场景 |
|---|---|---|
| `app.py` | 入口：单实例、选端口、起服务、开浏览器、日志 | 改启动行为、端口策略 |
| `server.py` | **主体**：来源表、抓取、去重、国内/国际分流、翻译、图片代理、`api_news()` | 绝大多数功能改动 |
| `static/index.html`、`static/intl.html` | 前端页面与交互（栏目、搜索、气泡） | 界面改动 |
| `data/keywords.txt` | 关键词数据（国家 / 地区 / 机构 / 赛事…） | 只改数据、不动代码 |
| `updater.py` | 在线升级：拉 `version.json`、校验 sha256 + Ed25519、下载与替换 | 升级链路 |
| `_ed25519.py` | 验签实现（内置公钥） | 升级安全 |
| `version.json` | **升级源（本地必须精简版，只写 `version`）** | ⚠ 见下方红字 |
| `version.json.sig` | 远端完整版 `version.json` 的签名 | 发版链路 |
| `build.py`、`build_exe.spec` | 打包（`python build.py` / `--list` 核对清单） | 改打包清单 |
| `tools/preflight_release.py` | **发版前自检闸门**（版本一致性、口径断言、事故复现项等） | 发版前必跑；新增检查项 |
| `tools/patch_index.py` | 页面静态资源的小工具 | 前端构建辅助 |
| `docs/` | 治理文档（合规 / 安全 / 贡献 / 签名政策 / 行为准则 / 更新记录） | 文档类改动 |
| `说明.txt` | 随程序给用户看的说明书 | 面向用户的功能说明 |

**本机状态**：`%LOCALAPPDATA%\hotnews\`（`config.local.json` 覆盖项、`translate_cache.json` 翻译缓存），
运行日志 `hotnews.log` 在 exe 同级（超过 1MB 自动滚动）。

> ⚠ **两条最容易踩的坑**（改这条链路前务必先读 `docs/CONTRIBUTING.md` 5.2 节）：
> 1. `launcher.url/sha256/size` **只能在 exe 打包定型之后补写** —— `build_exe.spec` 会把
>    `version.json` 打进 exe，先写自己的哈希就掉进
>    「改 version.json → exe 变 → sha256 变 → 又要改 version.json」的自引用死循环；
> 2. **任何远端都不能强推**。线上 `main` 那份完整版 `version.json` + `.sig` 就是
>    **所有已装用户的升级数据**，冲掉等于当场断掉在线升级；发布顺序固定为
>    「提交 → 只推 tag → 建发行版传附件 → 回读验真 → 最后推 main → `tools/verify_release.py`」。

---

## 三、想改一个功能，该动哪里

| 我想… | 去哪 |
|---|---|
| 加一个新闻源 | `server.py` 顶部的来源表（每条含 id / 名称 / 地址 / 栏目 / 地区），加完要实测：能连通 + 带图 + 有当天内容 |
| 改「开源热榜」（GitHub 榜，非新闻栏） | `server.py` 的「开源热榜」一节（窗口 / 条数 / 题材闸门 / `OSS_ENABLED` 总开关）＋ `static/index.html` 里 `OSS_*` 那几处。它**不在** `SOURCES` 里，单独一条链路与一个 `/api/oss` 接口，改它不该碰新闻口径 |
| 改"国内 / 国际"怎么分 | `server.py` 的判定函数：`_has_cn_marker_title()`（标题里的中国标记）、`_is_domestic_for_intl()`（各栏目分支）、出口 `api_news()` 再兜一道；纯外国宏观财经由 `_is_foreign_macro()` 判 |
| 改关键词表 | `data/keywords.txt`（纯数据，不用改代码） |
| 改界面版式 / 配色 | `static/index.html`、`static/intl.html` |
| 改翻译后端 | `config.json` 里的 `backend`（mymemory 免费兜底 / baidu / deepl），实现见 `server.py` 翻译段 |
| 改图片代理白名单 | `server.py` 的 `/img` 段（**重定向落地后必须再过一次白名单**，这是修过的安全点） |
| 改升级逻辑 | `updater.py`（版本来源、校验、替换脚本）；替换脚本 `hotnews_update.vbs` 必须写成 **UTF-16**（写成 UTF-8 会让升级整个失效） |

---

## 四、本地跑起来 & 提交前要跑的

```powershell
# 1) 源码运行
pip install -r requirements.txt
python app.py                     # 浏览器自动打开（默认 8000 起找可用端口）

# 2) 语法检查
python -m py_compile server.py app.py updater.py

# 3) 打包清单核对（不该出现历史 exe / 调试快照 / PyInstaller 中间目录）
python build.py --list

# 4) 发版前自检（发布/替换发行版附件前**必须全绿**）
python tools/preflight_release.py
```

**改完请照着这几条自查**：行尾入库与检出一律 **LF**（`.gitattributes` 已固定），**只有 `说明.txt` 保持 CRLF**；
界面/文档里的话不要出现"绕过 / 翻墙 / 梯子 / 科学上网 / 墙 / 破解 / 屏蔽"；
用户可见文案不要出现"参赛 / 评委 / 组委会"这类内部字样；
改了分流口径，请顺手在 `tools/preflight_release.py` 的真机语料断言里补一条（红了就要看得懂为什么）。

---

## 五、贡献怎么提

1. 先开 Issue 说清现象（**带上栏目名与原始链接**最好，方便复现）；
2. 加分源这类改动，Issue/PR 里请附上**实测三连**：能连通、带图（短边 ≥300）、有当天或近日内容；
3. 小步提交、一次一件事，提交信息用中文写清"改了什么、为什么"；
4. 详细约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。
