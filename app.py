import json
import math
import random
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from shapely.geometry import Polygon, Point

import streamlit as st
import streamlit.components.v1 as components

BASE_DIR = __file__.rsplit("/", 1)[0]
DB_PATH = "/Users/supermac/Documents/Project-AI/work-c/ZtoSQL/xianning.db"
BEAM_WIDTH = 60
SECTOR_RADIUS_M = 50
API_PORT = 8503

AMAP_KEY = "99934c3e39ece2d89f8211c83db7d0e3"
AMAP_SECURITY_CODE = "8a2ed5853e1c71ed20e8cd98ef24d726"

COLOR_MAP = {"700M": "#FFD700", "700MHz": "#FFD700"}

RSRP_GRADES = [
    {"label": "优", "range": "≥-95", "color": "#2ECC71", "cond": ">= -95"},
    {"label": "中", "range": "-105~-95", "color": "#F1C40F", "cond": ">= -105 AND {col} < -95"},
    {"label": "差", "range": "-115~-105", "color": "#E67E22", "cond": ">= -115 AND {col} < -105"},
    {"label": "极差", "range": "<-115", "color": "#E74C3C", "cond": "< -115"},
]

SINR_GRADES = [
    {"label": "优", "range": "≥15", "color": "#2ECC71", "cond": ">= 15"},
    {"label": "中", "range": "5~15", "color": "#F1C40F", "cond": ">= 5 AND {col} < 15"},
    {"label": "差", "range": "-3~5", "color": "#E67E22", "cond": ">= -3 AND {col} < 5"},
    {"label": "极差", "range": "<-3", "color": "#E74C3C", "cond": "< -3"},
]

PCT_GRADES = [
    {"label": "低覆盖", "range": "0~20%", "color": "#E74C3C"},
    {"label": "中覆盖", "range": "20~50%", "color": "#F1C40F"},
    {"label": "中高覆盖", "range": "50~80%", "color": "#3498DB"},
    {"label": "高覆盖", "range": "80~100%", "color": "#2ECC71"},
]


# --- Local API server for measurement data ---

_api_server_started = False


class MeasureAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        params = parse_qs(urlparse(self.path).query)
        gnbid = params.get("gnbid", [""])[0]
        ci = params.get("ci", [""])[0]
        indoor = params.get("indoor", ["0"])[0]

        if path == "/api/grid_analysis":
            self._handle_grid_analysis(gnbid, ci, indoor)
            return

        if path == "/api/grid_cell_detail":
            origin_lng = params.get("origin_lng", ["0"])[0]
            origin_lat = params.get("origin_lat", ["0"])[0]
            cell_lng = params.get("cell_lng", ["0"])[0]
            cell_lat = params.get("cell_lat", ["0"])[0]
            gx = params.get("gx", ["0"])[0]
            gy = params.get("gy", ["0"])[0]
            freq = params.get("freq", [""])[0]
            self._handle_grid_cell_detail(origin_lng, origin_lat, cell_lng, cell_lat, gx, gy, indoor, freq)
            return

        if path == "/api/point_detail":
            lng = params.get("lng", ["0"])[0]
            lat = params.get("lat", ["0"])[0]
            self._handle_point_detail(gnbid, ci, lng, lat, indoor)
            return

        self._handle_measurements(gnbid, ci, params, indoor)

    def _handle_measurements(self, gnbid, ci, params, indoor):
        metric = params.get("metric", ["rsrp"])[0]

        conn = sqlite3.connect(DB_PATH)
        try:
            val_col = "nr_ssb_rsrp" if metric == "rsrp" else "nr_ssb_sinr"

            where = 'gnbid=? AND ci=? AND longitude!="" AND latitude!="" AND ' + val_col + '!=""'
            q_params = [gnbid, ci]

            if indoor == "1":
                where += ' AND in_out_door="In_Door"'
            elif indoor == "2":
                where += ' AND in_out_door="Out_Door"'

            stat_row = conn.execute(
                f'SELECT COUNT(*), AVG(CAST({val_col} AS REAL)), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-105 AND CAST({val_col} AS REAL)<-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-115 AND CAST({val_col} AS REAL)<-105 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)<-115 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}',
                q_params,
            ).fetchone() if metric == "rsrp" else conn.execute(
                f'SELECT COUNT(*), AVG(CAST({val_col} AS REAL)), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=5 AND CAST({val_col} AS REAL)<15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-3 AND CAST({val_col} AS REAL)<5 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)<-3 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}',
                q_params,
            ).fetchone()
            total_count = stat_row[0] or 0
            avg_val = round(stat_row[1], 2) if stat_row[1] is not None else None
            grade_counts = [stat_row[2], stat_row[3], stat_row[4], stat_row[5]]

            rows = conn.execute(
                f'SELECT longitude, latitude, nr_ssb_rsrp, nr_ssb_sinr FROM "data" WHERE {where} LIMIT 5000',
                q_params,
            ).fetchall()
        finally:
            conn.close()

        idx = 2 if metric == "rsrp" else 3
        points = []
        for r in rows:
            if not r[0] or not r[1]:
                continue
            try:
                points.append([float(r[0]), float(r[1]), float(r[idx])])
            except (ValueError, TypeError):
                pass

        result = {"points": points, "count": total_count, "avg": avg_val, "grades": grade_counts}
        self._send_json(result)

    def _handle_grid_analysis(self, gnbid, ci, indoor):
        # 查询小区频点
        freq = ""
        conn = sqlite3.connect(DB_PATH)
        try:
            freq_row = conn.execute(
                'SELECT "频点" FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
                [gnbid, ci],
            ).fetchone()
            if freq_row and freq_row[0]:
                freq = str(freq_row[0])
        finally:
            conn.close()

        # 阶段1：查询小区采样点
        conn = sqlite3.connect(DB_PATH)
        try:
            indoor_clause = ""
            if indoor == "1":
                indoor_clause = ' AND in_out_door="In_Door"'
            elif indoor == "2":
                indoor_clause = ' AND in_out_door="Out_Door"'
            coord_clause = ' AND longitude!="" AND latitude!=""'

            cell_where = 'gnbid=? AND ci=? AND plmn="46000" AND nr_ssb_rsrp!="" AND nr_ssb_rsrp!="0" AND nr_ssb_sinr!="" AND nr_ssb_sinr!="0"' + coord_clause + indoor_clause
            if freq:
                cell_where += ' AND nr_earfcn=?'
                cell_rows = conn.execute(
                    f'SELECT longitude, latitude FROM "data" WHERE {cell_where}',
                    [gnbid, ci, freq],
                ).fetchall()
            else:
                cell_rows = conn.execute(
                    f'SELECT longitude, latitude FROM "data" WHERE {cell_where}',
                    [gnbid, ci],
                ).fetchall()
        finally:
            conn.close()

        cell_pts = []
        for r in cell_rows:
            try:
                cell_pts.append((float(r[0]), float(r[1])))
            except (ValueError, TypeError):
                pass

        if not cell_pts:
            self._send_json({"squares": [], "grid_params": None, "total_count": 0, "total_avg_rsrp": None, "grid_count": 0})
            return

        # 阶段2：构建栅格Polygon（Shapely）
        avg_lat = sum(p[1] for p in cell_pts) / len(cell_pts)
        cell_lng = 5.0 / 111320.0 / math.cos(avg_lat * math.pi / 180.0)
        cell_lat = 5.0 / 110540.0

        random.seed(f"{gnbid}_{ci}")
        ref_idx = random.randint(0, len(cell_pts) - 1)
        origin_lng = cell_pts[ref_idx][0] - cell_lng / 2
        origin_lat = cell_pts[ref_idx][1] - cell_lat / 2

        # 小区采样点落入栅格
        cell_grid = {}
        for lng, lat in cell_pts:
            gx = math.floor((lng - origin_lng) / cell_lng)
            gy = math.floor((lat - origin_lat) / cell_lat)
            cell_grid[(gx, gy)] = cell_grid.get((gx, gy), 0) + 1

        # 构建Shapely Polygon列表
        polygons = []
        for (gx, gy), count in cell_grid.items():
            sw_lng = origin_lng + gx * cell_lng
            sw_lat = origin_lat + gy * cell_lat
            ne_lng = origin_lng + (gx + 1) * cell_lng
            ne_lat = origin_lat + (gy + 1) * cell_lat
            poly = Polygon([(sw_lng, sw_lat), (ne_lng, sw_lat), (ne_lng, ne_lat), (sw_lng, ne_lat)])
            polygons.append({
                "gx": gx, "gy": gy,
                "polygon": poly,
                "cell_count": count,
                "plmn_count": 0,
            })

        # 阶段3：cell_count已在阶段2获得

        # 阶段4：通过Shapely Polygon包含关系统计总采样点数
        all_bounds = [p["polygon"].bounds for p in polygons]
        bounds_sw_lng = min(b[0] for b in all_bounds)
        bounds_sw_lat = min(b[1] for b in all_bounds)
        bounds_ne_lng = max(b[2] for b in all_bounds)
        bounds_ne_lat = max(b[3] for b in all_bounds)

        conn = sqlite3.connect(DB_PATH)
        try:
            plmn_where = 'plmn="46000" AND nr_ssb_rsrp!="" AND nr_ssb_rsrp!="0" AND nr_ssb_sinr!="" AND nr_ssb_sinr!="0"' + coord_clause + indoor_clause
            if freq:
                plmn_where += ' AND nr_earfcn=?'
            plmn_where += ' AND CAST(longitude AS REAL)>=? AND CAST(longitude AS REAL)<=? AND CAST(latitude AS REAL)>=? AND CAST(latitude AS REAL)<=?'
            bounds_params = [bounds_sw_lng, bounds_ne_lng, bounds_sw_lat, bounds_ne_lat]
            if freq:
                plmn_params = [freq] + bounds_params
            else:
                plmn_params = bounds_params

            total_row = conn.execute(
                f'SELECT COUNT(*), AVG(CAST(nr_ssb_rsrp AS REAL)) FROM "data" WHERE {plmn_where}',
                plmn_params,
            ).fetchone()
            total_count = total_row[0] or 0
            total_avg = round(total_row[1], 2) if total_row[1] is not None else None

            plmn_rows = conn.execute(
                f'SELECT longitude, latitude FROM "data" WHERE {plmn_where}',
                plmn_params,
            ).fetchall()
        finally:
            conn.close()

        # Shapely Polygon.covers 判定包含关系
        poly_map = {(p["gx"], p["gy"]): p for p in polygons}
        for r in plmn_rows:
            try:
                lng, lat = float(r[0]), float(r[1])
            except (ValueError, TypeError):
                continue
            pt = Point(lng, lat)
            gx = math.floor((lng - origin_lng) / cell_lng)
            gy = math.floor((lat - origin_lat) / cell_lat)
            key = (gx, gy)
            if key in poly_map and poly_map[key]["polygon"].covers(pt):
                poly_map[key]["plmn_count"] += 1

        # 阶段5：计算覆盖率并构建结果
        squares = []
        for p in polygons:
            pct = round(p["cell_count"] / p["plmn_count"] * 100) if p["plmn_count"] > 0 else 0
            bounds = p["polygon"].bounds
            squares.append({
                "sw_lng": bounds[0],
                "sw_lat": bounds[1],
                "ne_lng": bounds[2],
                "ne_lat": bounds[3],
                "gx": p["gx"],
                "gy": p["gy"],
                "count": p["cell_count"],
                "plmn_count": p["plmn_count"],
                "pct": pct,
            })

        result = {
            "squares": squares,
            "grid_params": {
                "origin_lng": origin_lng,
                "origin_lat": origin_lat,
                "cell_lng": cell_lng,
                "cell_lat": cell_lat,
                "freq": freq,
            },
            "total_count": total_count,
            "total_avg_rsrp": total_avg,
            "grid_count": len(squares),
        }
        self._send_json(result)

    def _handle_grid_cell_detail(self, origin_lng, origin_lat, cell_lng, cell_lat, gx, gy, indoor, freq):
        # 从栅格参数重建同一个Shapely Polygon
        origin_lng = float(origin_lng)
        origin_lat = float(origin_lat)
        cell_lng = float(cell_lng)
        cell_lat = float(cell_lat)
        gx = int(gx)
        gy = int(gy)
        sw_lng = origin_lng + gx * cell_lng
        sw_lat = origin_lat + gy * cell_lat
        ne_lng = origin_lng + (gx + 1) * cell_lng
        ne_lat = origin_lat + (gy + 1) * cell_lat
        poly = Polygon([(sw_lng, sw_lat), (ne_lng, sw_lat), (ne_lng, ne_lat), (sw_lng, ne_lat)])

        conn = sqlite3.connect(DB_PATH)
        try:
            where = 'plmn="46000" AND nr_ssb_rsrp!="" AND nr_ssb_rsrp!="0" AND nr_ssb_sinr!="" AND nr_ssb_sinr!="0" AND longitude!="" AND latitude!=""'
            if indoor == "1":
                where += ' AND in_out_door="In_Door"'
            elif indoor == "2":
                where += ' AND in_out_door="Out_Door"'
            if freq:
                where += ' AND nr_earfcn=?'
            where += ' AND CAST(longitude AS REAL)>=? AND CAST(longitude AS REAL)<=? AND CAST(latitude AS REAL)>=? AND CAST(latitude AS REAL)<=?'
            q_params = []
            if freq:
                q_params.append(freq)
            q_params.extend([sw_lng, ne_lng, sw_lat, ne_lat])
            rows = conn.execute(
                f'SELECT longitude, latitude, plmn, gnbid, ci, nr_earfcn, CAST(nr_ssb_rsrp AS REAL), CAST(nr_ssb_sinr AS REAL) '
                f'FROM "data" WHERE {where}',
                q_params,
            ).fetchall()
        finally:
            conn.close()

        # Shapely Polygon.covers 判定包含关系，与grid_analysis完全一致
        groups = {}
        for r in rows:
            try:
                lng, lat = float(r[0]), float(r[1])
            except (ValueError, TypeError):
                continue
            if not poly.covers(Point(lng, lat)):
                continue
            key = (r[2], r[3], r[4])  # plmn, gnbid, ci
            if key not in groups:
                groups[key] = {"nr_earfcn": r[5] or "", "rsrp_sum": 0.0, "sinr_sum": 0.0, "count": 0}
            rsrp = r[6] if r[6] else 0.0
            sinr = r[7] if r[7] else 0.0
            groups[key]["rsrp_sum"] += rsrp
            groups[key]["sinr_sum"] += sinr
            groups[key]["count"] += 1

        result = []
        for (plmn, gnbid, ci), info in groups.items():
            avg_rsrp = round(info["rsrp_sum"] / info["count"], 2) if info["count"] > 0 else None
            avg_sinr = round(info["sinr_sum"] / info["count"], 2) if info["count"] > 0 else None
            result.append({"nr_earfcn": info["nr_earfcn"], "gnbid": gnbid, "ci": ci, "avg_rsrp": avg_rsrp, "avg_sinr": avg_sinr, "count": info["count"]})
        self._send_json(result)

    def _handle_point_detail(self, gnbid, ci, lng, lat, indoor):
        conn = sqlite3.connect(DB_PATH)
        try:
            where = 'gnbid=? AND ci=? AND longitude!="" AND latitude!=""'
            q_params = [gnbid, ci]
            if indoor == "1":
                where += ' AND in_out_door="In_Door"'
            elif indoor == "2":
                where += ' AND in_out_door="Out_Door"'
            where += ' AND ABS(CAST(longitude AS REAL)-?)<0.000001 AND ABS(CAST(latitude AS REAL)-?)<0.000001'
            q_params.extend([lng, lat])
            row = conn.execute(
                f'SELECT gnbid, ci, nr_earfcn, nr_ssb_rsrp, nr_ssb_sinr, nr_neighbor_rsrp_list, nr_neighbor_pci_list, nr_neighbor_earfcn_list '
                f'FROM "data" WHERE {where} LIMIT 1',
                q_params,
            ).fetchone()
        finally:
            conn.close()
        if row:
            self._send_json({"gnbid": row[0], "ci": row[1], "nr_earfcn": row[2], "nr_ssb_rsrp": row[3], "nr_ssb_sinr": row[4], "nr_neighbor_rsrp_list": row[5], "nr_neighbor_pci_list": row[6], "nr_neighbor_earfcn_list": row[7]})
        else:
            self._send_json(None)

    def _send_json(self, result):
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_api_server():
    global _api_server_started
    if _api_server_started:
        return
    try:
        server = HTTPServer(("127.0.0.1", API_PORT), MeasureAPI)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        _api_server_started = True
    except OSError:
        pass


# --- Data loading ---

@st.cache_data
def load_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT "小区中文名", "物理站名", "RRU经度", "RRU纬度", "方位角", '
        '"区县", "覆盖类型", "工作频段", "频点", "Gnbid", "Cellid", "天线挂高", "nRPCI" '
        'FROM "5GBaseStation" '
        'WHERE "RRU经度" IS NOT NULL AND "RRU纬度" IS NOT NULL AND "方位角" IS NOT NULL'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- HTML template ---

def build_map_html(sectors_json):
    rsrp_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in RSRP_GRADES])
    sinr_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in SINR_GRADES])
    pct_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in PCT_GRADES])
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; }}
  #container {{ width: 100%; height: 100%; }}
  #panel {{
    position: absolute; top: 10px; left: 10px; z-index: 999;
    background: #fff; border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    width: 280px; max-height: 90vh; overflow-y: auto; font-family: sans-serif;
    padding: 12px; font-size: 13px;
  }}
  #panel input[type=text], #panel input[type=number] {{
    width: 100%; padding: 6px 8px; box-sizing: border-box; border: 1px solid #ccc;
    border-radius: 4px; margin-bottom: 8px;
  }}
  #panel h4 {{ margin: 0 0 8px 0; font-size: 14px; }}
  .radio-group {{ display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }}
  .radio-group label {{
    padding: 3px 8px; border: 1px solid #ccc; border-radius: 4px; cursor: pointer;
    font-size: 12px; background: #f9f9f9;
  }}
  .radio-group label.active {{ background: #1E90FF; color: #fff; border-color: #1E90FF; }}
  .radio-group input {{ display: none; }}
  #custom-radius {{ width: 80px; padding: 3px 6px; border: 1px solid #ccc; border-radius: 4px; font-size: 12px; display: none; }}
  .sel-item {{
    padding: 4px 6px; border-bottom: 1px solid #eee; display: flex;
    justify-content: space-between; align-items: center;
  }}
  .sel-item span {{ flex: 1; font-size: 12px; }}
  .sel-item button {{
    border: none; background: none; color: #e00; cursor: pointer; font-size: 15px;
  }}
  #sel-list {{ max-height: 300px; overflow-y: auto; }}
  #clear-btn {{
    margin-top: 6px; padding: 4px 10px; border: 1px solid #ccc; border-radius: 4px;
    background: #f5f5f5; cursor: pointer; font-size: 12px;
  }}
  #indoor-select {{
    padding: 3px 6px; border: 1px solid #ccc; border-radius: 4px; font-size: 12px;
    margin-bottom: 6px;
  }}
  #measure-section {{ display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid #eee; }}
  .metric-toggle {{ display: flex; gap: 0; margin-bottom: 6px; border: 1px solid #1E90FF; border-radius: 4px; overflow: hidden; }}
  .metric-toggle button {{
    flex: 1; padding: 5px 0; border: none; cursor: pointer; font-size: 12px;
    background: #fff; color: #1E90FF; transition: 0.15s;
  }}
  .metric-toggle button.active {{ background: #1E90FF; color: #fff; }}
  #loading-tip {{ display: none; color: #888; font-size: 12px; margin-top: 4px; }}
  .stat-row {{
    display: flex; justify-content: space-between; padding: 3px 0;
    font-size: 12px; color: #555;
  }}
  .stat-row .stat-val {{ font-weight: bold; color: #333; }}
  .legend-item {{
    display: flex; align-items: center; padding: 3px 0; font-size: 12px;
  }}
  .legend-dot {{
    width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; flex-shrink: 0;
  }}
  .legend-label {{ flex: 1; color: #555; }}
  .legend-count {{ font-weight: bold; color: #333; margin-right: 4px; }}
  .legend-pct {{ color: #888; font-size: 11px; min-width: 42px; text-align: right; }}
</style>
</head>
<body>
<div id="container"></div>
<div id="panel">
  <h4>扇区半径</h4>
  <div class="radio-group" id="radius-group">
    <label class="active"><input type="radio" name="radius" value="50" checked>50米</label>
    <label><input type="radio" name="radius" value="100">100米</label>
    <label><input type="radio" name="radius" value="150">150米</label>
    <label><input type="radio" name="radius" value="custom">自定义</label>
  </div>
  <input id="custom-radius" type="number" min="1" max="5000" placeholder="输入米数">
  <h4>小区搜索</h4>
  <input id="search" type="text" placeholder="输入小区中文名关键字...">
  <div id="search-results"></div>
  <div id="sel-header" style="display:none;"><h4>已选中 (<span id="sel-count">0</span>)</h4></div>
  <div id="sel-list"></div>
  <button id="clear-btn" style="display:none;">全部清除</button>

  <div id="measure-section">
    <h4>测量数据</h4>
    <div class="metric-toggle" style="margin-bottom:4px;">
      <button class="active" id="btn-scatter">散点</button>
      <button id="btn-grid">栅格</button>
    </div>
    <div id="scatter-controls">
    <div class="metric-toggle">
      <button class="active" id="btn-rsrp">RSRP</button>
      <button id="btn-sinr">SINR</button>
      <button id="btn-beam">波束</button>
    </div>
    </div>
    <select id="indoor-select">
      <option value="0">全部</option>
      <option value="1">室内</option>
      <option value="2">室外</option>
    </select>
    <div id="stats-container"></div>
    <div id="legend-container"></div>
    <div id="loading-tip">加载中...</div>
  </div>
</div>
<script>
  window._AMapSecurityConfig = {{ securityJsCode: '{AMAP_SECURITY_CODE}' }};
  var COLOR_MAP = {json.dumps(COLOR_MAP)};
  var BEAM_WIDTH = {BEAM_WIDTH};
  var RADIUS_M = {SECTOR_RADIUS_M};

  var GRADES = {{
    rsrp: {rsrp_grades_json},
    sinr: {sinr_grades_json}
  }};

  var PCT_GRADES = {pct_grades_json};

  var map, sectors = {sectors_json};
  var overlays = {{}}, dataMap = {{}};
  var selected = [];
  var currentMetric = "rsrp";
  var viewMode = "scatter";
  var scatterByCell = {{}};
  var gridByCell = {{}};
  var pendingLoads = 0;
  var pendingGridLoads = 0;
  var scatterLoadSeq = {{}};
  var gridLoadSeq = {{}};
  var activeInfoWindow = null;
  var showTimer = null;
  var hideTimer = null;

  function startShowTimer(type, obj) {{
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
    showTimer = setTimeout(function() {{
      if (type === "grid") {{
        fetchGridDetail(obj);
      }} else {{
        fetchScatterDetail(obj);
      }}
    }}, 2000);
  }}

  function startHideTimer() {{
    clearTimeout(showTimer);
    hideTimer = setTimeout(function() {{
      if (activeInfoWindow) {{
        activeInfoWindow.close();
        activeInfoWindow = null;
      }}
    }}, 2000);
  }}

  function closeActiveInfoWindow() {{
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    if (activeInfoWindow) {{
      activeInfoWindow.close();
      activeInfoWindow = null;
    }}
  }}

  function fetchGridDetail(gp) {{
    var sq = gp.sq;
    var gridParams = gp.gridParams;
    var indoor = document.getElementById("indoor-select").value;
    fetch("http://localhost:{API_PORT}/api/grid_cell_detail?origin_lng=" + gridParams.origin_lng + "&origin_lat=" + gridParams.origin_lat + "&cell_lng=" + gridParams.cell_lng + "&cell_lat=" + gridParams.cell_lat + "&gx=" + sq.gx + "&gy=" + sq.gy + "&freq=" + (gridParams.freq || "") + "&indoor=" + indoor)
      .then(function(r) {{ return r.json(); }})
      .then(function(details) {{
        var html = '<div style="font-size:13px;line-height:1.6;min-width:200px;">';
        html += '<div style="font-weight:bold;margin-bottom:4px;">栅格详情</div>';
        html += '<div>栅格小区采样点: ' + sq.count + '</div>';
        html += '<div>栅格总采样点数: ' + sq.plmn_count + '</div>';
        html += '<div>覆盖率: ' + sq.pct + '%</div>';
        html += '<hr style="margin:4px 0;border:none;border-top:1px solid #ccc;">';
        details.forEach(function(d) {{
          html += '<div>NR频点: ' + d.nr_earfcn + ' | GNBID: ' + d.gnbid + ' | CI: ' + d.ci + '</div>';
          html += '<div>RSRP均值: ' + d.avg_rsrp + ' dBm | SINR均值: ' + d.avg_sinr + ' dB</div>';
          html += '<div>采样点数: ' + d.count + '</div>';
        }});
        html += '</div>';
        if (activeInfoWindow) activeInfoWindow.close();
        activeInfoWindow = new AMap.InfoWindow({{content: html, offset: new AMap.Pixel(0, -10)}});
        activeInfoWindow.open(map, gp.getCenter());
      }});
  }}

  function fetchScatterDetail(marker) {{
    var s = dataMap[marker._cellName];
    var indoor = document.getElementById("indoor-select").value;
    fetch("http://localhost:{API_PORT}/api/point_detail?gnbid=" + encodeURIComponent(s.gnbid) + "&ci=" + encodeURIComponent(s.ci) + "&lng=" + marker._lng + "&lat=" + marker._lat + "&indoor=" + indoor)
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        if (!d) return;
        var html = '<div style="font-size:13px;line-height:1.6;min-width:200px;">';
        html += '<div style="font-weight:bold;margin-bottom:4px;">采样点详情</div>';
        html += '<div>GNBID: ' + d.gnbid + ' | CI: ' + d.ci + '</div>';
        html += '<div>主服务频点: ' + (d.nr_earfcn || '-') + '</div>';
        html += '<div>NR_SSB_RSRP: ' + (d.nr_ssb_rsrp || '-') + ' dBm</div>';
        html += '<div>NR_SSB_SINR: ' + (d.nr_ssb_sinr || '-') + ' dB</div>';
        html += '<div>邻区RSRP: ' + (d.nr_neighbor_rsrp_list || '-') + '</div>';
        html += '<div>邻区PCI: ' + (d.nr_neighbor_pci_list || '-') + '</div>';
        html += '<div>邻区频点: ' + (d.nr_neighbor_earfcn_list || '-') + '</div>';
        html += '</div>';
        if (activeInfoWindow) activeInfoWindow.close();
        activeInfoWindow = new AMap.InfoWindow({{content: html, offset: new AMap.Pixel(0, -10)}});
        activeInfoWindow.open(map, [marker._lng, marker._lat]);
      }});
  }}

  var s = document.createElement("script");
  s.src = "https://webapi.amap.com/maps?v=2.0&key={AMAP_KEY}";
  s.onload = init;
  document.head.appendChild(s);

  function createSectorPath(lng, lat, azimuth) {{
    var latRad = lat * Math.PI / 180;
    var dlng = RADIUS_M / 111320.0 / Math.cos(latRad);
    var dlat = RADIUS_M / 110540.0;
    var start = azimuth - BEAM_WIDTH / 2, end = azimuth + BEAM_WIDTH / 2;
    var path = [new AMap.LngLat(lng, lat)];
    for (var a = start; a <= end; a += 2) {{
      var rad = a * Math.PI / 180;
      path.push(new AMap.LngLat(lng + dlng * Math.sin(rad), lat + dlat * Math.cos(rad)));
    }}
    var radEnd = end * Math.PI / 180;
    path.push(new AMap.LngLat(lng + dlng * Math.sin(radEnd), lat + dlat * Math.cos(radEnd)));
    path.push(new AMap.LngLat(lng, lat));
    return path;
  }}

  function getColor(s) {{ return COLOR_MAP[s.band] || "#1E90FF"; }}

  function rsrpColor(v) {{
    if (v >= -95) return "#2ECC71";
    if (v >= -105) return "#F1C40F";
    if (v >= -115) return "#E67E22";
    return "#E74C3C";
  }}

  function sinrColor(v) {{
    if (v >= 15) return "#2ECC71";
    if (v >= 5) return "#F1C40F";
    if (v >= -3) return "#E67E22";
    return "#E74C3C";
  }}

  function pctColor(pct) {{
    if (pct >= 80) return "#2ECC71";
    if (pct >= 50) return "#3498DB";
    if (pct >= 20) return "#F1C40F";
    return "#E74C3C";
  }}

  function calcBearing(lng1, lat1, lng2, lat2) {{
    var dLng = (lng2 - lng1) * Math.PI / 180;
    var lat1R = lat1 * Math.PI / 180;
    var lat2R = lat2 * Math.PI / 180;
    var y = Math.sin(dLng) * Math.cos(lat2R);
    var x = Math.cos(lat1R) * Math.sin(lat2R) - Math.sin(lat1R) * Math.cos(lat2R) * Math.cos(dLng);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }}

  function isInBeam(bearing, azimuth) {{
    var diff = ((bearing - azimuth + 180 + 360) % 360) - 180;
    return Math.abs(diff) <= 60;
  }}

  function beamColor(lng, lat, s) {{
    var b = calcBearing(s.lng, s.lat, lng, lat);
    return isInBeam(b, s.azimuth) ? "#2ECC71" : "#E74C3C";
  }}

  function GridPolygon(sq, gridParams) {{
    this.sq = sq;
    this.gridParams = gridParams;
    var color = pctColor(sq.pct);
    this.polygon = new AMap.Polygon({{
      path: [
        [sq.sw_lng, sq.sw_lat],
        [sq.ne_lng, sq.sw_lat],
        [sq.ne_lng, sq.ne_lat],
        [sq.sw_lng, sq.ne_lat]
      ],
      fillColor: color,
      fillOpacity: 0.6,
      strokeColor: color,
      strokeWeight: 0.5,
      strokeOpacity: 0.8
    }});
    var self = this;
    this.polygon.on("mouseover", function() {{ startShowTimer("grid", self); }});
    this.polygon.on("mouseout", function() {{ startHideTimer(); }});
  }}

  GridPolygon.prototype.setMap = function(m) {{
    this.polygon.setMap(m);
  }};

  GridPolygon.prototype.getCenter = function() {{
    var sq = this.sq;
    return [(sq.sw_lng + sq.ne_lng) / 2, (sq.sw_lat + sq.ne_lat) / 2];
  }};

  function init() {{
    var lats = sectors.map(function(s){{ return s.lat; }}).filter(function(v){{ return v && !isNaN(v); }});
    var lngs = sectors.map(function(s){{ return s.lng; }}).filter(function(v){{ return v && !isNaN(v); }});
    var cLat = lats.reduce(function(a,b){{ return a+b; }}, 0) / lats.length;
    var cLng = lngs.reduce(function(a,b){{ return a+b; }}, 0) / lngs.length;
    map = new AMap.Map("container", {{ zoom: 13, center: [cLng, cLat] }});
    addSectors();
    setupSearch();
    setupRadius();
    setupMeasure();
  }}

  function addSectors() {{
    sectors.forEach(function(s) {{
      try {{
        if (!s.lat || !s.lng || s.azimuth == null || isNaN(s.lat) || isNaN(s.lng) || isNaN(s.azimuth)) return;
        var color = getColor(s);
        var path = createSectorPath(s.lng, s.lat, s.azimuth);
        var polygon = new AMap.Polygon({{
          path: path, fillColor: color, fillOpacity: 0.4,
          strokeColor: color, strokeWeight: 1, strokeOpacity: 0.8
        }});
        polygon.setMap(map);
        polygon.on("click", function() {{ toggleSelect(s.cell_name); }});
        overlays[s.cell_name] = polygon;
        dataMap[s.cell_name] = s;
      }} catch(e) {{}}
    }});
  }}

  function toggleSelect(name) {{
    var idx = selected.indexOf(name);
    if (idx >= 0) {{
      selected.splice(idx, 1);
      var s = dataMap[name], c = getColor(s);
      overlays[name].setOptions({{ fillColor: c, fillOpacity: 0.4, strokeWeight: 1 }});
      removeScatter(name);
      removeGrid(name);
    }} else {{
      selected.push(name);
      overlays[name].setOptions({{ fillColor: "#00FF00", fillOpacity: 0.7, strokeWeight: 2 }});
      if (viewMode === "scatter") {{
        loadScatter(name, currentMetric);
      }} else {{
        loadGrid(name);
      }}
    }}
    renderSelList();
    updateMeasureSection();
  }}

  function removeScatter(name) {{
    scatterLoadSeq[name] = (scatterLoadSeq[name] || 0) + 1;
    if (scatterByCell[name]) {{
      scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
      delete scatterByCell[name];
    }}
    renderStats();
  }}

  function loadScatter(name, metric) {{
    // Only remove existing scatter data, keep entry as placeholder to track in-flight request
    if (scatterByCell[name] && scatterByCell[name].markers) {{
      scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
    }}
    scatterLoadSeq[name] = (scatterLoadSeq[name] || 0) + 1;
    var mySeq = scatterLoadSeq[name];
    scatterByCell[name] = {{ markers: [], count: 0, avg: null, grades: [0,0,0,0], loading: true }};

    var s = dataMap[name];
    var indoor = document.getElementById("indoor-select").value;
    pendingLoads++;
    document.getElementById("loading-tip").style.display = "block";

    var apiMetric = metric === "beam" ? "rsrp" : metric;
    fetch("http://localhost:{API_PORT}/api/measurements?gnbid=" + encodeURIComponent(s.gnbid) + "&ci=" + encodeURIComponent(s.ci) + "&metric=" + apiMetric + "&indoor=" + indoor)
      .then(function(r) {{ return r.json(); }})
      .then(function(result) {{
        pendingLoads--;
        if (pendingLoads <= 0) {{
          document.getElementById("loading-tip").style.display = "none";
          pendingLoads = 0;
        }}
        if (mySeq !== scatterLoadSeq[name]) return;
        // If deselected while fetch was in-flight, discard and clean up
        if (selected.indexOf(name) < 0) {{
          delete scatterByCell[name];
          return;
        }}
        var points = result.points;
        var markers = [];
        var beamIn = 0, beamOut = 0;
        points.forEach(function(p) {{
          try {{
            var color;
            if (metric === "beam") {{
              color = beamColor(p[0], p[1], s);
              if (isInBeam(calcBearing(s.lng, s.lat, p[0], p[1]), s.azimuth)) beamIn++;
              else beamOut++;
            }} else {{
              var colorFn = metric === "rsrp" ? rsrpColor : sinrColor;
              color = colorFn(p[2]);
            }}
            var marker = new AMap.CircleMarker({{
              center: [p[0], p[1]],
              radius: 3,
              fillColor: color,
              fillOpacity: 0.7,
              strokeColor: color,
              strokeWeight: 0.5,
              strokeOpacity: 0.8
            }});
            marker._cellName = name;
            marker._lng = p[0];
            marker._lat = p[1];
            marker.on("mouseover", function() {{ startShowTimer("scatter", marker); }});
            marker.on("mouseout", function() {{ startHideTimer(); }});
            marker.setMap(map);
            markers.push(marker);
          }} catch(e) {{}}
        }});
        scatterByCell[name] = {{
          markers: markers,
          count: result.count,
          avg: result.avg,
          grades: result.grades,
          beamIn: metric === "beam" ? beamIn : 0,
          beamOut: metric === "beam" ? beamOut : 0
        }};
        renderStats();
      }})
      .catch(function() {{
        pendingLoads--;
        if (pendingLoads <= 0) {{
          document.getElementById("loading-tip").style.display = "none";
          pendingLoads = 0;
        }}
        if (scatterByCell[name] && scatterByCell[name].loading) {{
          delete scatterByCell[name];
        }}
      }});
  }}

  function reloadAllScatter() {{
    var names = Object.keys(scatterByCell);
    names.forEach(function(name) {{ removeScatter(name); }});
    selected.forEach(function(name) {{ loadScatter(name, currentMetric); }});
  }}

  function removeGrid(name) {{
    closeActiveInfoWindow();
    gridLoadSeq[name] = (gridLoadSeq[name] || 0) + 1;
    if (gridByCell[name]) {{
      gridByCell[name].gridPolygons.forEach(function(gp) {{ gp.setMap(null); }});
      delete gridByCell[name];
    }}
    renderStats();
  }}

  function loadGrid(name) {{
    if (gridByCell[name] && gridByCell[name].gridPolygons) {{
      gridByCell[name].gridPolygons.forEach(function(gp) {{ gp.setMap(null); }});
    }}
    gridLoadSeq[name] = (gridLoadSeq[name] || 0) + 1;
    var mySeq = gridLoadSeq[name];
    gridByCell[name] = {{ gridPolygons: [], total_count: 0, total_avg_rsrp: null, grid_count: 0, loading: true }};

    var s = dataMap[name];
    var indoor = document.getElementById("indoor-select").value;
    pendingGridLoads++;
    document.getElementById("loading-tip").style.display = "block";

    fetch("http://localhost:{API_PORT}/api/grid_analysis?gnbid=" + encodeURIComponent(s.gnbid) + "&ci=" + encodeURIComponent(s.ci) + "&indoor=" + indoor)
      .then(function(r) {{ return r.json(); }})
      .then(function(result) {{
        pendingGridLoads--;
        if (pendingGridLoads <= 0) {{
          document.getElementById("loading-tip").style.display = "none";
          pendingGridLoads = 0;
        }}
        if (mySeq !== gridLoadSeq[name]) return;
        if (selected.indexOf(name) < 0) {{
          delete gridByCell[name];
          return;
        }}
        var gridPolygons = [];
        var gridParams = result.grid_params;
        result.squares.forEach(function(sq) {{
          var gp = new GridPolygon(sq, gridParams);
          gp.setMap(map);
          gridPolygons.push(gp);
        }});
        gridByCell[name] = {{
          gridPolygons: gridPolygons,
          total_count: result.total_count,
          total_avg_rsrp: result.total_avg_rsrp,
          grid_count: result.grid_count,
          squares: result.squares
        }};
        renderStats();
      }})
      .catch(function() {{
        pendingGridLoads--;
        if (pendingGridLoads <= 0) {{
          document.getElementById("loading-tip").style.display = "none";
          pendingGridLoads = 0;
        }}
        if (gridByCell[name] && gridByCell[name].loading) {{
          delete gridByCell[name];
        }}
      }});
  }}

  function reloadAllGrid() {{
    var names = Object.keys(gridByCell);
    names.forEach(function(name) {{ removeGrid(name); }});
    selected.forEach(function(name) {{ loadGrid(name); }});
  }}

  function showScatter() {{
    closeActiveInfoWindow();
    Object.keys(gridByCell).forEach(function(name) {{
      if (gridByCell[name].gridPolygons) {{
        gridByCell[name].gridPolygons.forEach(function(gp) {{ gp.setMap(null); }});
      }}
    }});
    // Show scatter markers
    Object.keys(scatterByCell).forEach(function(name) {{
      if (scatterByCell[name].markers) {{
        scatterByCell[name].markers.forEach(function(m) {{ m.setMap(map); }});
      }}
    }});
  }}

  function showGrid() {{
    // Hide scatter markers
    Object.keys(scatterByCell).forEach(function(name) {{
      if (scatterByCell[name].markers) {{
        scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
      }}
    }});
    Object.keys(gridByCell).forEach(function(name) {{
      if (gridByCell[name].gridPolygons) {{
        gridByCell[name].gridPolygons.forEach(function(gp) {{ gp.setMap(map); }});
      }}
    }});
  }}

  function renderStats() {{
    var statsEl = document.getElementById("stats-container");
    var legendEl = document.getElementById("legend-container");
    statsEl.innerHTML = "";
    legendEl.innerHTML = "";

    if (viewMode === "scatter") {{
      renderScatterStats(statsEl, legendEl);
    }} else {{
      renderGridStats(statsEl, legendEl);
    }}
  }}

  function renderScatterStats(statsEl, legendEl) {{
    var names = Object.keys(scatterByCell);
    if (names.length === 0) return;

    if (currentMetric === "beam") {{
      var totalCount = 0, totalIn = 0, totalOut = 0;
      names.forEach(function(name) {{
        var info = scatterByCell[name];
        if (info) {{
          totalCount += info.count;
          totalIn += info.beamIn || 0;
          totalOut += info.beamOut || 0;
        }}
      }});
      var inPct = totalCount > 0 ? (totalIn / totalCount * 100).toFixed(1) : "0.0";

      var html = '<div class="stat-row"><span>采样点数</span><span class="stat-val">' + totalCount + '</span></div>';
      html += '<div class="stat-row"><span>波束内</span><span class="stat-val">' + totalIn + '</span></div>';
      html += '<div class="stat-row"><span>波束外</span><span class="stat-val">' + totalOut + '</span></div>';
      html += '<div class="stat-row"><span>波束内占比</span><span class="stat-val">' + inPct + '%</span></div>';
      statsEl.innerHTML = html;

      var legendHtml = '<div class="legend-item">' +
        '<span class="legend-dot" style="background:#2ECC71"></span>' +
        '<span class="legend-label">波束内(±60°)</span>' +
        '<span class="legend-count">' + totalIn + '</span>' +
        '<span class="legend-pct">' + inPct + '%</span>' +
      '</div>';
      var outPct = totalCount > 0 ? (totalOut / totalCount * 100).toFixed(1) : "0.0";
      legendHtml += '<div class="legend-item">' +
        '<span class="legend-dot" style="background:#E74C3C"></span>' +
        '<span class="legend-label">波束外</span>' +
        '<span class="legend-count">' + totalOut + '</span>' +
        '<span class="legend-pct">' + outPct + '%</span>' +
      '</div>';
      legendEl.innerHTML = legendHtml;
      return;
    }}

    var metricLabel = currentMetric === "rsrp" ? "RSRP" : "SINR";
    var metricUnit = currentMetric === "rsrp" ? "dBm" : "dB";
    var grades = GRADES[currentMetric];

    var totalCount = 0;
    var weightedSum = 0;
    var gradeTotals = [0, 0, 0, 0];

    names.forEach(function(name) {{
      var info = scatterByCell[name];
      if (info && info.avg != null) {{
        totalCount += info.count;
        weightedSum += info.count * info.avg;
      }}
      if (info && info.grades) {{
        for (var i = 0; i < 4; i++) gradeTotals[i] += info.grades[i];
      }}
    }});

    var overallAvg = totalCount > 0 ? (weightedSum / totalCount).toFixed(2) : "--";

    var html = '<div class="stat-row"><span>采样点数</span><span class="stat-val">' + totalCount + '</span></div>';
    html += '<div class="stat-row"><span>' + metricLabel + ' 均值</span><span class="stat-val">' + overallAvg + ' ' + metricUnit + '</span></div>';
    statsEl.innerHTML = html;

    var legendHtml = "";
    for (var i = 0; i < grades.length; i++) {{
      var g = grades[i];
      var cnt = gradeTotals[i];
      var pct = totalCount > 0 ? (cnt / totalCount * 100).toFixed(1) : "0.0";
      legendHtml += '<div class="legend-item">' +
        '<span class="legend-dot" style="background:' + g.color + '"></span>' +
        '<span class="legend-label">' + g.label + '(' + g.range + ')</span>' +
        '<span class="legend-count">' + cnt + '</span>' +
        '<span class="legend-pct">' + pct + '%</span>' +
      '</div>';
    }}
    legendEl.innerHTML = legendHtml;
  }}

  function renderGridStats(statsEl, legendEl) {{
    var names = Object.keys(gridByCell);
    if (names.length === 0) return;

    var totalGridCount = 0;
    var totalPlmnCount = 0;
    var totalCellCount = 0;
    var weightedSum = 0;
    var gradeSquareCounts = [0, 0, 0, 0];
    var gradeCellCounts = [0, 0, 0, 0];
    var gradePlmnCounts = [0, 0, 0, 0];

    names.forEach(function(name) {{
      var info = gridByCell[name];
      if (!info || info.loading) return;
      totalGridCount += info.grid_count;
      totalPlmnCount += info.total_count;
      if (info.total_avg_rsrp != null) {{
        weightedSum += info.total_count * info.total_avg_rsrp;
      }}
      if (info.squares) {{
        info.squares.forEach(function(sq) {{
          var pct = sq.pct;
          var gi = pct >= 80 ? 3 : pct >= 50 ? 2 : pct >= 20 ? 1 : 0;
          gradeSquareCounts[gi]++;
          gradeCellCounts[gi] += sq.count;
          gradePlmnCounts[gi] += sq.plmn_count;
          totalCellCount += sq.count;
        }});
      }}
    }});

    var overallAvg = totalPlmnCount > 0 ? (weightedSum / totalPlmnCount).toFixed(2) : "--";

    var html = '<div class="stat-row"><span>栅格小区采样点</span><span class="stat-val">' + totalCellCount + '</span></div>';
    html += '<div class="stat-row"><span>栅格总采样点数</span><span class="stat-val">' + totalPlmnCount + '</span></div>';
    html += '<div class="stat-row"><span>RSRP 均值</span><span class="stat-val">' + overallAvg + ' dBm</span></div>';
    html += '<div class="stat-row"><span>栅格数</span><span class="stat-val">' + totalGridCount + '</span></div>';
    statsEl.innerHTML = html;

    var legendHtml = "";
    for (var i = 0; i < PCT_GRADES.length; i++) {{
      var g = PCT_GRADES[i];
      var sqCnt = gradeSquareCounts[i];
      var cellCnt = gradeCellCounts[i];
      var plmnCnt = gradePlmnCounts[i];
      var smpPct = totalCellCount > 0 ? (cellCnt / totalCellCount * 100).toFixed(1) : "0.0";
      legendHtml += '<div class="legend-item">' +
        '<span class="legend-dot" style="background:' + g.color + '"></span>' +
        '<span class="legend-label">' + g.label + '(' + g.range + ')</span>' +
        '<span class="legend-count">' + sqCnt + '格</span>' +
        '<span class="legend-pct">' + smpPct + '%</span>' +
      '</div>';
    }}
    legendEl.innerHTML = legendHtml;
  }}

  function updateMeasureSection() {{
    var sec = document.getElementById("measure-section");
    sec.style.display = selected.length > 0 ? "block" : "none";
  }}

  function renderSelList() {{
    var header = document.getElementById("sel-header");
    var list = document.getElementById("sel-list");
    var count = document.getElementById("sel-count");
    var btn = document.getElementById("clear-btn");
    count.textContent = selected.length;
    header.style.display = selected.length ? "block" : "none";
    btn.style.display = selected.length ? "inline-block" : "none";
    list.innerHTML = "";
    selected.forEach(function(name, i) {{
      var s = dataMap[name];
      var div = document.createElement("div");
      div.className = "sel-item";
      div.innerHTML = "<span>" + s.station_name + " | GNBID:" + (s.gnbid || "") + " CI:" + (s.ci || "") + " | 频点:" + (s.freq || "") + " | PCI:" + (s.pci || "--") + " | 挂高:" + (s.height || "--") + "m</span>" +
        '<button data-idx="' + i + '">&times;</button>';
      div.querySelector("button").onclick = function() {{ toggleSelect(name); }};
      list.appendChild(div);
    }});
  }}

  document.getElementById("clear-btn").onclick = function() {{
    selected.slice().forEach(function(name) {{
      var s = dataMap[name], c = getColor(s);
      overlays[name].setOptions({{ fillColor: c, fillOpacity: 0.4, strokeWeight: 1 }});
      scatterLoadSeq[name] = (scatterLoadSeq[name] || 0) + 1;
      gridLoadSeq[name] = (gridLoadSeq[name] || 0) + 1;
    }});
    Object.keys(scatterByCell).forEach(function(name) {{
      if (scatterByCell[name].markers) {{
        scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
      }}
      delete scatterByCell[name];
    }});
    Object.keys(gridByCell).forEach(function(name) {{
      if (gridByCell[name].gridPolygons) {{
        gridByCell[name].gridPolygons.forEach(function(gp) {{ gp.setMap(null); }});
      }}
      delete gridByCell[name];
    }});
    selected = [];
    renderSelList();
    updateMeasureSection();
    renderStats();
  }};

  function rebuildSectors() {{
    for (var name in overlays) map.remove(overlays[name]);
    overlays = {{}}; dataMap = {{}};
    addSectors();
    selected.forEach(function(name) {{
      if (overlays[name]) overlays[name].setOptions({{ fillColor: "#00FF00", fillOpacity: 0.7, strokeWeight: 2 }});
    }});
  }}

  function setupRadius() {{
    var group = document.getElementById("radius-group");
    var customInput = document.getElementById("custom-radius");
    var labels = group.querySelectorAll("label");
    labels.forEach(function(label) {{
      label.addEventListener("click", function() {{
        labels.forEach(function(l) {{ l.classList.remove("active"); }});
        label.classList.add("active");
        var val = label.querySelector("input").value;
        if (val === "custom") {{
          customInput.style.display = "inline-block";
          customInput.focus();
        }} else {{
          customInput.style.display = "none";
          RADIUS_M = parseInt(val);
          rebuildSectors();
        }}
      }});
    }});
    customInput.addEventListener("change", function() {{
      var v = parseInt(customInput.value);
      if (v && v > 0) {{ RADIUS_M = v; rebuildSectors(); }}
    }});
  }}

  function setupSearch() {{
    var input = document.getElementById("search");
    var results = document.getElementById("search-results");
    input.addEventListener("input", function() {{
      var q = input.value.trim();
      results.innerHTML = "";
      if (!q) return;
      var matches = sectors.filter(function(s) {{ return s.cell_name.indexOf(q) >= 0; }}).slice(0, 10);
      matches.forEach(function(m) {{
        var div = document.createElement("div");
        div.style.cssText = "padding:4px 6px;cursor:pointer;border-bottom:1px solid #eee;";
        div.textContent = m.cell_name;
        div.onmouseover = function() {{ div.style.background = "#e8f0fe"; }};
        div.onmouseout = function() {{ div.style.background = ""; }};
        div.onclick = function() {{
          toggleSelect(m.cell_name);
          map.setZoomAndCenter(15, [m.lng, m.lat]);
          input.value = "";
          results.innerHTML = "";
        }};
        results.appendChild(div);
      }});
    }});
  }}

  function setupMeasure() {{
    document.getElementById("btn-scatter").onclick = function() {{
      if (viewMode === "scatter") return;
      viewMode = "scatter";
      document.getElementById("btn-scatter").classList.add("active");
      document.getElementById("btn-grid").classList.remove("active");
      document.getElementById("scatter-controls").style.display = "block";
      // Load scatter data for selected cells if not already loaded
      selected.forEach(function(name) {{
        if (!scatterByCell[name] || scatterByCell[name].loading) {{
          loadScatter(name, currentMetric);
        }}
      }});
      showScatter();
      renderStats();
    }};
    document.getElementById("btn-grid").onclick = function() {{
      if (viewMode === "grid") return;
      viewMode = "grid";
      document.getElementById("btn-grid").classList.add("active");
      document.getElementById("btn-scatter").classList.remove("active");
      document.getElementById("scatter-controls").style.display = "none";
      // Load grid data for selected cells if not already loaded
      selected.forEach(function(name) {{
        if (!gridByCell[name] || gridByCell[name].loading) {{
          loadGrid(name);
        }}
      }});
      showGrid();
      renderStats();
    }};
    document.getElementById("btn-rsrp").onclick = function() {{
      if (currentMetric === "rsrp") return;
      currentMetric = "rsrp";
      document.getElementById("btn-rsrp").classList.add("active");
      document.getElementById("btn-sinr").classList.remove("active");
      document.getElementById("btn-beam").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-sinr").onclick = function() {{
      if (currentMetric === "sinr") return;
      currentMetric = "sinr";
      document.getElementById("btn-sinr").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      document.getElementById("btn-beam").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-beam").onclick = function() {{
      if (currentMetric === "beam") return;
      currentMetric = "beam";
      document.getElementById("btn-beam").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      document.getElementById("btn-sinr").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("indoor-select").onchange = function() {{
      if (selected.length > 0) {{
        if (viewMode === "scatter") {{
          reloadAllScatter();
        }} else {{
          reloadAllGrid();
        }}
      }}
    }};
  }}
</script>
</body>
</html>"""


def main():
    st.set_page_config(layout="wide")
    st.markdown("""
        <style>
            .block-container { padding-top: 0; padding-bottom: 0; }
            header { display: none; }
        </style>
    """, unsafe_allow_html=True)

    start_api_server()

    data = load_data()

    sectors = [
        {
            "cell_name": d["小区中文名"],
            "station_name": d["物理站名"],
            "freq": str(d["频点"] or ""),
            "band": d["工作频段"] or "",
            "coverage": d["覆盖类型"] or "",
            "lat": float(d["RRU纬度"]),
            "lng": float(d["RRU经度"]),
            "azimuth": float(d["方位角"]),
            "gnbid": str(d["Gnbid"] or ""),
            "ci": str(d["Cellid"] or ""),
            "height": str(d["天线挂高"] or ""),
            "pci": str(d["nRPCI"] or ""),
        }
        for d in data
        if d["RRU经度"] and d["RRU纬度"] and d["方位角"] is not None
    ]

    html = build_map_html(json.dumps(sectors, ensure_ascii=False))
    components.html(html, height=950)


if __name__ == "__main__":
    main()
