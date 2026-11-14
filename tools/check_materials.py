"""校验 M8a 材料库，并用 curl 逐条实测链接；离线结果明确标 unverified。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import shutil
import subprocess
import sys
import warnings

# 允许直接 python tools/check_materials.py，不依赖调用者设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app.learning_resources import ALLOWED_DOMAINS, MATERIALS_PATH, Material, load_materials, validate_external_locator


def validate_quality_policy(path: Path) -> None:
    """硬性要求材料库声明评分口径且每条已评 1~5 分；0 分直接失败。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"无法读取材料库评分信息：{type(exc).__name__}") from exc
    # 不给口径或分值填默认值，否则占位数据仍可能一路通过验收。
    if not isinstance(payload, dict):
        raise ValueError("材料库必须为对象")
    rubric = payload.get("quality_rubric")
    if not isinstance(rubric, str) or not rubric.strip():
        raise ValueError("quality_rubric 必须存在且为非空字符串")
    rows = payload.get("materials")
    if not isinstance(rows, list):
        raise ValueError("materials 必须为列表")
    for row in rows:
        # bool、浮点数和字符串同样不能作为已经完成的整数人工评分。
        quality = row.get("quality") if isinstance(row, dict) else None
        if type(quality) is not int or not 1 <= quality <= 5:
            identifier = row.get("id", "未知材料") if isinstance(row, dict) else "未知材料"
            raise ValueError(f"{identifier}：quality 必须为 1~5 的整数；0 视为未评分，直接失败")


def probe_link(material: Material, *, offline: bool = False, timeout: float = 15) -> dict:
    """只探测给定材料地址；逐跳校验重定向，返回真实状态或未核实原因。"""
    result = dict(id=material.id, locator=material.locator, status_code=None,
                  status="unverified", redirects=[], reason="")
    if material.kind == "local":
        # load_materials 已检查文件存在；本地路径不需要 HTTP 也不标为 verified。
        return {**result, "status": "local", "reason": "本地文件存在，无 HTTP 状态码"}
    executable = shutil.which("curl")
    if offline or executable is None:
        return {**result, "reason": "--offline" if offline else "curl 不可用"}
    locator = material.locator  # 仅访问材料库中的起点及校验后的重定向目标。
    for _ in range(6):
        try:
            # 每一跳访问前检查官方主机；不自动抓取页面或发现新链接。
            validate_external_locator(locator)
            command = [executable, "--silent", "--show-error", "--output", os.devnull,
                       "--write-out", "%{json}", "--proto", "=https",
                       "--connect-timeout", str(min(timeout, 5)), "--max-time", str(timeout), locator]
            completed = subprocess.run(command, capture_output=True, encoding="utf-8",
                                       errors="replace", timeout=timeout + 2)
            data = json.loads(completed.stdout)
            code = int(data.get("http_code", data.get("response_code", 0)))  # 兼容 curl JSON 字段。
            result.update(status_code=code or None, effective_url=locator)
            if completed.returncode or not code:
                # 000、DNS/代理/证书错误都只是未核实，绝不能当作 HTTP 200。
                result["reason"] = f"curl exit={completed.returncode}；未取得成功响应"
                return result
            if 300 <= code < 400 and data.get("redirect_url"):
                result["redirects"].append({"url": locator, "status_code": code})
                locator = data["redirect_url"]
                continue
            result.update(status="verified" if code == 200 else "failed", reason=f"HTTP {code}")
            return result
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            # 不输出完整代理配置或环境变量；保留可复核的异常类型与安全原因。
            result["reason"] = f"{type(exc).__name__}：无法完成链接核验"
            return result
    return {**result, "status": "failed", "reason": "重定向超过 5 次"}


def main() -> int:
    """打印结构和每条地址的真实检查结果，输出通过数并返回相应退出码。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=MATERIALS_PATH)  # 也可审计待导入的候选库。
    parser.add_argument("--offline", action="store_true", help="完全禁止联网，所有外链标 unverified")
    parser.add_argument("--timeout", type=float, default=15)  # 每跳有上限，离线环境不会无限等待。
    parser.add_argument("--report", type=Path, help="保存本次实际结果的 JSON，不修改材料库")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于零")
    try:
        # 先执行已评分硬断言；非法库必须在任何联网之前退出失败。
        validate_quality_policy(args.path)
        # 重复标签 warning 单独展示，不吞掉也不冒充字段校验失败。
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            materials, version = load_materials(args.path)
    except ValueError as exc:
        print(f"结构校验：失败；{exc}")
        print("通过 0/1")
        return 1
    print(f"结构校验：通过；version={version}；材料 {len(materials)} 条")
    print("评分校验：通过；全部 quality 为 1~5 整数，0 分未评分直接失败")
    print("评分口径：通过；quality_rubric 存在且非空")
    count_ok = 18 <= len(materials) <= 21  # 保留十二条原材料并追加 FDE 能力材料。
    # 本脚本针对 M8a 种子库，数量不达用户明确要求时不能报全绿。
    print(f"材料数量：{'通过' if count_ok else '失败'}；要求 18~21 条")
    # 新条目必须是已核实白名单内的外链；不给非法域名或未评分材料补默认值。
    fde = [row for row in materials if row.id.startswith("fde-")]
    fde_ok = 6 <= len(fde) <= 8 and all(row.kind == "external" and
        urlsplit(row.locator).hostname in ALLOWED_DOMAINS and 2 <= len(row.tags) <= 4
        and 1 <= row.quality <= 5 for row in fde)
    print(f"FDE材料/域名：{'通过' if fde_ok else '失败'}；新增 {len(fde)} 条，外链域名均在 ALLOWED_DOMAINS 内")
    for notice in notices:
        print(f"warning：{notice.message}")
    # executor.map 保留原库顺序；每条结果都打印，单个站点失败不阻断其他站点。
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda row: probe_link(row, offline=args.offline, timeout=args.timeout), materials))
    passed = 3 + int(count_ok) + int(fde_ok)  # 结构、评分、口径、数量及 FDE 域名各计一项。
    for row in results:
        code = row["status_code"] if row["status_code"] is not None else "无"
        print(f"{row['id']} | HTTP {code} | {row['status']} | {row['locator']} | {row['reason']}")
        # 离线链接不进入通过数，汇总分母仍保留全部待核验项，避免假全过。
        passed += row["status"] in ("verified", "local")
    unverified = sum(row["status"] == "unverified" for row in results)  # 历史日期不抵扣本次未核实项。
    if unverified:
        print(f"联网检查未完成：{unverified} 条 unverified（离线或连接不可用，未计为通过）")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        # 时间戳仅进入独立的实测证据，不污染 LearningPlan 的确定性输出。
        report = dict(checked_at=datetime.now(timezone.utc).isoformat(), version=version,
                      count=len(materials), results=results)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"通过 {passed}/{len(materials) + 5}")
    # 2 表示结构通过但联网尚未完成；403/404 等有明确响应的失败则返回 1。
    if not count_ok or not fde_ok or any(row["status"] == "failed" for row in results):
        return 1
    return 2 if unverified else 0


if __name__ == "__main__":
    # 保留退出码，供 CI 区分通过、真实失败和离线未完成。
    raise SystemExit(main())
