import json
import os
import time

import requests as http_requests

from .config import ZHIPU_API_KEY, ZHIPU_API_URL, MODEL, MAX_TURNS
from .tools import TOOLS
from .skills.cell_eval import get_cell_analysis
from .mcp.db_query import execute_query
from .mcp.file_read import read_csv


_TOOL_DISPATCH = {
    "query_cell_analysis": get_cell_analysis,
    "read_csv": read_csv,
    "query_sql": execute_query,
}

_API_TIMEOUT = 180
_MAX_RETRIES = 2


class CellEvalAgent:
    def __init__(self):
        self.tools = TOOLS
        self.system_prompt = self._load_claude_md()

    @staticmethod
    def _load_claude_md():
        md_path = os.path.join(os.path.dirname(__file__), "CLAUDE.md")
        with open(md_path, "r") as f:
            return f.read()

    def evaluate(self, gnbid, ci, indoor):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"请评估小区 GNBID={gnbid} CI={ci} indoor={indoor}"},
        ]
        for _ in range(MAX_TURNS):
            resp = self._call_glm(messages)
            choice = resp["choices"][0]
            msg = choice["message"]
            messages.append(msg)

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return msg.get("content", "")

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                result = self._execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        return "分析超时，未能完成评估"

    def _call_glm(self, messages):
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = http_requests.post(
                    ZHIPU_API_URL,
                    headers={"Authorization": f"Bearer {ZHIPU_API_KEY}", "Content-Type": "application/json"},
                    json={"model": MODEL, "messages": messages, "tools": self.tools, "tool_choice": "auto"},
                    timeout=_API_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()
            except (http_requests.exceptions.Timeout, http_requests.exceptions.ConnectionError) as e:
                last_err = e
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(2 * (attempt + 1))
        raise last_err

    @staticmethod
    def _execute_tool(name, args):
        fn = _TOOL_DISPATCH.get(name)
        if not fn:
            return {"error": f"未知工具: {name}"}
        try:
            return fn(**args)
        except Exception as e:
            return {"error": str(e)}
