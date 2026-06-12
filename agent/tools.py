TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_cell_analysis",
            "description": "查询小区完整评估数据：7维评估(业务水平/面积水平/正对用户/正对栅格/主服覆盖/散点覆盖率/栅格优良率) + 5项射频专项(覆盖不足/越区覆盖/背向覆盖/精准覆盖/重叠覆盖)，返回层次化结构数据，包含scale/structure/quality/rf/stats五个分组",
            "parameters": {
                "type": "object",
                "properties": {
                    "gnbid": {"type": "string", "description": "基站GNBID"},
                    "ci": {"type": "string", "description": "小区CI"},
                    "indoor": {"type": "string", "description": "室内外筛选：0=全部，1=室内，2=室外", "enum": ["0", "1", "2"]}
                },
                "required": ["gnbid", "ci", "indoor"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_csv",
            "description": "读取output目录下的CSV数据文件，用于获取散点或栅格的详细数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "CSV文件名，如scatter_6311936_1.csv或grid_6311936_1.csv"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": "执行自定义SQL查询数据库，用于获取工具未覆盖的数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL查询语句，仅支持SELECT"}
                },
                "required": ["sql"]
            }
        }
    }
]
