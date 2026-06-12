from ..analysis import analyze_cell


def _weak_direction(azimuth_dist):
    """从12扇区分布中找弱覆盖集中方向"""
    if not azimuth_dist or max(azimuth_dist) == 0:
        return None
    max_idx = max(range(12), key=lambda i: azimuth_dist[i])
    return max_idx * 30 + 15


def _interf_direction(azimuth_dist):
    """从12扇区分布中找干扰集中方向"""
    return _weak_direction(azimuth_dist)


def _build_problems(a):
    """构建不合格项列表，按优先级排序：射频5项 > 质量2项 > 结构3项 > 规模2项"""
    problems = []
    # 射频5项
    if not a.weak_coverage_pass:
        detail = None
        wd = _weak_direction(a.weak_azimuth_dist)
        if wd is not None:
            detail = f"集中{wd}°方向"
        problems.append({"category": "覆盖不足", "dim": "信号", "metric": f"弱覆盖{a.weak_coverage_ratio}%", "value": a.weak_coverage_ratio, "threshold": 5, "unit": "%", "detail": detail})
    if not a.overshoot_pass:
        problems.append({"category": "越区覆盖", "dim": "信号", "metric": f"远距采样{a.overshoot_ratio}%", "value": a.overshoot_ratio, "threshold": 10, "unit": "%", "detail": f"最远{a.max_distance}m"})
    if not a.precision_pass:
        detail = f"最优{a.scatter_optimal_azimuth}°" if a.scatter_optimal_azimuth is not None else None
        problems.append({"category": "精准覆盖", "dim": "精准", "metric": f"方位角偏差{a.azimuth_deviation}°", "value": a.azimuth_deviation, "threshold": 30, "unit": "°", "detail": detail})
    if not a.interference_pass:
        detail = f"主邻差{a.avg_neighbor_diff}dB" if a.avg_neighbor_diff is not None else None
        id_dir = _interf_direction(a.interf_azimuth_dist)
        if id_dir is not None:
            detail = (detail + "，" if detail else "") + f"集中{id_dir}°方向"
        problems.append({"category": "重叠覆盖", "dim": "竞争", "metric": f"同频干扰{a.interference_ratio}%", "value": a.interference_ratio, "threshold": 10, "unit": "%", "detail": detail})
    if not a.backfire_pass:
        problems.append({"category": "背向覆盖", "dim": "精准", "metric": f"背向{a.backfire_ratio}%", "value": a.backfire_ratio, "threshold": 10, "unit": "%"})
    # 质量2项
    if not a.coverage_pass:
        problems.append({"category": "散点覆盖率", "dim": "覆盖", "metric": f"覆盖率{a.coverage_rate}%", "value": a.coverage_rate, "threshold": 95, "unit": "%"})
    if not a.excellence_pass:
        problems.append({"category": "栅格优良率", "dim": "覆盖", "metric": f"优良率{a.excellence_rate}%", "value": a.excellence_rate, "threshold": 95, "unit": "%"})
    # 结构3项
    if not a.beam_pass:
        problems.append({"category": "正对用户", "dim": "精准", "metric": f"波束内占比{a.beam_inner_ratio}%", "value": a.beam_inner_ratio, "threshold": 60, "unit": "%"})
    if not a.overlap_pass:
        problems.append({"category": "正对栅格", "dim": "精准", "metric": f"最优波束占比{a.optimal_beam_coverage_rate}%", "value": a.optimal_beam_coverage_rate, "threshold": 80, "unit": "%"})
    if not a.serving_pass:
        problems.append({"category": "主服覆盖", "dim": "精准", "metric": f"最优主服波束{a.beam_coverage_optimal}%", "value": a.beam_coverage_optimal, "threshold": 80, "unit": "%"})
    # 规模2项
    if not a.traffic_pass:
        problems.append({"category": "业务水平", "dim": "覆盖", "metric": f"业务量比值{a.traffic_ratio}", "value": a.traffic_ratio, "threshold": 1.0, "unit": ""})
    if not a.area_pass:
        problems.append({"category": "面积水平", "dim": "覆盖", "metric": f"面积比值{a.area_ratio}", "value": a.area_ratio, "threshold": 1.0, "unit": ""})
    return problems


def get_cell_analysis(gnbid, ci, indoor):
    """7维评估+5项射频专项，一次返回层次化结构数据"""
    a = analyze_cell(gnbid, ci, indoor)
    return {
        "cell_name": a.cell_name,
        "freq": a.freq,
        "current_azimuth": a.current_azimuth,
        "overall_pass": a.overall_pass,
        "scale_score": a.scale_score,
        "quality_score": a.quality_score,
        "rf_score": a.rf_score,
        "total_score": a.total_score,
        "problems": _build_problems(a),
        "scale": {
            "traffic": {
                "value": a.traffic_ratio,
                "cell_samples": a.sample_count,
                "freq_avg": a.freq_avg_samples,
                "pass": a.traffic_pass,
            },
            "area": {
                "value": a.area_ratio,
                "grid_count": a.grid_count,
                "grid_area_sqm": a.grid_area_sqm,
                "freq_avg_grids": a.freq_avg_grids,
                "pass": a.area_pass,
            },
        },
        "structure": {
            "toward_user": {
                "optimal_azimuth": a.scatter_optimal_azimuth,
                "beam_ratio": a.beam_inner_ratio,
                "optimal_beam_ratio": a.scatter_optimal_ratio,
                "pass": a.beam_pass,
            },
            "toward_grid": {
                "optimal_azimuth": a.grid_weighted_optimal_azimuth,
                "beam_grid_ratio": a.beam_grid_ratio,
                "optimal_beam_ratio": a.optimal_beam_coverage_rate,
                "pass": a.overlap_pass,
            },
            "serving": {
                "optimal_azimuth": a.grid_weighted_optimal_azimuth,
                "current_rate": a.beam_coverage_current,
                "optimal_rate": a.beam_coverage_optimal,
                "pass": a.serving_pass,
            },
        },
        "quality": {
            "scatter_coverage": {
                "rate": a.coverage_rate,
                "pass": a.coverage_pass,
            },
            "grid_excellence": {
                "rate": a.excellence_rate,
                "valid_grid_count": a.valid_grid_count,
                "pass": a.excellence_pass,
            },
        },
        "rf": {
            "weak_coverage": {
                "ratio": a.weak_coverage_ratio,
                "very_weak": a.very_weak_ratio,
                "azimuth_dist": a.weak_azimuth_dist,
                "pass": a.weak_coverage_pass,
            },
            "overshoot": {
                "ratio": a.overshoot_ratio,
                "max_distance": a.max_distance,
                "distribution": a.distance_distribution,
                "pass": a.overshoot_pass,
            },
            "backfire": {
                "ratio": a.backfire_ratio,
                "pass": a.backfire_pass,
            },
            "precision": {
                "beam_ratio": a.beam_inner_ratio,
                "optimal_azimuth": a.scatter_optimal_azimuth,
                "azimuth_deviation": a.azimuth_deviation,
                "pass": a.precision_pass,
            },
            "interference": {
                "ratio": a.interference_ratio,
                "avg_diff": a.avg_neighbor_diff,
                "azimuth_dist": a.interf_azimuth_dist,
                "pass": a.interference_pass,
            },
        },
        "stats": {
            "scatter": {
                "avg_rsrp": a.avg_rsrp,
                "avg_sinr": a.avg_sinr,
                "rsrp_grades": a.rsrp_grades,
                "sinr_grades": a.sinr_grades,
            },
            "grid": {
                "pct_grades": a.pct_grades,
                "weak_grades": a.weak_grades,
            },
        },
    }
