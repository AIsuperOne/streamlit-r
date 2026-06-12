"""小区分析编排模块：单次DB连接 + 调用algorithm/evaluation完成全部分析"""

import math
import sqlite3

from .config import DB_PATH
from .models import GridSquare, CellAnalysis, FREQ_EXPECTED_RADIUS, load_freq_baseline, _freq_baseline_cache
from .algorithm import (
    calc_bearing, circular_diff, meters_to_degrees,
    calc_grid_origin, build_grid_polygons, count_plmn_in_polygons,
    scatter_optimal_azimuth, grid_weighted_optimal_azimuth, grid_beam_coverage,
)
from .evaluation import compute_rf_metrics, evaluate_cell_results


# ============================================================
# 缓存
# ============================================================

_analysis_cache = {}


# ============================================================
# DB查询函数
# ============================================================

def _query_cell_freq(gnbid, ci, conn):
    row = conn.execute(
        'SELECT "频点" FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
        [gnbid, ci],
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


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


# ============================================================
# 栅格构建（编排：调用algorithm + DB查询）
# ============================================================

def _build_grid_squares(gnbid, ci, indoor, cell_lng_coord, cell_lat_coord, conn):
    """构建栅格，返回 (List[GridSquare], freq, grid_params)"""
    freq = _query_cell_freq(gnbid, ci, conn)
    cell_pts = _query_cell_points(gnbid, ci, indoor, freq, conn)
    if not cell_pts:
        return [], freq
    avg_lat = sum(p[1] for p in cell_pts) / len(cell_pts)
    cell_lng, cell_lat = meters_to_degrees(5.0, avg_lat)
    origin_lng, origin_lat = calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci)
    polygons = build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat)
    bounds_list = [p["bounds"] for p in polygons]
    bounds = (min(b[0] for b in bounds_list), max(b[2] for b in bounds_list),
              min(b[1] for b in bounds_list), max(b[3] for b in bounds_list))
    plmn_rows = _query_plmn_rows(indoor, freq, bounds, conn)
    count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat)

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
    return squares, freq, {"origin_lng": origin_lng, "origin_lat": origin_lat,
                           "cell_lng": cell_lng, "cell_lat": cell_lat, "freq": freq}


# ============================================================
# 核心：一次性分析（单连接）
# ============================================================

def analyze_cell(gnbid, ci, indoor):
    """一次性计算全部散点+栅格+波束+射频+评估指标，单次DB连接"""
    global _analysis_cache
    cache_key = (gnbid, ci, indoor)
    if cache_key in _analysis_cache:
        return _analysis_cache[cache_key]

    conn = sqlite3.connect(DB_PATH)
    try:
        result = _analyze_cell_inner(gnbid, ci, indoor, conn)
    finally:
        conn.close()

    _analysis_cache[cache_key] = result
    return result


def _analyze_cell_inner(gnbid, ci, indoor, conn):
    """核心编排：查询 → 计算 → 评估"""

    # === 查询1：小区基本信息 ===
    row = conn.execute(
        'SELECT "小区中文名", "物理站名", "RRU经度", "RRU纬度", "方位角", '
        '"覆盖类型", "工作频段", "频点", "天线挂高", "nRPCI" '
        'FROM "5GBaseStation" WHERE "Gnbid"=? AND "Cellid"=? LIMIT 1',
        [gnbid, ci],
    ).fetchone()
    if not row:
        raise ValueError(f"未找到小区 GNBID={gnbid} CI={ci}")

    cell_name = row[0] or f"{gnbid}/{ci}"
    cell_lng = float(row[2]) if row[2] else None
    cell_lat = float(row[3]) if row[3] else None
    current_azimuth = float(row[4]) if row[4] is not None else None
    freq = str(row[7]) if row[7] else ""

    # === 查询2：散点统计 ===
    where = 'gnbid=? AND ci=? AND rsrp IS NOT NULL AND sinr IS NOT NULL'
    params = [gnbid, ci]
    if indoor == "1":
        where += ' AND in_out_door="In_Door"'
    elif indoor == "2":
        where += ' AND in_out_door="Out_Door"'

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
        f'SUM(CASE WHEN rsrp>-105 AND sinr>-3 THEN 1 ELSE 0 END), '
        f'SUM(CASE WHEN rsrp<-105 THEN 1 ELSE 0 END), '
        f'SUM(CASE WHEN rsrp<-110 THEN 1 ELSE 0 END) '
        f'FROM "data" WHERE {where}', params,
    ).fetchone()

    sample_count = stat_row[0] or 0
    avg_rsrp = round(stat_row[1], 2) if stat_row[1] is not None else None
    avg_sinr = round(stat_row[2], 2) if stat_row[2] is not None else None
    rsrp_grades = [stat_row[3] or 0, stat_row[4] or 0, stat_row[5] or 0, stat_row[6] or 0]
    sinr_grades = [stat_row[7] or 0, stat_row[8] or 0, stat_row[9] or 0, stat_row[10] or 0]
    covered_count = stat_row[11] or 0
    coverage_rate = round(covered_count / sample_count * 100, 2) if sample_count > 0 else 0
    weak_count_115 = stat_row[12] or 0
    weak_count_120 = stat_row[13] or 0
    weak_coverage_ratio = round(weak_count_115 / sample_count * 100, 1) if sample_count > 0 else 0
    very_weak_ratio = round(weak_count_120 / sample_count * 100, 1) if sample_count > 0 else 0

    # === 查询3：散点方位角 + rsrp（用于波束分析 + RF专项）===
    beam_inner_ratio = None
    bearings = []
    scatter_pts_with_rsrp = []
    if cell_lng is not None and cell_lat is not None and current_azimuth is not None:
        beam_where = 'gnbid=? AND ci=? AND lng IS NOT NULL AND rsrp IS NOT NULL'
        beam_params = [gnbid, ci]
        if indoor == "1":
            beam_where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            beam_where += ' AND in_out_door="Out_Door"'
        pts = conn.execute(f'SELECT lng, lat, rsrp FROM "data" WHERE {beam_where}', beam_params).fetchall()
        for p in pts:
            if p[0] is None or p[1] is None or p[2] is None:
                continue
            bearing = calc_bearing(cell_lng, cell_lat, p[0], p[1])
            bearings.append(bearing)
            scatter_pts_with_rsrp.append((p[0], p[1], p[2]))
        if bearings:
            beam_inner = sum(1 for b in bearings if circular_diff(b, current_azimuth) <= 60)
            beam_inner_ratio = round(beam_inner / len(bearings) * 100, 1)

    # === 查询4：干扰分析 ===
    interf_where = 'gnbid=? AND ci=? AND nr_ssb_rsrp IS NOT NULL AND nr_neighbor_rsrp_list IS NOT NULL AND nr_neighbor_rsrp_list != ""'
    interf_params = [gnbid, ci]
    if indoor == "1":
        interf_where += ' AND in_out_door="In_Door"'
    elif indoor == "2":
        interf_where += ' AND in_out_door="Out_Door"'
    interf_rows = conn.execute(
        f'SELECT lng, lat, nr_ssb_rsrp, nr_earfcn, nr_neighbor_rsrp_list, nr_neighbor_earfcn_list '
        f'FROM "data" WHERE {interf_where}', interf_params
    ).fetchall()
    interf_count = 0
    interf_bad = 0
    diff_sum = 0.0
    interf_azimuth_dist = [0] * 12
    for row in interf_rows:
        try:
            rlng, rlat = row[0], row[1]
            serving_rsrp = float(row[2])
            serving_earfcn = row[3]
            nbr_rsrps = [float(x) for x in row[4].split(',') if x.strip()]
            nbr_earfcns = [x.strip() for x in row[5].split(',') if x.strip()]
        except (ValueError, AttributeError):
            continue
        co_rsrps = [nbr_rsrps[i] for i in range(min(len(nbr_rsrps), len(nbr_earfcns)))
                     if nbr_earfcns[i] == serving_earfcn]
        if not co_rsrps:
            continue
        strongest_co = max(co_rsrps)
        diff = serving_rsrp - strongest_co
        interf_count += 1
        diff_sum += diff
        if diff < 3:
            interf_bad += 1
            if cell_lng is not None and cell_lat is not None and rlng is not None and rlat is not None:
                bearing = calc_bearing(cell_lng, cell_lat, rlng, rlat)
                interf_azimuth_dist[int(bearing / 30) % 12] += 1
    interference_ratio = round(interf_bad / interf_count * 100, 1) if interf_count > 0 else 0
    avg_neighbor_diff = round(diff_sum / interf_count, 1) if interf_count > 0 else None

    # === 查询5：栅格数据 ===
    squares, grid_freq, grid_params = _build_grid_squares(gnbid, ci, indoor, cell_lng, cell_lat, conn)
    grid_count = len(squares)
    grid_area_sqm = grid_count * 25

    # 栅格分级统计
    pct_grades = [0, 0, 0, 0]
    weak_grades = [0, 0, 0, 0]
    valid_grid_count = 0
    for sq in squares:
        p = sq.pct
        pct_grades[0 if p < 20 else 1 if p < 50 else 2 if p < 80 else 3] += 1
        if sq.plmn_count > 0:
            valid_grid_count += 1
            w = sq.weak_pct
            weak_grades[0 if w < 20 else 1 if w < 50 else 2 if w < 80 else 3] += 1

    excellence_rate = round(sum(1 for s in squares if s.plmn_count > 0 and s.weak_pct < 20) / valid_grid_count * 100, 1) if valid_grid_count > 0 else 0

    # PLMN区域统计
    total_plmn_count = 0
    total_plmn_avg_rsrp = None
    if squares:
        all_sw_lng = min(sq.sw_lng for sq in squares)
        all_ne_lng = max(sq.ne_lng for sq in squares)
        all_sw_lat = min(sq.sw_lat for sq in squares)
        all_ne_lat = max(sq.ne_lat for sq in squares)
        plmn_where = 'plmn="46000" AND lng IS NOT NULL AND lng>=? AND lng<=? AND lat>=? AND lat<=?'
        plmn_params = [all_sw_lng, all_ne_lng, all_sw_lat, all_ne_lat]
        if indoor == "1":
            plmn_where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            plmn_where += ' AND in_out_door="Out_Door"'
        if grid_freq:
            plmn_where += " AND nr_earfcn=?"
            plmn_params.append(grid_freq)
        plmn_row = conn.execute(
            f'SELECT COUNT(*), AVG(rsrp) FROM "data" WHERE {plmn_where}', plmn_params
        ).fetchone()
        total_plmn_count = plmn_row[0] or 0
        total_plmn_avg_rsrp = round(plmn_row[1], 2) if plmn_row[1] is not None else None

    # === 纯计算：波束分析 ===
    scatter_opt_az, scatter_opt_ratio = scatter_optimal_azimuth(bearings)
    grid_weighted_opt, beam_total, _ = grid_weighted_optimal_azimuth(squares)
    optimal_beam_coverage_rate = round(beam_total / grid_count * 100, 1) if grid_count > 0 else 0

    beam_grid_ratio, beam_coverage_current, beam_coverage_optimal = grid_beam_coverage(
        squares, current_azimuth, grid_weighted_opt)

    # === 纯计算：RF专项 ===
    rf = compute_rf_metrics(
        scatter_pts_with_rsrp, bearings, cell_lng, cell_lat,
        freq, current_azimuth, beam_inner_ratio, scatter_opt_az)

    # === 纯计算：评估判定 ===
    baseline = load_freq_baseline(indoor)
    freq_avg_samples = baseline.get(freq, {}).get("avg_samples", 0) if baseline else 0
    freq_avg_grids = baseline.get(freq, {}).get("avg_grids", 0) if baseline else 0

    ev = evaluate_cell_results(
        sample_count, freq_avg_samples, grid_count, freq_avg_grids,
        beam_inner_ratio, optimal_beam_coverage_rate,
        beam_coverage_optimal, coverage_rate, excellence_rate,
        interference_ratio, weak_coverage_ratio,
        rf["overshoot_ratio"], rf["backfire_ratio"],
        azimuth_deviation=rf["azimuth_deviation"])

    return CellAnalysis(
        cell_name=cell_name, freq=freq, cell_lng=cell_lng, cell_lat=cell_lat,
        current_azimuth=current_azimuth,
        sample_count=sample_count, avg_rsrp=avg_rsrp, avg_sinr=avg_sinr,
        rsrp_grades=rsrp_grades, sinr_grades=sinr_grades,
        covered_count=covered_count, coverage_rate=coverage_rate,
        beam_inner_ratio=beam_inner_ratio,
        grid_count=grid_count, valid_grid_count=valid_grid_count, grid_area_sqm=grid_area_sqm,
        pct_grades=pct_grades, weak_grades=weak_grades,
        excellence_rate=excellence_rate,
        scatter_optimal_azimuth=scatter_opt_az,
        scatter_optimal_ratio=scatter_opt_ratio,
        grid_weighted_optimal_azimuth=grid_weighted_opt,
        beam_grid_ratio=beam_grid_ratio,
        optimal_beam_coverage_rate=optimal_beam_coverage_rate,
        beam_coverage_current=beam_coverage_current,
        beam_coverage_optimal=beam_coverage_optimal,
        interference_ratio=interference_ratio,
        avg_neighbor_diff=avg_neighbor_diff,
        interference_pass=ev["interference_pass"],
        weak_coverage_ratio=weak_coverage_ratio,
        very_weak_ratio=very_weak_ratio,
        weak_coverage_pass=ev["weak_coverage_pass"],
        weak_azimuth_dist=rf["weak_azimuth_dist"],
        interf_azimuth_dist=interf_azimuth_dist,
        overshoot_ratio=rf["overshoot_ratio"],
        max_distance=rf["max_distance"],
        distance_distribution=rf["distance_distribution"],
        backfire_ratio=rf["backfire_ratio"],
        azimuth_deviation=rf["azimuth_deviation"],
        overshoot_pass=ev["overshoot_pass"],
        backfire_pass=ev["backfire_pass"],
        precision_pass=ev["precision_pass"],
        freq_avg_samples=freq_avg_samples,
        freq_avg_grids=freq_avg_grids,
        traffic_ratio=ev["traffic_ratio"], traffic_pass=ev["traffic_pass"],
        area_ratio=ev["area_ratio"], area_pass=ev["area_pass"],
        beam_pass=ev["beam_pass"], overlap_pass=ev["overlap_pass"], serving_pass=ev["serving_pass"],
        coverage_pass=ev["coverage_pass"], excellence_pass=ev["excellence_pass"],
        overall_pass=ev["overall_pass"],
        scale_score=ev["scale_score"], quality_score=ev["quality_score"],
        rf_score=ev["rf_score"], total_score=ev["total_score"],
        squares=squares,
        grid_params=grid_params,
        total_plmn_count=total_plmn_count,
        total_plmn_avg_rsrp=total_plmn_avg_rsrp,
    )


# ============================================================
# 栅格数据子集（供grid_analysis端点使用）
# ============================================================

def compute_grid_data(gnbid, ci, indoor):
    """计算栅格+波束数据，复用analyze_cell缓存"""
    a = analyze_cell(gnbid, ci, indoor)

    if not a.squares:
        return [], None, 0, None, 0, None, None, None, None

    return (a.squares, a.grid_params, a.total_plmn_count, a.total_plmn_avg_rsrp,
            a.grid_count, a.beam_coverage_current, a.beam_coverage_optimal,
            a.grid_weighted_optimal_azimuth, a.optimal_beam_coverage_rate)
