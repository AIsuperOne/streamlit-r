"""一次性小区分析模块：3次DB查询计算全部散点+栅格+评估指标"""

import json
import math
import os
import random
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from shapely.geometry import Polygon as ShapelyPolygon, Point

from .config import DB_PATH, OUTPUT_DIR


# ============================================================
# 数据结构
# ============================================================

@dataclass
class GridSquare:
    """单个栅格，含全部属性（可视化+评估+波束分析）"""
    gx: int
    gy: int
    count: int           # 小区采样点数
    plmn_count: int      # PLMN总采样点数
    pct: int             # count / plmn_count * 100
    weak_count: int      # 弱覆盖点数(rsrp<-105或sinr<-3)
    weak_pct: int        # weak_count / plmn_count * 100
    center_lng: float    # 栅格中心经度
    center_lat: float    # 栅格中心纬度
    sw_lng: float        # 西南角经度
    sw_lat: float
    ne_lng: float        # 东北角经度
    ne_lat: float
    bearing: Optional[float] = None  # 从小区到栅格中心的方位角


@dataclass
class CellAnalysis:
    """一次性分析结果，涵盖散点+栅格+波束+评估全部指标"""
    # 基础信息
    cell_name: str
    freq: str
    cell_lng: Optional[float]
    cell_lat: Optional[float]
    current_azimuth: Optional[float]

    # 散点指标
    sample_count: int
    avg_rsrp: Optional[float]
    avg_sinr: Optional[float]
    rsrp_grades: List[int]        # [优≥-95, 中-105~-95, 差-115~-105, 极差<-115]
    sinr_grades: List[int]        # [优≥15, 中5~15, 差-3~5, 极差<-3]
    covered_count: int            # RSRP>-105 且 SINR>-3
    coverage_rate: Optional[float]
    beam_inner_ratio: Optional[float]  # 波束内占比（散点）

    # 栅格指标
    grid_count: int
    valid_grid_count: int   # plmn_count>0的有效栅格数
    grid_area_sqm: int
    pct_grades: List[int]         # [非主服0~20%, 弱主服20~50%, 竞争50~80%, 主控80~100%]
    weak_grades: List[int]        # [覆盖优良0~20%, 覆盖良好20~50%, 覆盖稍弱50~80%, 覆盖很弱80~100%]
    excellence_rate: float        # 弱覆盖<20%的栅格占比

    # 波束分析
    scatter_optimal_azimuth: Optional[int]      # 最优方位角（散点）
    scatter_optimal_ratio: Optional[float]
    grid_weighted_optimal_azimuth: Optional[int]  # 栅格加权法最优方位角(维度4)
    optimal_beam_coverage_rate: Optional[float]  # 最优波束占比（栅格）
    beam_coverage_current: Optional[float]      # 当前波束pct≥50%栅格占比
    beam_coverage_optimal: Optional[float]      # 最优主服波束占比（栅格）

    # 频点基准
    freq_avg_samples: float
    freq_avg_grids: float

    # 评估结果
    traffic_ratio: float
    traffic_pass: bool
    area_ratio: float
    area_pass: bool
    beam_pass: bool
    overlap_pass: bool
    coverage_pass: bool
    excellence_pass: bool
    overall_pass: bool


# ============================================================
# 共享辅助函数
# ============================================================

def calc_bearing(lng1, lat1, lng2, lat2):
    """Haversine方位角：正北0°，顺时针0~360°"""
    d_lng = math.radians(lng2 - lng1)
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    y = math.sin(d_lng) * math.cos(lat2_r)
    x = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(d_lng)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def circular_diff(a, b):
    """两角度最小差值(0~180°)"""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def meters_to_degrees(meters, avg_lat):
    """米→经纬度偏移"""
    lng_offset = meters / 111320.0 / math.cos(avg_lat * math.pi / 180.0)
    lat_offset = meters / 110540.0
    return lng_offset, lat_offset


# ============================================================
# 频点基准
# ============================================================

_freq_baseline_cache = {}
_analysis_cache = {}


def _load_freq_baseline(indoor):
    global _freq_baseline_cache
    if indoor in _freq_baseline_cache:
        return _freq_baseline_cache[indoor]
    path = os.path.join(OUTPUT_DIR, f"freq_baseline_indoor{indoor}.json")
    if not os.path.exists(path):
        _freq_baseline_cache[indoor] = None
        return None
    with open(path, "r") as f:
        data = json.load(f)
    _freq_baseline_cache[indoor] = data
    return data


# ============================================================
# 栅格构建（统一含center/sw/ne/bearing）
# ============================================================

def _build_grid_squares(gnbid, ci, indoor, cell_lng_coord, cell_lat_coord):
    """构建栅格，返回 List[GridSquare]，含全部属性

    与app.py和skills/__init__.py不同，此函数统一输出含center/sw/ne/bearing的GridSquare，
    避免需要两套_compute_grid_squares和_compute_grid_squares_with_center。
    """
    freq = _query_cell_freq(gnbid, ci)
    conn = sqlite3.connect(DB_PATH)
    try:
        cell_pts = _query_cell_points(gnbid, ci, indoor, freq, conn)
        if not cell_pts:
            return [], freq
        avg_lat = sum(p[1] for p in cell_pts) / len(cell_pts)
        cell_lng, cell_lat = meters_to_degrees(5.0, avg_lat)
        origin_lng, origin_lat = _calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci)
        polygons = _build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat)
        bounds_list = [p["bounds"] for p in polygons]
        bounds = (min(b[0] for b in bounds_list), max(b[2] for b in bounds_list),
                  min(b[1] for b in bounds_list), max(b[3] for b in bounds_list))
        plmn_rows = _query_plmn_rows(indoor, freq, bounds, conn)
    finally:
        conn.close()
    _count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat)

    squares = []
    for p in polygons:
        pct = round(p["cell_count"] / p["plmn_count"] * 100) if p["plmn_count"] > 0 else 0
        weak_pct = round(p["weak_count"] / p["plmn_count"] * 100) if p["plmn_count"] > 0 else 0
        center_lng = origin_lng + (p["gx"] + 0.5) * cell_lng
        center_lat = origin_lat + (p["gy"] + 0.5) * cell_lat
        bnd = p["bounds"]
        bearing = None
        if cell_lng_coord is not None and cell_lat_coord is not None:
            bearing = calc_bearing(cell_lng_coord, cell_lat_coord, center_lng, center_lat)
        squares.append(GridSquare(
            gx=p["gx"], gy=p["gy"], count=p["cell_count"],
            plmn_count=p["plmn_count"], pct=pct,
            weak_count=p["weak_count"], weak_pct=weak_pct,
            center_lng=center_lng, center_lat=center_lat,
            sw_lng=bnd[0], sw_lat=bnd[1], ne_lng=bnd[2], ne_lat=bnd[3],
            bearing=bearing,
        ))
    return squares, freq


def _query_cell_freq(gnbid, ci):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT "频点" FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
            [gnbid, ci],
        ).fetchone()
        return str(row[0]) if row and row[0] else ""
    finally:
        conn.close()


def _query_cell_points(gnbid, ci, indoor, freq, conn):
    conditions = ['gnbid=?', 'ci=?', 'rsrp IS NOT NULL', 'sinr IS NOT NULL', 'lng IS NOT NULL']
    params = [gnbid, ci]
    if indoor == "1":
        conditions.append('in_out_door="In_Door"')
    elif indoor == "2":
        conditions.append('in_out_door="Out_Door"')
    if freq:
        conditions.append("nr_earfcn=?")
        params.append(freq)
    where = " AND ".join(conditions)
    rows = conn.execute(f'SELECT lng, lat FROM "data" WHERE {where}', params).fetchall()
    return [(r[0], r[1]) for r in rows if r[0] is not None and r[1] is not None]


def _calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci):
    random.seed(f"{gnbid}_{ci}")
    ref_idx = random.randint(0, len(cell_pts) - 1)
    return cell_pts[ref_idx][0] - cell_lng / 2, cell_pts[ref_idx][1] - cell_lat / 2


def _build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat):
    cell_grid = {}
    for lng, lat in cell_pts:
        gx = math.floor((lng - origin_lng) / cell_lng)
        gy = math.floor((lat - origin_lat) / cell_lat)
        cell_grid[(gx, gy)] = cell_grid.get((gx, gy), 0) + 1
    polygons = []
    for (gx, gy), count in cell_grid.items():
        sw_lng = origin_lng + gx * cell_lng
        sw_lat = origin_lat + gy * cell_lat
        ne_lng = origin_lng + (gx + 1) * cell_lng
        ne_lat = origin_lat + (gy + 1) * cell_lat
        polygons.append({"gx": gx, "gy": gy, "cell_count": count, "plmn_count": 0, "weak_count": 0,
                         "bounds": (sw_lng, sw_lat, ne_lng, ne_lat)})
    return polygons


def _query_plmn_rows(indoor, freq, bounds, conn):
    conditions = ['plmn="46000"', 'lng IS NOT NULL', 'lng>=?', 'lng<=?', 'lat>=?', 'lat<=?']
    params = list(bounds)
    if indoor == "1":
        conditions.append('in_out_door="In_Door"')
    elif indoor == "2":
        conditions.append('in_out_door="Out_Door"')
    if freq:
        conditions.append("nr_earfcn=?")
        params.append(freq)
    where = " AND ".join(conditions)
    rows = conn.execute(f'SELECT lng, lat, rsrp, sinr FROM "data" WHERE {where}', params).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows if r[0] is not None and r[1] is not None]


def _count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat):
    poly_map = {}
    for p in polygons:
        b = p["bounds"]
        poly_map[(p["gx"], p["gy"])] = {"info": p, "polygon": ShapelyPolygon([
            (b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])])}
    for r in plmn_rows:
        lng, lat = r[0], r[1]
        pt = Point(lng, lat)
        gx = math.floor((lng - origin_lng) / cell_lng)
        gy = math.floor((lat - origin_lat) / cell_lat)
        key = (gx, gy)
        if key in poly_map and poly_map[key]["polygon"].covers(pt):
            poly_map[key]["info"]["plmn_count"] += 1
            rsrp, sinr = r[2], r[3]
            if rsrp is not None and sinr is not None and (rsrp < -105 or sinr < -3):
                poly_map[key]["info"]["weak_count"] += 1


# ============================================================
# 波束分析算法
# ============================================================

def _scatter_optimal_azimuth(bearings):
    """散点法最优方位角：区间覆盖法，找波束内散点最多的方位角

    用于维度3(覆盖最优性)：直接反映"天线是否对准用户"
    """
    if not bearings:
        return None, None
    half_beam_steps = 12  # ±60°/5 = 12
    angle_counts = [0] * 72
    for b in bearings:
        center = int(round(b / 5)) % 72
        for offset in range(-half_beam_steps, half_beam_steps + 1):
            angle_counts[(center + offset) % 72] += 1
    best_idx = max(range(72), key=lambda i: angle_counts[i])
    return best_idx * 5, round(angle_counts[best_idx] / len(bearings) * 100, 1)


def _grid_weighted_optimal_azimuth(squares):
    """栅格加权法最优方位角：pct加权的区间扫描

    用于维度4(重叠覆盖)：反映"哪个方向小区主导性最强"
    """
    if not squares:
        return None, 0, 0.0
    bearings_weights = []
    for sq in squares:
        if sq.bearing is not None:
            bearings_weights.append((sq.bearing, sq.pct / 100.0))
    if not bearings_weights:
        return None, 0, 0.0

    half_beam_steps = 12
    angle_weights = [0.0] * 72
    for b, w in bearings_weights:
        center = int(round(b / 5)) % 72
        for offset in range(-half_beam_steps, half_beam_steps + 1):
            angle_weights[(center + offset) % 72] += w
    best_idx = max(range(72), key=lambda i: angle_weights[i])
    optimal_azimuth = best_idx * 5

    beam_total = 0
    for sq in squares:
        if sq.bearing is not None and circular_diff(sq.bearing, optimal_azimuth) <= 60:
            beam_total += 1

    return optimal_azimuth, beam_total, round(angle_weights[best_idx], 1)


def _grid_beam_coverage(squares, current_azimuth, grid_weighted_optimal):
    """栅格波束覆盖：当前/最优方位角下pct≥50%栅格占比

    用于前端栅格波束模式显示
    """
    if not squares or current_azimuth is None:
        return None, None

    # 当前方位角波束覆盖
    current_beam = [sq for sq in squares if sq.bearing is not None and circular_diff(sq.bearing, current_azimuth) <= 60]
    current_rate = round(sum(1 for sq in current_beam if sq.pct >= 50) / len(current_beam) * 100, 1) if current_beam else 0

    # 最优方位角波束覆盖
    if grid_weighted_optimal is not None:
        opt_beam = [sq for sq in squares if sq.bearing is not None and circular_diff(sq.bearing, grid_weighted_optimal) <= 60]
        optimal_rate = round(sum(1 for sq in opt_beam if sq.pct >= 50) / len(opt_beam) * 100, 1) if opt_beam else 0
    else:
        optimal_rate = None

    return current_rate, optimal_rate


# ============================================================
# 核心：一次性分析
# ============================================================

def analyze_cell(gnbid, ci, indoor):
    """一次性计算全部散点+栅格+波束+评估指标，3次DB连接"""
    global _analysis_cache
    cache_key = (gnbid, ci, indoor)
    if cache_key in _analysis_cache:
        return _analysis_cache[cache_key]

    # === 查询1：小区基本信息 ===
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT "小区中文名", "物理站名", "RRU经度", "RRU纬度", "方位角", '
            '"覆盖类型", "工作频段", "频点", "天线挂高", "nRPCI" '
            'FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
            [gnbid, ci],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"未找到小区 GNBID={gnbid} CI={ci}")

    cell_name = row[0] or f"{gnbid}/{ci}"
    cell_lng = float(row[2]) if row[2] else None
    cell_lat = float(row[3]) if row[3] else None
    current_azimuth = float(row[4]) if row[4] is not None else None
    freq = str(row[7]) if row[7] else ""

    # === 查询2：散点数据 ===
    where = 'gnbid=? AND ci=? AND rsrp IS NOT NULL AND sinr IS NOT NULL'
    params = [gnbid, ci]
    if indoor == "1":
        where += ' AND in_out_door="In_Door"'
    elif indoor == "2":
        where += ' AND in_out_door="Out_Door"'

    conn = sqlite3.connect(DB_PATH)
    try:
        stat_row = conn.execute(
            f'SELECT COUNT(*), AVG(rsrp), AVG(sinr), '
            f'SUM(CASE WHEN rsrp>=-95 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN rsrp>=-105 AND rsrp<-95 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN rsrp>=-115 AND rsrp<-105 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN rsrp<-115 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN sinr>=15 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN sinr>=5 AND sinr<15 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN sinr>=-3 AND sinr<5 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN sinr<-3 THEN 1 ELSE 0 END), '
            f'SUM(CASE WHEN rsrp>-105 AND sinr>-3 THEN 1 ELSE 0 END) '
            f'FROM "data" WHERE {where}', params,
        ).fetchone()
    finally:
        conn.close()

    sample_count = stat_row[0] or 0
    avg_rsrp = round(stat_row[1], 2) if stat_row[1] is not None else None
    avg_sinr = round(stat_row[2], 2) if stat_row[2] is not None else None
    rsrp_grades = [stat_row[3] or 0, stat_row[4] or 0, stat_row[5] or 0, stat_row[6] or 0]
    sinr_grades = [stat_row[7] or 0, stat_row[8] or 0, stat_row[9] or 0, stat_row[10] or 0]
    covered_count = stat_row[11] or 0
    coverage_rate = round(covered_count / sample_count * 100, 2) if sample_count > 0 else 0

    # 散点方位角 + 波束内占比
    beam_inner_ratio = None
    bearings = []
    if cell_lng is not None and cell_lat is not None and current_azimuth is not None:
        beam_where = 'gnbid=? AND ci=? AND lng IS NOT NULL'
        beam_params = [gnbid, ci]
        if indoor == "1":
            beam_where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            beam_where += ' AND in_out_door="Out_Door"'
        conn = sqlite3.connect(DB_PATH)
        try:
            pts = conn.execute(f'SELECT lng, lat FROM "data" WHERE {beam_where}', beam_params).fetchall()
        finally:
            conn.close()
        bearings = [calc_bearing(cell_lng, cell_lat, p[0], p[1])
                    for p in pts if p[0] is not None and p[1] is not None]
        if bearings:
            beam_inner = sum(1 for b in bearings if circular_diff(b, current_azimuth) <= 60)
            beam_inner_ratio = round(beam_inner / len(bearings) * 100, 1)

    # === 查询3：栅格数据 ===
    squares, grid_freq = _build_grid_squares(gnbid, ci, indoor, cell_lng, cell_lat)
    grid_count = len(squares)
    grid_area_sqm = grid_count * 25

    # 栅格分级统计
    pct_grades = [0, 0, 0, 0]  # [非主服0~20%, 弱主服20~50%, 竞争50~80%, 主控80~100%]
    weak_grades = [0, 0, 0, 0]  # [覆盖优良0~20%, 覆盖良好20~50%, 覆盖稍弱50~80%, 覆盖很弱80~100%]
    valid_grid_count = 0
    for sq in squares:
        p = sq.pct
        pct_grades[0 if p < 20 else 1 if p < 50 else 2 if p < 80 else 3] += 1
        if sq.plmn_count > 0:
            valid_grid_count += 1
            w = sq.weak_pct
            weak_grades[0 if w < 20 else 1 if w < 50 else 2 if w < 80 else 3] += 1

    excellence_rate = round(sum(1 for s in squares if s.plmn_count > 0 and s.weak_pct < 20) / valid_grid_count * 100, 1) if valid_grid_count > 0 else 0

    # === 纯计算阶段：波束分析 ===
    scatter_optimal_azimuth, scatter_optimal_ratio = _scatter_optimal_azimuth(bearings)
    grid_weighted_opt, beam_total, _ = _grid_weighted_optimal_azimuth(squares)
    optimal_beam_coverage_rate = round(beam_total / grid_count * 100, 1) if grid_count > 0 else 0

    beam_coverage_current, beam_coverage_optimal = _grid_beam_coverage(
        squares, current_azimuth, grid_weighted_opt)

    # === 纯计算阶段：频点基准 + 评估判定 ===
    baseline = _load_freq_baseline(indoor)
    freq_avg_samples = baseline.get(freq, {}).get("avg_samples", 0) if baseline else 0
    freq_avg_grids = baseline.get(freq, {}).get("avg_grids", 0) if baseline else 0

    traffic_ratio = round(sample_count / freq_avg_samples, 2) if freq_avg_samples > 0 else 0
    traffic_pass = traffic_ratio >= 1.0
    area_ratio = round(grid_count / freq_avg_grids, 2) if freq_avg_grids > 0 else 0
    area_pass = area_ratio >= 1.0
    beam_pass = beam_inner_ratio is not None and beam_inner_ratio >= 60
    overlap_pass = (beam_coverage_optimal or 0) >= 70
    coverage_pass = coverage_rate > 95
    excellence_pass = excellence_rate > 95
    overall_pass = (traffic_pass and area_pass and beam_pass and
                    overlap_pass and coverage_pass and excellence_pass)

    result = CellAnalysis(
        cell_name=cell_name, freq=freq, cell_lng=cell_lng, cell_lat=cell_lat,
        current_azimuth=current_azimuth,
        sample_count=sample_count, avg_rsrp=avg_rsrp, avg_sinr=avg_sinr,
        rsrp_grades=rsrp_grades, sinr_grades=sinr_grades,
        covered_count=covered_count, coverage_rate=coverage_rate,
        beam_inner_ratio=beam_inner_ratio,
        grid_count=grid_count, valid_grid_count=valid_grid_count, grid_area_sqm=grid_area_sqm,
        pct_grades=pct_grades, weak_grades=weak_grades,
        excellence_rate=excellence_rate,
        scatter_optimal_azimuth=scatter_optimal_azimuth,
        scatter_optimal_ratio=scatter_optimal_ratio,
        grid_weighted_optimal_azimuth=grid_weighted_opt,
        optimal_beam_coverage_rate=optimal_beam_coverage_rate,
        beam_coverage_current=beam_coverage_current,
        beam_coverage_optimal=beam_coverage_optimal,
        freq_avg_samples=freq_avg_samples,
        freq_avg_grids=freq_avg_grids,
        traffic_ratio=traffic_ratio, traffic_pass=traffic_pass,
        area_ratio=area_ratio, area_pass=area_pass,
        beam_pass=beam_pass, overlap_pass=overlap_pass,
        coverage_pass=coverage_pass, excellence_pass=excellence_pass,
        overall_pass=overall_pass,
    )
    _analysis_cache[cache_key] = result
    return result


# ============================================================
# 栅格数据子集（供grid_analysis端点使用）
# ============================================================

def compute_grid_data(gnbid, ci, indoor):
    """计算栅格+波束数据，返回(squares, grid_params, beam_result)

    供_handle_grid_analysis使用，不包含散点统计和评估判定
    """
    freq = _query_cell_freq(gnbid, ci)
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            'SELECT "RRU经度", "RRU纬度", "方位角" FROM "5GBaseStation" '
            'WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
            [gnbid, ci],
        ).fetchone()
    finally:
        conn.close()
    cell_lng = float(row[0]) if row and row[0] else None
    cell_lat = float(row[1]) if row and row[1] else None
    current_azimuth = float(row[2]) if row and row[2] is not None else None

    squares, _ = _build_grid_squares(gnbid, ci, indoor, cell_lng, cell_lat)

    if not squares:
        return [], None, 0, None, 0, None, None, None

    # grid_params
    avg_lat = sum(sq.center_lat for sq in squares) / len(squares)
    cell_lng_deg, cell_lat_deg = meters_to_degrees(5.0, avg_lat)
    # 从第一个栅格反推origin
    first = squares[0]
    origin_lng = first.sw_lng - first.gx * cell_lng_deg
    origin_lat = first.sw_lat - first.gy * cell_lat_deg
    grid_params = {
        "origin_lng": origin_lng, "origin_lat": origin_lat,
        "cell_lng": cell_lng_deg, "cell_lat": cell_lat_deg, "freq": freq,
    }

    # PLMN统计（用于total_count和total_avg_rsrp）
    all_sw_lng = min(sq.sw_lng for sq in squares)
    all_ne_lng = max(sq.ne_lng for sq in squares)
    all_sw_lat = min(sq.sw_lat for sq in squares)
    all_ne_lat = max(sq.ne_lat for sq in squares)
    bounds = (all_sw_lng, all_ne_lng, all_sw_lat, all_ne_lat)

    conn = sqlite3.connect(DB_PATH)
    try:
        plmn_where = 'plmn="46000" AND lng IS NOT NULL AND lng>=? AND lng<=? AND lat>=? AND lat<=?'
        plmn_params = list(bounds)
        if indoor == "1":
            plmn_where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            plmn_where += ' AND in_out_door="Out_Door"'
        if freq:
            plmn_where += " AND nr_earfcn=?"
            plmn_params.append(freq)
        rows = conn.execute(
            f'SELECT COUNT(*), AVG(rsrp) FROM "data" WHERE {plmn_where}', plmn_params
        ).fetchone()
    finally:
        conn.close()
    total_count = rows[0] or 0
    total_avg_rsrp = round(rows[1], 2) if rows[1] is not None else None

    # 波束覆盖
    grid_weighted_opt, beam_total, _ = _grid_weighted_optimal_azimuth(squares)
    optimal_beam_coverage_rate = round(beam_total / len(squares) * 100, 1) if squares else 0
    beam_current, beam_optimal = _grid_beam_coverage(squares, current_azimuth, grid_weighted_opt)

    return squares, grid_params, total_count, total_avg_rsrp, len(squares), beam_current, beam_optimal, grid_weighted_opt, optimal_beam_coverage_rate
