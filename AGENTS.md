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

## 任务从哪来

GitHub Issues 里带 `good first issue` / `help wanted` 标签的条目，每条都写了**验收标准**。
