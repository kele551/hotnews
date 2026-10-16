# TASKS —— 可认领的任务清单（热点新闻 hotnews）

> 这里的每一行都是**能独立做完、且有明确验收**的事。规则很简单：
> **一次一件事 → 先看「涉及文件」→ 做完贴出「验收命令」的输出 → 把状态改成 `done`**。
> 大改动先开 Issue 说方案；验收命令必须**真的跑过**。
> 关联的 issue 写在下表 `issue` 列（**GitHub / Gitee 两侧同步维护；AtomGit 只镜像代码，不收 issue**）（带 `good first issue` 标签的适合第一次上手）。

| id | 任务 | 难度 | 涉及文件 | 验收（跑什么） | issue | 状态 |
|---|---|---|---|---|---|---|
| H-01 | README 补上 `docs/CHANGELOG.md` 与 `docs/CODE_OF_CONDUCT.md` 的链接（现在这两份是孤儿） | ★ | `README.md` | `git grep -c "CHANGELOG.md" -- README.md` 与 `…CODE_OF_CONDUCT.md…` 都 ≥1；六份治理文档都能点到 | #2 | open |
| H-02 | 校对 `docs/CONTRIBUTING.md` 里的 `version.json` 字节数（写 852 B / 25 B，实测 1004 B / 24 B）与结构示例 | ★ | `docs/CONTRIBUTING.md` | 文档数字与实测一致（远端完整版 1004 B、本地精简版 24 B） | #3 | open |
| H-03 | `docs/COMPLIANCE.md` 编号错乱（两个 `7.1`、中文与阿拉伯序号混用）；README 说"尚未闭环"与第 7 节标题"已闭环"矛盾 | ★★ | `docs/COMPLIANCE.md`、`README.md` | 章节编号唯一且连续；两处对"唯一例外是否闭环"的说法一致 | #4 | open |
| H-04 | 主源码文件头缺署名（`app.py` / `server.py` / `updater.py` / `build.py` / `check_sources.py` / `make_icon.py` / `_ed25519.py` / `tools/patch_index.py`），`说明.txt` 首部也无署名 | ★ | 上述文件 | 每个 `.py` 文件头有 `# 作者: 海风（kele551）   https://gitee.com/kele551/hotnews`；`说明.txt` 首部有署名行 | #5 | open |
| H-05 | `说明.txt` 两处「国内原来那栏」指代不明（删栏目名后留下的残句），与 README 的栏目名口径统一 | ★ | `说明.txt` | 两处都写成明确栏目名（如「民生栏 / 国内体育栏」），与 README 一致 | #6 | open |
| H-06 | README 里「数据源（v1.5.5）」同一句出现两次（H2 与折叠标题），删一处 | ★ | `README.md` | 全文搜索该标题只剩一处 | #7 | open |
| H-07 | `_ico_png/` 9 个 PNG 是图标生成的中间产物（打包排除、发布不用），移出仓库或加一句说明 | ★★ | `_ico_png/`、`build.py`、README | 仓库里不再有无人引用的中间产物（或 README 说明其用途） | #8 | open |
| H-08 | `.gitignore` 里 `Harnesssecrets*` 看似笔误，改成带注释的说明或删除 | ★ | `.gitignore` | 每条忽略规则都能说清为什么在 | #9 | open |

## 验收时请顺手遵守的约定

- ⚠ 动升级链路前先读 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) 第 5.2 节：**本地 `version.json` 必须精简**、**禁止强推 Gitee `master`**；
- 行尾 **LF**（只有 `说明.txt` 保持 CRLF）；用户可见文案不写"参赛 / 评委 / 组委会"；
- 对外文案不许出现 绕过 / 翻墙 / 梯子 / 科学上网 / 墙 / 破解 / 屏蔽；
- 详细约定见 [AGENTS.md](AGENTS.md)（AI 助手）/ [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)（人类贡献者）。
