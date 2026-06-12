"""评估标准：合格线常量 + RF专项计算 + 评估判定"""

from typing import List, Optional

from .models import FREQ_EXPECTED_RADIUS
from .algorithm import calc_bearing, circular_diff, haversine_meters


# ============================================================
# 评估合格线
# ============================================================

TRAFFIC_THRESHOLD = 1.0
AREA_THRESHOLD = 1.0
BEAM_RATIO_THRESHOLD = 60
BEAM_GRID_THRESHOLD = 80
SERVING_THRESHOLD = 80
COVERAGE_THRESHOLD = 95
EXCELLENCE_THRESHOLD = 95
WEAK_COVERAGE_THRESHOLD = 5
OVERSHOOT_THRESHOLD = 10
BACKFIRE_THRESHOLD = 10
INTERFERENCE_THRESHOLD = 10
PRECISION_DEVIATION_THRESHOLD = 30


# ============================================================
# RF专项指标计算
# ============================================================

def compute_rf_metrics(scatter_pts_with_rsrp, bearings, cell_lng, cell_lat,
                       freq, current_azimuth, beam_inner_ratio,
                       scatter_optimal_azimuth_val):
    """从散点数据计算RF专项指标（纯计算，无DB访问）

    Args:
        scatter_pts_with_rsrp: [(lng, lat, rsrp), ...]
        bearings: 散点方位角列表
        cell_lng/cell_lat: 基站经纬度
        freq: 频段
        current_azimuth: 当前方位角
        beam_inner_ratio: 波束内占比（散点）
        scatter_optimal_azimuth_val: 散点法最优方位角

    Returns:
        dict: {weak_azimuth_dist, overshoot_ratio, max_distance,
               distance_distribution, backfire_ratio, azimuth_deviation}
    """
    # 弱覆盖方位分布（使用已计算的bearings，避免重复计算）
    weak_azimuth_dist = [0] * 12
    for i, (lng, lat, rsrp) in enumerate(scatter_pts_with_rsrp):
        if rsrp < -105 and i < len(bearings):
            weak_azimuth_dist[int(bearings[i] / 30) % 12] += 1

    # 距离分布
    overshoot_ratio = 0.0
    max_distance = 0
    distance_distribution = [0, 0, 0, 0]
    if cell_lng is not None and cell_lat is not None and scatter_pts_with_rsrp:
        expected_r = FREQ_EXPECTED_RADIUS.get(freq, 800)
        near, mid, far, overshoot = 0, 0, 0, 0
        for lng, lat, _ in scatter_pts_with_rsrp:
            d = haversine_meters(cell_lng, cell_lat, lng, lat)
            if d > max_distance:
                max_distance = d
            ratio = d / expected_r if expected_r > 0 else 0
            if ratio < 0.5:
                near += 1
            elif ratio < 1.0:
                mid += 1
            elif ratio < 2.0:
                far += 1
            else:
                overshoot += 1
        total = near + mid + far + overshoot
        overshoot_ratio = round(overshoot / total * 100, 1) if total > 0 else 0
        distance_distribution = [near, mid, far, overshoot]
    max_distance = round(max_distance)

    # 背向覆盖：正对反方向±60°内的采样点占比
    backfire_ratio = 0
    if current_azimuth is not None and bearings:
        opposite = (current_azimuth + 180) % 360
        backfire_count = sum(1 for b in bearings if circular_diff(b, opposite) <= 60)
        backfire_ratio = round(backfire_count / len(bearings) * 100, 1)

    # 方位角偏差
    azimuth_deviation = None
    if scatter_optimal_azimuth_val is not None and current_azimuth is not None:
        azimuth_deviation = round(circular_diff(scatter_optimal_azimuth_val, current_azimuth), 1)

    return {
        "weak_azimuth_dist": weak_azimuth_dist,
        "overshoot_ratio": overshoot_ratio,
        "max_distance": max_distance,
        "distance_distribution": distance_distribution,
        "backfire_ratio": backfire_ratio,
        "azimuth_deviation": azimuth_deviation,
    }


# ============================================================
# 评估判定
# ============================================================

def evaluate_cell_results(sample_count, freq_avg_samples, grid_count, freq_avg_grids,
                          beam_inner_ratio, optimal_beam_coverage_rate,
                          beam_coverage_optimal, coverage_rate, excellence_rate,
                          interference_ratio, weak_coverage_ratio,
                          overshoot_ratio, backfire_ratio, azimuth_deviation=None):
    """根据原始指标计算各维度ratio和pass/fail判定

    Returns:
        dict: {traffic_ratio, traffic_pass, area_ratio, area_pass,
               beam_pass, overlap_pass, serving_pass, coverage_pass,
               excellence_pass, interference_pass, weak_coverage_pass,
               overshoot_pass, backfire_pass, precision_pass, overall_pass}
    """
    traffic_ratio = round(sample_count / freq_avg_samples, 2) if freq_avg_samples > 0 else 0
    traffic_pass = traffic_ratio >= TRAFFIC_THRESHOLD

    area_ratio = round(grid_count / freq_avg_grids, 2) if freq_avg_grids > 0 else 0
    area_pass = area_ratio >= AREA_THRESHOLD

    beam_pass = beam_inner_ratio is not None and beam_inner_ratio >= BEAM_RATIO_THRESHOLD
    overlap_pass = (optimal_beam_coverage_rate or 0) >= BEAM_GRID_THRESHOLD
    serving_pass = (beam_coverage_optimal or 0) >= SERVING_THRESHOLD
    coverage_pass = coverage_rate >= COVERAGE_THRESHOLD
    excellence_pass = excellence_rate >= EXCELLENCE_THRESHOLD
    interference_pass = interference_ratio <= INTERFERENCE_THRESHOLD
    weak_coverage_pass = weak_coverage_ratio <= WEAK_COVERAGE_THRESHOLD
    overshoot_pass = overshoot_ratio <= OVERSHOOT_THRESHOLD
    backfire_pass = backfire_ratio <= BACKFIRE_THRESHOLD
    precision_pass = azimuth_deviation is not None and azimuth_deviation <= PRECISION_DEVIATION_THRESHOLD

    overall_pass = (traffic_pass and area_pass and beam_pass and
                    overlap_pass and serving_pass and coverage_pass and excellence_pass and
                    interference_pass and weak_coverage_pass and overshoot_pass and
                    backfire_pass and precision_pass)

    # 评分：规模50 + 质量30 + 射频20 = 100
    scale_score = min(50, round(50 * traffic_ratio * area_ratio, 1))
    quality_score = min(30, round(30 * (coverage_rate / COVERAGE_THRESHOLD) * (excellence_rate / EXCELLENCE_THRESHOLD), 1))
    rf_score = sum(4 for p in [weak_coverage_pass, overshoot_pass, precision_pass, interference_pass, backfire_pass] if p)
    total_score = round(scale_score + quality_score + rf_score, 1)

    return {
        "traffic_ratio": traffic_ratio,
        "traffic_pass": traffic_pass,
        "area_ratio": area_ratio,
        "area_pass": area_pass,
        "beam_pass": beam_pass,
        "overlap_pass": overlap_pass,
        "serving_pass": serving_pass,
        "coverage_pass": coverage_pass,
        "excellence_pass": excellence_pass,
        "interference_pass": interference_pass,
        "weak_coverage_pass": weak_coverage_pass,
        "overshoot_pass": overshoot_pass,
        "backfire_pass": backfire_pass,
        "precision_pass": precision_pass,
        "overall_pass": overall_pass,
        "scale_score": scale_score,
        "quality_score": quality_score,
        "rf_score": rf_score,
        "total_score": total_score,
    }
