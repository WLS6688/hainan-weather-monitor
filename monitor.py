#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海南澄迈台风预警监控
==================================================
数据源1: 中国气象局官方接口 weather.cma.cn/api/map/alarm
        （按 adcode 精确查询，需带 UA + Referer 否则 403）
        用途：已发布的台风预警信号（反应式推送）
数据源2: 中央气象台台风网 nmc 台风路径接口
        list : http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default
        view : http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{id}
        用途：台风预报路径（每日报告中的台风趋势提示）
推送  : 企业微信群机器人 webhook（markdown 消息，不@任何人）

监控范围(仅台风):
  * 只关注【台风】类预警；雷电/暴雨/高温等一律不推送、不进报告
  * 实时推送【澄迈陆地台风预警】与【海南省级陆地台风预警】
    （海上预警、非澄迈陆地预警 -> 不实时推送，仅进入每日报告）

双层机制:
  [反应式] 气象台已发布台风预警信号 -> 立即/续推（实时）
  [预测式] nmc 台风预报路径 -> 仅作为“每日提示”放入每日天气报告，
           不在官方信号发布前做实时推送，避免刷屏

推送节奏(实时):
  * 每 10 分钟轮询：仅检测【新预警 / 预警变动】
  * 蓝/黄/橙/红 四级台风预警 -> 新发/变动 立即推送
  * 信号与内容未变、且级别为黄/橙/红 -> 每 120 分钟续推一次（持续提醒）
  * 蓝色预警只在新发时推一次，不做周期提醒；白色不推送
  * 轮询时间(10min) ≠ 推送时间(120min)，解耦避免刷屏

实时逼近预警(临门一脚):
  * 每 10 分钟轮询时额外检查 nmc 预报路径；若预报显示 <48 小时内进入海南影响框
    -> 实时推送一条“台风逼近预警”（含剩余小时数 + 强度趋势 + 七级风圈 + 影响时段 + 防御指引），每台风仅推一次，避免刷屏
  * 日常趋势提示仍在每日报告中给出，二者互补；无活跃威胁时不打扰

每日报告(09:00) — 含台风趋势提示:
  * 汇总当日生效的官方台风预警（陆地+海上）
  * 【台风趋势提示】：拉取 nmc 活跃台风预报路径（参考日本气象厅 JMA：强度等级 + 七级风圈）
      - 任一预报路径点进入“海南影响框” -> 提示“预计X日前后台风可能逼近/登录海南”
      - 已进入框/当前逼近 -> 提示“当前已进入海南影响范围”
      - 附：最新位置 / 强度 / 移动方向 / 强度趋势(加强中·减弱中·维持) / 七级风圈半径 / 预计影响时段 + 实时台风路径链接
      - 无直接影响海南的台风 -> 提示“未来数日无预报路径直接影响海南的台风”
  * 附加【澄迈今日天气预报】（Open-Meteo 免 key）
  * 按日期去重：同一天无论触发几次，只推一次

台风路径:
  * 推送消息附上实时台风路径网址（typhoon.nmc.cn）

抓取异常自告警:
  * 连续 3 轮抓取失败才告警（容忍凌晨维护/境外链路偶发抖动），恢复后推送“恢复正常”

去重  : state.json 记录每条预警的上次推送时间 + 每日报告日期 + 异常状态；
        仅在状态变化时才 git commit 回仓库，跨云端 runner 持久化。

注    : 网络层使用标准库 urllib，无需 pip 安装第三方依赖，运行更快更稳。

用法  :
  python monitor.py --mode poll    # 每10分钟（GitHub Actions 定时触发）
  python monitor.py --mode daily   # 每日报告（GitHub Actions 09:00 触发）
==================================================
"""
import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
import subprocess
from datetime import datetime, timezone, timedelta

API = "https://weather.cma.cn/api/map/alarm"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://weather.cma.cn/",
}
# 直查区县（只查澄迈；其余靠省级兜底覆盖）
REGIONS = {"澄迈": "469023"}
PROVINCE = "46"  # 海南省（兜底：抓全省台风预警，推送时再筛“澄迈/海南相关”）
# 仅关注台风类预警
TARGET_TYPES = ["台风"]
SEA_KEYWORDS = ["海面", "海上", "琼州海峡", "附近海域", "南海", "北部湾",
                "东海", "台湾海峡", "南海北部", "西沙", "南沙", "中沙"]
# 持续提醒：仅 黄/橙/红 三级做每 120 分钟续推；蓝/白预警只在新发时推一次，不做周期提醒
REPUSH_LEVELS = ["黄色", "橙色", "红色"]
REPUSH_INTERVAL = 7200  # 秒（120 分钟）
# 按级别附“防御指引”（参考中国气象局台风预警信号防御指南 / 香港天文台做法）
LEVEL_ADVICE = {
    "红色": "停止集会、停业（特殊行业除外），人员留在安全场所，做好强风暴雨防御。",
    "橙色": "加固门窗与高空悬挂物，渔船回港避风，减少外出，留意停课停工通知。",
    "黄色": "留意最新预警，固定易被吹落物品，海上作业注意安全。",
    "蓝色": "关注台风动态，提前做好准备。",
}
# 台风实时路径（推送时附上）
TYPHOON_TRACK_URL = "https://typhoon.nmc.cn/"
# 澄迈坐标（用于天气预报 API：Open-Meteo 免 key）
CHENGMAI_LAT, CHENGMAI_LON = 19.74, 110.00
STATE_FILE = "state.json"
WEBHOOK = os.environ.get("WECHAT_WEBHOOK")
BEIJING = timezone(timedelta(hours=8))
# 抓取异常自告警冷却时间（秒）
ERROR_ALERT_INTERVAL = 6 * 3600

# ---------- 台风路径监控（预测式） ----------
TYPHOON_LIST_URL = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default"
TYPHOON_VIEW_URL = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{id}"
# 海南影响框（纬度, 经度）。覆盖海南岛及周边海域，用于判断“趋向海南”
HAINAN_BOX = (16.0, 22.0, 104.0, 115.0)  # lat_min, lat_max, lon_min, lon_max
# nmc 强度代码 -> 中文
STRENGTH_CN = {
    "TD": "热带低压", "TS": "热带风暴", "STS": "强热带风暴",
    "TY": "台风", "STY": "强台风", "SuperTY": "超强台风",
}
# 强度等级排序（用于判定加强/减弱趋势，参考日本气象厅 JMA 分级）
STRENGTH_RANK = {"TD": 1, "TS": 2, "STS": 3, "TY": 4, "STY": 5, "SuperTY": 6}


def log(*a):
    print(f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}]", *a, flush=True)


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def http_post_json(url, payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def nmc_get_json(url):
    """拉取 nmc 台风接口（返回 JSONP，需去壳）。失败返回 None。"""
    raw = http_get(url, {"User-Agent": HEADERS["User-Agent"],
                         "Referer": "http://typhoon.nmc.cn/"}, timeout=15)
    m = re.search(r"\((\{.*\})\)", raw, re.S)
    if not m:
        return None
    return json.loads(m.group(1))


def fetch(adcode, tries=4):
    """拉取某 adcode 的预警；失败返回 None（与空列表区分）。"""
    url = f"{API}?adcode={adcode}"
    for i in range(tries):
        try:
            raw = http_get(url, HEADERS, timeout=15)
            d = json.loads(raw)
            return d.get("data") or []
        except Exception as e:
            log(f"fetch error {adcode}: {e}")
        time.sleep(2 ** (i + 1))  # 2,4,8,16s 指数退避，更耐偶发抖动
    return None


# WMO 天气代码 -> 中文描述（Open-Meteo 返回 code 需映射）
WMO_WEATHER = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def fetch_forecast(lat=CHENGMAI_LAT, lon=CHENGMAI_LON, tries=3):
    """拉取澄迈天气预报（Open-Meteo，免 key）。失败返回 None。"""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,wind_speed_10m_max"
           "&timezone=Asia%2FShanghai&forecast_days=1&wind_speed_unit=kmh")
    for i in range(tries):
        try:
            raw = http_get(url, {"User-Agent": "Mozilla/5.0"}, timeout=15)
            d = json.loads(raw)
            cur = d.get("current") or {}
            daily = d.get("daily") or {}
            return {
                "temp_now": cur.get("temperature_2m"),
                "code_now": cur.get("weather_code"),
                "wind_now": cur.get("wind_speed_10m"),
                "humidity": cur.get("relative_humidity_2m"),
                "date": (daily.get("time") or [None])[0],
                "code_day": (daily.get("weather_code") or [None])[0],
                "tmax": (daily.get("temperature_2m_max") or [None])[0],
                "tmin": (daily.get("temperature_2m_min") or [None])[0],
                "wind_max": (daily.get("wind_speed_10m_max") or [None])[0],
            }
        except Exception as e:
            log(f"forecast error: {e}")
        time.sleep(2 * (i + 1))
    return None


def parse(w):
    headline = w.get("headline", "") or w.get("title", "")
    desc = w.get("description", "")
    m = re.search(
        r"(.+?)(?:气象台|气象局).*?发布(.+?)"
        r"(蓝色|黄色|橙色|红色|白色|一级|二级|三级|四级|五级)?预警",
        headline,
    )
    issuer = m.group(1) if m else ""
    wtype = m.group(2) if m else ""
    level = m.group(3) if m else ""
    return {
        "id": w.get("id"),
        "headline": headline,
        "issuer": issuer,
        "type": wtype,
        "level": level,
        "effective": w.get("effective", ""),
        "description": desc,
        "is_sea": any(k in (headline + desc) for k in SEA_KEYWORDS),
    }


def is_target_type(a):
    """仅处理台风类预警。"""
    return any(t in (a["type"] or "") for t in TARGET_TYPES)


def should_push(a):
    """是否实时推送：仅澄迈陆地 + 海南省级陆地台风预警实时推；海上预警不实时推。"""
    if a["type"] == "台风":
        if a["region"] == "澄迈":
            return True
        if a["region"] == "海南省":
            return not a["is_sea"]   # 海上台风预警不实时推送（进每日报告）
    if a["is_sea"]:
        return False
    text = a["headline"] + a["description"]
    if a["region"] == "澄迈":
        return True
    if a["region"] == "海南省" and "澄迈" in text:
        return True
    return False


def is_blue_or_above(a):
    return a["level"] in REPUSH_LEVELS


def content_sig(a):
    return (a["headline"], a["description"], a["level"], a["type"], a["effective"])


def collect():
    """返回 (active_typhoon_alerts, errors)。"""
    alerts = {}
    errors = []
    for name, code in REGIONS.items():
        data = fetch(code)
        if data is None:
            errors.append(f"区县 {name}({code}) 获取失败")
            continue
        for w in data:
            p = parse(w)
            if not p["id"] or p["id"] in alerts:
                continue
            if not is_target_type(p):
                continue  # 非台风，忽略
            p["region"] = name
            alerts[p["id"]] = p
    prov = fetch(PROVINCE)
    if prov is None:
        errors.append("省级(46) 获取失败")
    else:
        for w in prov:
            p = parse(w)
            if not p["id"] or p["id"] in alerts:
                continue
            if not is_target_type(p):
                continue  # 非台风，忽略
            p["region"] = "海南省"
            alerts[p["id"]] = p
    return list(alerts.values()), errors


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def commit_state():
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode != 0:
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
            subprocess.run(["git", "commit", "-m", "chore: update warning state"], check=False)
            subprocess.run(["git", "push"], check=False)
            log("state 已提交")
    except Exception as e:
        log("commit_state error:", e)


def wechat_markdown(content):
    if not WEBHOOK:
        log("未配置 WECHAT_WEBHOOK，跳过推送")
        return
    try:
        http_post_json(WEBHOOK, {"msgtype": "markdown",
                                 "markdown": {"content": content}}, timeout=10)
        log("推送成功")
    except Exception as e:
        log("推送失败:", e)


def fmt_alert(a, repeat=False):
    level = a["level"] or "未知"
    t = a["type"] or ""
    eff = a["effective"]
    note = "\n> 持续预警 · 每2小时提醒" if repeat else ""
    advice = LEVEL_ADVICE.get(level, "")
    return (f"> **[{level}]{t}** {a['headline']}\n"
            f"> 发布：{a['issuer'] or a['region']} ｜ 时间：{eff}{note}\n"
            f"> 防御指引：{advice}\n"
            f"> 实时台风路径：{TYPHOON_TRACK_URL}\n"
            f"> 数据来源：中国气象局·中央气象台")


def fmt_time(s):
    if len(s) == 12:
        return f"{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    return s


# ---------- 台风路径监控（预测式） ----------
def fetch_typhoon_list():
    """返回活跃台风列表 [{id, en, cn, num, status}]。失败返回 []。"""
    d = nmc_get_json(TYPHOON_LIST_URL)
    if not d:
        return []
    out = []
    for x in d.get("typhoonList", []):
        if len(x) < 8:
            continue
        out.append({"id": str(x[0]), "en": x[1], "cn": x[2], "num": x[3], "status": x[7]})
    return out


def fetch_typhoon_track(tid):
    """返回 {cur, forecast} 或 None。
       cur     = 最新实况点 {time,lat,lon,strength,pressure,wind,move_dir,move_speed}
       forecast= 预报路径点列表 [{hour,time,lat,lon,pressure,wind,strength}]
    """
    d = nmc_get_json(TYPHOON_VIEW_URL.format(id=tid))
    if not d:
        return None
    ty = d.get("typhoon")
    if not isinstance(ty, list) or len(ty) < 9:
        return None
    pts = ty[8]
    actual = []
    for p in pts:
        if not isinstance(p, list) or len(p) < 10:
            continue
        try:
            # 七级风圈半径（nmc [10]：['30KTS', NE, SE, SW, NW, id]，取四象限最大，km）
            radius7 = None
            r = p[10]
            if isinstance(r, list) and r:
                inner = r[0] if (len(r) == 1 and isinstance(r[0], list)) else r
                nums = [x for x in inner[1:5] if isinstance(x, (int, float))]
                if nums:
                    radius7 = max(nums)
            actual.append({
                "time": str(p[1]), "lat": float(p[5]), "lon": float(p[4]),
                "strength": p[3], "pressure": p[6], "wind": p[7],
                "move_dir": p[8], "move_speed": p[9], "radius7": radius7,
            })
        except Exception:
            continue
    if not actual:
        return None
    actual.sort(key=lambda x: x["time"])
    cur = actual[-1]                                   # 最新实况点
    prev = actual[-2] if len(actual) >= 2 else None
    trend = intensity_trend(cur, prev)                 # 强度趋势（参考 JMA 分强度+风圈）
    # 预报路径：取最新实况点的 BABJ 预报（中央气象台预报）
    forecast = []
    last = pts[-1]
    babj = None
    if isinstance(last, list) and len(last) > 11 and isinstance(last[11], dict):
        babj = last[11].get("BABJ")
    if babj:
        for f in babj:
            if not isinstance(f, list) or len(f) < 8:
                continue
            try:
                forecast.append({
                    "hour": f[0], "time": str(f[1]),
                    "lon": float(f[2]), "lat": float(f[3]),
                    "pressure": f[4], "wind": f[5], "strength": f[7],
                })
            except Exception:
                continue
    return {"cur": cur, "forecast": forecast, "trend": trend}


def in_box(lat, lon):
    lat_min, lat_max, lon_min, lon_max = HAINAN_BOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def dir_cn(code):
    """nmc 移动方向代码 -> 中文，如 NNW->北西北（第二字母在前）。"""
    if not code:
        return "未知"
    m = {"N": "北", "S": "南", "E": "东", "W": "西"}
    if code in m:
        return m[code]
    if len(code) == 2:
        return m.get(code[1], "") + m.get(code[0], "")   # NW->西北
    if len(code) >= 3:
        base = m.get(code[2], "") + m.get(code[1], "")    # 末两位为主方向
        return m.get(code[0], "") + base                  # NNW->北西北
    return code


def fmt_dt(s):
    """'YYYYMMDDHHMM' -> 'M月D日 HH:MM'。"""
    if len(s) == 12:
        return f"{int(s[4:6])}月{int(s[6:8])}日 {s[8:10]}:{s[10:12]}"
    return s


def intensity_trend(cur, prev):
    """参考日本气象厅 JMA：按强度等级 + 风速判定加强/减弱。"""
    if not prev:
        return "强度平稳"
    rc = STRENGTH_RANK.get(cur.get("strength"), 0)
    rp = STRENGTH_RANK.get(prev.get("strength"), 0)
    if rc > rp:
        return "强度加强中"
    if rc < rp:
        return "减弱中"
    cw = cur.get("wind") or 0
    pw = prev.get("wind") or 0
    if cw > pw:
        return "强度略加强"
    if cw < pw:
        return "强度略减弱"
    return "强度维持"


def phase_cn(h):
    """小时 -> 中文时段（人话表述，参考日常预警口语）。"""
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "中午前后"
    if 13 <= h < 18:
        return "下午"
    if 18 <= h < 20:
        return "傍晚"
    if 20 <= h < 24:
        return "夜间"
    return "凌晨"  # 0-5


def fmt_window(box_fc):
    """进入海南框的预报点时间窗口 -> 精确时刻 + 人话时段（北京时）。"""
    if not box_fc:
        return ""
    times = sorted(f["time"] for f in box_fc)
    t0, t1 = times[0], times[-1]
    if t0 == t1:
        return f"{fmt_dt(t0)}（北京时）"
    m0, d0, h0 = int(t0[4:6]), int(t0[6:8]), int(t0[8:10])
    m1, d1, h1 = int(t1[4:6]), int(t1[6:8]), int(t1[8:10])
    precise = f"{fmt_dt(t0)} 至 {fmt_dt(t1)}"
    human = f"{m0}月{d0}日{phase_cn(h0)} 至 {m1}月{d1}日{phase_cn(h1)}"
    return f"{precise}（北京时），约 {human}"


def parse_nmc_time(s):
    """'YYYYMMDDHHMM' -> 北京时 epoch；失败返回 None。"""
    if len(s) != 12:
        return None
    try:
        dt = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                      int(s[8:10]), int(s[10:12]), tzinfo=BEIJING)
        return dt.timestamp()
    except Exception:
        return None


def typhoon_outlook():
    """返回每日报告用的【台风趋势提示】段落（markdown 行列表）。仅作提示，不实时推送。"""
    lines = []
    try:
        lst = fetch_typhoon_list()
    except Exception as e:
        log("typhoon list error:", e)
        return lines
    active = [x for x in lst if x.get("status") and x["status"] != "stop"]
    if not active:
        lines.append("> 台风趋势提示：目前西北太平洋/南海无编号活跃台风。")
        return lines
    # 海南影响框 + 邻近关注框（可能数日内影响海南）
    lat_min, lat_max, lon_min, lon_max = HAINAN_BOX
    near = (12.0, 24.0, 102.0, 122.0)
    for t in active:
        try:
            det = fetch_typhoon_track(t["id"])
        except Exception as e:
            log("track error", t["id"], e)
            continue
        if not det:
            continue
        cur = det["cur"]
        cur_lat, cur_lon = cur["lat"], cur["lon"]
        fc = det["forecast"]
        box_fc = [f for f in fc if in_box(f["lat"], f["lon"])]
        eta = min(box_fc, key=lambda f: f["time"]) if box_fc else None
        in_hainan = in_box(cur_lat, cur_lon)
        near_now = (near[0] <= cur_lat <= near[1] and near[2] <= cur_lon <= near[3])
        if not (eta or in_hainan or near_now):
            continue  # 与该区域无关，跳过
        scn = STRENGTH_CN.get(cur["strength"], cur["strength"] or "未知")
        mv = dir_cn(cur["move_dir"])
        spd = cur.get("move_speed")
        trend = det.get("trend", "强度平稳")
        lines.append(f"> 🌀 台风“{t['cn']}”（国际编号 {t['num']}）")
        pos = f"> 　当前：{cur_lat:.1f}°N, {cur_lon:.1f}°E，强度 {scn}"
        if cur.get("pressure"):
            pos += f"，中心气压 {cur['pressure']} hPa"
        lines.append(pos)
        lines.append(f"> 　移动：{mv}" + (f" {spd} km/h" if spd else ""))
        lines.append(f"> 　趋势：{trend}")
        if cur.get("radius7"):
            lines.append(f"> 　七级风圈半径约 {cur['radius7']} km（参考 JMA 风圈做法）")
        if in_hainan:
            lines.append("> 　状态：当前已进入/逼近海南影响范围，请密切关注官方台风预警信号。")
        elif eta:
            lines.append(f"> 　预报：预计 {fmt_dt(eta['time'])} 前后进入海南影响范围，"
                         f"请提前关注后续官方预警。")
            win = fmt_window(box_fc)
            if win:
                lines.append(f"> 　影响时段：{win}")
        else:
            lines.append("> 　状态：位于海南邻近海域，后续路径存在不确定性，建议持续关注。")
        lines.append(f"> 　实时路径：{TYPHOON_TRACK_URL}")
    if not lines:
        # 有活跃台风但均不直接影响海南
        return ["> 台风趋势提示：未来数日无预报路径直接影响海南的台风。"]
    return lines


def check_imminent(state):
    """临门一脚：预报显示 <48h 内进入海南影响框 -> 实时推一条逼近预警（每台风仅一次）。"""
    changed = False
    alerted = state.setdefault("imminent_alerted", {})
    now = time.time()
    try:
        lst = fetch_typhoon_list()
    except Exception as e:
        log("imminent list error:", e)
        return False
    active = [x for x in lst if x.get("status") and x["status"] != "stop"]
    for t in active:
        try:
            det = fetch_typhoon_track(t["id"])
        except Exception as e:
            log("imminent track error", t["id"], e)
            continue
        if not det:
            continue
        box_fc = [f for f in det["forecast"] if in_box(f["lat"], f["lon"])]
        if not box_fc:
            continue
        eta = min(box_fc, key=lambda f: f["time"])
        eta_epoch = parse_nmc_time(eta["time"])
        if eta_epoch is None:
            continue
        remain = eta_epoch - now
        if 0 <= remain <= 48 * 3600:
            key = str(t["id"])
            if key not in alerted:
                cur = det["cur"]
                scn = STRENGTH_CN.get(cur["strength"], cur["strength"] or "未知")
                hours = int(remain // 3600)
                pres = f"，中心气压 {cur['pressure']} hPa" if cur.get("pressure") else ""
                win = fmt_window(box_fc)
                radius7 = f"，七级风圈半径约 {cur['radius7']} km" if cur.get("radius7") else ""
                trend = det.get("trend", "强度平稳")
                msg = (f"> **【台风逼近预警】剩余约 {hours} 小时**\n"
                       f"> 台风 {t['cn']}（编号{t['num']}）预报路径预计 {fmt_dt(eta['time'])} "
                       f"前后进入海南影响范围。\n"
                       f"> 当前：{cur['lat']:.1f}°N, {cur['lon']:.1f}°E，强度 {scn}{pres}\n"
                       f"> 趋势：{trend}{radius7}\n"
                       f"> 影响时段：{win}\n"
                       f"> 防御指引：{LEVEL_ADVICE.get('红色', '')}\n"
                       f"> 请提前做好防风准备，并密切关注官方台风预警信号。\n"
                       f"> 实时台风路径：{TYPHOON_TRACK_URL}\n"
                       f"> 数据来源：中国气象局·中央气象台")
                wechat_markdown(msg)
                alerted[key] = {"cn": t["cn"], "num": t["num"], "eta": eta["time"]}
                changed = True
                log("[逼近] 实时推送:", t["cn"], "剩余约", hours, "小时")
    return changed


def mode_poll():
    now = time.time()
    active, errors = collect()
    state = load_state()
    active_ids = {a["id"] for a in active}
    removed_ids = set(state.keys()) - active_ids - {"last_daily_report_date",
                                                    "last_error_alert", "had_errors",
                                                    "fail_streak", "imminent_alerted"}

    pushed = 0
    changed = False

    for a in active:
        rec = state.get(a["id"])
        if rec is None:
            # 新预警
            pushed_now = False
            if should_push(a):
                wechat_markdown(fmt_alert(a))
                pushed += 1
                pushed_now = True
            state[a["id"]] = {
                "headline": a["headline"], "is_sea": a["is_sea"],
                "description": a["description"], "effective": a["effective"],
                "issuer": a["issuer"], "type": a["type"], "level": a["level"],
                "last_push": now if pushed_now else 0.0,
                "pushed": pushed_now,
            }
            changed = True
            log(f"[新]{'推送' if pushed_now else '记录'}: {a['headline']}")
            continue

        # 已有预警
        sig_now = content_sig(a)
        sig_old = (rec.get("headline"), rec.get("description"), rec.get("level"),
                   rec.get("type"), rec.get("effective"))
        if sig_now != sig_old:
            # 信号/内容变动 -> 立即推送
            if should_push(a):
                wechat_markdown(fmt_alert(a))
                pushed += 1
                rec["last_push"] = now
                rec["pushed"] = True
                log(f"[变动] 推送: {a['headline']}")
            else:
                log(f"[变动-非推送目标] 跳过: {a['headline']}")
            rec.update({"headline": a["headline"], "is_sea": a["is_sea"],
                        "description": a["description"], "effective": a["effective"],
                        "issuer": a["issuer"], "type": a["type"], "level": a["level"]})
            changed = True
        else:
            # 未变动 -> 黄/橙/红 且到 120 分钟才续推
            if should_push(a) and is_blue_or_above(a):
                last = rec.get("last_push", 0.0) or 0.0
                if now - last >= REPUSH_INTERVAL:
                    wechat_markdown(fmt_alert(a, repeat=True))
                    pushed += 1
                    rec["last_push"] = now
                    rec["pushed"] = True
                    changed = True
                    log(f"[持续] 每2小时提醒: {a['headline']}")

    # 预警解除通知（仅陆地、且曾推送过）
    lifted = 0
    for rid in removed_ids:
        old = state.get(rid, {})
        if old.get("is_sea"):
            continue
        if old.get("pushed"):
            wechat_markdown(f"> **预警解除**：{old.get('headline', '')}")
            lifted += 1
            log(f"[解除] 通知: {old.get('headline', '')}")

    for rid in removed_ids:
        state.pop(rid, None)
        changed = True

    # 台风逼近实时预警（临门一脚：<48h 进入海南框，每台风仅一次）
    if check_imminent(state):
        changed = True

    # 抓取异常自告警（容忍偶发失败，避免凌晨维护窗口/境外链路抖动误报）
    FAIL_TOLERANCE = 3  # 连续 3 轮（约 30 分钟）失败才告警，偶发抖动不触发
    fs = state.get("fail_streak", 0) or 0
    fs = fs + 1 if errors else 0
    if fs != (state.get("fail_streak", 0) or 0):
        state["fail_streak"] = fs
        changed = True
    real_failure = fs >= FAIL_TOLERANCE
    if real_failure:
        last_err = state.get("last_error_alert", 0.0) or 0.0
        if now - last_err >= ERROR_ALERT_INTERVAL:
            wechat_markdown("> **监控异常提醒**\n"
                            "> 气象预警数据连续获取失败，请检查接口或网络连通性。\n"
                            "> 错误：" + "；".join(errors) + f"\n> 已连续失败 {fs} 轮")
            state["last_error_alert"] = now
            changed = True
    if (not real_failure) and state.get("had_errors"):
        wechat_markdown("> **监控恢复正常**\n> 气象预警数据已可正常获取。")
        changed = True
    state["had_errors"] = bool(real_failure)

    if changed:
        save_state(state)
        commit_state()
    log(f"本轮：活跃台风 {len(active)} 条，新推送/提醒 {pushed} 条，"
        f"解除 {lifted} 条，错误 {len(errors)}")
    for e in errors:
        log("ERR:", e)


def mode_daily():
    state = load_state()
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    if state.get("last_daily_report_date") == today:
        log("今日每日台风报告已推送，跳过重复推送")
        return

    active, errors = collect()
    typhoon_alerts = [a for a in active if a["type"] == "台风"]
    other = [a for a in active if a["type"] != "台风"]
    land = [a for a in other if not a["is_sea"]]
    sea = [a for a in other if a["is_sea"]]

    lines = ["> 【澄迈台风预警每日报告】",
             f"> 生成时间：{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M')}",
             f"> 今日生效台风预警：共 {len(typhoon_alerts)} 条"]
    if not typhoon_alerts:
        lines.append("> 当前无生效台风预警。")
    else:
        for a in typhoon_alerts:
            tag = "（海上）" if a["is_sea"] else ""
            lines.append(f"> ・[{a['level'] or '未知'}]{a['type']} {a['headline']}{tag}")

    # 台风趋势提示（预测式；基于 nmc 预报路径，仅作每日提示，不实时推送）
    outlook = typhoon_outlook()
    if outlook:
        lines.append("> —— 台风趋势提示 ——")
        lines.extend(outlook)

    if sea:
        lines.append("> 其他海上预警（不实时推送）：")
        for a in sea:
            lines.append(f"> ・[{a['level'] or '未知'}]{a['type']} {a['headline']}")
    if land:
        lines.append("> 其他陆地预警（不实时推送）：")
        for a in land:
            lines.append(f"> ・[{a['level'] or '未知'}]{a['type']} {a['headline']}")

    # 澄迈今日天气预报（Open-Meteo，免 key）
    fc = fetch_forecast()
    if fc and fc.get("temp_now") is not None:
        w_now = WMO_WEATHER.get(fc.get("code_now"), "未知")
        w_day = WMO_WEATHER.get(fc.get("code_day"), "未知")
        lines.append("> 澄迈今日天气预报：")
        lines.append(f"> ・现在：{w_now} {fc['temp_now']}℃ ｜ 湿度 {fc['humidity']}% ｜ 风速 {fc['wind_now']} km/h")
        lines.append(f"> ・今日：{w_day} ｜ 气温 {fc['tmin']}~{fc['tmax']}℃ ｜ 最大风速 {fc['wind_max']} km/h")
    else:
        lines.append("> 澄迈天气预报：获取失败")
    for e in errors:
        lines.append(f"> 数据获取异常：{e}")

    lines.append("> ——")
    lines.append("> 数据来源：中国气象局·中央气象台（预警）／ 中央气象台台风网 nmc（路径）")

    wechat_markdown("\n".join(lines))

    state["last_daily_report_date"] = today
    save_state(state)
    commit_state()
    log("每日报告已推送")


if __name__ == "__main__":
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "poll"
    if mode == "daily":
        mode_daily()
    else:
        mode_poll()
