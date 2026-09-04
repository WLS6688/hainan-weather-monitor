# -*- coding: utf-8 -*-
"""
海南自然灾害预警监控脚本（独立版，用于 GitHub Actions / 云部署）
不依赖 WorkBuddy，可单独运行。

功能：
- 每日 09:00 首检：推送天气日报（澄迈天气 + 预警影响提示）
- 每小时轮询：仅当预警影响澄迈或海口时推送升级/降级/进展更新，否则静默
- 失败自检：关键数据源全部失败时推送【监控异常】提醒

监控范围：海口、三亚、澄迈 三城（预警影响判断）；天气日报仅报澄迈。
推送触发：预警影响澄迈或海口时推送；仅三亚受影响时，只在日报中提示。

环境变量：
- WECOM_WEBHOOK: 企业微信机器人 webhook（必填）
- MODE: daily（首检+日报）或 poll（轮询更新），默认 auto（按当前时间判断）

状态文件：本目录下 state.json（记录上次最高预警等级、今日是否已发日报、上次失败次数）
"""
import os
import sys
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# ---------- 配置 ----------
WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")
MODE = os.environ.get("MODE", "auto")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# 监控的三城：城市名 -> 城市代码
CITIES = {
    "海口": "101310101",
    "三亚": "101310201",
    "澄迈": "101310204",
}
REPORT_CITY = "澄迈"       # 天气日报只报这个城市
PUSH_CITIES = ["澄迈", "海口"]  # 预警影响这些城市时触发推送
PROVINCE = "海南省"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
REFERER = "https://www.weather.com.cn/"

# 预警等级权重
LEVEL_W = {"无": 0, "蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4}

CN_TZ = timezone(timedelta(hours=8))


# ---------- 工具函数 ----------
def now_str():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


def today_str():
    return datetime.now(CN_TZ).strftime("%Y年%m月%d日")


def http_get(url, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="ignore")


def post_wecom(msgtype, payload):
    """推送企业微信，返回是否成功"""
    if not WEBHOOK:
        print("[skip] no webhook")
        return False
    body = {"msgtype": msgtype}
    body.update(payload)
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
            ok = resp.get("errcode") == 0
            if not ok:
                print(f"[wecom err] {resp}")
            return ok
    except Exception as e:
        print(f"[wecom fail] {e}")
        return False


def send_text(content, at_all=False):
    p = {"text": {"content": content}}
    if at_all:
        p["text"]["mentioned_list"] = ["@all"]
    return post_wecom("text", p)


def send_markdown(content):
    return post_wecom("markdown", {"markdown": {"content": content}})


# ---------- 数据抓取 ----------
def _extract_weatherinfo(raw):
    m = re.search(r'weatherinfo":(\{.*?\})', raw)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {}


def _extract_alarms(raw, code):
    """从 dingzhi 接口返回中提取 alarmDZ 预警数组。alarmDZ 的 JSON 末尾无分号，需花括号配平。"""
    marker = 'alarmDZ' + code + ' ='
    idx = raw.find(marker)
    if idx == -1:
        return []
    brace_start = raw.find('{', idx)
    if brace_start == -1:
        return []
    depth = 0
    in_str = False
    escape = False
    for j in range(brace_start, len(raw)):
        c = raw[j]
        if in_str:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[brace_start:j+1]).get("w", [])
                    except Exception:
                        return []
    return []


def fetch_city_weather(city_name):
    """抓取单个城市的 (weatherinfo, alarms)。失败抛异常"""
    code = CITIES[city_name]
    raw = http_get(f"http://d1.weather.com.cn/dingzhi/{code}.html", headers={"Referer": REFERER})
    return _extract_weatherinfo(raw), _extract_alarms(raw, code)


def fetch_all_alarms():
    """抓取三城（海口/三亚/澄迈）的预警并合并去重（按 w16 唯一标识去重）。
    返回合并后的预警列表。"""
    merged = {}
    for city in CITIES:
        try:
            _, alarms = fetch_city_weather(city)
        except Exception as e:
            print(f"[warn] 抓取{city}预警失败: {e}")
            continue
        for a in alarms:
            key = a.get("w16", a.get("w11", "")) or (a.get("w5","") + a.get("w8",""))
            if key and key not in merged:
                merged[key] = a
    return list(merged.values())


def fetch_chengmai_weather():
    """返回 (weatherinfo, alarms)。weatherinfo 用澄迈，alarms 用三城合并。失败抛异常"""
    wi, _ = fetch_city_weather(REPORT_CITY)
    alarms = fetch_all_alarms()
    return wi, alarms


def fetch_chengmai_real():
    """实时观测：温度/湿度/能见度/AQI 等"""
    raw = http_get(f"http://d1.weather.com.cn/sk_2d/{CITIES[REPORT_CITY]}.html", headers={"Referer": REFERER})
    m = re.search(r'dataSK=(\{.*?\});?', raw)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}
    return {}


def fetch_national_alarm():
    """全国预警兜底，筛海南相关"""
    raw = http_get("https://product.weather.com.cn/alarm/grepalarm_cn.php", headers={"Referer": REFERER})
    # JSONP: var alarminfo={...}
    m = re.search(r'alarminfo=(\{.*\})', raw)
    if not m:
        return []
    data = json.loads(m.group(1))
    rows = data.get("data", [])
    hit = []
    for r in rows:
        region = r[0] if len(r) > 0 else ""
        title = r[6] if len(r) > 6 else ""
        # 只保留：省本级（海南省气象台等，不含具体市县名），或三城之一
        is_city = any(c in region for c in CITIES)
        is_province = ("海南" in region and not any(k in region for k in ["市", "县", "区"]))
        if is_city or is_province:
            hit.append({"region": region, "title": title, "link": r[1] if len(r) > 1 else ""})
    return hit


# ---------- 预警解析 ----------
def max_level(alarms):
    """从 alarmDZ 的 w 数组解析最高预警等级"""
    lv = "无"
    for a in alarms:
        color = a.get("w7", "")
        if color in LEVEL_W and LEVEL_W[color] > LEVEL_W[lv]:
            lv = color
    return lv


def alarm_has_typhoon(alarms):
    for a in alarms:
        txt = (a.get("w5", "") + a.get("w13", "") + a.get("w9", ""))
        if "台风" in txt:
            return True
    return False


def is_sea_alarm(alarm):
    """判断是否为海上预警（只影响海面，不影响陆地城市）。
    依据：类型/标题/详情中出现"海上"字样。
    """
    txt = (alarm.get("w5", "") + alarm.get("w13", "") + alarm.get("w9", ""))
    return "海上" in txt


def alarm_impact_cities(alarm):
    """判断单条预警影响哪些监控城市。
    返回 set，包含受影响的城市名（海口/三亚/澄迈）。
    规则：
    - 预警文字明确提到某城市名 → 影响该城市
    - 预警为全省/海南全岛/本岛级别（含"全省""全岛""本岛""海南省"等，且未点名其他市县）→ 影响三城
    - 预警明确提到其他市县名（儋州/东方/文昌等）且未提三城 → 不影响三城（空集）
    """
    txt = (alarm.get("w9", "") + alarm.get("w13", "") + alarm.get("w5", ""))
    impacted = set()
    for city in CITIES:
        if city in txt:
            impacted.add(city)
    # 判断是否全省/全岛级别
    province_wide = any(k in txt for k in ["全省", "全岛", "本岛", "海南省", "海南岛"])
    # 若已点名具体城市，以点名城市为准；否则若全省级则影响三城
    if impacted:
        return impacted
    if province_wide:
        return set(CITIES.keys())
    return set()


def alarms_affecting(alarms, cities):
    """返回影响指定城市集合的陆地预警列表（不含海上预警）"""
    return [a for a in alarms if not is_sea_alarm(a) and alarm_impact_cities(a) & set(cities)]


def max_level_affecting(alarms, cities):
    """影响指定城市的预警中的最高等级"""
    lv = "无"
    for a in alarms_affecting(alarms, cities):
        c = a.get("w7", "")
        if c in LEVEL_W and LEVEL_W[c] > LEVEL_W[lv]:
            lv = c
    return lv


# ---------- 状态读写 ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


# ---------- 消息构造 ----------
TYPHOON_LINKS = (
    "🌀 实时台风路径可查看：\n"
    "· 中央气象台：https://typhoon.nmc.cn/\n"
    "· 浙江水利厅：https://typhoon.slt.zj.gov.cn/#/"
)


def impact_label(alarm):
    """生成预警影响城市标签，如 '影响：澄迈、海口' 或 '影响：三亚' 或 '影响：全省（海口/三亚/澄迈）'"""
    cities = alarm_impact_cities(alarm)
    if not cities:
        return "影响：待确认"
    names = sorted(cities, key=lambda c: ["海口", "三亚", "澄迈"].index(c))
    return "影响：" + "、".join(names)


def build_daily_report(wi, real, alarms):
    def clean_temp(v):
        # 温度字段可能带 ℃ 符号（weatherinfo 的 temp 带 ℃，sk_2d 的 temp 是纯数字）
        v = str(v)
        return v.replace("℃", "").replace("°C", "").strip()

    temp = clean_temp(wi.get("temp", real.get("temp", "—")))
    weather = wi.get("weather", real.get("weather", "—"))
    wd = real.get("WD", wi.get("wd", "—"))
    ws = real.get("WS", wi.get("ws", "—"))
    sd = real.get("SD", "—")
    njd = real.get("njd", "—")
    aqi = real.get("aqi", "—")
    try:
        aqi_txt = aqi if aqi == "—" else f"{aqi}（{'优' if int(aqi) <= 50 else '良' if int(aqi) <= 100 else '轻度污染'}）"
    except Exception:
        aqi_txt = str(aqi)

    lines = [
        "━━━━━━━━━━━━",
        f"【天气日报】📅 {today_str()} {REPORT_CITY}",
        "━━━━━━━━━━━━",
        f"🌡 当前温度：{temp}°C",
        f"🌤 天气状况：{weather}",
        f"💨 风向风力：{wd} {ws}",
        f"💧 相对湿度：{sd}",
        f"👁 能见度：{njd}",
        f"🌫 AQI 空气质量：{aqi_txt}",
        "",
    ]

    # 影响澄迈/海口的陆地预警（需要重点提示）
    push_alarms = alarms_affecting(alarms, PUSH_CITIES)
    # 仅影响三亚、不影响澄迈/海口的陆地预警
    sanya_only = [
        a for a in alarms
        if not is_sea_alarm(a)
        and "三亚" in alarm_impact_cities(a)
        and not (alarm_impact_cities(a) & set(PUSH_CITIES))
    ]
    # 海上预警（不影响陆地城市）
    sea_alarms = [a for a in alarms if is_sea_alarm(a)]

    if push_alarms:
        lines.append("⚠️ 当前预警：")
        for a in push_alarms:
            color = a.get("w7", "")
            icon = {"蓝色": "🔵", "黄色": "🟡", "橙色": "🟠", "红色": "🔴"}.get(color, "⚪")
            lines.append(f"· {icon} {a.get('w5','')} {color}预警（{a.get('w8','')} 发布）")
            lines.append(f"  → {impact_label(a)}")
        lines.append("")
        if alarm_has_typhoon(push_alarms):
            lines.append(TYPHOON_LINKS)
            lines.append("")
        lines.append("请注意防范，关注最新预警信息。")
    elif sanya_only:
        # 仅三亚有预警，澄迈/海口不受影响 → 一行提示
        for a in sanya_only:
            color = a.get("w7", "")
            lines.append(f"⚠️ 三亚当前有 {a.get('w5','')} {color}预警，澄迈/海口不受影响。")
    elif sea_alarms:
        for a in sea_alarms:
            color = a.get("w7", "")
            lines.append(f"🌊 当前有海上 {a.get('w5','')} {color}预警，不影响澄迈/海口/三亚陆地。")
    else:
        lines.append("✅ 目前无自然灾害预警，请放心出行。")
    return "\n".join(lines)


def build_alarm_notice(alarms, lv):
    icon = {"蓝色": "⚠️", "黄色": "🚨", "橙色": "🚨", "红色": "🚨"}.get(lv, "⚠️")
    tag = "【紧急预警】" if lv in ("黄色", "橙色", "红色") else "【预警通知】"
    a = alarms[0] if alarms else {}
    lines = [
        f"{tag}{icon} {PROVINCE}发布{lv}预警",
        f"时间：{a.get('w8', now_str())}",
        f"类型：{a.get('w5','')}",
        f"标题：{a.get('w13','')}",
        f"{impact_label(a)}",
        "",
        f"详情：{a.get('w9','')}",
    ]
    if alarm_has_typhoon(alarms):
        lines.append("")
        lines.append(TYPHOON_LINKS)
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    state = load_state()
    prev_level = state.get("max_level", "无")
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    last_report_date = state.get("last_report_date", "")
    last_fail = state.get("last_fail", 0)  # 连续失败次数
    last_error_date = state.get("last_error_date", "")

    # 确定本次模式
    mode = MODE
    if mode == "auto":
        hh = datetime.now(CN_TZ).hour
        mm = datetime.now(CN_TZ).minute
        mode = "daily" if (hh == 9 and mm < 30) else "poll"

    # 抓取数据
    wi, alarms = {}, []
    fetch_failed = False
    try:
        wi, alarms = fetch_chengmai_weather()
    except Exception as e:
        print(f"[warn] 三城天气接口失败: {e}")
        fetch_failed = True

    # 仅当三城接口失败时，才尝试全国兜底
    if fetch_failed:
        try:
            national = fetch_national_alarm()
            if national:
                # 全国兜底只有标题和地区，从标题里提取颜色，并构造 alarms
                for n in national:
                    color = "蓝色"
                    for c in ("红色", "橙色", "黄色", "蓝色"):
                        if c in n["title"]:
                            color = c
                            break
                    alarms.append({"w5": n["title"], "w7": color, "w8": now_str(),
                                   "w13": n["title"], "w9": n["title"], "w11": n["link"],
                                   "w16": n["link"]})
                fetch_failed = False
        except Exception as e:
            print(f"[warn] 全国预警兜底也失败: {e}")

    # 完全失败
    all_failed = (fetch_failed and not alarms)
    if all_failed:
        last_fail += 1
        state["last_fail"] = last_fail
        # 连续失败 >= 2 或 daily 模式且失败，推异常提醒（每天最多一次）
        if last_fail >= 2 or mode == "daily":
            if last_error_date != today:
                send_text(f"【监控异常】⚠️ {now_str()} 海南自然灾害预警监控数据获取失败，可能是数据源网络问题，请人工查看中国气象局/海南气象局官网确认当前预警情况。", at_all=False)
                state["last_error_date"] = today
        save_state(state)
        print("[result] all_failed, checked")
        return

    # 成功，重置失败计数
    last_fail = 0
    state["last_fail"] = 0

    # 只关心影响澄迈/海口的预警
    push_alarms = alarms_affecting(alarms, PUSH_CITIES)
    lv = max_level_affecting(alarms, PUSH_CITIES)

    if mode == "daily":
        # 首检：发日报（含预警提示，无论是否影响澄迈/海口）
        real = {}
        try:
            real = fetch_chengmai_real()
        except Exception:
            real = {}
        report = build_daily_report(wi, real, alarms)
        send_markdown(report)
        state["last_report_date"] = today
        state["max_level"] = lv
        save_state(state)
        print(f"[result] daily report sent, max_level={lv}")
        return

    # poll 模式
    # 无影响澄迈/海口的预警：静默（但若之前有预警，说明解除）
    if lv == "无":
        if prev_level != "无":
            send_text(f"【预警解除/降级】✅ 影响澄迈/海口的预警已从{prev_level}调整为无/已解除", at_all=False)
        state["max_level"] = "无"
        save_state(state)
        print("[result] no alarm affecting push cities, silent")
        return

    # 有影响澄迈/海口的预警
    if lv != prev_level:
        if LEVEL_W[lv] > LEVEL_W[prev_level]:
            send_text(f"【预警升级】⚠️ 影响澄迈/海口的预警等级从{prev_level}提升至{lv}！", at_all=True)
        elif prev_level != "无":
            send_text(f"【预警解除/降级】影响澄迈/海口的预警等级已从{prev_level}调整为{lv}", at_all=True)
        # 等级变化时也发一条完整预警通知
        send_text(build_alarm_notice(push_alarms, lv), at_all=True)
    else:
        # 等级不变，发进展更新
        if lv in ("黄色", "橙色", "红色"):
            send_text(f"【紧急更新】🆘 {now_str()} {lv}预警最新进展：\n{get_alarm_brief(push_alarms)}", at_all=True)
        else:
            send_text(f"【预警更新】🔄 {now_str()} 蓝色预警最新进展：\n{get_alarm_brief(push_alarms)}", at_all=True)

    state["max_level"] = lv
    save_state(state)
    print(f"[result] poll done, max_level={lv}")


def get_alarm_brief(alarms):
    a = alarms[0] if alarms else {}
    return a.get("w9", a.get("w13", "暂无详细信息"))


if __name__ == "__main__":
    main()
