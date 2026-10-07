# -*- coding: utf-8 -*-
"""安全地给 static/index.html 打补丁：只做"插入"，绝不裁剪原文件。

【为什么单独写个脚本】之前用 PowerShell 的 Substring 插 CSS，把第一处 </style>
之后的内容全截掉了，整个页面的 HTML 和 JS 一起没了 —— 用户看到的就是"打开页面
什么都没有"。这里改用 Python，插入点全部用 insert()，并在最后校验文件结构。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ 的上一级才是 repo
HTML = os.path.join(BASE, "static", "index.html")

QUIT_CSS = """
/* 退出程序的整页提示（用户反馈"点了退出没反应"，其实是后台退了但页面没反馈） */
#quitMask{position:fixed;inset:0;background:rgba(248,250,252,.94);z-index:999;
  display:flex;align-items:center;justify-content:center;backdrop-filter:blur(3px)}
#quitMask .qm-card{background:#fff;border:1px solid #e6eaf0;border-radius:16px;
  padding:38px 46px;text-align:center;box-shadow:0 12px 40px rgba(20,30,50,.10);max-width:460px}
#quitMask .qm-ico{width:56px;height:56px;border-radius:50%;background:#e8f6ee;color:#2f9e5f;
  font-size:30px;line-height:56px;margin:0 auto 16px}
#quitMask h3{margin:0 0 12px;font-size:20px;color:#1a202c}
#quitMask p{margin:6px 0;color:#4a5568;font-size:14px;line-height:1.7}
#quitMask .qm-sub{color:#8896ab;font-size:13px;margin-top:12px}
#quitMask button{margin-top:20px;padding:10px 26px;border:1px solid #cfd8e3;background:#fff;
  border-radius:9px;font-size:14px;color:#2b6cb0;cursor:pointer}
#quitMask button:hover{background:#f2f7fd;border-color:#2b6cb0}
"""

JS = r"""
/* ================= 开机自启开关（用户要求"第一次使用时给个开关让用户选"） ================= */
async function refreshAutoStart(){
  try{
    const r = await fetch("/api/autostart");
    const j = await r.json();
    const cb = document.getElementById("autostart");
    if(cb) cb.checked = !!j.enabled;
  }catch(e){}
}
document.addEventListener("DOMContentLoaded", ()=>{
  const cb = document.getElementById("autostart");
  if(!cb) return;
  refreshAutoStart();
  cb.onchange = async ()=>{
    cb.disabled = true;
    try{
      const r = await fetch("/api/autostart", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({enabled: cb.checked})});
      const j = await r.json();
      cb.checked = !!j.enabled;
      cb.title = j.message || "";
    }catch(e){}
    cb.disabled = false;
  };
});

/* ================= 首屏加载提示（用户反馈"打开页面什么都没有"） ================= */
let _bootTry = 0;
function bootWatch(){
  try{
    if((state.items.length || hotItems.length) || _bootTry > 40) return;
    _bootTry++;
    const g = document.getElementById("grid");
    if(g && !g.querySelector(".card")){
      g.innerHTML = '<div class="empty">正在加载今天的新闻…<br>' +
        '<span style="color:#8896ab;font-size:13px">首次启动约需 10 秒，稍等一下就好</span></div>';
    }
    setTimeout(async ()=>{ try{ await load(false); }catch(e){} bootWatch(); }, 1500);
  }catch(e){}
}
bootWatch();
"""


def main():
    src = open(HTML, encoding="utf-8").read()
    before = len(src)
    changed = []

    # ① 退出遮罩样式：插在最后一处 </style> 之前
    if "#quitMask{" not in src:
        i = src.rfind("</style>")
        if i < 0:
            print("  ✗ 找不到 </style>")
            return 1
        src = src[:i] + QUIT_CSS + src[i:]
        changed.append("退出遮罩样式")

    # ② 开机自启 + 加载提示的脚本：插在最后一处 </script> 之前
    if "refreshAutoStart" not in src:
        i = src.rfind("</script>")
        if i < 0:
            print("  ✗ 找不到 </script>")
            return 1
        src = src[:i] + JS + src[i:]
        changed.append("自启开关+加载提示脚本")

    # ③ 工具栏里加复选框（放在「检查更新」按钮附近）
    if 'id="autostart"' not in src:
        anchor = 'id="reload"'
        i = src.find(anchor)
        if i > 0:
            # 回退到该标签的 '<' 位置，插到它前面
            j = src.rfind("<", 0, i)
            box = ('<label class="autostart" title="开机后在后台自动启动，'
                   '这样点「打开」时页面是现成的、秒开">'
                   '<input type="checkbox" id="autostart">开机启动</label>\n  ')
            src = src[:j] + box + src[j:]
            changed.append("自启复选框")
        else:
            print("  ✗ 找不到 reload 按钮，跳过复选框")

    if not changed:
        print("  无需改动")
        return 0

    # ④ 写回前校验结构：绝不能像上次那样把页面截断
    for need in ("<script", "</script>", "</body>", "</html>", "function load("):
        if need not in src:
            print(f"  ✗ 校验失败：结果里缺少 {need}，放弃写入")
            return 1
    if len(src) < before:
        print(f"  ✗ 校验失败：结果比原文还短（{len(src)} < {before}），放弃写入")
        return 1

    open(HTML, "w", encoding="utf-8", newline="").write(src)
    print(f"  已写入：{'、'.join(changed)}")
    print(f"  文件 {before} → {len(src)} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
