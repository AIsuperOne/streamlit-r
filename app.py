import json
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import streamlit as st
import streamlit.components.v1 as components

BASE_DIR = __file__.rsplit("/", 1)[0]
DB_PATH = "/Users/supermac/Documents/Project-AI/work-c/ZtoSQL/xianning.db"
BEAM_WIDTH = 60
SECTOR_RADIUS_M = 50
API_PORT = 8502

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


# --- Local API server for measurement data ---

_api_server_started = False


class MeasureAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        gnbid = params.get("gnbid", [""])[0]
        ci = params.get("ci", [""])[0]
        metric = params.get("metric", ["rsrp"])[0]
        indoor = params.get("indoor", ["0"])[0]

        conn = sqlite3.connect(DB_PATH)
        try:
            val_col = "nr_ssb_rsrp" if metric == "rsrp" else "nr_ssb_sinr"
            grades = RSRP_GRADES if metric == "rsrp" else SINR_GRADES

            where = 'gnbid=? AND ci=? AND longitude!="" AND latitude!="" AND ' + val_col + '!=""'
            q_params = [gnbid, ci]

            if indoor == "1":
                where += ' AND in_out_door="In_Door"'
            elif indoor == "2":
                where += ' AND in_out_door="Out_Door"'

            stat_row = conn.execute(
                f'SELECT COUNT(*), AVG(CAST({val_col} AS REAL)), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-105 AND CAST({val_col} AS REAL)<-95 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-115 AND CAST({val_col} AS REAL)<-105 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)<-115 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}',
                q_params,
            ).fetchone() if metric == "rsrp" else conn.execute(
                f'SELECT COUNT(*), AVG(CAST({val_col} AS REAL)), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=5 AND CAST({val_col} AS REAL)<15 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)>=-3 AND CAST({val_col} AS REAL)<5 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN CAST({val_col} AS REAL)<-3 THEN 1 ELSE 0 END) '
                f'FROM "data" WHERE {where}',
                q_params,
            ).fetchone()
            total_count = stat_row[0] or 0
            avg_val = round(stat_row[1], 2) if stat_row[1] is not None else None
            grade_counts = [stat_row[2], stat_row[3], stat_row[4], stat_row[5]]

            rows = conn.execute(
                f'SELECT longitude, latitude, nr_ssb_rsrp, nr_ssb_sinr FROM "data" WHERE {where} LIMIT 5000',
                q_params,
            ).fetchall()
        finally:
            conn.close()

        idx = 2 if metric == "rsrp" else 3
        points = []
        for r in rows:
            if not r[0] or not r[1]:
                continue
            try:
                points.append([float(r[0]), float(r[1]), float(r[idx])])
            except (ValueError, TypeError):
                pass

        result = {"points": points, "count": total_count, "avg": avg_val, "grades": grade_counts}
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


# --- Data loading ---

@st.cache_data
def load_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT "小区中文名", "物理站名", "RRU经度", "RRU纬度", "方位角", '
        '"区县", "覆盖类型", "工作频段", "频点", "Gnbid", "Cellid", "天线挂高" '
        'FROM "5GBaseStation" '
        'WHERE "RRU经度" IS NOT NULL AND "RRU纬度" IS NOT NULL AND "方位角" IS NOT NULL'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- HTML template ---

def build_map_html(sectors_json):
    rsrp_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in RSRP_GRADES])
    sinr_grades_json = json.dumps([{"label": g["label"], "range": g["range"], "color": g["color"]} for g in SINR_GRADES])
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
    <div class="metric-toggle">
      <button class="active" id="btn-rsrp">RSRP</button>
      <button id="btn-sinr">SINR</button>
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
    sinr: {sinr_grades_json}
  }};

  var map, sectors = {sectors_json};
  var overlays = {{}}, dataMap = {{}};
  var selected = [];
  var currentMetric = "rsrp";
  var scatterByCell = {{}};
  var pendingLoads = 0;

  var s = document.createElement("script");
  s.src = "https://webapi.amap.com/maps?v=2.0&key={AMAP_KEY}";
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

  function init() {{
    var lats = sectors.map(function(s){{ return s.lat; }}).filter(function(v){{ return v && !isNaN(v); }});
    var lngs = sectors.map(function(s){{ return s.lng; }}).filter(function(v){{ return v && !isNaN(v); }});
    var cLat = lats.reduce(function(a,b){{ return a+b; }}, 0) / lats.length;
    var cLng = lngs.reduce(function(a,b){{ return a+b; }}, 0) / lngs.length;
    map = new AMap.Map("container", {{ zoom: 13, center: [cLng, cLat] }});
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
    }} else {{
      selected.push(name);
      overlays[name].setOptions({{ fillColor: "#00FF00", fillOpacity: 0.7, strokeWeight: 2 }});
      loadScatter(name, currentMetric);
    }}
    renderSelList();
    updateMeasureSection();
  }}

  function removeScatter(name) {{
    if (scatterByCell[name]) {{
      scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
      delete scatterByCell[name];
    }}
    renderStats();
  }}

  function loadScatter(name, metric) {{
    // Only remove existing scatter data, keep entry as placeholder to track in-flight request
    if (scatterByCell[name] && scatterByCell[name].markers) {{
      scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
    }}
    scatterByCell[name] = {{ markers: [], count: 0, avg: null, grades: [0,0,0,0], loading: true }};

    var s = dataMap[name];
    var indoor = document.getElementById("indoor-select").value;
    pendingLoads++;
    document.getElementById("loading-tip").style.display = "block";

    fetch("http://localhost:{API_PORT}/api/measurements?gnbid=" + encodeURIComponent(s.gnbid) + "&ci=" + encodeURIComponent(s.ci) + "&metric=" + metric + "&indoor=" + indoor)
      .then(function(r) {{ return r.json(); }})
      .then(function(result) {{
        pendingLoads--;
        if (pendingLoads <= 0) {{
          document.getElementById("loading-tip").style.display = "none";
          pendingLoads = 0;
        }}
        // If deselected while fetch was in-flight, discard and clean up
        if (selected.indexOf(name) < 0) {{
          delete scatterByCell[name];
          return;
        }}
        var points = result.points;
        var colorFn = metric === "rsrp" ? rsrpColor : sinrColor;
        var markers = [];
        points.forEach(function(p) {{
          try {{
            var color = colorFn(p[2]);
            var marker = new AMap.CircleMarker({{
              center: [p[0], p[1]],
              radius: 3,
              fillColor: color,
              fillOpacity: 0.7,
              strokeColor: color,
              strokeWeight: 0.5,
              strokeOpacity: 0.8
            }});
            marker.setMap(map);
            markers.push(marker);
          }} catch(e) {{}}
        }});
        scatterByCell[name] = {{
          markers: markers,
          count: result.count,
          avg: result.avg,
          grades: result.grades
        }};
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

  function renderStats() {{
    var statsEl = document.getElementById("stats-container");
    var legendEl = document.getElementById("legend-container");
    statsEl.innerHTML = "";
    legendEl.innerHTML = "";
    var names = Object.keys(scatterByCell);
    if (names.length === 0) return;

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
      div.innerHTML = "<span>" + s.station_name + " | 频点:" + (s.freq || "") + " | 挂高:" + (s.height || "--") + "m</span>" +
        '<button data-idx="' + i + '">&times;</button>';
      div.querySelector("button").onclick = function() {{ toggleSelect(name); }};
      list.appendChild(div);
    }});
  }}

  document.getElementById("clear-btn").onclick = function() {{
    selected.slice().forEach(function(name) {{
      var s = dataMap[name], c = getColor(s);
      overlays[name].setOptions({{ fillColor: c, fillOpacity: 0.4, strokeWeight: 1 }});
    }});
    Object.keys(scatterByCell).forEach(function(name) {{
      if (scatterByCell[name].markers) {{
        scatterByCell[name].markers.forEach(function(m) {{ m.setMap(null); }});
      }}
      delete scatterByCell[name];
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
    document.getElementById("btn-rsrp").onclick = function() {{
      if (currentMetric === "rsrp") return;
      currentMetric = "rsrp";
      document.getElementById("btn-rsrp").classList.add("active");
      document.getElementById("btn-sinr").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("btn-sinr").onclick = function() {{
      if (currentMetric === "sinr") return;
      currentMetric = "sinr";
      document.getElementById("btn-sinr").classList.add("active");
      document.getElementById("btn-rsrp").classList.remove("active");
      reloadAllScatter();
    }};
    document.getElementById("indoor-select").onchange = function() {{
      if (selected.length > 0) reloadAllScatter();
    }};
  }}
</script>
</body>
</html>"""


def main():
    st.set_page_config(layout="wide")
    st.title("5G 基站扇形覆盖图")

    start_api_server()

    data = load_data()

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
        }
        for d in data
        if d["RRU经度"] and d["RRU纬度"] and d["方位角"] is not None
    ]

    html = build_map_html(json.dumps(sectors, ensure_ascii=False))
    components.html(html, height=800)


if __name__ == "__main__":
    main()
