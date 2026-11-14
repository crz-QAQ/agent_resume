"""保存用户要求的两份完整检查输出，并导出新增材料及原十二条守恒证据。"""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent


def main():
    """真实逐条探测外链，不用离线结果或历史状态冒充本次验证。"""
    original = json.loads((OUTPUT / "original_materials.json").read_text(encoding="utf-8"))
    current = json.loads((ROOT / "materials/materials.json").read_text(encoding="utf-8"))
    assert current["materials"][:12] == original["materials"] and len(original["materials"]) == 12
    added = current["materials"][12:]
    assert 6 <= len(added) <= 8 and all(row["id"].startswith("fde-") for row in added)
    (OUTPUT / "new_materials.json").write_text(json.dumps(added, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = 0
    for name, extra in (("check_materials", ["--timeout", "10", "--report", str(OUTPUT / "materials_link_check.json")]),
                        ("check_learning_resources", [])):
        result = subprocess.run([sys.executable, "-B", str(ROOT / f"tools/{name}.py"), *extra], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (OUTPUT / f"{name}.txt").write_bytes(result.stdout)
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.flush()
        status = status or result.returncode
    print("Original 12 material records unchanged; appended material JSON exported.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
