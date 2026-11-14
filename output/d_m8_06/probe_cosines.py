"""用项目既有 bge-small-zh 模型测量候选表述，不修改材料或推理阈值。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main():
    """禁网读取已有本地权重，打印真实余弦供验证相关性和区分度。"""
    import numpy as np
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("禁止联网")))
        from app.learning_resources import load_materials
        from app.scoring_semantic import _get_model, _encode_batch
        materials, _ = load_materials()
        model = _get_model()
        gaps = ["建立评测和反馈机制并根据真实业务结果迭代",
                "梳理业务流程、关键判断和专业经验并转化为AI场景方案",
                "推动方案从演示走向实际使用并解决使用中的问题",
                "把客户想解决的问题整理清楚，再列出优先顺序和成功标准",
                "深入一线业务识别值得AI解决的问题",
                "给客户的问题列出可执行的步骤和优先级"]
        result = {}
        for mode in ("title_tags", "title_tags_note", "tags_note"):
            documents = [(row.title + "\n" if mode.startswith("title") else "") + " ".join(row.tags)
                         + ("\n" + row.note if mode.endswith("note") else "") for row in materials]
            vectors = _encode_batch(model, [*gaps, *documents])
            similarities = vectors[:len(gaps)] @ vectors[len(gaps):].T
            rows = []
            for gap, scores in zip(gaps, similarities):
                order = np.argsort(-scores, kind="stable")
                rows.append({"gap": gap, "top": [{"id": materials[int(i)].id, "cos": float(scores[i])} for i in order[:4]],
                             "margin": float(scores[order[0]] - scores[order[1]])})
            result[mode] = rows
        output = Path(__file__).with_name("cosine_probe.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
