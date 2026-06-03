import sqlite3

from ..config import DB_PATH


def execute_query(sql, params=None):
    """执行SQL查询，返回结果列表"""
    if not sql.strip().upper().startswith("SELECT"):
        return {"error": "仅支持SELECT查询"}
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(sql, params or [])
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"columns": columns, "rows": rows[:200], "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()
