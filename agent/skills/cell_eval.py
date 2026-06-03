from ..analysis import analyze_cell


def get_cell_analysis(gnbid, ci, indoor):
    """综合评估：一次返回6维全量数据"""
    a = analyze_cell(gnbid, ci, indoor)
    return {
        "cell_name": a.cell_name,
        "freq": a.freq,
        "current_azimuth": a.current_azimuth,
        # 维度1：业务量
        "cell_samples": a.sample_count,
        "freq_avg_samples": a.freq_avg_samples,
        "traffic_ratio": a.traffic_ratio,
        "traffic_pass": a.traffic_pass,
        # 维度2：覆盖面积
        "grid_count": a.grid_count,
        "grid_area_sqm": a.grid_area_sqm,
        "freq_avg_grids": a.freq_avg_grids,
        "area_ratio": a.area_ratio,
        "area_pass": a.area_pass,
        # 维度3：覆盖最优性
        "beam_inner_ratio": a.beam_inner_ratio,

        "beam_pass": a.beam_pass,
        # 维度4：重叠覆盖
        "grid_weighted_optimal_azimuth": a.grid_weighted_optimal_azimuth,
        "optimal_beam_coverage_rate": a.optimal_beam_coverage_rate,
        "beam_coverage_optimal": a.beam_coverage_optimal,
        "overlap_pass": a.overlap_pass,
        # 维度5：散点覆盖率
        "coverage_rate": a.coverage_rate,
        "coverage_pass": a.coverage_pass,
        # 维度6：栅格优良率
        "excellence_rate": a.excellence_rate,
        "valid_grid_count": a.valid_grid_count,
        "excellence_pass": a.excellence_pass,
        "overall_pass": a.overall_pass,
        # 散点统计
        "avg_rsrp": a.avg_rsrp,
        "avg_sinr": a.avg_sinr,
        "rsrp_grades": a.rsrp_grades,
        "sinr_grades": a.sinr_grades,
        "covered_count": a.covered_count,
        # 栅格统计
        "pct_grades": a.pct_grades,
        "weak_grades": a.weak_grades,
        # 波束分析
        "scatter_optimal_azimuth": a.scatter_optimal_azimuth,
        "scatter_optimal_ratio": a.scatter_optimal_ratio,
        "grid_weighted_optimal_azimuth": a.grid_weighted_optimal_azimuth,
        "beam_coverage_current": a.beam_coverage_current,
        "beam_coverage_optimal": a.beam_coverage_optimal,
    }
