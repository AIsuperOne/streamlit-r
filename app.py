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

COVERAGE_GRADES = [
    {"label": "未覆盖", "range": "RSRP≤-105或SINR≤-3", "color": "#E74C3C"},
    {"label": "覆盖", "range": "RSRP>-105且SINR>-3", "color": "#2ECC71"},
]

WEAK_GRADES = [
    {"label": "覆盖优良", "range": "0~20%", "color": "#2ECC71"},
    {"label": "覆盖良好", "range": "20~50%", "color": "#F1C40F"},
    {"label": "覆盖稍弱", "range": "50~80%", "color": "#E67E22"},
    {"label": "覆盖很弱", "range": "80~100%", "color": "#E74C3C"},
]


# ============================================================
# 算法层：纯函数，无副作用，不访问数据库
# ============================================================

def meters_to_degrees(meters, avg_lat):
    """【坐标转换】米→经纬度偏移量
    实现：经度修正cos(lat)，1°≈111320m(经度方向)/110540m(纬度方向)
    输入：meters(米), avg_lat(参考纬度)
    输出：(lng_offset, lat_offset)
    """
    lng_offset = meters / 111320.0 / math.cos(avg_lat * math.pi / 180.0)
    lat_offset = meters / 110540.0
    return lng_offset, lat_offset


def calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci):
    """【栅格原点】确定栅格坐标系的随机原点
    实现：random.seed(gnbid_ci)保证同一小区生成确定性原点，
         随机选一个采样点作为参考点，偏移半格使参考点位于栅格中央
    输入：cell_pts[(lng,lat),...], cell_lng, cell_lat, gnbid, ci
    输出：(origin_lng, origin_lat)
    """
    random.seed(f"{gnbid}_{ci}")
    ref_idx = random.randint(0, len(cell_pts) - 1)
    origin_lng = cell_pts[ref_idx][0] - cell_lng / 2
    origin_lat = cell_pts[ref_idx][1] - cell_lat / 2
    return origin_lng, origin_lat


def bin_point_to_grid(lng, lat, origin_lng, origin_lat, cell_lng, cell_lat):
    """【数学落格】将采样点坐标映射到栅格索引
    实现：math.floor((coord-origin)/cell_size)
         用floor不用int：int(-8.9)=-8截断向零错误，floor(-8.9)=-9向负无穷正确
    输入：lng, lat, origin_lng, origin_lat, cell_lng, cell_lat
    输出：(gx, gy) 栅格索引
    """
    gx = math.floor((lng - origin_lng) / cell_lng)
    gy = math.floor((lat - origin_lat) / cell_lat)
    return gx, gy


def build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat):
    """【栅格构建】将小区采样点落格并构建Shapely Polygon列表
    实现：1.对每个采样点执行数学落格，统计每个格子的cell_count
         2.对每个非空格子计算sw/ne坐标，构建Shapely Polygon
    输入：cell_pts[(lng,lat),...], origin_lng, origin_lat, cell_lng, cell_lat
    输出：[{gx, gy, polygon, cell_count, plmn_count}, ...]
    """
    cell_grid = {}
    for lng, lat in cell_pts:
        gx, gy = bin_point_to_grid(lng, lat, origin_lng, origin_lat, cell_lng, cell_lat)
        cell_grid[(gx, gy)] = cell_grid.get((gx, gy), 0) + 1

    polygons = []
    for (gx, gy), count in cell_grid.items():
        sw_lng = origin_lng + gx * cell_lng
        sw_lat = origin_lat + gy * cell_lat
        ne_lng = origin_lng + (gx + 1) * cell_lng
        ne_lat = origin_lat + (gy + 1) * cell_lat
        poly = Polygon([(sw_lng, sw_lat), (ne_lng, sw_lat), (ne_lng, ne_lat), (sw_lng, ne_lat)])
        polygons.append({"gx": gx, "gy": gy, "polygon": poly, "cell_count": count, "plmn_count": 0, "weak_count": 0})
    return polygons


def count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat):
    """【PLMN统计】用Shapely covers()判定PLMN采样点落入栅格
    实现：先数学落格快速定位候选栅格(gx,gy)，再Shapely covers()精确判定
         两步过滤：数学落格O(1)定位→covers()精确排除边界点
         同时统计弱覆盖点(rsrp<-105或sinr<-3)
    输入：plmn_rows[(lng,lat,rsrp,sinr),...], polygons, origin_lng, origin_lat, cell_lng, cell_lat
    输出：无返回，直接修改polygons[i]["plmn_count"]和polygons[i]["weak_count"]
    """
    poly_map = {(p["gx"], p["gy"]): p for p in polygons}
    for r in plmn_rows:
        if r[0] is None or r[1] is None:
            continue
        lng, lat = r[0], r[1]
        pt = Point(lng, lat)
        gx, gy = bin_point_to_grid(lng, lat, origin_lng, origin_lat, cell_lng, cell_lat)
        key = (gx, gy)
        if key in poly_map and poly_map[key]["polygon"].covers(pt):
            poly_map[key]["plmn_count"] += 1
            rsrp, sinr = r[2], r[3]
            if rsrp is not None and sinr is not None and (rsrp < -105 or sinr < -3):
                poly_map[key]["weak_count"] += 1


def calc_coverage(cell_count, plmn_count):
    """【覆盖率计算】cell_count / plmn_count × 100
    实现：plmn_count=0时返回0，避免除零
    输入：cell_count(小区采样点数), plmn_count(总采样点数)
    输出：int 覆盖率百分比
    """
    return round(cell_count / plmn_count * 100) if plmn_count > 0 else 0


def reconstruct_polygon(origin_lng, origin_lat, cell_lng, cell_lat, gx, gy):
    """【栅格重建】从grid_params重建Shapely Polygon，避免经纬度精度丢失
    实现：用origin + gx*cell_size计算sw/ne坐标，与grid_analysis完全相同的算术路径，
         避免通过URL传递rounded sw/ne导致的浮点精度损失
    输入：origin_lng, origin_lat, cell_lng, cell_lat, gx, gy
    输出：Shapely Polygon
    """
    origin_lng, origin_lat = float(origin_lng), float(origin_lat)
    cell_lng, cell_lat = float(cell_lng), float(cell_lat)
    gx, gy = int(gx), int(gy)
    sw_lng = origin_lng + gx * cell_lng
    sw_lat = origin_lat + gy * cell_lat
    ne_lng = origin_lng + (gx + 1) * cell_lng
    ne_lat = origin_lat + (gy + 1) * cell_lat
    return Polygon([(sw_lng, sw_lat), (ne_lng, sw_lat), (ne_lng, ne_lat), (sw_lng, ne_lat)])


def calc_bearing(lng1, lat1, lng2, lat2):
    """【方位角计算】Haversine公式计算从(lng1,lat1)到(lng2,lat2)的方位角
    实现：正北为0°，顺时针增长，与JS端calcBearing逻辑一致
    输入：lng1, lat1, lng2, lat2（经纬度）
    输出：float 方位角 0~360°
    """
    d_lng = math.radians(lng2 - lng1)
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    y = math.sin(d_lng) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(d_lng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def build_where(indoor, freq=None, with_cell=False, with_plmn=False,
                with_signal=False, with_coords=False, with_bounds=False):
    """【SQL条件构建】统一构建WHERE子句及参数
    实现：按开关组合plmn/rsrp/sinr/coord/indoor/earfcn/bounds条件，
         避免各handler重复拼接SQL字符串
    输入：indoor(0/1/2), freq(频点), with_cell(加gnbid+ci), with_plmn(加plmn=46000),
         with_signal(加rsrp/sinr有效), with_coords(加经纬度非空), with_bounds(加空间范围)
    输出：(where_string, params_list)
    """
    conditions = []
    params = []

    if with_cell:
        conditions.extend(["gnbid=?", "ci=?"])
        # 调用方需自行添加gnbid/ci参数到params
    if with_plmn:
        conditions.append('plmn="46000"')
    if with_signal:
        conditions.extend(['rsrp IS NOT NULL', 'sinr IS NOT NULL'])
    if with_coords:
        conditions.append("lng IS NOT NULL")
    if indoor == "1":
        conditions.append('in_out_door="In_Door"')
    elif indoor == "2":
        conditions.append('in_out_door="Out_Door"')
    if freq:
        conditions.append("nr_earfcn=?")
        params.append(freq)
    if with_bounds:
        conditions.extend(["lng>=?", "lng<=?", "lat>=?", "lat<=?"])
        # bounds参数顺序：(sw_lng, ne_lng, sw_lat, ne_lat)，非shapely.bounds的(minx,miny,maxx,maxy)

    where = " AND ".join(conditions)
    return where, params


# ============================================================
# 数据层：数据库访问函数
# ============================================================

def query_cell_freq(gnbid, ci):
    """查询5GBaseStation小区频点
    输入：gnbid, ci
    输出：str 频点值，不存在则返回空串
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT "频点" FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
            [gnbid, ci],
        ).fetchone()
        return str(row[0]) if row and row[0] else ""
    finally:
        conn.close()


def query_cell_points(gnbid, ci, indoor, freq, conn=None):
    """查询小区采样点(经纬度)
    输入：gnbid, ci, indoor, freq, conn(可选，外部连接)
    输出：[(lng, lat), ...] 采样点坐标列表
    """
    where, params = build_where(indoor, freq=freq, with_cell=True, with_plmn=True,
                                with_signal=True, with_coords=True)
    params = [gnbid, ci] + params
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(f'SELECT lng, lat FROM "data" WHERE {where}', params).fetchall()
    finally:
        if own_conn:
            conn.close()
    return [(r[0], r[1]) for r in rows if r[0] is not None and r[1] is not None]


def query_plmn_points_with_stats(indoor, freq, bounds, conn=None):
    """查询PLMN采样点+统计(总数/RSRP均值)
    输入：indoor, freq, bounds(sw_lng, ne_lng, sw_lat, ne_lat), conn(可选，外部连接)
    输出：(plmn_rows, total_count, total_avg_rsrp)
    优化：用覆盖索引(plmn,nr_earfcn,lng,lat,rsrp)直接取lng/lat/rsrp，Python端统计
          sinr不在覆盖索引中需回表，用于弱覆盖计算
    """
    where, params = build_where(indoor, freq=freq, with_plmn=True,
                                with_coords=True, with_bounds=True)
    params = params + list(bounds)

    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f'SELECT lng, lat, rsrp, sinr FROM "data" WHERE {where}', params
        ).fetchall()
    finally:
        if own_conn:
            conn.close()

    plmn_rows = []
    rsrp_sum = 0.0
    rsrp_count = 0
    for r in rows:
        if r[0] is None or r[1] is None:
            continue
        plmn_rows.append((r[0], r[1], r[2], r[3]))
        if r[2] is not None:
            rsrp_sum += r[2]
            rsrp_count += 1
    total_count = len(plmn_rows)
    total_avg = round(rsrp_sum / rsrp_count, 2) if rsrp_count > 0 else None
    return plmn_rows, total_count, total_avg


def query_measurements(gnbid, ci, metric, indoor):
    """查询散点测量数据(点列表+统计+等级)
    输入：gnbid, ci, metric(rsrp/sinr/coverage), indoor
    输出：{points, count, avg, grades}
    """
    if metric == "coverage":
        where = 'gnbid=? AND ci=? AND lng IS NOT NULL AND rsrp IS NOT NULL AND sinr IS NOT NULL'
        q_params = [gnbid, ci]
        if indoor == "1":
            where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            where += ' AND in_out_door="Out_Door"'
        conn = sqlite3.connect(DB_PATH)
        try:
            stat_row = conn.execute(
                f'SELECT COUNT(*), '
                f'SUM(CASE WHEN rsrp>-105 AND sinr>-3 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN NOT(rsrp>-105 AND sinr>-3) THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}', q_params,
            ).fetchone()
            total_count = stat_row[0] or 0
            covered = stat_row[1] or 0
            not_covered = stat_row[2] or 0
            avg_val = round(covered / total_count * 100, 2) if total_count > 0 else None

            rows = conn.execute(
                f'SELECT lng, lat, rsrp, sinr FROM "data" WHERE {where} LIMIT 5000',
                q_params,
            ).fetchall()
        finally:
            conn.close()

        points = []
        for r in rows:
            if r[0] is None or r[1] is None or r[2] is None or r[3] is None:
                continue
            covered_flag = 1 if (r[2] > -105 and r[3] > -3) else 0
            points.append([r[0], r[1], covered_flag])
        return {"points": points, "count": total_count, "avg": avg_val, "grades": [not_covered, covered]}

    val_col = "rsrp" if metric == "rsrp" else "sinr"
    where = f'gnbid=? AND ci=? AND lng IS NOT NULL AND {val_col} IS NOT NULL'
    q_params = [gnbid, ci]

    if indoor == "1":
        where += ' AND in_out_door="In_Door"'
    elif indoor == "2":
        where += ' AND in_out_door="Out_Door"'

    conn = sqlite3.connect(DB_PATH)
    try:
        if metric == "rsrp":
            stat_row = conn.execute(
                f'SELECT COUNT(*), AVG({val_col}), '
                f'SUM(CASE WHEN {val_col}>=-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}>=-105 AND {val_col}<-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}>=-115 AND {val_col}<-105 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}<-115 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}', q_params,
            ).fetchone()
        else:
            stat_row = conn.execute(
                f'SELECT COUNT(*), AVG({val_col}), '
                f'SUM(CASE WHEN {val_col}>=15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}>=5 AND {val_col}<15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}>=-3 AND {val_col}<5 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN {val_col}<-3 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}', q_params,
            ).fetchone()
        total_count = stat_row[0] or 0
        avg_val = round(stat_row[1], 2) if stat_row[1] is not None else None
        grade_counts = [stat_row[2], stat_row[3], stat_row[4], stat_row[5]]

        rows = conn.execute(
            f'SELECT lng, lat, {val_col} FROM "data" WHERE {where} LIMIT 5000',
            q_params,
        ).fetchall()
    finally:
        conn.close()

    points = []
    for r in rows:
        if r[0] is None or r[1] is None or r[2] is None:
            continue
        points.append([r[0], r[1], r[2]])

    return {"points": points, "count": total_count, "avg": avg_val, "grades": grade_counts}


def query_grid_detail_points(poly, indoor, freq):
    """查询栅格详情采样点，用Shapely covers()过滤，按plmn/gnbid/ci聚合
    输入：poly(Shapely Polygon), indoor, freq
    输出：[{nr_earfcn, gnbid, ci, avg_rsrp, avg_sinr, count}, ...]
    优化：用覆盖索引快速取空间数据，rsrp/sinr用数值列
    """
    minx, miny, maxx, maxy = poly.bounds
    bounds = (minx, maxx, miny, maxy)
    where, params = build_where(indoor, freq=freq, with_plmn=True,
                                with_coords=True, with_bounds=True)
    params = params + list(bounds)

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f'SELECT lng, lat, plmn, gnbid, ci, nr_earfcn, rsrp, sinr '
            f'FROM "data" WHERE {where}', params,
        ).fetchall()
    finally:
        conn.close()

    groups = {}
    for r in rows:
        if r[0] is None or r[1] is None:
            continue
        lng, lat = r[0], r[1]
        if not poly.covers(Point(lng, lat)):
            continue
        if r[6] is None or r[7] is None:
            continue
        key = (r[2], r[3], r[4])  # plmn, gnbid, ci
        if key not in groups:
            groups[key] = {"nr_earfcn": r[5] or "", "rsrp_sum": 0.0, "sinr_sum": 0.0, "count": 0}
        groups[key]["rsrp_sum"] += r[6]
        groups[key]["sinr_sum"] += r[7]
        groups[key]["count"] += 1

    result = []
    for (plmn, gnbid, ci), info in groups.items():
        avg_rsrp = round(info["rsrp_sum"] / info["count"], 2) if info["count"] > 0 else None
        avg_sinr = round(info["sinr_sum"] / info["count"], 2) if info["count"] > 0 else None
        result.append({"nr_earfcn": info["nr_earfcn"], "gnbid": gnbid, "ci": ci,
                        "avg_rsrp": avg_rsrp, "avg_sinr": avg_sinr, "count": info["count"]})
    return result


def query_point_detail(gnbid, ci, lng, lat, indoor):
    """查询采样点详情(含邻区信息)
    输入：gnbid, ci, lng, lat, indoor
    输出：dict or None
    """
    where = 'gnbid=? AND ci=? AND lng IS NOT NULL'
    q_params = [gnbid, ci]
    if indoor == "1":
        where += ' AND in_out_door="In_Door"'
    elif indoor == "2":
        where += ' AND in_out_door="Out_Door"'
    where += ' AND ABS(lng-?)<0.000001 AND ABS(lat-?)<0.000001'
    q_params.extend([lng, lat])

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            f'SELECT gnbid, ci, nr_earfcn, nr_ssb_rsrp, nr_ssb_sinr, '
            f'nr_neighbor_rsrp_list, nr_neighbor_pci_list, nr_neighbor_earfcn_list '
            f'FROM "data" WHERE {where} LIMIT 1', q_params,
        ).fetchone()
    finally:
        conn.close()

    if row:
        return {"gnbid": row[0], "ci": row[1], "nr_earfcn": row[2], "nr_ssb_rsrp": row[3],
                "nr_ssb_sinr": row[4], "nr_neighbor_rsrp_list": row[5],
                "nr_neighbor_pci_list": row[6], "nr_neighbor_earfcn_list": row[7]}
    return None


def query_optimal_azimuth(gnbid, ci, indoor):
    """查询最优方位角
    算法：1.从5GBaseStation取小区经纬度和当前方位角
         2.查询该小区采样点(lng,lat)，按indoor筛选
         3.对每个采样点计算相对小区的方位角(bearing)
         4.用区间覆盖法O(N)计算每个5°角度的波束内点数
         5.点数最多的角度为最优方位角
    输入：gnbid, ci, indoor(0=全部,1=室内,2=室外)
    输出：{optimal_azimuth, optimal_ratio, current_azimuth, current_ratio, total_count}
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT "RRU经度", "RRU纬度", "方位角" FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
            [gnbid, ci],
        ).fetchone()
        if not row or not row[0] or not row[1] or row[2] is None:
            return {"optimal_azimuth": None, "optimal_ratio": None,
                    "current_azimuth": None, "current_ratio": None, "total_count": 0}
        cell_lng, cell_lat, current_azimuth = float(row[0]), float(row[1]), float(row[2])

        where = 'gnbid=? AND ci=? AND lng IS NOT NULL'
        params = [gnbid, ci]
        if indoor == "1":
            where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            where += ' AND in_out_door="Out_Door"'

        points = conn.execute(f'SELECT lng, lat FROM "data" WHERE {where}', params).fetchall()
    finally:
        conn.close()

    bearings = [calc_bearing(cell_lng, cell_lat, p[0], p[1]) for p in points if p[0] is not None and p[1] is not None]

    if not bearings:
        return {"optimal_azimuth": None, "optimal_ratio": None,
                "current_azimuth": current_azimuth, "current_ratio": None, "total_count": 0}

    half_beam_steps = int(BEAM_WIDTH / 2 / 5)  # 30°/5 = 6
    angle_counts = [0] * 72
    for b in bearings:
        center = int(round(b / 5)) % 72
        for offset in range(-half_beam_steps, half_beam_steps + 1):
            angle_counts[(center + offset) % 72] += 1

    best_idx = max(range(72), key=lambda i: angle_counts[i])
    best_angle = best_idx * 5
    best_count = angle_counts[best_idx]

    current_center = int(round(current_azimuth / 5)) % 72
    current_count = angle_counts[current_center]

    return {
        "optimal_azimuth": best_angle,
        "optimal_ratio": round(best_count / len(bearings) * 100, 1),
        "current_azimuth": current_azimuth,
        "current_ratio": round(current_count / len(bearings) * 100, 1),
        "total_count": len(bearings),
    }


@st.cache_data
def load_base_stations():
    """加载5GBaseStation基站数据(Streamlit缓存)
    输出：[dict, ...] 基站记录列表
    """
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


# ============================================================
# API层：HTTP请求处理（瘦handler，调用算法层+数据层）
# ============================================================

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

        if path == "/api/optimal_azimuth":
            self._handle_optimal_azimuth(gnbid, ci, indoor)
            return

        self._handle_measurements(gnbid, ci, params, indoor)

    def _handle_measurements(self, gnbid, ci, params, indoor):
        metric = params.get("metric", ["rsrp"])[0]
        result = query_measurements(gnbid, ci, metric, indoor)
        self._send_json(result)

    def _handle_grid_analysis(self, gnbid, ci, indoor):
        # 1.查询小区频点
        freq = query_cell_freq(gnbid, ci)

        # 2.查询小区采样点 + 4.查询PLMN采样点（共享DB连接）
        conn = sqlite3.connect(DB_PATH)
        try:
            cell_pts = query_cell_points(gnbid, ci, indoor, freq, conn=conn)
            if not cell_pts:
                self._send_json({"squares": [], "grid_params": None, "total_count": 0, "total_avg_rsrp": None, "grid_count": 0})
                return

            # 3.构建栅格（算法：坐标转换→原点计算→落格→Polygon构建）
            avg_lat = sum(p[1] for p in cell_pts) / len(cell_pts)
            cell_lng, cell_lat = meters_to_degrees(5.0, avg_lat)
            origin_lng, origin_lat = calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci)
            polygons = build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat)

            # 4.查询PLMN采样点+统计（算法：Shapely covers()包含判定）
            all_bounds = [p["polygon"].bounds for p in polygons]
            bounds = (min(b[0] for b in all_bounds), max(b[2] for b in all_bounds),
                      min(b[1] for b in all_bounds), max(b[3] for b in all_bounds))
            plmn_rows, total_count, total_avg = query_plmn_points_with_stats(indoor, freq, bounds, conn=conn)
        finally:
            conn.close()
        count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat)

        # 5.计算覆盖率并构建结果（算法：cell_count/plmn_count）
        squares = []
        for p in polygons:
            pct = calc_coverage(p["cell_count"], p["plmn_count"])
            weak_pct = round(p["weak_count"] / p["plmn_count"] * 100) if p["plmn_count"] > 0 else 0
            bounds = p["polygon"].bounds
            squares.append({
                "sw_lng": bounds[0], "sw_lat": bounds[1], "ne_lng": bounds[2], "ne_lat": bounds[3],
                "gx": p["gx"], "gy": p["gy"],
                "count": p["cell_count"], "plmn_count": p["plmn_count"], "pct": pct,
                "weak_count": p["weak_count"], "weak_pct": weak_pct,
            })

        self._send_json({
            "squares": squares,
            "grid_params": {"origin_lng": origin_lng, "origin_lat": origin_lat,
                            "cell_lng": cell_lng, "cell_lat": cell_lat, "freq": freq},
            "total_count": total_count, "total_avg_rsrp": total_avg, "grid_count": len(squares),
        })

    def _handle_grid_cell_detail(self, origin_lng, origin_lat, cell_lng, cell_lat, gx, gy, indoor, freq):
        # 从栅格参数重建Polygon（算法：避免精度丢失）
        poly = reconstruct_polygon(origin_lng, origin_lat, cell_lng, cell_lat, gx, gy)
        result = query_grid_detail_points(poly, indoor, freq)
        self._send_json(result)

    def _handle_point_detail(self, gnbid, ci, lng, lat, indoor):
        result = query_point_detail(gnbid, ci, lng, lat, indoor)
        self._send_json(result)

    def _handle_optimal_azimuth(self, gnbid, ci, indoor):
        result = query_optimal_azimuth(gnbid, ci, indoor)
        self._send_json(result)

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


# ============================================================
# 呈现层：HTML/JS模板 + Streamlit入口
# ============================================================

def build_map_html(sectors_json):
    rsrp_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in RSRP_GRADES])
    sinr_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in SINR_GRADES])
    pct_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in PCT_GRADES])
    coverage_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in COVERAGE_GRADES])
    weak_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in WEAK_GRADES])
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
      <button id="btn-coverage">覆盖率</button>
    </div>
    </div>
    <div id="grid-controls" style="display:none;">
    <div class="metric-toggle">
      <button class="active" id="btn-cell-pct">小区占比</button>
      <button id="btn-weak-pct">覆盖比例</button>
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
    sinr: {sinr_grades_json},
    coverage: {coverage_grades_json},
    weak: {weak_grades_json}
  }};

  var PCT_GRADES = {pct_grades_json};

  var map, sectors = {sectors_json};
  var overlays = {{}}, dataMap = {{}};
  var selected = [];
  var currentMetric = "rsrp";
  var viewMode = "scatter";
  var gridMode = "cell";
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
  s.src = "https://webapi.amap.com/maps?v=2.0&key={AMAP_KEY}&plugin=AMap.Map3D";
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

  function coverageColor(v) {{
    return v >= 1 ? "#2ECC71" : "#E74C3C";
  }}

  function weakColor(pct) {{
    if (pct >= 80) return "#E74C3C";
    if (pct >= 50) return "#E67E22";
    if (pct >= 20) return "#F1C40F";
    return "#2ECC71";
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
    var color = gridMode === "weak" ? weakColor(sq.weak_pct) : pctColor(sq.pct);
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

  function updateGridColors() {{
    Object.keys(gridByCell).forEach(function(name) {{
      if (!gridByCell[name] || !gridByCell[name].gridPolygons) return;
      gridByCell[name].gridPolygons.forEach(function(gp) {{
        var pct = gridMode === "weak" ? gp.sq.weak_pct : gp.sq.pct;
        var colorFn = gridMode === "weak" ? weakColor : pctColor;
        var color = colorFn(pct);
        gp.polygon.setOptions({{ fillColor: color, strokeColor: color }});
      }});
    }});
  }}

  function init() {{
    var lats = sectors.map(function(s){{ return s.lat; }}).filter(function(v){{ return v && !isNaN(v); }});
    var lngs = sectors.map(function(s){{ return s.lng; }}).filter(function(v){{ return v && !isNaN(v); }});
    var cLat = lats.reduce(function(a,b){{ return a+b; }}, 0) / lats.length;
    var cLng = lngs.reduce(function(a,b){{ return a+b; }}, 0) / lngs.length;
    map = new AMap.Map("container", {{ zoom: 13, center: [cLng, cLat], viewMode: '3D', pitch: 50, rotation: 0 }});
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
            }} else if (metric === "coverage") {{
              color = coverageColor(p[2]);
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
          beamOut: metric === "beam" ? beamOut : 0,
          optimalAzimuth: null,
          optimalRatio: null,
          currentRatio: null
        }};
        if (metric === "beam") {{
          fetch("http://localhost:{API_PORT}/api/optimal_azimuth?gnbid=" + encodeURIComponent(s.gnbid) + "&ci=" + encodeURIComponent(s.ci) + "&indoor=" + indoor)
            .then(function(r) {{ return r.json(); }})
            .then(function(opt) {{
              if (mySeq !== scatterLoadSeq[name]) return;
              if (scatterByCell[name]) {{
                scatterByCell[name].optimalAzimuth = opt.optimal_azimuth;
                scatterByCell[name].optimalRatio = opt.optimal_ratio;
                scatterByCell[name].currentRatio = opt.current_ratio;
                renderStats();
              }}
            }});
        }}
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
    Object.keys(scatterByCell).forEach(function(name) {{
      if (scatterByCell[name].markers) {{
        scatterByCell[name].markers.forEach(function(m) {{ m.setMap(map); }});
      }}
    }});
  }}

  function showGrid() {{
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
      var bestOptimal = null, bestOptimalRatio = -1, bestCurrentRatio = null;
      names.forEach(function(name) {{
        var info = scatterByCell[name];
        if (info) {{
          totalCount += info.count;
          totalIn += info.beamIn || 0;
          totalOut += info.beamOut || 0;
          if (info.optimalRatio != null && info.optimalRatio > bestOptimalRatio) {{
            bestOptimalRatio = info.optimalRatio;
            bestOptimal = info.optimalAzimuth;
            bestCurrentRatio = info.currentRatio;
          }}
        }}
      }});
      var inPct = totalCount > 0 ? (totalIn / totalCount * 100).toFixed(1) : "0.0";

      var html = '<div class="stat-row"><span>采样点数</span><span class="stat-val">' + totalCount + '</span></div>';
      html += '<div class="stat-row"><span>波束内</span><span class="stat-val">' + totalIn + '</span></div>';
      html += '<div class="stat-row"><span>波束外</span><span class="stat-val">' + totalOut + '</span></div>';
      html += '<div class="stat-row"><span>波束内占比</span><span class="stat-val">' + inPct + '%</span></div>';
      if (bestOptimal != null) {{
        html += '<div class="stat-row" style="color:#2ECC71"><span>最优方位角</span><span class="stat-val">' + bestOptimal + '° (' + bestOptimalRatio + '%)</span></div>';
      }}
      if (bestCurrentRatio != null) {{
        html += '<div class="stat-row"><span>当前方位角占比</span><span class="stat-val">' + bestCurrentRatio + '%</span></div>';
      }}
      statsEl.innerHTML = html;
      return;
    }}

    if (currentMetric === "coverage") {{
      var totalCount = 0, totalCovered = 0, totalNotCovered = 0;
      names.forEach(function(name) {{
        var info = scatterByCell[name];
        if (info && info.grades) {{
          totalCount += info.count;
          totalNotCovered += info.grades[0] || 0;
          totalCovered += info.grades[1] || 0;
        }}
      }});
      var covPct = totalCount > 0 ? (totalCovered / totalCount * 100).toFixed(1) : "0.0";

      var html = '<div class="stat-row"><span>采样点数</span><span class="stat-val">' + totalCount + '</span></div>';
      html += '<div class="stat-row"><span>覆盖点数</span><span class="stat-val">' + totalCovered + '</span></div>';
      html += '<div class="stat-row"><span>未覆盖点数</span><span class="stat-val">' + totalNotCovered + '</span></div>';
      html += '<div class="stat-row"><span>覆盖率</span><span class="stat-val">' + covPct + '%</span></div>';
      statsEl.innerHTML = html;

      var notCovPct = totalCount > 0 ? (totalNotCovered / totalCount * 100).toFixed(1) : "0.0";
      var legendHtml = '<div class="legend-item">' +
        '<span class="legend-dot" style="background:#2ECC71"></span>' +
        '<span class="legend-label">覆盖(RSRP>-105且SINR>-3)</span>' +
        '<span class="legend-count">' + totalCovered + '</span>' +
        '<span class="legend-pct">' + covPct + '%</span>' +
      '</div>';
      legendHtml += '<div class="legend-item">' +
        '<span class="legend-dot" style="background:#E74C3C"></span>' +
        '<span class="legend-label">未覆盖</span>' +
        '<span class="legend-count">' + totalNotCovered + '</span>' +
        '<span class="legend-pct">' + notCovPct + '%</span>' +
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

    if (gridMode === "weak") {{
      var totalGridCount = 0;
      var totalPlmnCount = 0;
      var totalWeakCount = 0;
      var gradeSquareCounts = [0, 0, 0, 0];
      var gradePlmnCounts = [0, 0, 0, 0];

      names.forEach(function(name) {{
        var info = gridByCell[name];
        if (!info || info.loading) return;
        totalGridCount += info.grid_count;
        if (info.squares) {{
          info.squares.forEach(function(sq) {{
            var wp = sq.weak_pct;
            var gi = wp >= 80 ? 3 : wp >= 50 ? 2 : wp >= 20 ? 1 : 0;
            gradeSquareCounts[gi]++;
            gradePlmnCounts[gi] += sq.plmn_count;
            totalPlmnCount += sq.plmn_count;
            totalWeakCount += sq.weak_count || 0;
          }});
        }}
      }});

      var area = totalGridCount * 25;
      var weakPct = totalPlmnCount > 0 ? (totalWeakCount / totalPlmnCount * 100).toFixed(1) : "0.0";

      var html = '<div class="stat-row"><span>栅格数</span><span class="stat-val">' + totalGridCount + '</span></div>';
      html += '<div class="stat-row"><span>覆盖面积</span><span class="stat-val">' + area + ' m²</span></div>';
      html += '<div class="stat-row"><span>弱覆盖占比</span><span class="stat-val">' + weakPct + '%</span></div>';
      statsEl.innerHTML = html;

      var legendHtml = "";
      for (var i = 0; i < GRADES.weak.length; i++) {{
        var g = GRADES.weak[i];
        var sqCnt = gradeSquareCounts[i];
        var smpPct = totalPlmnCount > 0 ? (gradePlmnCounts[i] / totalPlmnCount * 100).toFixed(1) : "0.0";
        legendHtml += '<div class="legend-item">' +
          '<span class="legend-dot" style="background:' + g.color + '"></span>' +
          '<span class="legend-label">' + g.label + '(' + g.range + ')</span>' +
          '<span class="legend-count">' + sqCnt + '格</span>' +
          '<span class="legend-pct">' + smpPct + '%</span>' +
        '</div>';
      }}
      legendEl.innerHTML = legendHtml;
      return;
    }}

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
      div.innerHTML = "<span>" + s.station_name + " | GNBID:" + (s.gnbid || "") + " CI:" + (s.ci || "") + " | 频点:" + (s.freq || "") + " | 方位角:" + (s.azimuth != null ? s.azimuth : "--") + "° | PCI:" + (s.pci || "--") + " | 挂高:" + (s.height || "--") + "m</span>" +
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
      document.getElementById("grid-controls").style.display = "none";
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
      gridMode = "cell";
      document.getElementById("btn-grid").classList.add("active");
      document.getElementById("btn-scatter").classList.remove("active");
      document.getElementById("scatter-controls").style.display = "none";
      document.getElementById("grid-controls").style.display = "block";
      document.getElementById("btn-cell-pct").classList.add("active");
      document.getElementById("btn-weak-pct").classList.remove("active");
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
      document.getElementById("btn-coverage").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-sinr").onclick = function() {{
      if (currentMetric === "sinr") return;
      currentMetric = "sinr";
      document.getElementById("btn-sinr").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      document.getElementById("btn-beam").classList.remove("active");
      document.getElementById("btn-coverage").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-beam").onclick = function() {{
      if (currentMetric === "beam") return;
      currentMetric = "beam";
      document.getElementById("btn-beam").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      document.getElementById("btn-sinr").classList.remove("active");
      document.getElementById("btn-coverage").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-coverage").onclick = function() {{
      if (currentMetric === "coverage") return;
      currentMetric = "coverage";
      document.getElementById("btn-coverage").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      document.getElementById("btn-sinr").classList.remove("active");
      document.getElementById("btn-beam").classList.remove("active");
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
    document.getElementById("btn-cell-pct").onclick = function() {{
      if (gridMode === "cell") return;
      gridMode = "cell";
      document.getElementById("btn-cell-pct").classList.add("active");
      document.getElementById("btn-weak-pct").classList.remove("active");
      updateGridColors();
      renderStats();
    }};
    document.getElementById("btn-weak-pct").onclick = function() {{
      if (gridMode === "weak") return;
      gridMode = "weak";
      document.getElementById("btn-weak-pct").classList.add("active");
      document.getElementById("btn-cell-pct").classList.remove("active");
      updateGridColors();
      renderStats();
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

    data = load_base_stations()

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
