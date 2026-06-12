"""数据结构定义：栅格、小区分析结果、频段常量、频点基准"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from .config import OUTPUT_DIR


# ============================================================
# 频段预期覆盖半径（米）
# ============================================================

FREQ_EXPECTED_RADIUS = {
    "700M": 1500, "700MHz": 1500,
    "2.6G": 800, "2.6GHz": 800,
    "4.9G": 500, "4.9GHz": 500,
}


# ============================================================
# 栅格
# ============================================================

@dataclass
class GridSquare:
    """单个5m×5m栅格，含全部属性（可视化+评估+波束分析）"""
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


# ============================================================
# 小区分析结果
# ============================================================

@dataclass
class CellAnalysis:
    """一次性分析结果，涵盖散点+栅格+波束+射频+评估全部指标"""
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
    grid_weighted_optimal_azimuth: Optional[int]  # 栅格加权法最优方位角
    beam_grid_ratio: Optional[float]           # 波束占比（栅格）
    optimal_beam_coverage_rate: Optional[float]  # 最优波束占比（栅格）
    beam_coverage_current: Optional[float]      # 主服波束占比（栅格）
    beam_coverage_optimal: Optional[float]      # 最优主服波束占比（栅格）

    # 干扰分析
    interference_ratio: Optional[float]    # 同频干扰比例(%)
    avg_neighbor_diff: Optional[float]     # 平均主邻差(dB)
    interference_pass: bool

    # 弱覆盖分析
    weak_coverage_ratio: float             # RSRP<-105占比(%)
    very_weak_ratio: float                 # RSRP<-110占比(%)
    weak_coverage_pass: bool

    # 射频专项
    weak_azimuth_dist: List[int]           # 12扇区弱覆盖分布
    interf_azimuth_dist: List[int]         # 12扇区干扰方位分布
    overshoot_ratio: float                 # 越区覆盖比例(%)
    max_distance: int                      # 最大覆盖距离(m)
    distance_distribution: List[int]       # [近/中/远/越区]
    backfire_ratio: float                  # 背向覆盖比例(%)
    azimuth_deviation: Optional[float]     # 方位角偏差(°)
    overshoot_pass: bool
    backfire_pass: bool
    precision_pass: bool

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
    serving_pass: bool
    coverage_pass: bool
    excellence_pass: bool
    overall_pass: bool

    # 评分
    scale_score: float           # 规模评分 0~50
    quality_score: float         # 质量评分 0~30
    rf_score: int                # 射频评分 0~20
    total_score: float           # 总分 0~100

    # 栅格数据（供compute_grid_data复用）
    squares: List[GridSquare] = field(default_factory=list)
    grid_params: Optional[dict] = None
    total_plmn_count: int = 0
    total_plmn_avg_rsrp: Optional[float] = None


# ============================================================
# 频点基准加载
# ============================================================

_freq_baseline_cache = {}


def load_freq_baseline(indoor):
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
