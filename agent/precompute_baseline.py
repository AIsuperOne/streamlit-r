"""并行离线预计算同频点基准统计：每个频点的平均采样点数和平均栅格数

用法：python3 -m agent.precompute_baseline
"""
import json
import os
import sqlite3
import sys

from multiprocessing import Pool, cpu_count

from .config import DB_PATH, OUTPUT_DIR


def _compute_one_cell(args):
    gnbid, ci, indoor = args
    import sqlite3
    from .analysis import _build_grid_squares
    from .config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            squares, _, _ = _build_grid_squares(gnbid, ci, indoor, None, None, conn)
        finally:
            conn.close()
        return len(squares) if squares else 0
    except Exception:
        return 0


def precompute(indoor="0"):
    conn = sqlite3.connect(DB_PATH)
    freqs = conn.execute(
        'SELECT DISTINCT "频点" FROM "5GBaseStation" WHERE "频点" IS NOT NULL'
    ).fetchall()
    result = {}
    total_freqs = len(freqs)
    for idx, (freq,) in enumerate(freqs):
        freq_str = str(freq)
        cell_count = conn.execute(
            'SELECT COUNT(*) FROM "5GBaseStation" WHERE "频点"=?', [freq]
        ).fetchone()[0]

        where = 'plmn="46000" AND nr_earfcn=? AND rsrp IS NOT NULL AND sinr IS NOT NULL'
        params = [freq_str]
        if indoor == "1":
            where += ' AND in_out_door="In_Door"'
        elif indoor == "2":
            where += ' AND in_out_door="Out_Door"'
        total = conn.execute(f'SELECT COUNT(*) FROM "data" WHERE {where}', params).fetchone()[0]

        cells = conn.execute(
            'SELECT "Gnbid", "Cellid" FROM "5GBaseStation" WHERE "频点"=?', [freq]
        ).fetchall()
        tasks = [(str(g), str(c), indoor) for g, c in cells]

        n_workers = min(cpu_count(), 8)
        with Pool(n_workers) as pool:
            grid_counts = pool.map(_compute_one_cell, tasks, chunksize=50)

        valid_grids = [g for g in grid_counts if g > 0]
        avg_grids = round(sum(valid_grids) / len(valid_grids), 1) if valid_grids else 0
        avg_samples = round(total / cell_count, 1) if cell_count > 0 else 0
        result[freq_str] = {
            "cell_count": cell_count,
            "total_samples": total,
            "avg_samples": avg_samples,
            "avg_grids": avg_grids,
        }
        print(f"[{idx + 1}/{total_freqs}] freq={freq_str}: {cell_count} cells, avg_samples={avg_samples}, avg_grids={avg_grids}")

    conn.close()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"freq_baseline_indoor{indoor}.json")
    with open(path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Saved to {path}")
    return result


if __name__ == "__main__":
    for indoor in ["0", "1", "2"]:
        r = precompute(indoor)
        print(f"indoor={indoor}: {len(r)} freqs computed\n")
