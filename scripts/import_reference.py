"""Inspect the restricted contest spreadsheets locally and export source-index metadata."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from zhiyu.catalog import ROUTES, find_source, read_rules
from zhiyu.knowledge import Knowledge


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def inspect_source(folder, workbook=None):
    rule_file = workbook or find_source(folder)
    if rule_file is None:
        raise ValueError("未找到资审关单场景话术 V1.0 文件")
    qa_file = folder / "驳回外呼QA.xlsx"
    rules = read_rules(rule_file)
    sources = [{"row": rule.row, "categories": list(rule.categories), "route": rule.route,
                "script_exists": True, "script_cell": rule.script_cell} for rule in rules]
    records = {rule.row: rule for rule in rules}
    knowledge = Knowledge.load()
    for case in knowledge.cases.values():
        if case.scene_row in records and ROUTES[records[case.scene_row].route] != case.route:
            raise ValueError(f"{case.id} 的源表路线和场景配置冲突")
    files = {rule_file.name: digest(rule_file)}
    sheets = {}
    if qa_file.is_file():
        qa = load_workbook(qa_file, read_only=True, data_only=True)
        try:
            sheets = {sheet.title: sum(any(cell is not None and str(cell).strip() for cell in row)
                                      for row in sheet.iter_rows(min_row=2, values_only=True)) for sheet in qa}
        finally:
            qa.close()
        files[qa_file.name] = digest(qa_file)
    return {"rows": sources, "counts": dict(Counter(row["route"] for row in sources)),
            "aq_rows": sheets, "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(".."))
    parser.add_argument("--workbook", type=Path, help="指定「场景汇总」原表路径")
    parser.add_argument("--output", type=Path, default=Path("runtime/reference-index.json"))
    args = parser.parse_args()
    data = inspect_source(args.source, args.workbook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"资料校验完成 {data['counts']}，详情位于 {args.output}")
