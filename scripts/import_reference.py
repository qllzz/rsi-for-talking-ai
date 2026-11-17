"""Inspect the restricted contest spreadsheets locally and export source-index metadata."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from zhiyu.knowledge import Knowledge


def value(table, row, col):
    item = table.cell(row, col).value
    if item is None:
        for area in table.merged_cells.ranges:
            if area.min_row <= row <= area.max_row and area.min_col <= col <= area.max_col:
                item = table.cell(area.min_row, area.min_col).value
                break
    return str(item or "").strip()


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def inspect_source(folder):
    rules = list(folder.glob("*资审*V1.0.xlsx"))
    if len(rules) != 1:
        raise ValueError("需要唯一的资审关单场景话术 V1.0 文件")
    rule_file = rules[0]
    qa_file = folder / "驳回外呼QA.xlsx"
    rules_sheet = load_workbook(rule_file, data_only=True)["场景汇总"]
    sources = []
    for row in range(1, rules_sheet.max_row + 1):
        route = value(rules_sheet, row, 4)
        if route not in ("电话", "京麦驳回"):
            continue
        keys = [value(rules_sheet, row, index) for index in range(1, 4)]
        sources.append({"row": row, "categories": keys, "route": route,
                        "script_exists": bool(value(rules_sheet, row, 5))})
    records = {item["row"]: item for item in sources}
    knowledge = Knowledge.load()
    for case in knowledge.cases.values():
        if records[case.scene_row]["route"] != {"phone": "电话", "jingmai": "京麦驳回"}[case.route]:
            raise ValueError(f"{case.id} 的源表路线和场景配置冲突")
    qa = load_workbook(qa_file, read_only=True, data_only=True)
    sheets = {sheet.title: sum(any(cell is not None and str(cell).strip() for cell in row)
                              for row in sheet.iter_rows(min_row=2, values_only=True)) for sheet in qa}
    return {"rows": sources, "counts": dict(Counter(row["route"] for row in sources)), "aq_rows": sheets,
            "files": {file.name: digest(file) for file in (rule_file, qa_file)}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(".."))
    parser.add_argument("--output", type=Path, default=Path("runtime/reference-index.json"))
    args = parser.parse_args()
    data = inspect_source(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"资料校验完成 {data['counts']}，详情位于 {args.output}")
