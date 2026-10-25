# AGENTS.md —— 给 AI 编程助手的协作说明（热点新闻 hotnews）

> 这份文件面向用 AI 助手（Claude Code / Codex / Cursor / DSH 等）改这个仓库的人。
> **动手前先读 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**（结构、改哪儿、怎么验）；
> ⚠ 动**升级链路**（`version.json` / `updater.py`）之前，先读 [CONTRIBUTING.md](docs/CONTRIBUTING.md) 第 5.2 节的两条红线。

## 这是什么

本机网页应用：`app.py` 选端口起服务 → `server.py`（FastAPI，约 7000 行）抓取、去重、国内/国际分流、翻译、图片代理 →
页面在 `static/`；`updater.py` 负责在线升级（`version.json` + **Ed25519 签名**校验）。

**合规底线**：只聚合**公开的标题与链接**，不存储、不转载正文，每条保留原文跳转；
用户数据不采集不上传；数据源默认只用境内权威媒体（有两家境外娱乐媒体是**唯一例外**，见 `docs/COMPLIANCE.md` 第 7 节，且有一键关闭的总开关）。

## 环境与命令

```powershell
pip install -r requirements.txt
python app.py                        # 跑起来（浏览器自动打开，默认 8000 起找端口）
python -m py_compile server.py app.py updater.py
python build.py --list               # 打包清单核对（不该出现历史 exe / 调试快照 / PyInstaller 中间目录）
python build.py                      # 打包
python tools/preflight_release.py    # 发版前自检（发布前必须全绿；含真机语料断言）
```

## 禁区（先看这里，再改）

1. **本地 `version.json` 必须保持精简版（只写 `version`）**。写成完整版会掉进
   「改 version.json → exe 变 → sha256 变 → 又要改 version.json」的自引用死循环。
2. **绝对不要强推（`git push -f`）Gitee 的 `master`**。远端 `master` 比本地多两个提交
   （完整版 `version.json` + 它的签名），那是**所有已装用户的升级数据**，冲掉 = 在线升级当场失效。
   往 Gitee 推改动一律用 CONTRIBUTING 5.2 的**做法二**（以远端为基开临时分支、只重放本次改动、快进推送）。
3. **替换脚本 `hotnews_update.vbs` 必须写成 UTF-16**（写成 UTF-8 带 BOM 会让 Windows Script Host 报错，升级整个失效）。
4. **分流口径不要随手改**：国际栏**不收主体是中国**的稿件；纯外国宏观财经从国内财经挪到国际财经。
   改了判据请在 `tools/preflight_release.py` 的真机语料断言里补一条（红了必须能解释为什么）。
5. **对外文案红线词**：绕过 / 翻墙 / 梯子 / 科学上网 / 墙 / 破解 / 屏蔽 —— 一个都不要出现。
6. **用户可见文案不要出现"参赛 / 评委 / 组委会"**这类内部字样。
7. **行尾**：入库/检出一律 **LF**（`.gitattributes` 已固定），**只有 `说明.txt` 保持 CRLF**；不要整文件重写行尾。
8. **署名与产品名不要改**：`海风（kele551）`；产品名「热点新闻 hotnews」。
9. **仓库里不许出现本机路径、私人邮箱、令牌**（`tools/preflight_release.py` 的 ⑥ 节会拦，且已把 `tools/*.py` 一起纳入扫描）。

## 提交与验收

- 提交信息：中文，`类型: 说明`（`fix:` / `feat:` / `docs:` / `chore:`），**一次一件事**；
- **版本号只在发版时改**；
- 加分/换源这类改动，请在 PR / 回复里附**实测三连**：能连通、带图（短边 ≥300）、有当天或近日内容；
- 界面/口径类改动，说明你**怎么验证的**（跑了哪条命令、看了哪个栏目）。

## 平台（三个，代码一致）

代码同时镜像到三处：**GitCode（主源）** / **GitHub（镜像）** / **AtomGit（代码镜像）**。
**二进制发行包只发 Gitee 与 GitHub** —— AtomGit 目前没有附件上传接口（它的 Release 只提供源码归档）。

```powershell
git push atomgit main --tags                    # AtomGit（令牌走 GIT_ASKPASS，不会出现在命令行）
# GitHub：走工作区的纯 REST 工具（origin 虽然是 GitHub，但这条线路走 REST 更稳）
# Gitee：**不能直接 push main**，必须用 CONTRIBUTING 5.2 的「做法二」（远端 master 带签名数据）
```

三个平台改完都要推，**内容必须与 Gitee `master` 一致**（判定方法：`git diff atomgit/main gitee/master` 应为空）。

## 任务从哪来

GitHub Issues 里带 `good first issue` / `help wanted` 标签的条目，每条都写了**验收标准**。
仓库里还有一份机器可读的任务清单 [TASKS.md](TASKS.md)。

---

## English summary (for AI agents)

- **What it is**: a local web app — `app.py` picks a port and starts `server.py` (FastAPI, ~7000 lines) which
  fetches, de-duplicates, splits domestic/international, translates and proxies images; the UI lives in
  `static/`; `updater.py` handles signed online updates.
- **Run / verify**: `python app.py` · `python -m py_compile server.py app.py updater.py` ·
  `python build.py --list` · `python tools/preflight_release.py` (the release gate — must be all green).
- **Never**:
  - **don't hand-edit `version.json`** — the local copy must stay minimal (`{"version": "..."}`);
    the remote copy is the full, signed update source;
  - **never force-push** (`git push -f`) the Gitee `master` branch: it carries the signed `version.json`
    commits that every installed client reads. Use the temp-branch replay in CONTRIBUTING §5.2;
  - write `hotnews_update.vbs` as **UTF-16** (UTF-8 with BOM makes Windows Script Host fail and breaks updates);
  - change the domestic/international rules casually — the release gate asserts them with a real corpus;
  - rewrite line endings wholesale (this repo stores **LF**, except `说明.txt` which stays CRLF);
  - introduce local machine paths, private emails or tokens into the repo;
  - banned wording anywhere: 绕过 / 翻墙 / 梯子 / 科学上网 / 墙 / 破解 / 屏蔽.
- **Commits**: one change per commit, Chinese conventional prefixes (`fix:` / `feat:` / `docs:` / `chore:`),
  and state how you verified it (which command, which column).
- **Backlog**: [TASKS.md](TASKS.md) and issues labelled `good first issue`.
