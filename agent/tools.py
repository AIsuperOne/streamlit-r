TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_cell_analysis",
            "description": "查询小区完整6维评估数据：业务量、覆盖面积、覆盖最优性、重叠覆盖、散点覆盖率、栅格优良率，返回全部指标和判定结果，包含散点统计、栅格统计和波束分析数据",
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
