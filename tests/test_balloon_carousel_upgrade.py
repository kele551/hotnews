"""气泡「轮播投放」升级（2026-11-13）的回归测试。

覆盖这一轮用户认领的五件事里能纯函数验证的四件：
  ① 换条动效：淡入进度 0~1，起点不满黑、300ms 到 1（_fade_p）；
  ② 原创席位：一组里至少 1 条原创/深度稿，没有原创货时安静跳过；
  ③ 几家在报：合并后的条目在来源行写「N 家在报」，原创的那条写「原创」；
  ⑤ 时段主题：早/午/晚三档给出的栏目顺序。
④ 的手感（滚轮 / 右键不感兴趣）在 test_balloon_click.py 之外单独验存储层：
  _balloon_mute_add 写盘 → _balloon_mute_set 读回、过期自动失效。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  [ok] " + name)
    else:
        _failed += 1
        print("  [FAIL] " + name + ("  " + detail if detail else ""))


def mk(i, cls, orig=False, also=0, label=None, age_min=1):
    return {"id": "t%d" % i, "title": "第%d条新闻标题内容用于轮播测试" % i, "cls": cls,
            "label": label or ("源%d" % i), "source": label or ("源%d" % i),
            "published": time.time() - age_min * 60, "cross_hot": 0,
            "original": orig, "also_n": also}


print("── ① 换条动效 ──")


class F(object):
    def __init__(self, t0):
        self._fade_t0 = t0


_now = time.monotonic()
check("刚开播时进度是 0（还在淡入起点）", server._balloon_fade_p(_now) < 0.05)
check("过了 500ms 进度到 1", server._balloon_fade_p(_now - 0.501) >= 1.0)
check("中途进度在 0~1 之间", 0.1 < server._balloon_fade_p(_now - 0.24) < 0.95)
check("没有起点（老对象）时按 1 处理，不闪", server._balloon_fade_p(None) == 1.0)

print("── ② 原创席位 ──")

_items = [mk(i, "科技" if i % 3 else "财经") for i in range(1, 10)] + [mk(99, "头条", True, 3, "新京报")]
_picked = server._pick_balloon_items(_items)
check("一组 10 条照旧挑得满", len(_picked) == 10, "len=%d" % len(_picked))
check("里面有原创那条", any(x.get("original") for x in _picked),
      "orig=%r" % [x["title"][:6] for x in _picked if x.get("original")])

_no_orig = [mk(i, "科技") for i in range(20, 30)]
_p2 = server._pick_balloon_items(_no_orig)
check("没有原创货时安静跳过、条数不变", len(_p2) == 10, "len=%d" % len(_p2))

print("── ③ 几家在报 ──")

check("3 家一起报 → 来源行写「3 家在报」",
      "3 家在报" in server._balloon_meta_line(mk(1, "头条", False, 3)))
check("只有 1 家（also_n=0）时不写，不硬编",
      "家在报" not in server._balloon_meta_line(mk(2, "头条")))
check("原创那条的来源行带「原创」",
      "原创" in server._balloon_meta_line(mk(3, "头条", True)))
check("来源 + 原创 + 几家 三段按顺序拼",
      server._balloon_meta_line(mk(4, "头条", True, 4, "财新")) == "财新 · 原创 · 4 家在报")

print("── ⑤ 时段主题 ──")

_t8 = time.mktime((2026, 11, 13, 8, 0, 0, 0, 0, 0))
_t12 = time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, 0))
_t21 = time.mktime((2026, 11, 13, 21, 0, 0, 0, 0, 0))
_t3 = time.mktime((2026, 11, 13, 3, 0, 0, 0, 0, 0))
check("早上 8 点播 头条 + 财经", server._balloon_hour_theme(_t8) == ("头条", "财经"))
check("午间 12 点播 科技 + 软件", server._balloon_hour_theme(_t12) == ("科技", "软件"))
check("晚上 21 点播 原创", server._balloon_hour_theme(_t21) == ("原创",))
check("凌晨不改版面（空主题）", server._balloon_hour_theme(_t3) == ())

print("── ④ 不感兴趣（存储层）──")

_tmp = os.path.join(tempfile.mkdtemp(prefix="hn_mute_"), "balloon_mute.json")
_old = server.BALLOON_MUTE_PATH
try:
    server.BALLOON_MUTE_PATH = _tmp
    check("没点过右键时集合是空的", server._balloon_mute_set() == set())
    server._balloon_mute_add("娱乐")
    check("右键之后「娱乐」进了集合", "娱乐" in server._balloon_mute_set())
    _raw = json.load(open(_tmp, encoding="utf-8"))
    check("落盘结构是 {v, cls:{名: 到期时间戳}}",
          "cls" in _raw and abs(float(_raw["cls"]["娱乐"]) - (time.time() + 24 * 3600)) < 120,
          "raw=%r" % _raw)
    server.BALLOON_MUTE_PATH = _tmp
    _raw["cls"]["科技"] = time.time() - 10      # 造一个已过期的
    json.dump(_raw, open(_tmp, "w", encoding="utf-8"), ensure_ascii=False)
    check("过期的自动失效，不误伤", "科技" not in server._balloon_mute_set())
    with open(_tmp, "w", encoding="utf-8") as f:
        f.write("{坏 json")
    check("坏文件也读得回空集合，不抛", server._balloon_mute_set() == set())
finally:
    server.BALLOON_MUTE_PATH = _old

print()
print(("失败 %d 条" % _failed) if _failed else "全部通过  失败 0 条")
sys.exit(1 if _failed else 0)
