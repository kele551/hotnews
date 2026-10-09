# 参与贡献（CONTRIBUTING）

作者：**海风（kele551）** · 仓库：https://gitee.com/kele551/hotnews （GitHub 同名镜像）

hotnews（热点新闻检索）是一个人维护的小项目：Windows 单文件 exe + 本机 FastAPI 后端
+ 原生前端（HTML / CSS / JS，无框架、无打包器）。欢迎反馈与 PR，
但请先花两分钟看完这个文件 —— 里面每一条基本都是踩过坑之后写下来的。

---

## 一、提 Issue

- **Bug**：用 [Bug 反馈](.github/ISSUE_TEMPLATE/bug_report.yml) 模板。
  版本号、Windows 版本、复现步骤是必填；栏目空白类问题请附日志片段或 `check_sources.py` 的输出。
- **功能建议**：用 [功能建议](.github/ISSUE_TEMPLATE/feature_request.yml) 模板。
  先说清「想解决什么问题」，再说方案。
- **安全漏洞**：**不要开公开 Issue**，按 [SECURITY.md](SECURITY.md) 的私下渠道报告。
- 提交前请顺手做两件事：
  1. 在 README 的「实测失败、**没有采用**的源」清单里找一下 —— 你遇到的「某个源没内容」，
     很可能那个源本来就没被采用；
  2. 贴日志前把私人信息（真实邮箱、本机用户名、内网地址）删掉。

**响应预期**：一般 **3 个工作日内**给首次回应。这个项目不为单个 bug 单独发版，
修复会攒到下一个真功能版本一起发（原因见下面「五、版本与发布」）。

---

## 二、提 PR

1. **先开 Issue 打个招呼**（改数据源、改抓取逻辑、改打包配置这三类尤其必要）——
   避免写完才发现方向不对。
2. Fork，从 `master` 切一个描述性的分支，例如 `fix/cn-soft-empty`、`feat/balloon-hover`。
3. 改完之后**在本地真跑一遍**（见第三节），按要求填完 PR 模板。
4. 一个 PR 只做一件事：不要「顺手把三个 bug 一起修了并重排了缩进」。
5. 不要提交构建产物：`dist/`、`build/`、`runtime/`、`*.exe`、`*.zip`、`*.log` 都在 `.gitignore` 里，
   请保持这样。附件请用 Issue 传，别进仓库。

> `master` 分支只由维护者合并。维护者推 Gitee 时会同步 GitHub 镜像。

---

## 三、本地构建与自查

### 3.1 跑起来

```bash
pip install -r requirements.txt      # fastapi / uvicorn / feedparser / httpx
python app.py                        # 自动选端口（8000 起）并打开浏览器
# 等价写法：python -m uvicorn server:app --port 8000
```

### 3.2 提交前必须跑的自查

```bash
# 1) 语法检查（改了哪个文件都要能过）
python -m py_compile app.py server.py updater.py _ed25519.py build.py check_sources.py make_icon.py tools/patch_index.py

# 2) 数据源自检（改了 SOURCES / 解析逻辑 / 时效过滤时必跑）
python check_sources.py
#    会打印每个 Region 的：条数、最新/最旧时间、带图数、分类分布、前 8 条
```

### 3.3 打包（改了 `build_exe.spec` / 图标 / `static/` 时）

```bash
python -m PyInstaller build_exe.spec --clean --noconfirm    # 产出 dist/热点新闻.exe
python build.py                                            # 产出源码包 + 绿色版 zip
python make_icon.py                                        # 重新生成图标（改了图标才需要）
```

四个必须知道的坑：

1. **`version.json` 不能带 BOM**。用 PowerShell 的 `Set-Content -Encoding UTF8` 写会带 BOM；
   `build_exe.spec` 已改成按 `utf-8-sig` 读来容忍，但**线上升级源必须是干净的无 BOM 版本**。
   手改请用：
   `[System.IO.File]::WriteAllText($p, $txt, (New-Object System.Text.UTF8Encoding($false)))`
2. **`python build.py` 必须在干净检出上跑**。它只排除 `.git` / `__pycache__` 等目录，
   **不排除 `dist/` 与 `build/`**；先跑过 PyInstaller 再跑 `build.py`，
   打包产物会被一起卷进源码包。
3. **绿色版 zip 依赖仓库外的 `runtime/`**（自带 embeddable Python，已在 `.gitignore` 里）。
   只有本机有 `runtime/` 的维护者能打出完整绿色包；CI 里打出来的绿色包是残包，
   所以 `.github/workflows/build.yml` 会把它删掉、不上传。
4. **`_ed25519` 是函数内 import 的**，PyInstaller 静态分析扫不到，必须留在
   `build_exe.spec` 的 `hiddenimports` 里；同理 `feedparser` 必须保持模块级 import，
   别改成懒加载 —— 否则打包后验签会失败（fail-closed 会直接拒绝升级）。

### 3.4 CI 帮你做了什么

`.github/workflows/build.yml` 在 **Windows runner** 上装 `requirements.txt` + PyInstaller，
跑上面的语法检查与 `build_exe.spec`，校验 exe 文件属性与 `version.json` 版本号一致、
署名正确、`version.json` 无 BOM，然后打印每个产物的 SHA256 并上传 artifact。
PR 上这条 CI 红了，请先修到绿。

---

## 四、代码风格

### 4.1 Python

- 文件头保留 `# -*- coding: utf-8 -*-`；新增文件按署名规则加一行：
  `# 作者: 海风（kele551）   https://gitee.com/kele551/hotnews`
- 缩进 4 空格；模块级常量用大写 + 下划线，且**紧跟一行中文注释说明为什么取这个值**
  （例如 `MIN_IMG_SHORT_SIDE = 300` 后面要写清「为什么不是 200、为什么不是 400」）——
  这个仓库里最值钱的东西就是这些「为什么」，改数值前先读注释。
- 注释与日志用中文；日志走 `print("[ok] ...")` / `print("[warn] ...")` 这套既有格式，
  别引第三方日志库（打包体积和依赖都划不来）。
- **不要为了「优雅」引入新依赖**。核心依赖就 `fastapi / uvicorn / feedparser / httpx` 四个；
  加密相关的（Ed25519）是仓库内零依赖的 `_ed25519.py`，刻意不引 `cryptography`。
- 网络请求统一走既有的 `_fetch()` 与共享连接池（`_CLIENT`），
  不要自己 `with httpx.Client(...)` 新建连接 —— 那等于每个请求重做一次 TCP + TLS 握手。
- 抓取失败要能降级：单个源失败不能影响整块栏目，且日志要留一行能被搜到的原因。

### 4.2 前端（`static/`）

- 原生 HTML / CSS / JS，**不引入框架、不引入构建步骤**；明快浅色、大字号。
- `static/index.html` 很大，改它请用「只插入、不裁剪」的做法。
  仓库里的 `tools/patch_index.py` 就是这么写的 —— 它的存在是因为曾经用 PowerShell
  的 `Substring` 插 CSS，把第一处 `</style>` 之后的内容全截掉了，用户看到的是白页。
- 改动后请手动确认：页面能打开、搜索能搜、切国内/国际正常、右下角气泡能弹。

### 4.3 文档与对外文案

- 中文文档一律 UTF-8（无 BOM）；`README.md`、`说明.txt` 里的表格与清单要跟着代码一起改，
  **别让文档停留在旧版本**（`说明.txt` 的第一行与「数据源」一节的版本号必须与 `version.json` 一致）。
- **行尾**：仓库 `core.autocrlf=true`，多数文件是 LF；`说明.txt` 是 CRLF。
  **不要整文件重写行尾** —— 会产生「整个文件都变了」的假 diff，把真正的改动埋掉。
- **用词红线（重要）**：README、`说明.txt`、Release 说明、仓库简介都会被平台做内容审查。
  **凡是「对抗平台限制 / 突破访问限制 / 去除限制」这一类含义的措辞，一律不要写。**
  本账号下另一个仓库曾经因为 README 措辞被判内容违规，raw 地址直接返回 **HTTP 451**，
  用户在浏览器里点开只看到违规提示。改成中性技术表述，例如：
  - 说「不依赖域名解析」，而不是暗示改 hosts 那套说法；
  - 说「本地网络分流 / 连接优化」，而不是暗示突破访问限制的说法；
  - 说「按源站要求带请求头获取」，而不是暗示去除站方限制的说法；
  - 说「时效过滤被整段跳过」，而不是暗示绕开规则的说法。

  **功能与行为该怎么做还怎么做，只是文案别给自己找麻烦。**
  改完对外文案，提交前自己再过一遍这几类说法。
- **不要写私人邮箱**：会被爬虫抓去发垃圾邮件。对外联系一律用仓库 Issues 或 GitHub
  的私密安全公告。
- **署名**：中文场景统一「海风（kele551）」；ASCII 场景（exe 文件属性、英文文档）
  用 `HaiFeng (kele551)`，版权行 `Copyright (C) 2026 HaiFeng (kele551)`。
  仓库地址写 `gitee.com/kele551/hotnews`。**文件名不带版本号**（就叫 `热点新闻.exe`），
  版本号只写进 `version.json` 与 exe 文件属性。

### 4.4 数据源（增删的硬门槛）

新增一个源必须**先实测**，并在 Issue / PR 里贴出结果，四项缺一不可：

| 项 | 门槛 |
|---|---|
| 连通 | 真的能抓到（不是「应该能」） |
| 带图 | 短边 ≥ `MIN_IMG_SHORT_SIDE`（现为 **300**）；无图的条目不显示，源也可能因此整块消失 |
| 时效 | 有当天或近日内容；停更的源一律不要（曾有一批源停在 2025-06 还返回 200，是「新闻太旧」的根因） |
| 通道 | 公开的 RSS / JSON 接口 / 首页；不使用登录态、不读付费内容；站方明确表示不希望被访问的不加 |

删源、换源同样要在 PR 里写清**为什么**，并把失败原因补进 README 的
「实测失败、**没有采用**的源」表 —— 那张表的作用就是让后面的人别再重复试一遍。

---

## 五、版本与发布

- **版本号只给「新功能」用**。修 bug 不单独发版：修复先做在本地攒着，
  跟下一个真功能版本一起发。维护者明确反对「几天一版、一天几版」。
- **没测过的东西不交付**。任何发布之前要跑该项目的发版前自检并全绿
  （含冷启动、完整升级演练、失败分支、注册入口还原、版本一致性）；
  红一条不发。线上每发现一个新问题，就把那一幕补成一个检查项。
- **发布流程**（维护者执行）：由维护者工作区里的 `tools/publish.py <版本号>` 完成
  —— 改版本号 → 打包 → 生成升级源 `version.json`（并用离线私钥做 Ed25519 签名）→
  推 Gitee → 建 Release 传附件 → 同步 GitHub → 两端回下载核对 SHA256。
  发布脚本与签名私钥**不在本仓库内**，也不应该被复制进来。
- 发布相关的产物来源、签名计划与 SHA256 核验方法，见
  [CODE_SIGNING_POLICY.md](CODE_SIGNING_POLICY.md)。

---

## 六、提交信息规范

采用 Conventional Commits 风格，**描述用中文**，一次提交只做一件事。

```
<类型>(<范围>): <一句话说清改了什么>
```

| 类型 | 用在 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `chore` | 杂项（构建脚本、依赖、治理文件） |
| `refactor` | 不改变行为的重构 |

范围可以省略，也可以写模块（`源` / `气泡` / `升级` / `界面` / `打包`）。

真实例子（见 `git log`）：

```
fix(待发): 修凤凰娱乐 no_ts 未定义致娱乐栏目丢失 + 升级闸门改 fail-closed + /api 副作用接口加本机令牌与同源校验
docs: 对外文案措辞中性化，避免平台内容审查误判
```

要求：

- 第一行说清「改了什么、为什么」，别只写「更新」「优化」「fix bug」；
- 正文里写清**怎么验证的**（跑了什么命令、看到什么结果）；
- 不要把多个不相关的改动塞进一笔提交；也不要 amend 已经推上去的提交。

---

## 七、许可与署名

- 本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。提交贡献即表示你同意以同一许可分发。
- 新增源文件请带上作者署名行（中文场景「海风（kele551）」）；文档改动不需要另加署名。
- 参与本项目即表示你同意遵守上面的用词与隐私约定（不写敏感措辞、不写私人邮箱）。
