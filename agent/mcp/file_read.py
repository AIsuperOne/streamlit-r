import csv
import os

from ..config import OUTPUT_DIR


def read_csv(filename):
    """读取output目录下的CSV文件，返回前100行"""
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        return {"error": f"文件不存在: {filename}"}
    if not filepath.startswith(OUTPUT_DIR):
        return {"error": "不允许访问output目录以外的文件"}
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return {"filename": filename, "rows": rows[:100], "total": len(rows)}
