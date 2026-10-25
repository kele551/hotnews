# -*- coding: utf-8 -*-
# 作者: 海风（kele551）   https://gitcode.com/kele551/hotnews
"""热点新闻 —— 发版前自检闸门（必须全绿才允许发布）

为什么这个文件在仓库里：
    两个姊妹项目（狐径 FoxPath、桌面壁纸）都已经把发版前自检**随仓库提供**
    （各自 `tools/preflight_release.py`），任何人克隆下来都能自己跑一遍。
    热点新闻此前只有维护者工作区里那一份，仓库里没有 —— 外人克隆后无门可跑。
    这个文件就是把那道闸门补进仓库：**克隆 → `python tools/preflight_release.py`
    → 逐项 [PASS]/[FAIL]/[SKIP] → 给出退出码**，不需要任何本机绝对路径。

这个脚本的分工（重要）：
    · 只用**标准库**就能查的（语法、版本一致性、治理文件、数据源栏目登记、对外文档体检）
      一律自己查，不依赖第三方包，也不依赖维护者本机；
    · 需要第三方依赖（fastapi / httpx / feedparser）或需要联网的检查，
      条件不满足时打印 [SKIP] 并说明原因 —— **跳过不算失败**；
    · 远端平台、网络这类我们控制不了的事只算 [WARN]，不阻断发布。

用法：
  python tools/preflight_release.py [--offline] [--no-links] [-h]

  --offline   完全不联网（跳过「链接与徽章」「线上升级源对齐」两节）
  --no-links  只跳过「链接与徽章」一节
  --e2e       额外**真联网抓一遍**（端到端：条数与栏目覆盖）—— 慢（1~3 分钟）但最能查出
              "某个源退化 / 整栏消失"这类回归；默认不跑
  -h, --help  只打印这段说明就退出（不联网、不写任何文件）

会动到系统的地方：没有。这个脚本只读文件/接口，不写仓库、不改系统设置、不结束任何进程。

判据：红项（FAIL）一条都不许有 —— 有一条就不许发布；
      跳过（SKIP）与警告（WARN）不阻断发布，但会在结尾单独列出来，发布前应当看一眼。

退出码：全绿（允许 SKIP/WARN）= 0；有 [FAIL] = 1；开关写错 = 2。
"""
import ast
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 控制台编码可能是 GBK（Windows 中文默认）：结论行的 ✅/❌ 会让 print 抛
# UnicodeEncodeError，于是"检查明明全绿"却以 traceback + 退出码 1 收场。
# 两个姊妹项目的自检脚本都是这么处理的（狐径 tools/preflight_release.py）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 不给仓库留垃圾：下面第 ⑤ 节会 import server，默认会写 __pycache__ —— 关掉它。
sys.dont_write_bytecode = True

# 仓库根：本脚本既可能躺在**仓库**里的 <仓库>\tools\（随仓库入库的那份，源码就在上一级），
# 也可能躺在**维护者工作区**的 <项目>\tools\（源码在旁边 <项目>\repo）。
# 两种位置都自己认出来 —— 写法与狐径 tools/preflight_release.py:53-55 一致。
_HERE = os.path.dirname(os.path.abspath(__file__))
_UP = os.path.dirname(_HERE)
REPO = _UP if os.path.exists(os.path.join(_UP, 'server.py')) else os.path.join(_UP, 'repo')

# 【栏目定稿】2026-10-09 用户逐条确认的最终栏目；任何一处对不上都算红。
CN_FINAL = ('头条', '科技', '财经', '娱乐', '软件', '热榜')
INTL_FINAL = ('要闻', '科技', '财经', '娱乐', '体育', '热榜')
# 已经**整栏下线**的栏目名（国内不许再登记，国际栏的「要闻 / 体育」是正式栏目、不在其中）
DOMESTIC_GONE = ('社会', '要闻', '体育')

GITCODE_REPO = 'https://gitcode.com/kele551/hotnews'
GITEE_REPO = GITCODE_REPO      # 历史变量名（部分小节仍按这个名字引用；主源已迁 GitCode）
GITHUB_REPO = 'https://github.com/kele551/hotnews'
UA = 'HotnewsPreflight/1.0 (+%s)' % GITCODE_REPO

USAGE = """热点新闻发版前自检 —— 用法

  python tools/preflight_release.py [开关]

开关:
  -h, --help   只打印这段说明就退出（不联网、不写任何文件）
      --offline   完全不联网：跳过「链接与徽章」「线上升级源对齐」两节
      --no-links  只跳过「链接与徽章」一节（徽章/链接可访问性）

看什么（逐项打印 [PASS] / [FAIL] / [SKIP]）:
  ① 仓库位置与治理文件（LICENSE / README / CONTRIBUTING / COMPLIANCE / SECURITY /
     CODE_OF_CONDUCT / CODE_SIGNING_POLICY / CHANGELOG / requirements.txt / CI 与 Issue 模板）
  ② 语法：全部源码都能 compile（用标准库，不落 __pycache__）
  ③ 版本一致性：version.json 是唯一来源，README / 说明.txt / CHANGELOG 的版本字样必须跟上
  ④ 数据源栏目登记（静态读 server.py 的 SOURCES，不需要第三方依赖）
  ⑤ 关键回归断言（需要 fastapi/httpx/feedparser；缺依赖则 SKIP）
  ⑥ 对外文档体检（私人邮箱 0 命中、署名与仓库地址、依赖清单）
  ⑦ 链接与徽章可访问性（需要联网，--no-links/--offline 可跳过）
  ⑧ 线上升级源版本号对齐（只算 WARN）
  ⑨ 端到端抓取（默认不跑；加 --e2e 真联网抓一遍，查"整栏消失"这类回归）

判据: 红项（FAIL）一条都不许有 —— 有一条就不许发布；
      跳过（SKIP）与警告（WARN）不阻断发布，但结尾会单独列出来，发布前看一眼。
退出码: 全绿 = 0；有 FAIL = 1；开关写错 = 2。
"""

results = []      # [(名称, 是否通过, 说明)]  —— 红项进这里
skips = []        # [(名称, 为什么跳过)]      —— 跳过不算失败，但必须说清原因
warnings = []     # [(名称, 是否通过, 说明)]  —— 远端/网络这类只警告
_T0 = time.time()


def say(s=''):
    print(s)
    sys.stdout.flush()


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    say('  [%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  ' + str(detail)) if detail else ''))
    return bool(ok)


def skip(name, why):
    """条件不满足（缺依赖 / 缺成品 / 网络不可用）时用：打印 [SKIP]，**不计入失败**。"""
    skips.append((name, why))
    say('  [SKIP] %s  （%s）' % (name, why))
    return True


def check_warn(name, ok, detail=''):
    """警告级检查：不计入红项、不阻断发布。给"远端平台/网络"这类控制不了的事用。"""
    warnings.append((name, bool(ok), detail))
    say('  [%s] %s%s' % ('PASS' if ok else 'WARN', name, ('  ' + str(detail)) if detail else ''))
    return bool(ok)


def finish():
    """统一出口：打印汇总（红项 + 跳过 + 警告）并给出结论与退出码。"""
    say()
    say('=' * 74)
    bad = [n for n, ok, _ in results if not ok]
    warn = [(n, d) for n, ok, d in warnings if not ok]
    say('自检结果: %d 项，通过 %d，失败 %d；跳过 %d；警告 %d（耗时 %.0f 秒）'
        % (len(results), len(results) - len(bad), len(bad), len(skips), len(warn),
           time.time() - _T0))
    for n in bad:
        say('   FAIL -> %s' % n)
    for n, why in skips:
        say('   SKIP -> %s（%s）' % (n, why))
    for n, d in warn:
        say('   WARN -> %s%s' % (n, ('  ' + str(d)) if d else ''))
    if skips:
        say('   （跳过项不算失败：多半是本机没装依赖、没有成品，或没联网 —— 请在能做到的环境里再跑一遍）')
    if warn:
        say('   （警告项不阻断发布：通常是"线上还没传/还没发布"，发布前看一眼即可）')
    if bad:
        say('结论: ❌ 有红项，禁止发布')
    else:
        say('结论: ✅ 全绿，可以发布')
    sys.exit(1 if bad else 0)


def read(path, enc='utf-8-sig'):
    """读文本，宽容一点：读不到就返回 None（调用处报 FAIL/SKIP，别抛异常把自检崩掉）。"""
    try:
        return io.open(path, encoding=enc, errors='replace', newline='').read()
    except OSError:
        return None


def ver_tuple(v):
    """版本号按**元组**比，不按字符串比 —— '1.10' < '1.9' 的字符串比较是错的。"""
    try:
        return tuple(int(x) for x in str(v).strip().lstrip('v').split('.'))
    except Exception:
        return (0,)


# ══════════════════════════════════════════════════════════════════════════
# 命令行开关（写法与狐径 tools/preflight_release.py:441-456 一致）
# ══════════════════════════════════════════════════════════════════════════
KNOWN_ARGS = ('-h', '--help', '--offline', '--no-links', '--e2e')
ARGS = list(sys.argv[1:])
if '-h' in ARGS or '--help' in ARGS:
    say(USAGE)
    say('   （本次运行脚本所在仓库: %s）' % REPO)
    sys.exit(0)
_unknown = [a for a in ARGS if a.startswith('-') and a not in KNOWN_ARGS]
if _unknown:
    say('不认识的开关: %s' % ' '.join(_unknown))
    say()
    say(USAGE)
    sys.exit(2)
OFFLINE = '--offline' in ARGS
NO_LINKS = OFFLINE or '--no-links' in ARGS
E2E = '--e2e' in ARGS

say('=' * 74)
say('热点新闻 发版前自检%s' % ('（--offline：不联网）' if OFFLINE else ''))
say('=' * 74)
say('  仓库: %s' % REPO)
say()

# ══════════════════════════════════════════════════════════════════════════
# ① 仓库位置与治理文件（自包含：只看文件在不在）
# ══════════════════════════════════════════════════════════════════════════
say('① 仓库位置与治理文件（克隆下来就该有；这一节就是仓库治理清单）')
if not check('在仓库里运行（上一级找得到 server.py）',
             os.path.exists(os.path.join(REPO, 'server.py')),
             '解析出的仓库 = %s（脚本应放在 <仓库>/tools/ 下）' % REPO):
    finish()

GOVERNANCE = (
    ('LICENSE', '许可'),
    ('README.md', '门面与使用说明'),
    ('docs/CONTRIBUTING.md', '贡献指南'),
    ('docs/COMPLIANCE.md', '合规声明'),
    ('docs/SECURITY.md', '安全报告渠道'),
    ('docs/CODE_OF_CONDUCT.md', '行为准则'),
    ('docs/CODE_SIGNING_POLICY.md', '签名与校验说明'),
    ('docs/CHANGELOG.md', '更新记录'),
    ('requirements.txt', '依赖清单'),
    ('.github/PULL_REQUEST_TEMPLATE.md', 'PR 模板'),
    ('.github/ISSUE_TEMPLATE/bug_report.yml', 'Bug Issue 模板'),
    ('.github/ISSUE_TEMPLATE/feature_request.yml', '功能 Issue 模板'),
    ('.github/ISSUE_TEMPLATE/config.yml', 'Issue 模板配置'),
    ('.github/workflows/build.yml', 'CI 构建'),
    ('docs/demo/index.html', '演示页'),
    ('tools/preflight_release.py', '本自检脚本（随仓库提供）'),
)
_missing = [p for p, _ in GOVERNANCE if not os.path.exists(os.path.join(REPO, p))]
check('治理与协作文件齐全（%d 项）' % len(GOVERNANCE), not _missing,
      ('缺: ' + '、'.join(_missing)) if _missing else 'LICENSE / CONTRIBUTING / COMPLIANCE / SECURITY / CI 模板都在')

SOURCE_FILES = ('server.py', 'app.py', 'updater.py', '_ed25519.py', 'build.py',
                'check_sources.py', 'make_icon.py', 'build_exe.spec',
                'static/index.html', 'static/intl.html', 'data/keywords.txt',
                'version.json', 'config.json', 'tools/patch_index.py')
_missing2 = [p for p in SOURCE_FILES if not os.path.exists(os.path.join(REPO, p))]
check('源码与资源文件齐全（%d 项）' % len(SOURCE_FILES), not _missing2,
      ('缺: ' + '、'.join(_missing2)) if _missing2 else '')

# ══════════════════════════════════════════════════════════════════════════
# ② 语法：全部源码 compile 一遍（标准库就够，不需要装依赖）
# ══════════════════════════════════════════════════════════════════════════
say()
say('② 语法：全部源码都能 compile（标准库；不落 __pycache__，不给仓库留垃圾）')
_py_files = []
for _root, _dirs, _fs in os.walk(REPO):
    _dirs[:] = [d for d in _dirs if d not in {'.git', '__pycache__', 'runtime', 'dist', 'build', 'build_tmp', 'docs'}]
    for _f in sorted(_fs):
        if _f.endswith('.py'):
            _py_files.append(os.path.join(_root, _f))
_syntax_bad = []
for _p in _py_files:
    _src = read(_p, enc='utf-8-sig')
    if _src is None:
        _syntax_bad.append('%s（读不出来）' % os.path.relpath(_p, REPO))
        continue
    try:
        compile(_src, _p, 'exec')
    except SyntaxError as _e:
        _syntax_bad.append('%s:%s %s' % (os.path.relpath(_p, REPO), _e.lineno, _e.msg))
check('全部 .py 语法通过（%d 个文件）' % len(_py_files), not _syntax_bad,
      ('；'.join(_syntax_bad[:3])) if _syntax_bad else '')

# ══════════════════════════════════════════════════════════════════════════
# ③ 版本一致性（version.json 是唯一来源）
# ══════════════════════════════════════════════════════════════════════════
say()
say('③ 版本一致性：version.json ↔ README ↔ 说明.txt ↔ CHANGELOG')
_vjp = os.path.join(REPO, 'version.json')
_vraw = None
try:
    _vraw = io.open(_vjp, 'rb').read()
except OSError as _e:
    check('version.json 存在且可读', False, str(_e))
try:
    vj = json.loads(_vraw.decode('utf-8'))
except Exception as _e:
    vj = {}
    check('version.json 是合法 JSON', False, type(_e).__name__)
else:
    check('version.json 是合法 JSON', True)

# 【坑】version.json 不能带 BOM：用 PowerShell 的 Set-Content -Encoding UTF8 写会带 BOM，
# 线上升级源必须干净（见 CONTRIBUTING「打包」一节第 1 条坑）。
check('version.json 不带 BOM', bool(_vraw) and not _vraw.startswith(b'\xef\xbb\xbf'),
      '首三字节=%s' % (_vraw[:3].hex() if _vraw else '读不到'))

VER = str(vj.get('version') or '').strip().lstrip('v')
check('version.json 里 version 非空且形如 X.Y.Z',
      bool(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', VER)), 'version=%r' % vj.get('version'))
if not VER:
    finish()

# README：动态徽章（shields.io 现读线上 release，永远不过期）优先，没有才回落到静态徽章。
# 【2026-10-15 教训】README 门面改版把静态徽章换成了动态徽章，规则还去找静态的 → 自检假红。
_DYN_BADGE = re.compile(r'img\.shields\.io/github/v/release/', re.I)
_rm = read(os.path.join(REPO, 'README.md')) or ''
if _DYN_BADGE.search(_rm):
    check('README 版本徽章 == v%s' % VER, True,
          '用动态徽章（shields.io 现读线上 release，不会停在旧版本）')
else:
    _badge = re.findall(r'version-v([0-9.]+)-blue', _rm)
    check('README 版本徽章 == v%s' % VER, _badge == [VER], '实际=%s' % (_badge or '没找到'))

# ⚠ 不要顺手把 docs/demo/hotnews-demo-v*.mp4 也纳入检查：那个文件名是**故意冻结**的，
#   对外发布的演示视频链接就指向它，改名会让这些链接失效。
README_VER_PATS = (
    ('README 下载文件名', r'`hotnews-v([0-9.]+)\.exe`'),
    ('README 数据源小节标题', r'^##\s*数据源（v([0-9.]+)）'),
    ('README 数据源折叠标题', r'<summary><b>数据源（v([0-9.]+)）</b>'),
)
for _label, _pat in README_VER_PATS:
    _got = re.findall(_pat, _rm, re.M)
    check('%s == v%s' % (_label, VER), _got == [VER], '实际=%s' % (_got or '没找到'))

# 说明.txt：CONTRIBUTING「文档与对外文案」一节明确要求
# 「说明.txt 的第一行与「数据源」一节的版本号必须与 version.json 一致」。
# 这里再补一条**更宽的**判据：整份说明里提到的最高版本号必须就是当前版本 ——
# 只查两三处（第一行/小节标题）挡不住"正文里还写着上一版"的漏网。
_ins = read(os.path.join(REPO, '说明.txt')) or ''
_ins_stale = []
for _label, _pat in (('第一行', r'^hotnews\s*·\s*热点新闻检索 v([0-9.]+)'),
                     ('「三、数据源（vX.Y.Z ...）」小节', r'^三、数据源（v([0-9.]+)')):
    _got = re.findall(_pat, _ins, re.M)
    if _got != [VER]:
        _ins_stale.append('%s 写的是 %s' % (_label, '/'.join(_got) or '没找到'))
_ins_all = re.findall(r'v([0-9]+\.[0-9]+\.[0-9]+)', _ins)
_ins_top = max(_ins_all, key=ver_tuple) if _ins_all else None
if _ins_top != VER:
    _ins_stale.append('正文里最高只写到 v%s' % (_ins_top or '无'))
check('说明.txt 的版本字样都跟上了 v%s（第一行 / 数据源小节 / 全篇最高版本）' % VER,
      not _ins_stale, '；'.join(_ins_stale))
if _ins_stale:
    say('     → 修法：把 说明.txt 里落后的版本字样改成 v%s（第一行、'
        '「三、数据源（vX.Y.Z）」、正文各小节）' % VER)

_cl = read(os.path.join(REPO, 'docs', 'CHANGELOG.md')) or ''
check('CHANGELOG 里有 v%s 段落' % VER,
      bool(re.search(r'^##\s+v?%s\b' % re.escape(VER), _cl, re.M)),
      '（发布说明从这里取）')

# 升级源的签名文件：线上升级源带 Ed25519 签名，仓库里这份签名必须跟着版本走。
_sig = read(os.path.join(REPO, 'version.json.sig')) or ''
check('version.json.sig 存在且是 64 字节签名的十六进制（128 位十六进制字符）',
      bool(re.fullmatch(r'[0-9a-fA-F]{128}', _sig.strip())),
      '长度=%d 字符' % len(_sig.strip()))

# ══════════════════════════════════════════════════════════════════════════
# ④ 数据源栏目登记（静态读 server.py 的 SOURCES —— 不需要第三方依赖）
# ══════════════════════════════════════════════════════════════════════════
# 用 ast 只解析语法树、**不 import server**：克隆下来的机器没装 fastapi 也能查这一节。
# 这正是本项目历史上真出过事的地方（v1.5.2 把 _MEFCL_COOKIE 误删、娱乐整栏消失）。
say()
say('④ 数据源栏目登记（静态读 server.py 的 SOURCES）')
_srv_txt = read(os.path.join(REPO, 'server.py')) or ''
_sources, _cn_split, _funcs = {}, None, set()
try:
    _tree = ast.parse(_srv_txt)
    for _n in _tree.body:
        if isinstance(_n, ast.Assign):
            for _t in _n.targets:
                if not isinstance(_t, ast.Name):
                    continue
                if _t.id == 'SOURCES' and isinstance(_n.value, ast.Dict):
                    for _k, _v in zip(_n.value.keys, _n.value.values):
                        _key = getattr(_k, 'value', None)
                        if _key in ('cn', 'intl') and isinstance(_v, ast.List):
                            _rows = []
                            for _el in _v.elts:
                                if isinstance(_el, ast.Dict):
                                    _d = {}
                                    for _kk, _vv in zip(_el.keys, _el.values):
                                        _nm = getattr(_kk, 'value', None)
                                        if _nm and isinstance(_vv, ast.Constant):
                                            _d[_nm] = _vv.value
                                    _rows.append(_d)
                            _sources[_key] = _rows
                if _t.id == 'CN_SPLIT_CLS':
                    try:
                        _cn_split = set(ast.literal_eval(_n.value))
                    except Exception:
                        _cn_split = None
        if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _funcs.add(_n.name)
except SyntaxError as _e:
    say('     ⚠ server.py 解析失败: %s' % _e)

if not _sources:
    check('能从 server.py 里读出 SOURCES（cn / intl 两张表）', False,
          '解析不到 —— 若只是把 SOURCES 换了写法，请同步更新本自检的第 ④ 节')
else:
    _cn_cls = {r.get('cls') for r in _sources.get('cn') or [] if r.get('cls')}
    _intl_cls = {r.get('cls') for r in _sources.get('intl') or [] if r.get('cls')}
    for _reg, _rows in _sources.items():
        _tally = {}
        for _r in _rows:
            _tally[_r.get('cls') or '?'] = _tally.get(_r.get('cls') or '?', 0) + 1
        say('     SOURCES[%s]: %d 家源 —— %s' % (
            _reg, len(_rows),
            ' '.join('%s%d' % (k, v) for k, v in sorted(_tally.items(), key=lambda x: -x[1]))))
    check('国内源登记的栏目 ⊆ 定稿 6 栏（%s）' % '/'.join(CN_FINAL),
          _cn_cls <= set(CN_FINAL),
          '多出来的: %s' % '、'.join(sorted(_cn_cls - set(CN_FINAL))))
    check('国内源不再登记 社会 / 要闻 / 体育',
          not (_cn_cls & set(DOMESTIC_GONE)),
          '还留着: %s' % '、'.join(sorted(_cn_cls & set(DOMESTIC_GONE))))
    check('国际源登记的栏目 ⊆ 定稿 6 栏（%s）' % '/'.join(INTL_FINAL),
          _intl_cls <= set(INTL_FINAL),
          '多出来的: %s' % '、'.join(sorted(_intl_cls - set(INTL_FINAL))))
    check('国际频道保留「要闻」与「体育」两个栏目', {'要闻', '体育'} <= _intl_cls,
          '、'.join(sorted(_intl_cls)))
    check('每个定稿栏目在国内源里至少还有 1 家源（不许整栏被清空）',
          bool(_cn_cls) and not ((set(CN_FINAL) - {'热榜'}) - _cn_cls),
          '缺源的栏目: %s' % '、'.join(sorted((set(CN_FINAL) - {'热榜'}) - _cn_cls)))
    check('国内频道 id 里已无 cn-cnsport / cn-cnsoc / cn-cnsocial（国内体育与民生源都删了）',
          not ({r.get('id') for r in _sources.get('cn') or []}
               & {'cn-cnsport', 'cn-cnsoc', 'cn-cnsocial'}))
check('CN_SPLIT_CLS 用「头条」且不含 社会 / 要闻 / 体育',
      bool(_cn_split) and ('头条' in _cn_split) and not (_cn_split & set(DOMESTIC_GONE)),
      '、'.join(sorted(_cn_split)) if _cn_split else '解析不到 CN_SPLIT_CLS')
check('国内 / 国际划分的三条判据函数都在（_has_cn_marker_title / _is_domestic_for_intl / _is_foreign_macro）',
      {'_has_cn_marker_title', '_is_domestic_for_intl', '_is_foreign_macro'} <= _funcs,
      '这三个里有缺的: %s' % '、'.join(sorted(
          {'_has_cn_marker_title', '_is_domestic_for_intl', '_is_foreign_macro'} - _funcs)) or '三个都在')

# 已下线栏目名的**窄扫描**：只抓"被当成栏目名"的那种写法（一个独立的带引号字面量），
# 注释、正则候选、句子里的词一律不算 —— 这正是维护者工作区 tools\column_scan.py 的判据。
# 为什么不搬那份 scanner：它不在本仓库里（只在维护者工作区），搬进来就破坏"克隆即可运行"。
_scanned, _hit_terms = 0, []
SCAN_EXT = ('.py', '.html', '.md', '.txt', '.json', '.yml', '.yaml', '.spec')
SCAN_SKIP_DIRS = {'runtime', '.git', '__pycache__', 'docs', 'build_tmp', 'dist', 'build'}
SCAN_SKIP_FILES = {'keywords.txt', 'hotnews_cn_test.json'}
_QUOTED_SOCIAL = re.compile(r'["\']社会["\']')
_SELF = os.path.abspath(__file__)          # 本脚本自己
for _root, _dirs, _fs in os.walk(REPO):
    _dirs[:] = [d for d in _dirs if d not in SCAN_SKIP_DIRS]
    for _f in _fs:
        if _f.startswith('_') or _f in SCAN_SKIP_FILES:
            continue
        if not _f.lower().endswith(SCAN_EXT):
            continue
        _p = os.path.join(_root, _f)
        if os.path.abspath(_p) == _SELF:
            continue    # 这个脚本自己的 DOMESTIC_GONE 常量里当然写着「社会」，不算
        _scanned += 1
        _s = read(_p) or ''
        for _i, _l in enumerate(_s.split('\n'), 1):
            if _QUOTED_SOCIAL.search(_l):
                _hit_terms.append('%s:%d %s' % (os.path.relpath(_p, REPO), _i, _l.strip()[:70]))
check('全仓库没有把「社会」当栏目名写死（%d 个文本文件；注释/正则候选不算）' % _scanned,
      not _hit_terms, '；'.join(_hit_terms[:3]))
skip('「要闻 / 体育」是否只出现在国际语境',
     '需要逐行语境判定（工作区的 tools/column_scan.py 才有这套判据，它没有入库）；'
     '本机另有一份回归测试 tools/test_column_scan.py，需要时在维护者工作区里跑')

# ══════════════════════════════════════════════════════════════════════════
# ⑤ 关键回归断言（需要第三方依赖；缺依赖就 SKIP，不算失败）
# ══════════════════════════════════════════════════════════════════════════
# 这些断言都是**离线纯函数**，不需要联网、不需要起服务 —— 但需要 server.py 能 import，
# 所以要先装依赖（pip install -r requirements.txt）。没装就 SKIP。
say()
say('⑤ 关键回归断言（离线纯函数；需要 fastapi/httpx/feedparser）')


class _Cap(object):
    """把 import 期间 server.py 打印的东西收起来，别把自检输出冲乱。"""

    def __init__(self):
        self.buf = io.StringIO()
        self.old = None

    def __enter__(self):
        self.old, sys.stdout = sys.stdout, self
        return self

    def write(self, s):
        self.buf.write(s)

    def flush(self):
        pass

    def __exit__(self, *a):
        sys.stdout = self.old
        return False


_sv = None
_cap = _Cap()
try:
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    os.chdir(REPO)      # server.py 按自身位置定位资源；换个 cwd 更贴近真实运行
    with _cap:
        import server as _sv  # noqa: F401
except ModuleNotFoundError as _e:
    _sv = None
    skip('关键回归断言（国内/国际划分、图片地址规范化、升级通道证书兜底）',
         '缺少第三方依赖 %s —— 先 pip install -r requirements.txt 再跑' % _e.name)
except Exception as _e:
    _sv = None
    check('server.py 能 import', False, '%s: %s' % (type(_e).__name__, _e))

if _sv is not None:
    check('server.py 能 import', True)

    # ⑫ 图片地址规范化（2026-10-15 事故固化：快科技那类"两个协议黏在一起"的畸形地址）
    _URL_CASES = (
        ('https://img-kp.mydrivers.comhttps://img1.mydrivers.com/img/20261014/a.png',
         'https://img1.mydrivers.com/img/20261014/a.png'),
        ('https:https://img1.mydrivers.com/img/a.png',
         'https://img1.mydrivers.com/img/a.png'),
        ('//img1.mydrivers.com/img/a.png',
         'https://img1.mydrivers.com/img/a.png'),
        ('/img/a.png', 'https://news.mydrivers.com/img/a.png'),
        ('https://news.mydrivers.com/r?u=http://x.com/a.png',
         'https://news.mydrivers.com/r?u=http://x.com/a.png'),   # 查询串里带协议：不许误伤
    )
    _bad_url = []
    for _src, _want in _URL_CASES:
        _got = _sv._norm_url(_src, 'https://news.mydrivers.com')
        if _got != _want:
            _bad_url.append('%s -> %s（期望 %s）' % (_src[:40], _got, _want))
    check('畸形图片地址都被规范化成单协议地址（%d 例）' % len(_URL_CASES), not _bad_url,
          '；'.join(_bad_url[:2]))

    # ⑬ 国内 / 国际划分（2026-10-15 用户口径）。语料是 2026-10-15 真机抓到的
    #    国际栏 97 条 + 国内栏 200 条里逐条人工核对过的标题。
    #    ⚠ 别为了让它变绿去删用例：红了说明判据退化了，去修判据。
    _MUST_DROP = (
        ('热榜', '澳前总理陆克文现身南锣鼓巷，用中文评价北京变化'),
        ('要闻', '“中国鄂温克驯鹿社区的气候与社会变迁”边会在冰岛举行'),
        ('要闻', '“中国绿”风行河内'),
        ('要闻', '视频丨中国联合展台亮相法国戛纳电视节'),
        ('科技', '机器人助力中国爱饶山区珍稀植物、野生动物研究'),
        ('体育', '亚冠精英联赛：上海海港主场战平大田韩亚市民'),
        ('要闻', '北京12345论坛如何重塑全球城市治理谈判'),
        ('要闻', '习近平、博科出席北京合作文件签字仪式'),
        ('财经', '市场驱动型人民币与全球再平衡之路'),
        ('要闻', '欧洲议会议员：北京12345热线缩小了需求和服务之间的差距'),
        ('财经', '全球最大的工业博览会在上海开幕'),
        ('要闻', '中国副总理呼吁中美贸易关系更加稳定'),
    )
    _MUST_KEEP = (
        '凤凰直击：俄军演练用地面机器人撤离伤员',
        '美媒爆料：白宫新发言人并未通过美国律师资格考试',
        '涉及乌克兰，勒庞警告欧盟：这无异于自杀',
        '美国中期选举最重要的议题是什么？外媒：餐桌、油箱',
        '“我希望美军不仅撤离冲绳，还要撤离整个日本”',
        '胡塞武装用中企零件？中方回应',
        '鲁比奥即将开启亚洲两国行，来干什么？',
        '波兰外长喊话马斯克：大人物，你又在拿你的产品玩弄政治吗？',
        '国际足联被曝花400万美元租特朗普大厦办公室，却大多闲置！FIFA：符合当地法律',
        '莫斯科警告说，东京的信息收集尝试将使两国关系紧张',
        '伊朗就空军基地事件指控传唤英国特使',
        '美以伊三方瞄准两场关键选举：内塔尼亚胡和特朗普多次通电话，被曝力推选前打伊朗',
        '“特朗普下令：赴美生子，倒查6年”',
        '民进党当局向欧洲推销自己，拿大陆说事',
    )
    _bad_drop = [t for _c, t in _MUST_DROP
                 if not _sv._is_domestic_for_intl({'title': t, 'cls': _c})]
    check('国际栏「中国主体」稿全部拦得住（%d 条真机语料）' % len(_MUST_DROP), not _bad_drop,
          ('漏 %d 条: %s' % (len(_bad_drop), _bad_drop[0][:40])) if _bad_drop else '')
    _bad_keep = [t for t in _MUST_KEEP if _sv._looks_domestic_news_item({'title': t})]
    check('国际栏「真国际稿」不被误杀（%d 条真机语料）' % len(_MUST_KEEP), not _bad_keep,
          ('误杀 %d 条: %s' % (len(_bad_keep), _bad_keep[0][:40])) if _bad_keep else '')

    # 国内财经 → 国际财经：纯外国宏观搬去国际，涉华财经留在国内
    _MACRO_YES = (
        '美CPI全面升温响“通胀螺旋”警报，10月加息概率一度五成，“新美联储通讯社”：盯紧沃什本周讲话',
        '美CPI重燃加息押注，纳指跌近2%，芯片指数六连跌，CoreWeave暴跌15%，美元创18个月新高',
        '标普500终结逾六年来最长平静期，53个交易日来首次跌超1%，AI交易退潮',
        '美联储褐皮书示警油价“凶猛”：半数地区价格压力强劲或加剧，企业面临涨价与利润挤压',
    )
    _MACRO_NO = (
        'A股三大指数集体收涨，两市成交额1.8万亿',
        '人民币汇率创年内新高，离岸人民币一度升破7.0',
        '中国央行开展5000亿元MLF操作，利率维持不变',
        '创业板算力ETF放量上涨，广发基金提示溢价风险',
    )
    _bad_macro = ([t for t in _MACRO_YES if not _sv._is_foreign_macro({'title': t})]
                  + [t for t in _MACRO_NO if _sv._is_foreign_macro({'title': t})])
    check('纯外国宏观财经会挪去国际、涉华财经留在国内（%d+%d 条语料）'
          % (len(_MACRO_YES), len(_MACRO_NO)), not _bad_macro,
          ('有问题 %d 条: %s' % (len(_bad_macro), _bad_macro[0][:40])) if _bad_macro else '')

    # ⑭ 升级通道证书兜底（2026-10-12 事故固化：_MEI 里的 cacert.pem 被清理后仍要能检查更新）
    try:
        import ssl as _ssl
        import certifi as _certifi
        import updater as _up
        _up._VERIFY = None
        _ctx = _up._updater_verify()
        check('_updater_verify() 正常能建出 SSLContext', isinstance(_ctx, _ssl.SSLContext),
              type(_ctx).__name__)
        _orig_where = _certifi.where
        try:
            _certifi.where = lambda: os.path.join(REPO, '__故意不存在__', 'cacert.pem')
            _up._VERIFY = None
            _ctx2 = _up._updater_verify()
            _ok2, _d2 = isinstance(_ctx2, _ssl.SSLContext), type(_ctx2).__name__
        except Exception as _e2:
            _ok2, _d2 = False, '%s: %s' % (type(_e2).__name__, _e2)
        finally:
            _certifi.where = _orig_where
        check('cacert.pem 不存在时退到系统证书库（不再秒报「无法连接更新源」）', _ok2, _d2)
    except ModuleNotFoundError as _e:
        skip('升级通道证书兜底断言', '缺少第三方依赖 %s' % _e.name)
    except Exception as _e:
        check('升级通道证书兜底断言可执行', False, '%s: %s' % (type(_e).__name__, _e))

    # ⑮ 静态界面文件自愈（2026-10-18 事故固化：%TEMP%\_MEIxxxx\static 被清理后，
    #    页面只剩兜底文字、用户报「打开没有显示」—— 服务其实一直正常）
    try:
        import static_heal as _shd
        import shutil as _shr
        import tempfile as _tf
        _st_root = _tf.mkdtemp(prefix='hotnews_static_')
        try:
            _st_src = os.path.join(_st_root, 'src')
            _shr.copytree(os.path.join(REPO, 'static'), _st_src)
            _st_la = os.path.join(_st_root, 'localappdata')
            _d1 = _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            check('打包态把 static/ 复制到稳定目录（%LOCALAPPDATA%\\hotnews\\static-版本）',
                  _d1 != _st_src and os.path.isfile(os.path.join(_d1, 'index.html')), _d1)
            # 模拟"临时目录被清理"：把解包目录整个删掉，再启动一次
            _shr.rmtree(_st_src, ignore_errors=True)
            _d2 = _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            check('临时目录被清空后仍能从稳定副本取到 index.html',
                  os.path.isfile(os.path.join(_d2, 'index.html')), _d2)
            # 源码态不复制（改模板立即生效）
            check('源码态不做复制（改了 static/ 立即生效）',
                  _shd.ensure_static(os.path.join(_st_root, 'nope'), REPO, '9.9.9', False,
                                     localappdata=_st_la) == os.path.join(_st_root, 'nope'))
            # 旧版本副本要被清掉，不能越攒越多
            _old = os.path.join(_st_la, 'hotnews', 'static-1.0.0')
            os.makedirs(_old, exist_ok=True)
            open(os.path.join(_old, 'index.html'), 'w', encoding='utf-8').write('old')
            _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            check('旧版本的 static-* 副本会被清掉', not os.path.isdir(_old), _old)

            # 【2026-10-25 修】同一个版本号重新打包：包里界面变了必须以包为准，
            #   否则稳定副本永远读上一次那一份 —— 新界面在 exe 里带着，打开却"改了没生效"。
            #   同时"直接改稳定副本"的手工补丁通道不能被顶死。
            _shr.copytree(os.path.join(REPO, 'static'), _st_src)      # 模拟 %TEMP% 又解包出来了
            _cur = os.path.join(_st_la, 'hotnews', 'static-9.9.9')
            _live = os.path.join(_cur, 'index.html')
            _shr.rmtree(os.path.join(_st_la, 'hotnews', '界面备份'), ignore_errors=True)
            _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            check('包内界面没变时不折腾本地副本（签名对得上，也不产生备份）',
                  _shd.read_stamp(_cur) == _shd.bundle_sig(_st_src)
                  and not os.path.isdir(_shd.backup_dir(_st_la)),
                  _shd.read_stamp(_cur)[:12])
            with io.open(_live, 'w', encoding='utf-8', newline='\n') as _f:
                _f.write('<html>手工补丁</html>')                      # 手工补丁通道
            _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            _now = read(_live) or ''
            check('手工补丁（直接改稳定副本）重启后还在', _now == '<html>手工补丁</html>', _now[:40])
            with io.open(os.path.join(_st_src, 'intl.html'), 'w', encoding='utf-8',
                         newline='\n') as _f:
                _f.write('<html>包里换了新界面</html>')                 # 同版本号重新打包
            _shd.ensure_static(_st_src, REPO, '9.9.9', True, localappdata=_st_la)
            _bd = _shd.backup_dir(_st_la)
            _bk = sorted(f for f in (os.listdir(_bd) if os.path.isdir(_bd) else [])
                         if f.startswith('index.html.'))
            _intl_new = read(os.path.join(_cur, 'intl.html')) or ''
            check('同版本号重打包：新界面生效（读的是包里那套）',
                  _intl_new == '<html>包里换了新界面</html>' and os.path.isfile(_live), _intl_new[:40])
            _kept = (read(os.path.join(_bd, _bk[0])) or '') if _bk else ''
            check('同版本号重打包：被顶掉的手工补丁进了「界面备份」，随时能换回去',
                  len(_bk) == 1 and _kept == '<html>手工补丁</html>', _bk)
        finally:
            _shr.rmtree(_st_root, ignore_errors=True)

        # 模板读失败不许把空串永久缓存（否则文件回来了页面也一直空，只能重启）
        # 做法：把两个查找目录都指向不存在的路径 → 一定读失败；再恢复 → 必须能读回来。
        _keep_dir = _sv.STATIC_DIR
        _keep_base = _sv.BASE_DIR
        _keep_tpl = dict(_sv._HTML_TPL)
        try:
            _sv.STATIC_DIR = os.path.join(_st_root, '不存在的目录')
            _sv.BASE_DIR = os.path.join(_st_root, '也不存在')
            _sv._HTML_TPL.clear()
            _bad = _sv._html_template('index.html')
            _cached_bad = 'index.html' in _sv._HTML_TPL
            _sv.STATIC_DIR = _keep_dir
            _sv.BASE_DIR = _keep_base
            _good = _sv._html_template('index.html')
        finally:
            _sv.STATIC_DIR = _keep_dir
            _sv.BASE_DIR = _keep_base
            _sv._HTML_TPL.clear()
            _sv._HTML_TPL.update(_keep_tpl)
        check('模板读失败不缓存空串，恢复后能重新读到',
              (not _bad) and (not _cached_bad) and bool(_good), 'len=%d' % len(_good or ''))

        # 模板改了要**不重启就生效**：源码态改 static/、打包态直接改稳定副本那份 html，
        # 都指着这条（缓存认文件 mtime+大小，见 server._html_template）。
        _tmp_static = _tf.mkdtemp(prefix='hotnews_tpl_')      # 自己的临时目录，别碰已删的 _st_root
        _tpl_p = os.path.join(_tmp_static, 'index.html')
        try:
            _sv._HTML_TPL.clear()
            _sv.STATIC_DIR = _tmp_static
            _sv.BASE_DIR = os.path.join(_tmp_static, '也不存在')
            with io.open(_tpl_p, 'w', encoding='utf-8', newline='\n') as _f:
                _f.write('<html>第一版</html>')
            _t1 = _sv._html_template('index.html')
            with io.open(_tpl_p, 'w', encoding='utf-8', newline='\n') as _f:
                _f.write('<html>第二版（内容变了，没重启）</html>')
            _t2 = _sv._html_template('index.html')
            _t3 = _sv._html_template('index.html')          # 再读一次：必须还是第二版，且走缓存
            check('改了模板不重启也能读到新内容（手工补丁通道的前提）',
                  _t1 == '<html>第一版</html>' and _t2 == '<html>第二版（内容变了，没重启）</html>'
                  and _t3 == _t2, '%s|%s' % ((_t1 or '')[:12], (_t2 or '')[:12]))
        finally:
            _shr.rmtree(_tmp_static, ignore_errors=True)
            _sv.STATIC_DIR = _keep_dir
            _sv.BASE_DIR = _keep_base
            _sv._HTML_TPL.clear()
            _sv._HTML_TPL.update(_keep_tpl)

        # 界面文件缺失时给的是说明页，不再是裸露的 <h1>hotnews</h1>
        _idx_src = read(os.path.join(REPO, 'server.py')) or ''
        check('界面文件缺失时返回说明页（不再只有 hotnews 三个字）',
              '界面文件缺失' in (_sv._MISSING_UI_PAGE or '')
              and 'return HTMLResponse("<h1>hotnews</h1>")' not in _idx_src)
    except ModuleNotFoundError as _e:
        skip('静态界面文件自愈断言', '缺少第三方依赖 %s' % _e.name)
    except Exception as _e:
        check('静态界面文件自愈断言可执行', False, '%s: %s' % (type(_e).__name__, _e))

    # 抓取/import 期间打出来的东西里不该有异常痕迹
    _cap_txt = _cap.buf.getvalue()
    _traces = [l for l in _cap_txt.split('\n')
               if re.search(r'失败: (NameError|AttributeError|TypeError|KeyError|UnboundLocalError)',
                            l) or 'Traceback' in l]
    check('import server 期间没有异常痕迹', not _traces, (_traces[0][:90] if _traces else ''))
    if _traces:
        for _l in _traces[:5]:
            say('     | %s' % _l.strip()[:100])

# ══════════════════════════════════════════════════════════════════════════
# ⑥ 对外文档体检（自包含）
# ══════════════════════════════════════════════════════════════════════════
say()
say('⑥ 对外文档体检（私人邮箱 / 署名 / 依赖清单）')
DOCS = ('README.md', 'docs/CONTRIBUTING.md', 'docs/COMPLIANCE.md', 'docs/SECURITY.md',
        'docs/CODE_OF_CONDUCT.md', 'docs/CODE_SIGNING_POLICY.md', 'docs/CHANGELOG.md',
        '说明.txt', '.github/PULL_REQUEST_TEMPLATE.md', 'docs/demo/index.html')
_texts = {}
for _f in DOCS:
    _t = read(os.path.join(REPO, _f))
    if _t is not None:
        _texts[_f] = _t
check('对外文档读得到（%d 份）' % len(DOCS), len(_texts) == len(DOCS),
      '缺: %s' % '、'.join(f for f in DOCS if f not in _texts))

# 2026-10-16: 把仓库自己的 tools/*.py 也纳入下面同一套邮箱检查 —— 之前只扫对外文档，
# 结果这个自检脚本的注释里就长期留着一个真实邮箱（还是"加空格"的写法）。
for _f in ['tools/' + _n for _n in sorted(os.listdir(os.path.join(REPO, 'tools')))
           if _n.endswith('.py')]:
    _t = read(os.path.join(REPO, _f))
    if _t is not None:
        _texts[_f] = _t

# 私人邮箱检查：先把正文归一化（去空白与零宽字符、全角转半角）再匹配。
# 归一化之后再匹配，把邮箱中间插空格 / 零宽字符这类规避写法会被还原成正常邮箱、照样命中
# （狐径的自检就是这么修的）。**不额外做"邮箱域名裸关键字"检查**：本项目的 README 要
# 正经登记数据源域名（如 ent.sina.com.cn），那种检查会把数据源当成私人邮箱误报。
FULLWIDTH = {0x3000: ' ', 0xFF20: '@', 0xFF0E: '.', 0xFF0B: '+', 0xFF0D: '-'}
for _i in range(0xFF10, 0xFF1A):
    FULLWIDTH[_i] = chr(_i - 0xFF10 + ord('0'))
for _i in range(0xFF21, 0xFF3B):
    FULLWIDTH[_i] = chr(_i - 0xFF21 + ord('A'))
for _i in range(0xFF41, 0xFF5B):
    FULLWIDTH[_i] = chr(_i - 0xFF41 + ord('a'))
MAIL_OK_DOMAINS = ('github.com', 'gitee.com', 'users.noreply.github.com')


def flatten(t):
    return ''.join(FULLWIDTH.get(ord(c), c)
                   for c in t if c not in ' \t\r\n\u00a0\u200b\u200c\u200d\ufeff')


_mail_hits = []
for _f, _t in sorted(_texts.items()):
    _flat = flatten(_t)
    for _m in re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', _flat):
        if not any(d in _m.lower() for d in MAIL_OK_DOMAINS):
            _mail_hits.append('%s: %s' % (_f, _m))
check('对外文档里没有私人邮箱（已归一化空格/全角；允许 GitHub noreply）',
      not _mail_hits, '；'.join(_mail_hits[:3]))

check('README 里有署名「海风（kele551）」与仓库地址',
      ('海风（kele551）' in _rm) and ('gitcode.com/kele551/hotnews' in _rm))
check('docs/COMPLIANCE.md 里有署名与仓库地址',
      ('海风（kele551）' in (_texts.get('docs/COMPLIANCE.md') or ''))
      and ('gitcode.com/kele551/hotnews' in (_texts.get('docs/COMPLIANCE.md') or '')))

_req = read(os.path.join(REPO, 'requirements.txt')) or ''
_lack = [d for d in ('fastapi', 'uvicorn', 'feedparser', 'httpx') if d not in _req]
check('requirements.txt 列出四个核心依赖（fastapi / uvicorn / feedparser / httpx）',
      not _lack, '缺: %s' % '、'.join(_lack))
check('docs/COMPLIANCE.md 有「第三方组件与许可」小节',
      '第三方组件与许可' in (_texts.get('docs/COMPLIANCE.md') or ''))
check('docs/CONTRIBUTING.md 写明提交前必须跑本自检（tools/preflight_release.py）',
      'preflight_release.py' in (_texts.get('docs/CONTRIBUTING.md') or ''),
      '（贡献指南里要有这条，别人才知道有门可跑）')
skip('对外文案红线词 0 命中',
     'CONTRIBUTING「文档与对外文案」一节明确要求**不要在仓库里逐词列举**那些词'
     '（清单自身会被内容扫描命中），所以这条判据不放进仓库脚本；'
     '维护者工作区有专门的验证脚本（见 docs/COMPLIANCE.md 第 8 节「自查方式」）')

# ══════════════════════════════════════════════════════════════════════════
# ⑦ 链接与徽章的可访问性（需要联网；不可用就 SKIP）
# ══════════════════════════════════════════════════════════════════════════
say()
say('⑦ 链接与徽章可访问性（README 里的 http(s) 链接，含徽章图）')


def http_status(url, timeout=15):
    """只取状态码。

    两个真实的坑（都踩过，才写成这样）：
      · README 里的徽章地址带**原始中文**（`img.shields.io/badge/形态-单文件exe-blue`），
        浏览器会自动百分号编码，urllib 不会 —— 直接请求会抛 UnicodeEncodeError，
        于是好好的徽章被误报成"主机不可达"。所以先 quote 一遍。
      · GitHub 对某些 HTML 路径的 **HEAD** 回 404，浏览器里其实是能打开的
        （实测 /stargazers 就是），拿 HEAD 的 404 当死链会假红。所以 2xx 之外的
        结果一律再用 GET 确认一次，以 GET 为准。
    """
    def _one(method):
        u = urllib.parse.quote(url, safe=':/?#[]@!$&\'()*+,;=%~')
        try:
            req = urllib.request.Request(u, method=method,
                                         headers={'User-Agent': UA, 'Accept': '*/*'})
            if method == 'GET':
                req.add_header('Range', 'bytes=0-0')
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return getattr(r, 'status', 200), ''
        except urllib.error.HTTPError as e:
            return e.code, ''
        except Exception as e:
            return None, type(e).__name__

    st, note = _one('HEAD')
    if st is not None and st < 400:
        return st, ''
    st2, note2 = _one('GET')
    if st2 is not None:
        return st2, ''
    return (st if st is not None else None), (note or note2)


def net_alive():
    """先探一个"一直通"的域名（api.github.com）判断这台机器有没有网。"""
    return http_status('https://api.github.com')[0] is not None


_LINK_LIMIT_OTHER = 18      # 普通链接（仓库页/发行版页/下载链接…）最多查这么多
_LINK_LIMIT_BADGE = 8       # 徽章图最多查这么多（README 里往往有二十几个，全查既慢又没必要）
_BADGE_RX = re.compile(r'(shields\.io|hits\.sh|\.svg(\?|$))', re.I)


def pick_links(urls):
    """挑要检查的链接：**先普通链接、后徽章**，各自限量。

    不这么挑的话，README 开头的二十几个徽章会把名额全占了，真正要命的
    下载链接 / 发行版页 / 仓库页一个都查不到（第一版就是这么错的）。
    """
    badges = [u for u in urls if _BADGE_RX.search(u)]
    others = [u for u in urls if u not in badges]
    return others[:_LINK_LIMIT_OTHER] + badges[:_LINK_LIMIT_BADGE]


if NO_LINKS:
    skip('README 链接与徽章可访问性', '已按 %s 跳过' % ('--offline' if OFFLINE else '--no-links'))
elif not net_alive():
    skip('README 链接与徽章可访问性',
         '本机网络不可用（连 api.github.com 都不通）—— 请在能联网的机器上重跑，或加 --no-links')
else:
    _urls = []
    # README 里的地址有三种写法：HTML 属性（src="..."）、Markdown 链接、以及**行内代码**
    # （`` `https://.../version.json` ``）。最后那种如果直接抓，会把结尾的反引号和紧跟的
    # 中文说明一起吃进来 → 变成一条不存在的地址、假报死链（第一版就这么假红过三条）。
    # 办法：把反引号一律换成引号，让正则的"引号/空白即终止"规则一并管住它。
    for _u in re.findall(r'https?://[^\s"\'<>\)\]]+', _rm.replace('`', '"')):
        _u = _u.rstrip('.,;:、。）】`"\'')
        # 非徽章站点的地址一定是纯 ASCII（徽章那几种确实会把中文写在路径里），
        # 所以截到第一个中日韩字符为止：既保住徽章，又不会把后面的中文说明吃进来。
        if not re.search(r'(shields\.io|hits\.sh)', _u):
            _mm = re.match(r'https?://[^\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]+', _u)
            if _mm:
                _u = _mm.group(0).rstrip('.,;:、。）】`"\'')
        if _u and _u not in _urls:
            _urls.append(_u)
    _total_urls = len(_urls)
    _urls = pick_links(_urls)
    if _total_urls > len(_urls):
        say('     （README 共 %d 条链接，本次按"先普通链接、后徽章"的上限查 %d 条）'
            % (_total_urls, len(_urls)))
    _dead, _netfail, _info = [], [], []
    for _i, _u in enumerate(_urls):
        _st, _note = http_status(_u)
        # GitHub 对**脚本请求**的这几个页面一律回 404（实测连 torvalds/linux/stargazers
        # 都是 404），浏览器里其实打得开 —— 拿它判死链会假红，只打印、不计红。
        _bot404 = bool(re.search(r'github\.com/[^/]+/[^/]+/(stargazers|watchers|graphs/.*)$', _u))
        if _st in (404, 410) and _bot404:
            _info.append('%s -> HTTP %s（GitHub 对脚本请求的已知 404，浏览器可打开）' % (_u, _st))
            say('     · HTTP %s  %s（GitHub 对脚本请求的已知 404）' % (_st, _u))
        elif _st in (404, 410):
            _dead.append('%s -> HTTP %s' % (_u, _st))
            say('     · HTTP %s  %s' % (_st, _u))
        elif _st is None:
            _netfail.append('%s -> %s' % (_u, _note))
            say('     · 网络失败(%s)  %s' % (_note, _u))
        else:
            if _st >= 400:
                _info.append('%s -> HTTP %s（限流/需登录，不算死链）' % (_u, _st))
            say('     · HTTP %s  %s' % (_st, _u))
        # 平台限速：Gitee 侧间隔拉到 3 秒（2026-10-08 密集抓 Gitee 自己抓出过 451）
        if _i < len(_urls) - 1:
            time.sleep(3.0 if 'gitee.com' in _u else 0.5)
    check('README 里 %d 条链接都不存在 404/410（这些才是死链）' % len(_urls), not _dead,
          '；'.join(_dead[:3]))
    check_warn('README 里的链接主机都连得上（有没有 DNS/连接层面的失效）', not _netfail,
               '；'.join(_netfail[:3]))

# ══════════════════════════════════════════════════════════════════════════
# ⑧ 线上升级源版本号对齐（只算 WARN）
# ══════════════════════════════════════════════════════════════════════════
say()
say('⑧ 线上升级源版本号对齐（只警告，不阻断：发布前本来就会"仓库比线上新"）')
if OFFLINE:
    skip('线上升级源 version.json 的版本号', '已按 --offline 跳过')
elif not net_alive():
    skip('线上升级源 version.json 的版本号', '本机网络不可用')
else:
    def _remote_ver(label, url, headers=None):
        try:
            req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode('utf-8', 'replace'))
            if isinstance(j, dict) and j.get('content'):
                return json.loads(base64.b64decode(j['content']).decode('utf-8', 'replace')).get('version')
            return (j or {}).get('version')
        except Exception as e:
            check_warn('%s 上的 version.json 能读到' % label, False,
                       '%s（网络抽风/还没发布都可能，属正常）' % type(e).__name__)
            return None

    _ghv = _remote_ver('GitHub(main)', 'https://api.github.com/repos/kele551/hotnews/contents/version.json?ref=main',
                       headers={'User-Agent': UA, 'Accept': 'application/vnd.github+json'})
    if _ghv:
        check_warn('GitHub main 的 version.json 版本 == 本地 v%s' % VER,
                   str(_ghv).lstrip('v') == VER,
                   '线上=%s 本地=%s（发布前"仓库更新"属正常）' % (_ghv, VER))
    time.sleep(3.0)   # Gitee 限速：任何两次请求之间 ≥3 秒
    _gtv = _remote_ver('Gitee(master)',
                       'https://gitee.com/api/v5/repos/kele551/hotnews/contents/version.json?ref=master')
    if _gtv:
        check_warn('Gitee master 的 version.json 版本 == 本地 v%s' % VER,
                   str(_gtv).lstrip('v') == VER, '线上=%s 本地=%s' % (_gtv, VER))

# ══════════════════════════════════════════════════════════════════════════
# ⑨ 端到端抓取（默认不跑；--e2e 才真联网抓一遍）
# ══════════════════════════════════════════════════════════════════════════
# 为什么默认不跑：这一节要真联网抓一圈（1~3 分钟），而且结果会随"当时的源状态"浮动。
# 但它正是本项目最贵的一课换来的 —— v1.5.2 发出去之后真机上"国内新闻半天抓不出来"：
# 换源那批清理死代码时误删了 `_MEFCL_COOKIE` / `_MEFCL_LAST` 两个模块级变量（mefcl 解析还在用）
# → NameError → 一个源炸掉把整个国内栏带崩。当时只看了"服务起没起来"，没看**内容**。
# 所以这一节平时不挡路，需要时（例如改了源 / 改了筛选 / 发版前）用 --e2e 专门跑。
say()
say('⑨ 端到端抓取（真联网；默认不跑，加 --e2e 才跑）')
if not E2E:
    skip('端到端抓取（_collect 的条数与栏目覆盖）',
         '默认不跑：要真联网抓一遍（1~3 分钟）；发版前或改了数据源时请加 --e2e 跑一次')
elif OFFLINE:
    skip('端到端抓取', '已按 --offline 跳过（这一节必须真联网，--offline 与 --e2e 是互斥的）')
elif _sv is None:
    skip('端到端抓取', '缺少第三方依赖或 server.py 未能 import（见第 ⑤ 节的原因）')
elif not net_alive():
    skip('端到端抓取', '本机网络不可用 —— 这一节必须真联网')
else:
    _e2e_data = {}
    _cap2 = _Cap()
    with _cap2:
        for _rg in ('cn', 'intl'):
            try:
                _e2e_data[_rg] = _sv._collect(_rg) or []
            except Exception as _e:
                _e2e_data[_rg] = []
                check('_collect(%s) 不抛异常' % _rg, False, '%s: %s' % (type(_e).__name__, _e))
            say('     _collect(%s) -> %d 条' % (_rg, len(_e2e_data[_rg])))
    _e2e_log = _cap2.buf.getvalue()
    # 门槛说明：2026-10-15 真机实测国内 150+ 条、国际 90+ 条、各自覆盖 5~6 个栏目；
    # 这里落在 60 条 / 5 个栏目 / 3 个栏目是**留了余量**的（不贴着实测值卡）。
    # 红了先去看是哪个源退化了（server.py 的 SOURCES），不要去调这个数字。
    for _rg, _least in (('cn', 60), ('intl', 60)):
        check('_collect(%s) 返回条目 ≥ %d' % (_rg, _least),
              len(_e2e_data[_rg]) >= _least, '实际 %d 条' % len(_e2e_data[_rg]))
    for _rg, _final, _cover in (('cn', CN_FINAL, 5), ('intl', INTL_FINAL, 3)):
        _tally = {}
        for _it in _e2e_data[_rg]:
            _c = _it.get('cls') or '?'
            _tally[_c] = _tally.get(_c, 0) + 1
        say('     %s 栏目分布: %s' % (_rg, ' '.join(
            '%s%d' % (k, v) for k, v in sorted(_tally.items(), key=lambda x: -x[1]))))
        _coverd = [c for c in _tally if c in _final]
        check('%s 覆盖 ≥ %d 个定稿栏目' % (_rg, _cover), len(_coverd) >= _cover,
              '实际 %d 个: %s' % (len(_coverd), '、'.join(_coverd)))
        check('%s 实际出现的栏目都在定稿清单里' % _rg,
              not (set(_tally) - set(_final) - {'?'}),
              '多出来的: %s' % '、'.join(sorted(set(_tally) - set(_final) - {'?'})))
        if _rg == 'cn':
            check('国内栏没混进已下线栏目（社会 / 要闻 / 体育）',
                  not (set(_tally) & set(DOMESTIC_GONE)),
                  '出现了: %s' % '、'.join(sorted(set(_tally) & set(DOMESTIC_GONE))))
        else:
            check('国际栏仍然有「要闻」与「体育」两个栏目',
                  {'要闻', '体育'} <= set(_tally),
                  '实际: %s' % '、'.join(sorted(_tally)))
    _e2e_traces = [l for l in _e2e_log.split('\n')
                   if re.search(r'失败: (NameError|AttributeError|TypeError|KeyError|UnboundLocalError)', l)
                   or '源解析异常' in l or 'Traceback' in l]
    check('抓取日志里没有解析异常（NameError / AttributeError / …）', not _e2e_traces,
          (_e2e_traces[0][:90] if _e2e_traces else ''))
    _tail = [l for l in _e2e_log.strip().split('\n') if l.strip()][-6:]
    for _l in _tail:
        say('     | %s' % _l.strip()[:100])

# ══════════════════════════════════════════════════════════════════════════
# 老仓库（Gitee）地址清扫（章程 4.19.5，2026-10-23 用户要求）
#   发布文件里一律不许再出现老仓库（Gitee）域名；历史的 CHANGELOG 除外。
#   version.json 是带签名的线上升级源，这里只警告（改了必须重新签名并发版）。
# ══════════════════════════════════════════════════════════════════════════
say()
say('老仓库地址清扫：发布文件里不含 Gitee 域名')
# 关键字拆开拼：不然"禁止清单"自己会被扫成命中（2026-10-19 邮箱那次就是这么翻车的）
_NEEDLE = 'gitee' + '.com/kele551'
_NEEDLE2 = 'raw.gitee' + 'usercontent'
_bad_gitee = []
_scan_ext = ('.md', '.txt', '.py', '.ps1', '.json', '.html', '.yml', '.yaml')
_skip_names = {'CHANGELOG.md', 'version.json'}
for _dp, _dn, _fn in os.walk(REPO):
    if os.sep + '.git' in _dp:
        continue
    _dn[:] = [d for d in _dn if d not in ('.git', 'node_modules', 'build_tmp')]
    for _f in _fn:
        if not _f.lower().endswith(_scan_ext) or _f in _skip_names:
            continue
        _p = os.path.join(_dp, _f)
        try:
            _t = io.open(_p, encoding='utf-8-sig', errors='replace').read()
        except Exception:
            continue
        for _i, _ln in enumerate(_t.splitlines(), 1):
            if _NEEDLE in _ln or _NEEDLE2 in _ln:
                _bad_gitee.append('%s:%d' % (os.path.relpath(_p, REPO), _i))
check('发布文件里没有老仓库（Gitee）地址', not _bad_gitee,
      '；'.join(_bad_gitee[:5]) + (' 等 %d 处' % len(_bad_gitee) if len(_bad_gitee) > 5 else ''))

_vj_gitee = []
try:
    _vjt = io.open(os.path.join(REPO, 'version.json'), encoding='utf-8-sig').read()
    for _ln in _vjt.splitlines():
        if _NEEDLE in _ln:
            _vj_gitee.append(_ln.strip()[:80])
except Exception:
    pass
check_warn('version.json（线上升级源）里也不含 Gitee 地址', not _vj_gitee,
           '；'.join(_vj_gitee[:2]) + '（改了要重新签名并发版）' if _vj_gitee else '')


finish()
