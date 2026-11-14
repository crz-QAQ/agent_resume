"""按任务指定 curl 参数实测候选域名与材料，不以网页搜索结果代替本机可达性。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]
DOMAINS = ["python.langchain.com", "huggingface.co", "www.promptingguide.ai", "docs.trychroma.com",
           "www.anthropic.com", "docs.llamaindex.ai", "learn.microsoft.com"]
LINKS = {
    "fde-rag": "https://huggingface.co/learn/cookbook/advanced_rag",
    "fde-evaluation": "https://huggingface.co/learn/cookbook/rag_evaluation",
    "fde-requirements": "https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ai/plan",
    "fde-prompt-engineering": "https://www.promptingguide.ai/introduction/tips",
    "fde-agent-workflows": "https://www.anthropic.com/research/building-effective-agents",
    "fde-knowledge-base": "https://docs.dify.ai/en/cloud/use-dify/knowledge/overview",
    "fde-delivery-acceptance": "https://learn.microsoft.com/en-us/azure/well-architected/ai/test",
    "fde-vector-search": "https://docs.trychroma.com/docs/overview/getting-started",
}


def probe(item):
    """GET 并跟随重定向，10 秒上限；Windows 的 os.devnull 等价于 /dev/null。"""
    key, url = item
    command = ["curl.exe", "-sL", "-o", os.devnull, "-w", "%{http_code}", "-m", "10", url]
    result = subprocess.run(command, capture_output=True, text=True, timeout=12)
    return {"id": key, "url": url, "status_code": result.stdout.strip(), "curl_exit": result.returncode}


def main():
    """保存全部结果以及修改前的十二条材料快照。"""
    snapshot = OUTPUT / "original_materials.json"
    if not snapshot.exists():
        snapshot.write_bytes((ROOT / "materials/materials.json").read_bytes())
    with ThreadPoolExecutor(max_workers=4) as executor:
        domains = list(executor.map(probe, [(domain, f"https://{domain}/") for domain in DOMAINS]))
        links = list(executor.map(probe, LINKS.items()))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "command": 'curl -sL -o /dev/null -w "%{http_code}" -m 10 URL',
              "windows_null_device": "nul", "domains": domains, "materials": links}
    (OUTPUT / "curl_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"{row['id']} | HTTP {row['status_code']} | curl exit {row['curl_exit']} | {row['url']}"
             for row in [*domains, *links]]
    (OUTPUT / "curl_probe.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
