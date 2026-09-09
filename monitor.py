#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海南澄迈台风预警监控
==================================================
数据源 : 中国气象局官方接口 weather.cma.cn/api/map/alarm
        （按 adcode 精确查询，需带 UA + Referer 否则 403）
推送  : 企业微信群机器人 webhook（markdown 消息，不@任何人）

监控范围(仅台风):
  * 只关注【台风】类预警；雷电/暴雨/高温等一律不推送、不进报告
  * 仅实时推送【澄迈陆地】台风预警：
      - 澄迈县(adcode 469023)发布的台风预警
      - 省级(adcode 46)发布、且正文提及"澄迈"的台风预警
  * 海上台风预警 -> 不实时推送，仅进入每日报告

推送节奏:
  * 每 10 分钟轮询：检测是否有【新预警 / 预警变动】
  * 蓝/黄/橙/红 四级台风预警 -> 新发/变动 立即推送
  * 信号与内容未变、且级别为黄/橙/红 -> 每 120 分钟续推一次（持续提醒）
  * 蓝色预警只在新发时推一次，不做周期提醒；白色不推送
  * 轮询时间(10min) ≠ 推送时间(120min)，解耦避免刷屏

每日报告:
  * 每天 09:00(北京时间) 推送一次台风预警汇总（陆地+海上）
  * 附加【澄迈今日天气预报】（Open-Meteo 免 key）
  * 按日期去重：同一天无论触发几次，只推一次

台风路径:
  * 台风预警推送消息附上实时台风路径网址（typhoon.nmc.cn）

抓取异常自告警:
  * 连续抓取失败时，每隔 6 小时推送一次"监控异常提醒"，恢复后推送"恢复正常"
  * 避免工作流/接口静默失效而无人察觉

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
PROVINCE = "46"  # 海南省（兜底：抓全省台风预警，推送时再筛"澄迈"）
# 仅关注台风类预警
TARGET_TYPES = ["台风"]
SEA_KEYWORDS = ["海面", "海上", "琼州海峡", "附近海域", "南海", "北部湾",
                "东海", "台湾海峡", "南海北部", "西沙", "南沙", "中沙"]
# 持续提醒：仅 黄/橙/红 三级做每 120 分钟续推；蓝/白预警只在新发时推一次，不做周期提醒
REPUSH_LEVELS = ["黄色", "橙色", "红色"]
REPUSH_INTERVAL = 7200  # 秒（120 分钟）
# 台风实时路径（推送时附上）
TYPHOON_TRACK_URL = "https://typhoon.nmc.cn/"
# 澄迈坐标（用于天气预报 API：Open-Meteo 免 key）
CHENGMAI_LAT, CHENGMAI_LON = 19.74, 110.00
STATE_FILE = "state.json"
WEBHOOK = os.environ.get("WECHAT_WEBHOOK")
BEIJING = timezone(timedelta(hours=8))
# 抓取异常自告警冷却时间（秒）
ERROR_ALERT_INTERVAL = 6 * 3600


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
    """是否实时推送：澄迈陆地台风预警。海上不推送；非澄迈不推送。"""
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
    return (f"> **[{level}]{t}** {a['headline']}\n"
            f"> 发布：{a['issuer'] or a['region']} ｜ 时间：{eff}{note}\n"
            f"> 实时台风路径：{TYPHOON_TRACK_URL}")


def mode_poll():
    now = time.time()
    active, errors = collect()
    state = load_state()
    active_ids = {a["id"] for a in active}
    removed_ids = set(state.keys()) - active_ids - {"last_daily_report_date",
                                                    "last_error_alert", "had_errors",
                                                    "fail_streak"}

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
    land = [a for a in active if not a["is_sea"]]
    sea = [a for a in active if a["is_sea"]]

    lines = ["> 【澄迈台风预警每日报告】",
             f"> 生成时间：{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M')}",
             f"> 今日生效台风预警：共 {len(active)} 条（陆地 {len(land)} / 海上 {len(sea)}）"]
    if not active:
        lines.append("> 当前无生效台风预警。")
    for a in land:
        tag = "（影响澄迈）" if should_push(a) else ""
        lines.append(f"> ・[{a['level'] or '未知'}]{a['type']} {a['headline']}{tag}")
    if sea:
        lines.append("> 海上台风预警（不实时推送）：")
        for a in sea:
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
