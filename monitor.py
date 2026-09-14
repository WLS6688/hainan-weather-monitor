#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海南澄迈自然灾害预警监控
==================================================
数据源1: 中国气象局官方接口 weather.cma.cn/api/map/alarm
        （按 adcode 精确查询，需带 UA + Referer 否则 403）
        用途：已发布的自然灾害预警信号（反应式推送）
数据源2: 中央气象台台风网 nmc 台风路径接口
        list : http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default
        view : http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{id}
        用途：台风实时路径数据（生成中文路径图 + 每日报告趋势提示）
推送  : 企业微信群机器人 webhook（分级@ + markdown + 中文路径图）

监控类型（台风 + 汛期/台风次生灾害）:
  * 台风：含热带风暴、强热带风暴、热带气旋、热带低压等全部强度等级
  * 暴雨、大风、雷电、洪水、山洪、地质灾害、风暴潮、海浪
  * 实时推送【澄迈陆地上述预警】与【海南省级陆地上述预警】
    （海上预警、非澄迈陆地预警 -> 不实时推送，仅进入每日报告）

双层机制:
  [反应式] 气象台已发布台风预警信号 -> 立即/续推（实时）
  [预测式] nmc 台风预报路径 -> 仅作为“每日提示”放入每日天气报告，
           不在官方信号发布前做实时推送，避免刷屏

推送节奏(实时):
  * 每 10 分钟轮询：仅检测【新预警 / 预警变动】
  * 红/橙两级预警（且影响澄迈/海口陆地）-> 分级@推送；蓝/黄 -> 仅进每日报告
  * 信号与内容未变、且级别为黄/橙/红 -> 每 120 分钟续推一次（持续提醒）
  * 蓝色预警只在新发时推一次，不做周期提醒；白色不推送
  * 轮询时间(10min) ≠ 推送时间(120min)，解耦避免刷屏

实时逼近预警(临门一脚):
    * 每 10 分钟轮询时额外检查 nmc 预报路径；若预报显示 <48 小时内进入海南影响框
    -> 实时推送一条“台风逼近预警”（含剩余小时数 + 强度趋势 + 七级风圈 + 影响时段 + 影响预估 + 防御指引），每台风仅推一次，避免刷屏
  * 日常趋势提示仍在每日报告中给出，二者互补；无活跃威胁时不打扰

每日报告(09:00) — 含台风趋势提示:
  * 汇总当日生效的监控类型预警（陆地+海上）
  * 【台风趋势提示】：拉取 nmc 活跃台风预报路径（参考日本气象厅 JMA：强度等级 + 七级风圈）
      - 任一预报路径点进入“海南影响框” -> 提示“预计X日前后台风可能逼近/登录海南”
      - 已进入框/当前逼近 -> 提示“当前已进入海南影响范围”
      - 附：最新位置 / 强度 / 移动方向 / 强度趋势(加强中·减弱中·维持) / 七级风圈半径 / 预计影响时段 / 影响预估(区域·风级·降水·风暴潮) + 可点击台风路径链接
      - 无直接影响海南的台风 -> 提示“未来数日无预报路径直接影响海南的台风”
  * 附加【澄迈今日天气预报】（Open-Meteo 免 key）
  * 按日期去重：同一天无论触发几次，只推一次

台风路径:
  * 由 nmc 实时路径数据生成中文路径图（assets/SimHei.ttf）作为 image 消息推送
  * 同时附带可点击链接：typhoon.nmc.cn/web.html（跳转中央气象台台风网查看详情）

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
import base64
import hashlib
import io
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
# 直查区县（澄迈、海口；其余靠省级兜底覆盖）
REGIONS = {"澄迈": "469023", "海口": "460100"}
# 陆上即时推送目标区域：预警影响这些区域（且非海上）时才实时推送；其余只进每日报告
PUSH_REGIONS = ["澄迈", "海口"]
PROVINCE = "46"  # 海南省（兜底：抓全省预警，推送时再筛“澄迈/海口相关”）
# 省级兜底列表会同时返回全省各市县自行发布的预警（如"三亚市气象台发布暴雨橙色预警"）。
# 它们不属于监控范围，若全量收录会让每日报告被几十条其他市县预警淹没，
# 故只保留「省级台发布 / 标题正文命中澄迈·海口 / 台风类」三类。
PROVINCE_ISSUERS = ("海南省气象台", "海南省气象局", "海南省气象服务中心",
                    "海南省气象灾害防御中心")
# 监控的预警类型：
#  - 台风（含热带风暴 / 强热带风暴 / 热带气旋 / 热带低压等全部强度等级，中国气象局均以“台风预警信号”发布）
#  - 汛期及台风次生灾害：暴雨、大风、雷电、洪水、山洪、地质灾害、风暴潮、海浪
TARGET_TYPES = ["台风", "暴雨", "大风", "雷电", "洪水", "山洪", "地质灾害", "风暴潮", "海浪"]
# 台风家族别名归一化：标题里出现这些词时统一归为“台风”类
TYPHOON_ALIASES = ["热带风暴", "热带气旋", "热带低压", "强热带风暴", "超强台风", "强台风"]
# 兜底解析用关键词：正则未命中时，从标题/描述里按关键词识别灾种与等级（避免漏报）
TYPE_KEYWORDS = ["台风", "暴雨", "大风", "雷电", "洪水", "山洪", "地质灾害", "风暴潮",
                 "海浪", "高温", "寒潮", "大雾", "霾", "冰雹", "霜冻", "沙尘暴",
                 "道路结冰", "干旱", "海上大风"]
LEVEL_KEYWORDS = ["红色", "橙色", "黄色", "蓝色", "白色"]
# 海域关键词：**只在预警"信号名/标题"中出现**才判定为海上预警。
# 为什么不用正文判断：正文里"南海""东海"常出现在天气系统表述中
# （如"受南海热带低压残涡和冷空气共同影响"），会把海口/澄迈的陆地大风、暴雨预警
# 误判为海上预警 -> 该实时推送的被过滤掉、日报也归错分区。
SEA_KEYWORDS = ["海面", "海上", "琼州海峡", "附近海域", "北部湾",
                "西沙", "南沙", "中沙", "南海北部", "台湾海峡"]
# 正文中出现即可确认是海上（语义无歧义，不会用于描述天气系统）
SEA_DESC_STRICT = ["琼州海峡", "附近海域"]
# 持续提醒：仅"红/橙"两级（即会即时推送的等级）做每 120 分钟续推；
# 蓝/黄预警本就不即时推送，只进每日报告，因此不存在续推。
REPUSH_INTERVAL = 7200  # 秒（120 分钟）
# 等级序（由轻到重），用于判定"预警降级"
LEVEL_RANK = {"白色": 0, "蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4}
# 变动去重窗口：同一预警在 N 秒内的内容抖动只推一次，避免反复 @ 刷屏。
# 说明：常规轮询间隔为 10 分钟（600s），故该窗口主要用于防「手动触发 / 重试」造成的重复推送。
PUSH_DEDUP_SECONDS = 600
# 台风强度/路径"显著变化"再次提醒的门槛
IMMINENT_ETA_ADVANCE = 6 * 3600   # 预计进入时间提前 ≥6 小时 -> 补推一条升级提示
# 台风路径图：是否生成并推送路径图（依赖 matplotlib，运行时按需自装；装不上则自动降级为纯文字）
ENABLE_TRACK_IMAGE = True
# 通用防御指引基准（按等级）
LEVEL_ADVICE = {
    "红色": "立即停止户外活动，人员留在安全场所，切断危险电源，远离危房、边坡与河道。",
    "橙色": "停止露天活动，加固门窗，远离低洼积水与河道，注意防风防涝。",
    "黄色": "留意最新预警，减少外出，收好易被吹落物品，注意防雷防涝。",
    "蓝色": "关注预警动态，提前做好准备。",
}
# 暴雨专属防御指引
RAIN_ADVICE = {
    "红色": "停止集会，停课停工；切断低洼处电源，远离河道、地下空间与积水路段，严防内涝与山洪。",
    "橙色": "避免涉水通行，远离河道与排水不畅路段；危房、低洼住户提前转移。",
    "黄色": "注意积水与雷电，减少外出，勿在树下、广告牌下停留。",
    "蓝色": "关注雨情，提前清理排水口，出行注意积水。",
}
# 各灾种防御指引（类型+等级）。未列出灾种回落到 LEVEL_ADVICE。
TYPE_ADVICE = {
    "台风": LEVEL_ADVICE,
    "暴雨": RAIN_ADVICE,
    "大风": {
        "红色": "立即停止户外及高空作业，船舶回港避风，加固或拆除易被吹落物。",
        "橙色": "停止露天活动，加固门窗与高空悬挂物，船舶回港。",
        "黄色": "减少外出，固定易被吹落物品，注意防风。",
        "蓝色": "关注大风动态，收好阳台物品。",
    },
    "雷电": {
        "红色": "立即撤离空旷高地与水域，远离金属物与电力设施，暂停户外作业。",
        "橙色": "远离树木、金属物与水体，暂停户外活动。",
        "黄色": "减少外出，避免户外使用电子设备与金属物。",
        "蓝色": "关注雷电动态，雷雨时尽量在室内。",
    },
    "洪水": {
        "红色": "立即转移危险区人员，切断电源，远离河道与低洼地带。",
        "橙色": "危险区人员提前转移，远离河道，做好防洪准备。",
        "黄色": "留意水情，低洼住户提前防范，避免涉水。",
        "蓝色": "关注水情变化，提前准备。",
    },
    "山洪": {
        "红色": "立即向高处转移，远离沟谷河道，切勿沿行洪道撤离。",
        "橙色": "危险区人员迅速转移，远离山洪沟与河道。",
        "黄色": "留意山洪预警，避免在沟谷、河道停留。",
        "蓝色": "关注山洪动态，远离危险溪谷。",
    },
    "地质灾害": {
        "红色": "危险区人员立即转移，远离边坡、陡崖与泥石流沟。",
        "橙色": "危险区人员提前撤离，加强巡查监测。",
        "黄色": "留意险情，避免在边坡、陡崖下停留。",
        "蓝色": "关注地质灾害风险，远离隐患点。",
    },
    "风暴潮": {
        "红色": "沿海低洼地带人员立即转移，加固海堤，船舶进港避风。",
        "橙色": "沿海作业人员撤离，加固设施，船舶回港。",
        "黄色": "关注潮位，海上作业注意安全。",
        "蓝色": "关注风暴潮动态，提前防范。",
    },
    "海浪": {
        "红色": "海上作业全面停止，船舶回港避风，人员撤离危险岸段。",
        "橙色": "停止海上作业，船舶回港，远离危险海岸。",
        "黄色": "海上船只注意风浪，谨慎作业。",
        "蓝色": "关注海浪动态，水上活动注意安全。",
    },
}


def level_advice(a):
    """根据预警类型+等级返回防御指引。"""
    d = TYPE_ADVICE.get(a["type"], LEVEL_ADVICE)
    return d.get(a["level"], LEVEL_ADVICE.get(a["level"], ""))


# 台风实时路径（点击可跳转中央气象台台风网，查看实时动态/历史轨迹/预报）
TYPHOON_TRACK_URL = "https://typhoon.nmc.cn/web.html"
# 非台风类预警的官方详情页（中央气象台预警发布）
WARN_DETAIL_URL = "https://www.nmc.cn/publish/alarm.html"
# 澄迈坐标（用于天气预报 API：Open-Meteo 免 key）
CHENGMAI_LAT, CHENGMAI_LON = 19.74, 110.00
STATE_FILE = "state.json"
# state.json 中的"元数据键"（非预警记录）。计算"预警解除"时必须排除，
# 否则元数据变动会被误判成"某条预警解除"并推送解除通知。
META_STATE_KEYS = {"last_daily_report_date", "last_error_alert", "had_errors",
                   "fail_streak", "imminent_alerted", "disturbance_alerted",
                   "_dist_last_check", "_last_crash"}
PUSH_LOG = "data/push_log.jsonl"   # 推送审计日志（JSONL，逐条追加，可溯源）
ARCHIVE_DIR = "data/archive"       # 供人阅读的月度归档（Markdown，随仓库提交）
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


def norm_text(s):
    """归一化文本：剔除时间戳与"继续发布/重新发布"等发布动作词，用于内容指纹比对。

    目的：同一条预警"继续发布"（仅发布时间不同）不算新变化、不重复打扰；
    而"风力加大""雨量上调"等实质变化仍能被检出并推送。"""
    s = s or ""
    s = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分", "", s)
    s = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", s)
    s = re.sub(r"\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分", "", s)
    s = re.sub(r"\d{1,2}月\d{1,2}日\d{1,2}时", "", s)
    s = re.sub(r"\d{1,2}日\d{1,2}时\d{1,2}分", "", s)
    s = re.sub(r"\d{1,2}日\d{1,2}时", "", s)
    s = re.sub(r"\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{1,2}", "", s)
    s = s.replace("继续发布", "").replace("重新发布", "")
    return re.sub(r"\s+", "", s)


def parse(w):
    headline = w.get("headline", "") or w.get("title", "")
    desc = w.get("description", "")
    m = re.search(
        r"(.+?(?:气象台|气象局|预警发布中心|气象灾害防御中心)).*?发布(.+?)"
        r"(蓝色|黄色|橙色|红色|白色|一级|二级|三级|四级|五级)?预警",
        headline,
    )
    issuer = m.group(1) if m else ""
    wtype = m.group(2) if m else ""
    level = m.group(3) if m else ""

    # —— 兜底解析：正则未命中时，用关键词从 标题+描述 中识别灾种与等级 ——
    hay = (headline or "") + " " + (desc or "")
    if not issuer:
        mi = re.search(r"([\u4e00-\u9fa5]{2,12}?(?:气象台|气象局|预警发布中心|气象灾害防御中心))",
                       hay)
        issuer = mi.group(1) if mi else ""
    if not wtype:
        for kw in TYPE_KEYWORDS:
            if kw in hay:
                wtype = kw
                break
    if not level:
        for lk in LEVEL_KEYWORDS:
            if lk in hay:
                level = lk
                break
    # 等级别名归一化：一级/二级/三级/四级/五级 -> 红/橙/黄/蓝/白
    LEVEL_ALIAS = {"一级": "红色", "二级": "橙色", "三级": "黄色", "四级": "蓝色", "五级": "白色"}
    level = LEVEL_ALIAS.get(level, level)
    # 台风家族别名归一化：热带风暴/热带气旋/热带低压等统一归为“台风”
    if wtype:
        for _al in TYPHOON_ALIASES:
            if _al in wtype:
                wtype = "台风"
                break

    # —— 稳定键 ——
    # 官方 id 形如 "46902341600000_20260914101031"（下划线后是本次发布时刻），
    # "继续发布" 会换新 id。若直接用 id 当去重键，会把"继续发布"误判成
    # "旧预警解除 + 新预警发布"，导致群里反复出现"预警解除"与重复 @。
    # 故用「发布机构前缀 + 灾种代码（不含等级）」做稳定键：
    #   - 继续发布 / 等级升降 -> 同一键，走"内容变动"分支，只推真正有变化的那次；
    #   - 真正解除 -> 该键从活跃列表消失，才发"预警解除"。
    raw_id = str(w.get("id") or "")
    prefix = raw_id.split("_")[0]
    raw_type = str(w.get("type") or "")          # 官方灾种代码，如 p0002=暴雨 / p0007=大风
    disaster = raw_type[:5] if len(raw_type) >= 5 else (wtype or "")
    key = (f"{prefix}|{disaster}" if (prefix or disaster)
           else (raw_id or norm_text(headline) or headline))

    return {
        "id": w.get("id"),
        "key": key,
        "headline": headline,
        "issuer": issuer,
        "type": wtype,
        "level": level,
        "effective": w.get("effective", ""),
        "description": desc,
        "is_sea": (any(k in (headline or "") for k in SEA_KEYWORDS)
                   or any(k in (desc or "") for k in SEA_DESC_STRICT)),
        "raw_extra": {"type": raw_type} if raw_type else {},
    }


def is_target_type(a):
    """仅处理监控列表内的预警类型（台风及汛期/台风次生灾害）。"""
    return any(t in (a["type"] or "") for t in TARGET_TYPES)


def should_push(a):
    """是否实时推送：仅 澄迈/海口 陆地预警 + 海南省级陆地预警实时推；海上预警不实时推。"""
    if a.get("is_sea"):
        return False                       # 海上预警一律不实时推送（进每日报告）
    region = a.get("region") or ""
    if region in PUSH_REGIONS:
        return True                        # 澄迈/海口本区预警
    if region == "海南省":
        if a.get("type") == "台风":
            return True                    # 省级陆地台风预警
        text = (a.get("headline") or "") + (a.get("description") or "")
        return any(r in text for r in PUSH_REGIONS)   # 省级其他预警：命中澄迈/海口才推
    return False


def content_sig(a):
    """预警"内容指纹"：实质内容变化即视为预警更新，触发（去重后的）再次推送。

    - 用 norm_text 归一化标题/正文：剔除时间戳与"继续发布"，避免"继续发布"被当成更新；
    - 不含 effective（每次继续发布都会变），否则去重形同失效；
    - 纳入官方灾种代码，等级升降（代码末位变化）也能被捕获。
    结果：风力加大 / 雨量上调 / 等级升降 都会通知，而单纯的"继续发布"不会重复打扰。
    """
    extra = tuple(sorted((str(k), str(v)) for k, v in (a.get("raw_extra") or {}).items()))
    return (norm_text(a["headline"]), norm_text(a["description"]),
            a["level"], a["type"], extra)


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
            if not p["key"] or p["key"] in alerts:
                continue
            if not is_target_type(p):
                continue  # 非监控类型（台风/暴雨/大风/雷电/洪水等），忽略
            p["region"] = name
            alerts[p["key"]] = p
    prov = fetch(PROVINCE)
    if prov is None:
        errors.append("省级(46) 获取失败")
    else:
        for w in prov:
            p = parse(w)
            if not p["key"] or p["key"] in alerts:
                continue
            if not is_target_type(p):
                continue  # 非监控类型（台风/暴雨/大风/雷电/洪水等），忽略
            text = p["headline"] + p["description"]
            keep = (any(s in (p["issuer"] or "") for s in PROVINCE_ISSUERS)
                    or any(r in text for r in PUSH_REGIONS)
                    or p["type"] == "台风")
            if not keep:
                continue  # 其他市县的本地预警：不属监控范围，丢弃
            p["region"] = "海南省"
            alerts[p["key"]] = p
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
    """把 state.json 与推送日志/归档一起提交回仓库（持久化去重状态 + 留存溯源记录）。"""
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        if os.path.isdir("data"):
            subprocess.run(["git", "add", "data"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode != 0:
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
            subprocess.run(["git", "commit", "-m", "chore: update warning state & push log"], check=False)
            r = subprocess.run(["git", "push"], check=False).returncode
            if r != 0:
                # 轮询任务与每日报告可能同一分钟并发触发，先 rebase 再重推一次
                log("push 失败，尝试 pull --rebase 后重试")
                subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
                subprocess.run(["git", "push"], check=False)
            log("state 与推送日志已提交")
    except Exception as e:
        log("commit_state error:", e)


def record_push(kind, alert=None, msgtype="", extra=None):
    """把一次推送记入审计日志（data/push_log.jsonl）+ 月度 Markdown 归档，便于事后溯源。

    kind  : alert / daily / imminent / lifted / error / recovered
    alert : 预警 dict（可选），会抽取 type/level/region/issuer/headline 等字段
    extra : 追加的自定义字段（如 image、count、eta 等）
    """
    now = datetime.now(BEIJING)
    rec = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "msgtype": msgtype}
    if alert:
        rec.update({
            "type": alert.get("type"), "level": alert.get("level"),
            "region": alert.get("region"), "is_sea": bool(alert.get("is_sea")),
            "issuer": alert.get("issuer"), "effective": alert.get("effective"),
            "headline": alert.get("headline"),
        })
    if extra:
        rec.update(extra)
    try:
        d = os.path.dirname(PUSH_LOG)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(PUSH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("写入推送日志失败:", e)
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        ap = os.path.join(ARCHIVE_DIR, now.strftime("%Y-%m") + ".md")
        tag = f"{rec.get('type') or ''}{rec.get('level') or ''}".strip() or kind
        head = rec.get("headline") or rec.get("note") or ""
        with open(ap, "a", encoding="utf-8") as f:
            f.write(f"- `{rec['ts']}` ｜ {tag} ｜ {rec.get('region') or ''} ｜ {msgtype} ｜ {head}\n")
    except Exception as e:
        log("写入归档失败:", e)
    return rec


def export_history(fmt="csv"):
    """把推送日志导出为便于查看/表格分析的文件（csv 或 md）。返回输出路径。"""
    recs = []
    try:
        with open(PUSH_LOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        recs.append(json.loads(ln))
                    except Exception:
                        pass
    except FileNotFoundError:
        log("暂无推送日志:", PUSH_LOG)
        return None
    os.makedirs("data", exist_ok=True)
    if fmt == "md":
        out = "data/history.md"
        cols = ["ts", "kind", "type", "level", "region", "msgtype", "headline"]
        with open(out, "w", encoding="utf-8") as f:
            f.write("| " + " | ".join(cols) + " |\n")
            f.write("|" + "---|" * len(cols) + "\n")
            for r in recs:
                f.write("| " + " | ".join(str(r.get(c, "") or "").replace("|", "/") for c in cols) + " |\n")
    else:
        out = "data/history.csv"
        import csv
        cols = ["ts", "kind", "type", "level", "region", "is_sea", "issuer", "effective",
                "msgtype", "headline"]
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in recs:
                w.writerow([r.get(c, "") for c in cols])
    log(f"已导出 {len(recs)} 条推送到 {out}")
    return out


def wechat_markdown(content):
    """发送 markdown 消息。返回是否成功（供调用方决定是否落盘状态/记日志）。"""
    if not WEBHOOK:
        log("未配置 WECHAT_WEBHOOK，跳过推送")
        return False
    try:
        http_post_json(WEBHOOK, {"msgtype": "markdown",
                                 "markdown": {"content": content}}, timeout=10)
        log("推送成功")
        return True
    except Exception as e:
        log("推送失败:", e)
        return False


def wechat_text(content, mention_list=None, mention_mobile=None):
    """文本消息：唯一能 @all / @指定人的类型（markdown 不支持 @all）。返回是否成功。"""
    if not WEBHOOK:
        log("未配置 WECHAT_WEBHOOK，跳过推送")
        return False
    text = {"content": content}
    if mention_list:
        text["mentioned_list"] = mention_list          # ["@all"] 或 ["userid", ...]
    if mention_mobile:
        text["mentioned_mobile_list"] = mention_mobile  # ["138xxxx"] 或 ["@all"]
    try:
        http_post_json(WEBHOOK, {"msgtype": "text", "text": text}, timeout=10)
        log("text 推送成功")
        return True
    except Exception as e:
        log("text 推送失败:", e)
        return False


def wechat_image(png_bytes):
    """图片消息：把生成的台风路径图以 base64 发出（方案 A 下呈现路径图的最佳方式）。返回是否成功。"""
    if not WEBHOOK or not png_bytes:
        return False
    try:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        md5 = hashlib.md5(png_bytes).hexdigest()
        http_post_json(WEBHOOK, {"msgtype": "image",
                                 "image": {"base64": b64, "md5": md5}}, timeout=15)
        log("路径图推送成功")
        return True
    except Exception as e:
        log("路径图推送失败:", e)
        return False


def push_tier(a):
    """返回预警的即时推送档位：'red' / 'orange' / None（蓝黄及海上不即时推送）。"""
    if not should_push(a):
        return None
    if a["level"] == "红色":
        return "red"
    if a["level"] == "橙色":
        return "orange"
    return None  # 蓝/黄/海上：仅进入每日报告，不弹群消息


def push_alert_graded(a, repeat=False):
    """按等级分级推送。返回是否成功发出。

    红  -> text(含@all) + markdown 详情卡（text 保证全员必达，详情卡保留完整信息）
    橙  -> markdown 详情卡 + text(含@all) 提示（markdown 不支持 @all，故补一条 text 触发提醒）
    蓝/黄/海上 -> 不在此推送（由每日报告汇总）
    """
    tier = push_tier(a)
    if tier is None:
        return False
    title = alert_title(a)
    core = alert_core(a)
    note = "（持续预警·每2小时提醒）" if repeat else ""
    if tier == "red":
        text = title + "\n"
        if core:
            text += core + "\n"
        text += (f"发布：{a['issuer'] or a['region']} ｜ {a['effective']}{note}\n"
                 f"防御指引：{level_advice(a)}")
        ok_t = wechat_text(text, mention_list=["@all"])
        ok_m = wechat_markdown(fmt_alert(a, repeat=repeat))   # 详情卡（@all 由上方 text 保证）
        ok = ok_t or ok_m
        record_push("alert", a, msgtype="text+markdown",
                    extra={"tier": "red", "repeat": repeat, "ok": ok})
    else:  # orange
        ok_m = wechat_markdown(fmt_alert(a, repeat=repeat))
        ok_t = wechat_text(f"⚠️ {title}{a.get('region', '')}已生效，请全员关注防御。",
                           mention_list=["@all"])
        ok = ok_t or ok_m
        record_push("alert", a, msgtype="markdown+text",
                    extra={"tier": "orange", "repeat": repeat, "ok": ok})
    return ok


def load_cjk_font():
    """加载中文字体，返回 matplotlib 字体名；失败返回 None（调用方退化为英文标签）。"""
    try:
        import matplotlib
        fm = matplotlib.font_manager
        cand = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "SimHei.ttf")]
        for p in cand:
            if os.path.exists(p):
                try:
                    fm.fontManager.addfont(p)
                    return fm.FontProperties(fname=p).get_name()
                except Exception:
                    pass
        for f in fm.fontManager.ttflist:
            if any(k in (f.name or "") for k in ("SimHei", "Hei", "Noto Sans CJK",
                                                 "Source Han", "WenQuanYi", "YaHei",
                                                 "PingFang")):
                return f.name
    except Exception:
        pass
    return None


def render_track_image(cur, forecast, track=None, title="台风路径预报"):
    """生成台风路径图 PNG（bytes）。依赖 matplotlib，运行时按需自装；失败返回 None。

    采用中央气象台实时路径数据绘制（实际数据，非过度渲染）；中文标注，
    并配合消息内的可点击链接查看实时动态。"""
    if not ENABLE_TRACK_IMAGE:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            "matplotlib"], check=False, timeout=240)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as e:
            log("matplotlib 不可用，跳过路径图:", e)
            return None
    try:
        cn = False
        try:
            fname = load_cjk_font()
            if fname:
                plt.rcParams["font.sans-serif"] = [fname]
                plt.rcParams["axes.unicode_minus"] = False
                cn = True
        except Exception:
            pass

        lat_min, lat_max, lon_min, lon_max = HAINAN_BOX
        pad = 5
        fig, ax = plt.subplots(figsize=(5.6, 5.6), dpi=130)
        ax.set_facecolor("#eef5fb")
        ax.set_xlim(lon_min - pad, lon_max + pad)
        ax.set_ylim(lat_min - pad, lat_max + pad)
        ax.set_xlabel("经度 (°E)" if cn else "Longitude (deg E)")
        ax.set_ylabel("纬度 (°N)" if cn else "Latitude (deg N)")
        ax.set_title(title if cn else "Typhoon Track & Forecast",
                     fontsize=13, fontweight="bold")
        ax.grid(True, linestyle=":", alpha=0.35)

        # 海南岛轮廓（浅色，仅作定位参照）
        hn_lon = [108.6, 109.2, 109.9, 110.6, 111.0, 110.5, 109.8, 108.9, 108.6]
        hn_lat = [19.9, 20.1, 19.7, 19.3, 18.4, 18.2, 18.1, 18.4, 19.9]
        ax.fill(hn_lon, hn_lat, color="#d7ead0", edgecolor="#5a8a3c",
                linewidth=0.8, alpha=0.7)
        # 主要城市标注
        city_en = {"澄迈": "Chengmai", "海口": "Haikou", "三亚": "Sanya"}
        for clon, clat, cname in [(CHENGMAI_LON, CHENGMAI_LAT, "澄迈"),
                                   (110.20, 20.04, "海口"), (109.51, 18.25, "三亚")]:
            ax.plot(clon, clat, "k*", markersize=7)
            ax.text(clon + 0.15, clat - 0.35, cname if cn else city_en[cname], fontsize=8)

        # 历史实况轨迹
        if track:
            tlon = [p["lon"] for p in track if "lon" in p]
            tlat = [p["lat"] for p in track if "lat" in p]
            if len(tlon) > 1:
                ax.plot(tlon, tlat, "-", color="#185FA5", linewidth=1.8, alpha=0.85)
                ax.scatter(tlon, tlat, color="#185FA5", s=12, zorder=3)
        # 预报路径（含不确定性锥）
        if forecast:
            flon = [f["lon"] for f in forecast]
            flat = [f["lat"] for f in forecast]
            if len(flon) > 1:
                ax.plot(flon, flat, "--", color="#E24B4A", linewidth=1.8)
                for i in range(1, len(flon)):
                    dx = flon[i] - flon[i - 1]
                    dy = flat[i] - flat[i - 1]
                    r = min(2.4, (dx * dx + dy * dy) ** 0.5 * 1.6 + 0.3)
                    ax.add_patch(plt.Circle((flon[i], flat[i]), r,
                                  color="#E24B4A", alpha=0.08))
                ax.scatter(flon, flat, color="#E24B4A", s=20, marker="^", zorder=4)
        # 当前位置
        ax.plot(cur["lon"], cur["lat"], "o", color="#A32D2D", markersize=12,
                markeredgecolor="white", zorder=5)
        ax.text(cur["lon"] + 0.2, cur["lat"] + 0.2, "当前位置" if cn else "Now",
                fontsize=9, color="#A32D2D")
        # 图例
        from matplotlib.lines import Line2D
        ax.legend(handles=[
            Line2D([0], [0], color="#185FA5", lw=1.8, label="历史路径" if cn else "Past track"),
            Line2D([0], [0], color="#E24B4A", lw=1.8, linestyle="--",
                   label="预报路径" if cn else "Forecast"),
            Line2D([0], [0], marker="o", color="#A32D2D", lw=0,
                   label="当前位置" if cn else "Current",
                   markerfacecolor="#A32D2D", markersize=9),
        ], loc="lower left", fontsize=8, framealpha=0.9)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log("路径图渲染失败:", e)
        return None


def alert_title(a):
    """规范短标题（不含重复字样），如【台风红色预警】。"""
    t = (a.get("type") or "").strip()
    lv = (a.get("level") or "").strip()
    return f"【{t}{lv}预警】" if (t or lv) else "【气象预警】"


def clean_headline(h):
    """去掉标题里的“XX气象台发布 / XX预警[I级]”等外壳，只保留实质描述。"""
    s = re.sub(r"[\[【][^\]】]*[\]】]", "", h or "")
    s = re.sub(r"^[\u4e00-\u9fa5]{2,12}?(?:气象台|气象局|预警发布中心|气象灾害防御中心)", "", s)
    s = s.replace("发布", "")
    s = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日(\d{1,2}时)?(\d{1,2}分)?", "", s)   # 去掉裸时间戳
    s = re.sub(r"(蓝色|黄色|橙色|红色|白色)(预警)?(信号)?", "", s)
    s = s.replace("预警", "")
    return re.sub(r"^[，。、,;；:：\-\s]+|[，。、,;；:：\-\s]+$", "", s).strip()


def alert_core(a):
    """返回预警的"实质内容"（与标题/发布信息不重复）；无实质内容返回空串。

    官方 description 的格式固定为「机构 + 时间 + 信号名 + ：+ 真正内容」，
    冒号前全是套话，冒号后才是要读的东西，故直接取冒号后部分。
    """
    desc = (a.get("description") or "").strip()
    if desc:
        parts = re.split(r"[：:]", desc, 1)
        body = re.sub(r"\s+", "", (parts[1] if len(parts) > 1 else desc)).strip()
        if len(re.sub(r"[，。、,;；:：\-\s]", "", body)) >= 4:
            return body
    # 回退：标题经清洗后的实质内容
    h = clean_headline(a.get("headline") or "")
    if len(re.sub(r"[，。、,;；:：\-\s]", "", h)) >= 4:
        return h
    return ""


def fmt_alert(a, repeat=False):
    note = " ｜ 持续预警·每2小时提醒" if repeat else ""
    advice = level_advice(a)
    link = (f"> [实时台风路径·点击查看]({TYPHOON_TRACK_URL})" if a.get("type") == "台风"
            else f"> [预警详情·点击查看]({WARN_DETAIL_URL})")
    lines = [f"> **{alert_title(a)}**"]
    core = alert_core(a)
    if core:
        lines.append(f"> {core}")
    lines.append(f"> 发布：{a['issuer'] or a['region']} ｜ {a['effective']}{note}")
    lines.append(f"> 防御指引：{advice}")
    lines.append(link)
    lines.append("> 数据来源：中国气象局·中央气象台")
    return "\n".join(lines)


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
    return {"cur": cur, "forecast": forecast, "trend": trend, "track": actual}


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
    """nmc 时间 'YYYYMMDDHHMM'（**世界时 UTC**）-> 'M月D日 HH:MM'（北京时 = UTC+8）。

    已用接口的 epoch 字段核对过：nmc 的 time 字段与配套 epoch 一致，均为 UTC，
    而中央气象台台风网页面展示的是北京时，故此处必须 +8 小时。"""
    if len(s) != 12:
        return s
    try:
        dt = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                      int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc)
        loc = dt.astimezone(BEIJING)
        return f"{loc.month}月{loc.day}日 {loc.strftime('%H:%M')}"
    except Exception:
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


def nmc_bjt(s):
    """nmc 世界时时间串 -> (月, 日, 时) 北京时；失败返回 None。"""
    if len(s) != 12:
        return None
    try:
        dt = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                      int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc).astimezone(BEIJING)
        return dt.month, dt.day, dt.hour
    except Exception:
        return None


def fmt_window(box_fc):
    """进入海南框的预报点时间窗口 -> 精确时刻 + 人话时段（北京时）。"""
    if not box_fc:
        return ""
    times = sorted(f["time"] for f in box_fc)
    t0, t1 = times[0], times[-1]
    if t0 == t1:
        return f"{fmt_dt(t0)}（北京时）"
    a, b = nmc_bjt(t0), nmc_bjt(t1)
    precise = f"{fmt_dt(t0)} 至 {fmt_dt(t1)}"
    if not (a and b):
        return f"{precise}（北京时）"
    human = (f"{a[0]}月{a[1]}日{phase_cn(a[2])} 至 "
             f"{b[0]}月{b[1]}日{phase_cn(b[2])}")
    return f"{precise}（北京时），约 {human}"


def haversine(lat1, lon1, lat2, lon2):
    """两点间大圆距离（km）。"""
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    dphi = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dl / 2) ** 2
    return R * 2 * asin(sqrt(a))


def wind_desc(ms):
    """风速(m/s) -> 阵风等级中文描述（参考蒲福风级）。"""
    if ms is None:
        return "风力不明"
    if ms < 10.8:
        return "6级以下"
    if ms < 17.2:
        return "6-7级"
    if ms < 24.5:
        return "8-9级"
    if ms < 32.7:
        return "10-11级"
    if ms < 41.5:
        return "12-13级"
    if ms < 50.9:
        return "14-15级"
    return "16级或以上"


def impact_assessment(det, box_fc, in_hainan):
    """根据预报进入海南框的点，估算对海南/澄迈的影响（区域 + 风级 + 降水 + 风暴潮）。
    返回完整 markdown 行（以 '> 　' 开头），无明确影响时返回 ''。参考香港天文台“影响”段落做法。"""
    cur = det["cur"]
    # 进入框的点中离澄迈最近者；若当前已在框内则一并纳入
    cand = [(haversine(f["lat"], f["lon"], CHENGMAI_LAT, CHENGMAI_LON), f) for f in box_fc]
    if in_hainan:
        cand.append((haversine(cur["lat"], cur["lon"], CHENGMAI_LAT, CHENGMAI_LON), cur))
    if not cand:
        return ""
    cand.sort(key=lambda x: x[0])
    mind, near = cand[0]
    wspd = near.get("wind") or cur.get("wind")
    lvl = wind_desc(wspd)
    # 区域 + 海南方位（南部/东部/西部/北部）判定
    on_island = (18.0 <= near["lat"] <= 20.1 and 108.5 <= near["lon"] <= 111.1)
    if on_island:
        if near["lat"] < 19.2:
            sub = "南部"
        elif near["lat"] > 19.8:
            sub = "北部"
        else:
            sub = "中部"
        if near["lon"] < 109.8:
            sub += "西部"
        elif near["lon"] > 110.2:
            sub += "东部"
        area = f"海南岛{sub}（含澄迈一带）" if mind < 80 else f"海南岛{sub}（陆地）"
    elif mind < 150:
        area = "海南岛周边近海及沿海"
    else:
        area = "海南邻近海域"
    rank = STRENGTH_RANK.get(near.get("strength") or cur.get("strength"), 0)
    storm = "，并伴风暴潮风险" if rank >= 4 else ""
    rain = "暴雨到大暴雨" if rank >= 3 else ("大雨" if rank >= 2 else "阵雨或雷阵雨")
    return ("> 　影响预估：预计对" + area + "带来风雨影响，"
            f"沿海及近海阵风可达 {lvl}，伴{rain}{storm}；"
            "澄迈需关注防风、防涝及海上作业安全。")


# ---- 热带扰动物理监测（Open-Meteo 风速+气压代理，不依赖编号）----
# 解决盲区：nmc 仅收录已编号台风，未编号热带扰动（南海土台风前身）无路径数据；
# 用近海风速+海平面气压作代理，气压偏低(热带低压特征)可过滤多数冷空气大风误报。
SEA_POINTS = [
    ("澄迈近海", 19.6, 110.6),
    ("海南东北部近海", 19.5, 111.0),
    ("海南东南部近海", 18.6, 111.0),
    ("海南南部近海", 18.3, 109.6),
    ("三亚近海", 17.8, 109.5),
    ("海南西部近海", 19.3, 108.7),
]
DIST_WIND_MS = 10.8     # 持续风 ≥6级
DIST_PRES = 1003.0      # 且海平面气压 ≤1003 hPa（热带系统低压特征）
DIST_GUST_MS = 17.2     # 或阵风 ≥8级
DIST_GUST_PRES = 1005.0


def fetch_sea_wind(lat, lon, tries=2):
    """Open-Meteo 拉取近海未来48h 风速/阵风/海平面气压(m/s,hPa)。失败返回 None。"""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&hourly=wind_speed_10m,wind_gusts_10m,surface_pressure"
           "&wind_speed_unit=ms&forecast_days=2&timezone=Asia%2FShanghai")
    for i in range(tries):
        try:
            raw = http_get(url, {"User-Agent": "Mozilla/5.0"}, timeout=15)
            d = json.loads(raw)
            h = d.get("hourly") or {}
            return {"ws": h.get("wind_speed_10m") or [],
                    "gust": h.get("wind_gusts_10m") or [],
                    "pres": h.get("surface_pressure") or []}
        except Exception as e:
            log("sea_wind error:", e)
        time.sleep(2 * (i + 1))
    return None


def disturbance_signal():
    """扫描海南近海监测点，若检出热带系统大风信号则返回最强点 dict，否则 None。"""
    best = None
    for name, lat, lon in SEA_POINTS:
        d = fetch_sea_wind(lat, lon)
        if not d or not d["pres"]:
            continue
        for ws, g, p in zip(d["ws"], d["gust"], d["pres"]):
            if ws is None or p is None:
                continue
            hit = (ws >= DIST_WIND_MS and p <= DIST_PRES) or \
                  (g and g >= DIST_GUST_MS and p <= DIST_GUST_PRES)
            if hit and (best is None or p < best["pressure"]):
                best = {"region": name, "wind_ms": ws,
                        "gust_ms": g or ws, "pressure": p}
    return best


def fmt_disturbance(sig):
    return ("> **【热带系统大风监测 · 非官方预警】**\n"
            f"> 监测到{sig['region']}未来48h持续风可达 {wind_desc(sig['wind_ms'])}、"
            f"阵风 {wind_desc(sig['gust_ms'])}，\n"
            f"> 海平面气压偏低（约 {sig['pressure']:.0f} hPa），可能为热带扰动影响。\n"
            f"> 此为气象数据代理提示（非气象台正式预警），请以官方台风预警信号为准。")


def disturbance_outlook():
    """每日报告用的物理监测补充段（非官方）。"""
    sig = disturbance_signal()
    if not sig:
        return []
    return ["> —— 热带扰动监测（非官方） ——", fmt_disturbance(sig)]


def check_disturbance(state):
    """实时轮询中的物理监测补充：检出信号且达推送条件 -> 实时提示（每天≤1次，升级再提示）。"""
    rec = state.setdefault("disturbance_alerted", {})
    now = time.time()
    if now - (state.get("_dist_last_check", 0.0) or 0.0) < 3600:
        return False   # 每1小时才查一次 Open-Meteo（6个近海点），降低外部调用量
    state["_dist_last_check"] = now
    sig = disturbance_signal()
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    if not sig:
        if rec.get("active"):
            rec["active"] = False
            rec.pop("pressure", None)
            rec.pop("date", None)
            return True
        return False
    first_today = rec.get("date") != today
    stronger = (rec.get("pressure") is None) or (sig["pressure"] <= rec["pressure"] - 5)
    if first_today or stronger:
        wechat_markdown(fmt_disturbance(sig))
        rec["date"] = today
        rec["active"] = True
        rec["pressure"] = sig["pressure"]
        rec["region"] = sig["region"]
        log("[扰动监测] 实时提示:", sig["region"], sig["pressure"])
        return True
    return False


def parse_nmc_time(s):
    """nmc 时间 'YYYYMMDDHHMM'（**世界时 UTC**）-> epoch；失败返回 None。"""
    if len(s) != 12:
        return None
    try:
        dt = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                      int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc)
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
        imp = impact_assessment(det, box_fc, in_hainan)
        if (eta or in_hainan) and imp:
            lines.append(imp)
        lines.append(f"> 　实时路径：{TYPHOON_TRACK_URL}")
    if not lines:
        # 有活跃台风但均不直接影响海南
        return ["> 台风趋势提示：未来数日无预报路径直接影响海南的台风。"]
    return lines


def imminent_message(t, det, box_fc, eta, remain, escalated=None):
    """构造台风趋向/逼近提醒的 markdown（首次与"再提醒"共用）。"""
    cur = det["cur"]
    scn = STRENGTH_CN.get(cur["strength"], cur["strength"] or "未知")
    pres = f"，中心气压 {cur['pressure']} hPa" if cur.get("pressure") else ""
    radius7 = f"，七级风圈半径约 {cur['radius7']} km" if cur.get("radius7") else ""
    trend = det.get("trend", "强度平稳")
    win = fmt_window(box_fc)
    imp = impact_assessment(det, box_fc, in_box(cur["lat"], cur["lon"]))
    hours = int(remain // 3600)
    if remain < 0:
        title = "【台风已进入海南影响范围】"
        sub = "当前预报路径已进入海南影响范围，请立即关注官方台风预警信号。"
    elif remain <= 48 * 3600:
        title = f"【台风逼近预警】剩余约 {hours} 小时"
        sub = (f"台风 {t['cn']}（编号{t['num']}）预报路径预计 {fmt_dt(eta['time'])} "
               f"前后进入海南影响范围。")
    else:
        title = f"【台风趋向海南影响区】预计约 {hours} 小时后进入"
        sub = (f"台风 {t['cn']}（编号{t['num']}）预报路径预计 {fmt_dt(eta['time'])} "
               f"前后进入海南影响范围。")
    head = ""
    if escalated == "强度升级":
        head = f"> ⬆️ **预警更新：台风强度已升级**（当前 {scn}）\n"
    elif escalated == "预计时间提前":
        head = f"> ⬆️ **预警更新：预计进入时间明显提前**（现预计 {fmt_dt(eta['time'])}）\n"
    msg = (f"{head}"
           f"> **{title}**\n"
           f"> {sub}\n"
           f"> 当前：{cur['lat']:.1f}°N, {cur['lon']:.1f}°E，强度 {scn}{pres}\n"
           f"> 趋势：{trend}{radius7}\n"
           f"> 影响时段：{win}\n")
    if imp:
        msg += f"{imp}\n"
    msg += (f"> 防御指引：{LEVEL_ADVICE.get('红色', '')}\n"
            f"> 请提前做好防风准备，并密切关注官方台风预警信号。\n"
            f"> [实时台风路径·点击查看]({TYPHOON_TRACK_URL})\n"
            f"> 数据来源：中国气象局·中央气象台")
    return msg, title


def check_imminent(state):
    """台风趋向/逼近实时预警。

    - 预报路径进入海南影响框 -> 首次实时推一条；
    - 之后仅在【显著变化】时补推：强度升级（≥1 级）或预计进入时间提前 ≥6 小时；
    - 台风停编后清理其记录，避免 state.json 无限增长。
    """
    changed = False
    alerted = state.setdefault("imminent_alerted", {})
    now = time.time()
    try:
        lst = fetch_typhoon_list()
    except Exception as e:
        log("imminent list error:", e)
        return False
    if not lst:
        return False   # 列表拉取失败：保留既有状态，避免把在编台风误清理后又重复告警
    active = [x for x in lst if x.get("status") and x["status"] != "stop"]
    for t in active:
        try:
            det = fetch_typhoon_track(t["id"])
            if not det:
                continue
            box_fc = [f for f in det["forecast"] if in_box(f["lat"], f["lon"])]
            if not box_fc:
                continue
            eta = min(box_fc, key=lambda f: f["time"])
            eta_epoch = parse_nmc_time(eta["time"])
            if eta_epoch is None:
                continue
            cur = det["cur"]
            rank = STRENGTH_RANK.get(cur.get("strength"), 0)
            key = str(t["id"])
            old = alerted.get(key)
            remain = eta_epoch - now
            if old is None:
                escalated = "首次"
            elif rank - (old.get("rank") or 0) >= 1:
                escalated = "强度升级"
            elif (old.get("eta_epoch") or eta_epoch) - eta_epoch >= IMMINENT_ETA_ADVANCE:
                escalated = "预计时间提前"
            else:
                continue   # 无显著变化，不重复打扰
            msg, title = imminent_message(t, det, box_fc, eta, remain, escalated)
            ok = wechat_markdown(msg)
            # 附台风路径图（方案 A 下用 image 消息呈现路径，最直观）
            img = render_track_image(cur, det["forecast"], det.get("track"),
                                     title=f"台风“{t['cn']}”路径预报")
            img_ok = wechat_image(img) if img else False
            record_push("imminent",
                        msgtype=("markdown+image" if img_ok else "markdown"),
                        extra={"cn": t["cn"], "num": t["num"], "eta": eta["time"],
                               "eta_bjt": fmt_dt(eta["time"]),
                               "remain_hours": int(remain // 3600),
                               "strength": cur.get("strength"), "rank": rank,
                               "escalate": escalated, "ok": ok, "note": title})
            alerted[key] = {"cn": t["cn"], "num": t["num"], "eta": eta["time"],
                            "eta_epoch": eta_epoch, "rank": rank,
                            "strength": cur.get("strength")}
            changed = True
            log(f"[{escalated}] 实时推送:", t["cn"], "剩余约", int(remain // 3600), "小时")
        except Exception as e:
            log("imminent 处理异常", t.get("id"), repr(e))
            continue
    # 清理已停编台风的历史记录
    alive = {str(x["id"]) for x in active}
    for k in [k for k in alerted if k not in alive]:
        alerted.pop(k, None)
        changed = True
    return changed


def mode_poll():
    now = time.time()
    state = load_state()
    changed = False
    pushed = 0
    try:
        active, errors = collect()
    except Exception as e:
        log("collect 异常:", repr(e))
        active, errors = [], [f"collect 异常: {e}"]

    # 稳定键集合（不含 id 里的发布时刻）；元数据键一律排除，避免被误判为"预警解除"
    active_keys = {a["key"] for a in active}
    removed_ids = {k for k in state.keys()
                   if k not in active_keys
                   and k not in META_STATE_KEYS
                   and not k.startswith("_")}

    for a in active:
        rec = state.get(a["key"])
        if rec is None:
            # 新预警
            pushed_now = False
            if push_tier(a):   # 仅 红/橙 进入此分支；蓝/黄/海上不即时推送
                push_alert_graded(a)
                pushed_now = True
                pushed += 1
            state[a["key"]] = {
                "headline": a["headline"], "is_sea": a["is_sea"],
                "description": a["description"], "effective": a["effective"],
                "issuer": a["issuer"], "type": a["type"], "level": a["level"],
                "sig": list(content_sig(a)),
                "last_push": now if pushed_now else 0.0,
                "pushed": pushed_now,
            }
            changed = True
            log(f"[新]{'推送' if pushed_now else '记录'}: {a['headline']}")
            continue

        # 已有预警：比对"内容指纹"（已归一化，继续发布不算变化）
        sig_now = content_sig(a)
        if list(sig_now) != rec.get("sig"):
            last = rec.get("last_push", 0.0) or 0.0
            tier = push_tier(a)
            old_rank = LEVEL_RANK.get(rec.get("level"), -1)
            new_rank = LEVEL_RANK.get(a["level"], -1)
            downgraded = (bool(rec.get("pushed")) and old_rank >= 0 and new_rank >= 0
                          and new_rank < old_rank)
            if tier and (now - last) >= PUSH_DEDUP_SECONDS:
                push_alert_graded(a)
                pushed += 1
                rec["last_push"] = now
                rec["pushed"] = True
                log(f"[变动] 推送: {a['headline']}")
            elif downgraded:
                ok = wechat_markdown(
                    f"> **预警降级**：{rec.get('level')} → {a['level']}\n"
                    f"> {alert_title(a)}\n"
                    f"> 发布：{a['issuer'] or a['region']} ｜ {a['effective']}\n"
                    f"> 请以最新预警信号为准。")
                record_push("downgraded", a, msgtype="markdown",
                            extra={"from_level": rec.get("level"), "ok": ok})
                rec["last_push"] = now
                log(f"[降级] 通知: {rec.get('level')} → {a['level']} {a['headline']}")
            elif tier:
                log(f"[变动-去重窗口内] 跳过: {a['headline']}")
            else:
                log(f"[变动-非推送目标] 跳过: {a['headline']}")
            rec.update({"headline": a["headline"], "is_sea": a["is_sea"],
                        "description": a["description"], "effective": a["effective"],
                        "issuer": a["issuer"], "type": a["type"], "level": a["level"],
                        "sig": list(sig_now)})
            changed = True
        else:
            # 未变动 -> 红/橙 持续提醒（每120分钟，且 ≥去重窗口）
            if push_tier(a):
                last = rec.get("last_push", 0.0) or 0.0
                if now - last >= REPUSH_INTERVAL:
                    push_alert_graded(a, repeat=True)
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
            ok = wechat_markdown(f"> **预警解除**：{old.get('headline', '')}")
            record_push("lifted", msgtype="markdown",
                        extra={"level": old.get("level"), "type": old.get("type"),
                               "region": old.get("region"), "headline": old.get("headline", ""),
                               "ok": ok})
            lifted += 1
            log(f"[解除] 通知: {old.get('headline', '')}")

    for rid in removed_ids:
        state.pop(rid, None)
        changed = True

    # 台风趋向/逼近实时预警（预报路径进入海南框即实时推；之后仅强度升级/时间提前再提醒）
    # 注意：这两步各自独立 try，任一失败都不影响"状态落盘"，否则状态丢失会导致下轮重复推送
    try:
        if check_imminent(state):
            changed = True
    except Exception as e:
        log("check_imminent 异常:", repr(e))
        state["_last_crash"] = {"at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
                                "where": "check_imminent", "err": repr(e)}
        changed = True
    # 热带扰动物理监测实时补充（非官方，每天≤1次）
    try:
        if check_disturbance(state):
            changed = True
    except Exception as e:
        log("check_disturbance 异常:", repr(e))
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
            ok = wechat_markdown("> **监控异常提醒**\n"
                                 "> 气象预警数据连续获取失败，请检查接口或网络连通性。\n"
                                 "> 错误：" + "；".join(errors) + f"\n> 已连续失败 {fs} 轮")
            record_push("error", msgtype="markdown",
                        extra={"fail_streak": fs, "errors": errors, "ok": ok})
            state["last_error_alert"] = now
            changed = True
    if (not real_failure) and state.get("had_errors"):
        ok = wechat_markdown("> **监控恢复正常**\n> 气象预警数据已可正常获取。")
        record_push("recovered", msgtype="markdown", extra={"ok": ok})
        changed = True
    state["had_errors"] = bool(real_failure)

    # 无论是否 changed 都尝试落盘：避免异常路径下状态丢失导致下轮重复推送
    try:
        if changed:
            save_state(state)
            commit_state()
    except Exception as e:
        log("state 保存失败:", repr(e))
    log(f"本轮：活跃预警 {len(active)} 条，新推送/提醒 {pushed} 条，"
        f"解除 {lifted} 条，错误 {len(errors)}")
    for e in errors:
        log("ERR:", e)


def pick_primary_typhoon():
    """返回对海南影响最直接的活跃台风 det（dict，含 cn 字段），无则返回 None。"""
    try:
        lst = fetch_typhoon_list()
    except Exception:
        return None
    active = [x for x in lst if x.get("status") and x["status"] != "stop"]
    best, best_score = None, -1
    for t in active:
        try:
            det = fetch_typhoon_track(t["id"])
        except Exception:
            continue
        if not det:
            continue
        cur = det["cur"]
        box_fc = [f for f in det["forecast"] if in_box(f["lat"], f["lon"])]
        score = 2 if in_box(cur["lat"], cur["lon"]) else (1 if box_fc else 0)
        if score > best_score:
            best_score, best = score, det
            best["cn"] = t["cn"]
    return best


def mode_daily():
    state = load_state()
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    if state.get("last_daily_report_date") == today:
        log("今日每日报告已推送，跳过重复推送")
        return

    try:
        active, errors = collect()
    except Exception as e:
        log("collect 异常:", repr(e))
        active, errors = [], [f"collect 异常: {e}"]
    land = [a for a in active if not a["is_sea"]]
    sea = [a for a in active if a["is_sea"]]
    typhoon_alerts = [a for a in active if a["type"] == "台风"]

    def lv(a):
        return a["level"] or "未知"

    lines = ["> 【澄迈自然灾害预警每日报告】",
             f"> 生成时间：{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M')}",
             f"> 今日生效预警：共 {len(active)} 条（台风 {len(typhoon_alerts)} 条）"]

    # 一、陆地预警（按等级；红/橙已实时推送，蓝/黄仅日报）
    lines.append("> —— 一、陆地预警（按等级） ——")
    if not land:
        lines.append("> 当前无生效陆地预警。")
    else:
        for lvl in ["红色", "橙色", "黄色", "蓝色"]:
            for a in land:
                if a["level"] != lvl:
                    continue
                tag = "已实时推送" if lvl in ("红色", "橙色") else "未推送·仅日报"
                lines.append(f"> ・[{lv(a)}]{a['type']} {a['headline']}（{tag}）")

    # 二、海上预警专区（仅展示·不实时推送）
    lines.append("> —— 二、海上预警专区（仅展示·不实时推送） ——")
    if not sea:
        lines.append("> 当前无生效海上预警。")
    else:
        for a in sea:
            lines.append(f"> ・[{lv(a)}]{a['type']} {a['headline']}")

    # 台风趋势提示（仅台风；基于 nmc 预报路径，仅作每日提示，不实时推送）
    outlook = typhoon_outlook()
    if outlook:
        lines.append("> —— 台风趋势提示 ——")
        lines.extend(outlook)
    # 热带扰动监测（非官方；基于 Open-Meteo 近海风速+气压代理，覆盖未编号扰动）
    dist = disturbance_outlook()
    if dist:
        lines.extend(dist)

    # 台风路径可点击链接（有活跃台风时）
    if typhoon_alerts:
        lines.append(f"> [🌀 实时台风路径·点击查看]({TYPHOON_TRACK_URL})")

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
    record_push("daily", msgtype="markdown",
                extra={"active": len(active), "land": len(land), "sea": len(sea),
                       "typhoon": len(typhoon_alerts)})

    # 附台风路径图（取对海南影响最直接的活跃台风；方案 A 用 image 消息呈现）
    det_primary = pick_primary_typhoon()
    if det_primary:
        img = render_track_image(det_primary["cur"], det_primary["forecast"],
                                 det_primary.get("track"),
                                 title=f"台风“{det_primary.get('cn', '')}”路径预报")
        if img:
            wechat_image(img)
            record_push("daily_image", msgtype="image",
                        extra={"cn": det_primary.get("cn", "")})

    state["last_daily_report_date"] = today
    save_state(state)
    commit_state()
    log("每日报告已推送")


if __name__ == "__main__":
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "poll"
    if mode == "daily":
        mode_daily()
    elif mode == "export":
        # 导出推送历史：python3 monitor.py --mode export [--format csv|md]
        fmt = (sys.argv[sys.argv.index("--format") + 1]
               if "--format" in sys.argv else "csv")
        out = export_history(fmt)
        print(("已导出：" + out) if out else "暂无推送历史可导出")
    else:
        mode_poll()
