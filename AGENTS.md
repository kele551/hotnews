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

1. **`version.json` 的 `launcher` 段只能在 exe 定型之后写**。`build_exe.spec` 会把这份文件
   打进 exe，而打包只读它的 `version` 字段；如果打包前先写 `launcher.sha256`，那个值就是
   "本 exe 的哈希"，一改文件 exe 又变 —— 自引用死循环。所以发版顺序是「打包 → 算真实
   sha256/size → 补写完整版 `version.json` → 重签 → 提交」，不是「先写好清单再打包」。
2. **绝对不要强推（`git push -f`）任何远端任何分支**。线上 `main` 的完整版 `version.json`
   与它的 `.sig` 就是**所有已装用户的升级数据**，冲掉 = 在线升级当场失效（历史教训来自
   老仓库 `master`：它比本地多两个提交，正是这两份文件）。发布顺序见 CONTRIBUTING 5.2。
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
二进制发行包发 **GitCode 与 GitHub** 两处；AtomGit 会在几分钟内把 GitHub 的发行版**连同附件**
一起镜像过去（2026-10-26 实测；早期"AtomGit 没有附件上传接口、只能发源码"的说法已过期）。

```powershell
# 远端名字按本机 config 为准：gitcode=主源、github=镜像、atomgit=代码镜像
# 注意：老仓库（gitee）2026-10-17 起停更，别再往它推，也别在对外文案里写它的地址。
git push gitcode main --tags                    # GitCode（主源，升级源就从这里读）
git push github main --tags                     # GitHub（镜像；git 端口不通时借狐径的本地代理重试）
git push atomgit main --tags                    # AtomGit（令牌走 GIT_ASKPASS，不会出现在命令行）
```

三个平台改完都要推，**内容必须逐字节一致**（判定方法：`git diff github/main gitcode/main` 应为空）。

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
  - **don't hand-edit `version.json` before building** — `launcher.url/sha256/size` are written
    *after* the exe is final (the exe bundles this file, so writing its own hash first is a loop);
  - **never force-push** (`git push -f`) any remote/branch: the full signed `version.json` (+`.sig`)
    on `main` is the update data every installed client reads;
  - write `hotnews_update.vbs` as **UTF-16** (UTF-8 with BOM makes Windows Script Host fail and breaks updates);
  - change the domestic/international rules casually — the release gate asserts them with a real corpus;
  - rewrite line endings wholesale (this repo stores **LF**, except `说明.txt` which stays CRLF);
  - introduce local machine paths, private emails or tokens into the repo;
  - banned wording anywhere: 绕过 / 翻墙 / 梯子 / 科学上网 / 墙 / 破解 / 屏蔽.
- **Commits**: one change per commit, Chinese conventional prefixes (`fix:` / `feat:` / `docs:` / `chore:`),
  and state how you verified it (which command, which column).
- **Backlog**: [TASKS.md](TASKS.md) and issues labelled `good first issue`.
