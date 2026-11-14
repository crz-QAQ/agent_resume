"""复用已有公开表单浏览器流程，只更新本次三类提示的期望。"""
from pathlib import Path

root = Path(__file__).resolve().parents[2]
source = (root / "output/d_m9c_01/check_download_ui.cjs").read_text(encoding="utf-8")
old = 'note.includes("1 条需你补全真实信息（如在职月份）")'
new = 'note.includes("0 条需你补全真实信息") && note.includes("1 条跨段落需手动改")'
assert old in source
target = Path(__file__).with_name("check_download_ui.cjs")
target.write_text(source.replace(old, new).replace("5/1 explanation", "5/0/1 explanation"), encoding="utf-8")
print(target)
