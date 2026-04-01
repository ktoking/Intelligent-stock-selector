#!/usr/bin/env python3
"""
解析 cif1_ddl.sql，提取所有 _bak 表的字段定义，生成可导入 cif_back_up sheet 的 CSV。
参考 Sheet4 配置列格式：No, App Name, Database Name, Table Name Matches, Column Name Matches, Reason, Column Description, Remark
注：cif1~cif5 需分别录入，每个 (表, 列) 生成 5 行（cif1~cif5）
"""
import re
import csv
import sys
from pathlib import Path

DDL_PATH = Path("/Users/kaiyi.wang/IdeaProjects/sg-npt/databasescript/v2.79_0105/corebank-01/init/DDL/cif1_ddl.sql")
OUTPUT_CSV = Path(__file__).resolve().parent.parent / "data" / "cif_back_up_sheet4_format.csv"
CIF_DBS = ["cif1", "cif2", "cif3", "cif4", "cif5"]


def parse_field_line(line: str) -> dict | None:
    """解析单行字段定义，返回 {field_name, comment}"""
    line = line.strip().rstrip(",")
    if not line or line.startswith("PRIMARY") or line.startswith("UNIQUE") or line.startswith("KEY ") or line.startswith(")"):
        return None

    match = re.match(r"`([^`]+)`\s+\w+(?:\([^)]+\))?\s+(.*)", line)
    if not match:
        return None

    field_name = match.group(1)
    rest = match.group(2)
    comment_match = re.search(r"COMMENT\s+['\"]([^'\"]*)['\"]", rest, re.IGNORECASE)
    comment = comment_match.group(1).strip() if comment_match else ""
    return {"field_name": field_name, "comment": comment}


def parse_ddl(content: str) -> list[tuple[str, str, str]]:
    """返回 [(table_name, field_name, comment), ...]"""
    results = []
    table_match = re.finditer(
        r"CREATE TABLE `([^`]+)`\s*\((.*?)\)\s*ENGINE=",
        content,
        re.DOTALL | re.IGNORECASE,
    )

    for m in table_match:
        table_name = m.group(1)
        if not table_name.endswith("_bak"):
            continue
        body = m.group(2)
        for line in body.split("\n"):
            parsed = parse_field_line(line)
            if parsed:
                results.append((table_name, parsed["field_name"], parsed["comment"]))
    return results


def main():
    ddl_path = DDL_PATH
    if len(sys.argv) > 1:
        ddl_path = Path(sys.argv[1])

    if not ddl_path.exists():
        print(f"错误: DDL 文件不存在: {ddl_path}", file=sys.stderr)
        sys.exit(1)

    content = ddl_path.read_text(encoding="utf-8")
    fields = parse_ddl(content)

    output_path = OUTPUT_CSV
    if len(sys.argv) > 2:
        output_path = Path(sys.argv[2])

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 按 Sheet4 格式：每个 (表, 列) 对 cif1~cif5 各生成一行
    rows = []
    no = 1
    for table_name, col_name, comment in fields:
        for db in CIF_DBS:
            rows.append({
                "No": no,
                "App Name": "Maribank DBPortal",
                "Database Name": db,
                "Table Name Matches": table_name,
                "Column Name Matches": col_name,
                "Reason": "Added as part of BAU v2.79_0105 (CIF)",
                "Column Description": comment,
                "Remark": "cif",
            })
            no += 1

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "No", "App Name", "Database Name", "Table Name Matches",
            "Column Name Matches", "Reason", "Column Description", "Remark"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"已生成 {len(rows)} 行（{len(fields)} 字段 × 5 库），输出: {output_path}")
    print("打开 cif_back_up sheet，从第 2 行起粘贴 CSV 数据（第 1 行为表头）。")


if __name__ == "__main__":
    main()
