from ..analysis import analyze_cell


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
