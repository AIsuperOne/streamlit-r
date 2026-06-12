"""纯算法函数：方位角、距离、栅格构建、波束分析（无DB依赖，无副作用）"""

import math
import random
from typing import List, Optional, Tuple

from .models import GridSquare


# ============================================================
# 方位角与距离
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


def haversine_meters(lng1, lat1, lng2, lat2):
    """Haversine距离(米)"""
    R = 6371000
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ============================================================
# 栅格构建
# ============================================================

def calc_grid_origin(cell_pts, cell_lng, cell_lat, gnbid, ci):
    """确定栅格原点：random.seed选参考点，偏移半格"""
    random.seed(f"{gnbid}_{ci}")
    ref_idx = random.randint(0, len(cell_pts) - 1)
    return cell_pts[ref_idx][0] - cell_lng / 2, cell_pts[ref_idx][1] - cell_lat / 2


def build_grid_polygons(cell_pts, origin_lng, origin_lat, cell_lng, cell_lat):
    """数学落格：gx=floor((lng-origin)/cell_size)，统计每格cell_count"""
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
                         "rsrp_sum": 0.0, "rsrp_count": 0, "sinr_sum": 0.0, "sinr_count": 0,
                         "bounds": (sw_lng, sw_lat, ne_lng, ne_lat)})
    return polygons


def count_plmn_in_polygons(plmn_rows, polygons, origin_lng, origin_lat, cell_lng, cell_lat):
    """数学落格映射PLMN归属：gx/gy键判定 → 统计plmn_count/weak_count/rsrp_sum/sinr_sum"""
    poly_map = { (p["gx"], p["gy"]): p for p in polygons }
    for r in plmn_rows:
        lng, lat = r[0], r[1]
        gx = math.floor((lng - origin_lng) / cell_lng)
        gy = math.floor((lat - origin_lat) / cell_lat)
        key = (gx, gy)
        if key in poly_map:
            poly_map[key]["plmn_count"] += 1
            rsrp, sinr = r[2], r[3]
            if rsrp is not None:
                poly_map[key]["rsrp_sum"] += rsrp
                poly_map[key]["rsrp_count"] += 1
            if sinr is not None:
                poly_map[key]["sinr_sum"] += sinr
                poly_map[key]["sinr_count"] += 1
            if rsrp is not None and sinr is not None and (rsrp < -105 or sinr < -3):
                poly_map[key]["weak_count"] += 1


# ============================================================
# 波束分析
# ============================================================

def scatter_optimal_azimuth(bearings):
    """散点法最优方位角：区间覆盖法，5°步长扫描72个角度"""
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


def grid_weighted_optimal_azimuth(squares: List[GridSquare]):
    """栅格加权法最优方位角：pct加权的区间扫描"""
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


def grid_beam_coverage(squares: List[GridSquare], current_azimuth, grid_weighted_optimal):
    """栅格波束覆盖：当前/最优方位角下pct≥50%栅格占比

    返回 (beam_grid_ratio, current_rate, optimal_rate)
    """
    if not squares or current_azimuth is None:
        return None, None, None

    current_beam = [sq for sq in squares if sq.bearing is not None and circular_diff(sq.bearing, current_azimuth) <= 60]
    beam_grid_ratio = round(len(current_beam) / len(squares) * 100, 1) if squares else 0
    current_rate = round(sum(1 for sq in current_beam if sq.pct >= 50) / len(current_beam) * 100, 1) if current_beam else 0

    if grid_weighted_optimal is not None:
        opt_beam = [sq for sq in squares if sq.bearing is not None and circular_diff(sq.bearing, grid_weighted_optimal) <= 60]
        optimal_rate = round(sum(1 for sq in opt_beam if sq.pct >= 50) / len(opt_beam) * 100, 1) if opt_beam else 0
    else:
        optimal_rate = None

    return beam_grid_ratio, current_rate, optimal_rate
