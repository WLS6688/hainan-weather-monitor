#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_typhoon_card.py — 用企业微信群机器人发送「会议式」台风路径模板卡片。

复用中央气象台台风网公开接口（typhoon.nmc.cn），仅依赖标准库 urllib，
无需 pip 安装任何第三方包（与现有 monitor.py 的约定保持一致）。

三种用法：
  python push_typhoon_card.py                # 默认：text_notice 卡片（一个主按钮进路径图）
  python push_typhoon_card.py --with-image   # news_notice 卡片（默认顶部放官方概率预报图）
  python push_typhoon_card.py --with-image --screenshot  # news_notice 用 playwright 截交互地图
  python push_typhoon_card.py --dry          # 只打印待发送的 JSON，不真正推送

环境变量：
  WECHAT_WEBHOOK    群机器人 webhook 地址（必填；未设置时自动降级为 --dry）
  TYPHOON_PATH_URL  卡片按钮/跳转项指向的路径图地址（默认官方 nmc 网页）
  TYPHOON_SHOT_URL  自定义要抓图的产品页/网页（默认 probability-img1.html 概率预报图）
  GITHUB_TOKEN      --screenshot 时用于把截图上传到仓库图床（jsDelivr），可选
  CDN_REPO          --screenshot 时图床仓库，默认 WLS6688/hainan-weather-monitor

说明：
  - text_notice 卡片 = 标题 + 台风实况 + 一个大按钮（card_action），点击直达路径图。
    最稳的形式，不需要任何图片托管，与腾讯会议卡片同一套机制。
  - news_notice 卡片（默认 --with-image）= 顶部一张官方概率预报图 + 多个可点跳转项。
    图片直接取自中央气象台公开图床 image.nmc.cn（产品页 <img id=imgpath> 写死的
    最新官方图），企业微信服务器可自行拉取，因此【无需 playwright、无需自建图床、
    零算力】。不点开也能一眼看到路径概率预报图。
  - 只有加 --screenshot 时才走 playwright 截官方交互地图 + 上传图床（需装
    playwright+chromium 与 GITHUB_TOKEN），用于想用「交互地图截图」而非「概率预报产品图」时。
  - 无活跃台风时，默认静默跳过（加 --quiet=0 可改发一条「当前无活跃台风」提示卡）。
  - 【仅推送影响海南的台风】：脚本会自动判定——活跃台风的任一路径点（实况或预报）
    落入海南本岛+邻近海域矩形框，或距海南中心不超过 HAINAN_MAX_KM（默认 550km，可配置）
    时才推送；不影响海南的台风（如远洋台风）会被静默跳过，避免群里刷无关预警。
"""

import os
import sys
import json
import time
import base64
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from math import radians, sin, cos, asin, sqrt

BEIJING = timezone(timedelta(hours=8))

# ---------- 海南判定（仅推送影响海南的台风） ----------
# 海南本岛中心（用于距离判定）
HAINAN_CENTER = (19.2, 109.7)
# 距海南中心多近算“影响海南”（km），可用环境变量 HAINAN_MAX_KM 调整（默认 550）
HAINAN_MAX_KM = float(os.environ.get("HAINAN_MAX_KM", "550"))
# 本岛 + 邻近海域矩形框（纬度、经度范围），落入即视为直接影响海南
HAINAN_BOX = dict(lat_min=17.0, lat_max=21.5, lon_min=107.5, lon_max=112.0)

# ---------- 配置 ----------
TYPHOON_LIST_URL = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default"
TYPHOON_VIEW_URL = "http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_{id}"
PATH_URL = os.environ.get("TYPHOON_PATH_URL") or "https://typhoon.nmc.cn/web.html"
WEBHOOK = os.environ.get("WECHAT_WEBHOOK")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
CDN_REPO = os.environ.get("CDN_REPO", "WLS6688/hainan-weather-monitor")
CDN_PATH = "assets/typhoon_latest.png"

STRENGTH_CN = {
    "TD": "热带低压", "TS": "热带风暴", "STS": "强热带风暴",
    "TY": "台风", "STY": "强台风", "SuperTY": "超强台风", "": "—",
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; hainan-typhoon-card/1.0)",
      "Referer": "http://typhoon.nmc.cn/"}


def log(*a):
    print(f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}]", *a, flush=True)


# ---------- 网络 ----------
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
    """拉取 nmc 台风接口（JSONP 需去壳），失败返回 None。"""
    import re
    try:
        raw = http_get(url, UA, timeout=15)
    except Exception as e:
        log("接口请求失败：", e)
        return None
    m = re.search(r"\((\{.*\})\)", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------- 台风数据 ----------
def fetch_active_typhoon():
    """返回当前活跃台风 [{id, en, cn, num, status}]，取第一个 start。无则 None。"""
    d = nmc_get_json(TYPHOON_LIST_URL)
    if not d:
        return None
    for x in d.get("typhoonList", []):
        if len(x) < 8:
            continue
        item = {"id": str(x[0]), "en": x[1], "cn": x[2], "num": x[3], "status": x[7]}
        if item["status"] == "start":
            return item
    return None


def fetch_typhoon_track(tid):
    """返回当前实况概要 + 全部路径点（含预报），形如
    {lat, lon, strength, pressure, wind, move_dir, move_speed, trend, points}
    points 为 [(lat, lon), ...]（按时间排序），用于“是否影响海南”判定。"""
    d = nmc_get_json(TYPHOON_VIEW_URL.format(id=tid))
    if not d:
        return None
    ty = d.get("typhoon")
    if not isinstance(ty, list) or len(ty) < 9:
        return None
    pts = [p for p in ty[8] if isinstance(p, list) and len(p) >= 10]
    if not pts:
        return None
    pts.sort(key=lambda p: str(p[1]))
    cur = pts[-1]
    try:
        cur_d = {
            "lat": float(cur[5]), "lon": float(cur[4]),
            "strength": cur[3] or "", "pressure": cur[6], "wind": cur[7],
            "move_dir": cur[8] or "", "move_speed": cur[9],
        }
    except Exception:
        return None
    prev = pts[-2] if len(pts) >= 2 else None
    trend = "强度平稳"
    if prev:
        rank = {"TD": 1, "TS": 2, "STS": 3, "TY": 4, "STY": 5, "SuperTY": 6}
        rc = rank.get(cur_d["strength"], 0)
        rp = rank.get(prev[3] or "", 0)
        if rc > rp:
            trend = "强度加强中"
        elif rc < rp:
            trend = "减弱中"
    cur_d["trend"] = trend
    # 全部路径点（实况 + 预报），供海南判定
    all_pts = []
    for p in pts:
        try:
            all_pts.append((float(p[5]), float(p[4])))
        except Exception:
            pass
    cur_d["points"] = all_pts
    return cur_d


def haversine(lat1, lon1, lat2, lon2):
    """两经纬度间大圆距离（km）。"""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def affects_hainan(track):
    """判断台风是否影响海南：任一路径点（实况/预报）落在海南邻近区即视为影响。
    判定条件（满足其一）：
      (1) 落入本岛 + 邻近海域矩形框 HAINAN_BOX；
      (2) 距海南中心 HAINAN_CENTER 不超过 HAINAN_MAX_KM（默认 550km，可配置）。
    """
    if not track or not track.get("points"):
        return False, None
    best = None
    for lat, lon in track["points"]:
        if HAINAN_BOX["lat_min"] <= lat <= HAINAN_BOX["lat_max"] and \
           HAINAN_BOX["lon_min"] <= lon <= HAINAN_BOX["lon_max"]:
            return True, (lat, lon)
        dist = haversine(HAINAN_CENTER[0], HAINAN_CENTER[1], lat, lon)
        if best is None or dist < best[0]:
            best = (dist, (lat, lon))
    if best and best[0] <= HAINAN_MAX_KM:
        return True, best[1]
    return False, (best[0] if best else None, None)


def dir_cn(code):
    m = {"N": "北", "S": "南", "E": "东", "W": "西"}
    if not code or str(code) in ("0", "00", "none", "null"):
        return "未知"
    code = str(code)
    if code in m:
        return m[code]
    if len(code) == 2:
        return m.get(code[1], "") + m.get(code[0], "")
    if len(code) >= 3:
        return m.get(code[0], "") + m.get(code[2], "") + m.get(code[1], "")
    return code


def move_text(code, speed):
    if code in (None, "", "0", "00") or speed in (None, 0, "0"):
        return "暂不确定"
    return f"{dir_cn(code)} {speed} km/h"


# ---------- 卡片构造 ----------
def build_text_notice(ty, track):
    strength = STRENGTH_CN.get(track["strength"], track["strength"] or "—") if track else "—"
    title_desc = f"当前活跃：{ty['cn']}({ty['en']}) 第{ty['num']}号"
    horiz = [
        {"keyname": "强度", "value": strength},
        {"keyname": "中心气压", "value": f"{track['pressure']} hPa" if track and track.get("pressure") else "—"},
        {"keyname": "最大风速", "value": f"{track['wind']} m/s" if track and track.get("wind") else "—"},
    ]
    if track:
        horiz.append({"keyname": "中心位置", "value": f"{track['lat']:.1f}°N {track['lon']:.1f}°E"})
        horiz.append({"keyname": "移动", "value": move_text(track['move_dir'], track['move_speed'])})
        horiz.append({"keyname": "趋势", "value": track["trend"]})

    card = {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "text_notice",
            "source": {"desc": "中央气象台 · 实时"},
            "main_title": {
                "title": "台风路径实时跟踪",
                "desc": title_desc,
            },
            "sub_title_text": "点击下方按钮，进入中央气象台台风路径实时图",
            "horizontal_content_list": horiz,
            "card_action": {
                "type": 1,                       # 1 = 打开链接
                "url": PATH_URL,
            },
        },
    }
    return card


def build_news_notice(ty, track, image_url):
    jump = [
        {"type": 1, "title": "中央气象台路径图", "url": PATH_URL},
        {"type": 1, "title": "Windy 实时风场", "url": "https://www.windy.com/?typhoon"},
    ]
    card = {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "news_notice",
            "source": {"desc": "中央气象台 · 实时"},
            "main_title": {
                "title": f"{ty['cn']}({ty['en']}) 第{ty['num']}号 路径",
                "desc": (STRENGTH_CN.get(track["strength"], "—") if track else "查看实时路径"),
            },
            "card_image": {
                "type": 0,                       # 0 = 大图（建议宽高比约 1.91:1）
                "url": image_url,
            },
            "image_text": "中央气象台台风路径概率预报图",
            "jump_list": jump,
            "card_action": {"type": 1, "url": PATH_URL},
        },
    }
    return card


# ---------- 官方概率预报图（直接取公开图床，无需截图/图床） ----------
def fetch_official_image():
    """从中央气象台概率预报产品页直接取最新官方概率预报图 URL。

    产品页（probability-img1.html）会把当前活跃台风的最新概率预报图
    以静态 <img id="imgpath" src="..."> 形式写死在 HTML 里，地址在公开
    图床 image.nmc.cn 上，企业微信服务器可直接拉取——因此 news_notice
    卡片无需 playwright 截图、也无需自建图床，零算力。
    """
    url = os.environ.get("TYPHOON_SHOT_URL",
                         "https://www.nmc.cn/publish/typhoon/probability-img1.html")
    try:
        html = http_get(url, UA, timeout=20)
    except Exception as e:
        log("概率产品页请求失败：", e)
        return None
    import re
    m = re.search(r'id=imgpath[^>]*\bsrc="([^"]+\.JPG[^"]*)"', html)
    if not m:
        m = re.search(r'<img[^>]+id=imgpath[^>]+src="([^"]+)"', html)
    if not m:
        log("未在产品页找到官方图地址")
        return None
    img = m.group(1).split("?")[0]          # 去掉缓存时间戳，基础 URL 稳定
    # 校验可达且确为图片
    try:
        req = urllib.request.Request(img, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            if r.status != 200 or not ct.startswith("image"):
                log("官方图不可达或不是图片：", r.status, ct)
                return None
    except Exception as e:
        log("官方图校验失败：", e)
        return None
    return img


# ---------- 可选：官方截图 + 图床（仅 --screenshot 时使用） ----------
def screenshot_official(out_path, url=None):
    """用 playwright 截官方网页（canvas 动态绘制），需 Actions 环境装 playwright。
    默认截中央气象台台风网实时路径图；可用 TYPHOON_SHOT_URL 换成概率预报图等产品页。
    """
    url = url or os.environ.get("TYPHOON_SHOT_URL", "https://typhoon.nmc.cn/web.html")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("未安装 playwright，跳过官方截图（news_notice 不可用）")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            # 1200x628 ≈ 1.91:1，贴合 news_notice 大图推荐比例；2x 倍率更清晰
            page = browser.new_page(viewport={"width": 1200, "height": 628},
                                    device_scale_factor=2)
            page.goto(url, timeout=30000, wait_until="load")
            try:
                page.wait_for_selector("canvas", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)          # 等底图瓦片 + 路径绘制完成
            page.screenshot(path=out_path, full_page=False)
            browser.close()
        return out_path
    except Exception as e:
        log("截图失败：", e)
        return None


def upload_image_to_cdn(png_path):
    """上传图片到仓库图床，返回 jsDelivr 公网 URL（供企业微信抓取）。"""
    if not GITHUB_TOKEN:
        log("未设置 GITHUB_TOKEN，无法上传图床")
        return None
    api = f"https://api.github.com/repos/{CDN_REPO}/contents/{CDN_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hainan-typhoon-card",
    }
    with open(png_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    # 先查是否已存在（需要 sha 才能更新）
    sha = None
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            sha = json.loads(resp.read().decode()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log("查询图床文件失败：", e)
    body = {
        "message": f"chore: update typhoon card image {datetime.now(BEIJING).strftime('%Y%m%d-%H%M')}",
        "content": content,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(body).encode(),
                                 headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read().decode())
    except Exception as e:
        log("上传图床失败：", e)
        return None
    # 注意：jsDelivr 有缓存（默认数分钟~数小时），路径图更新会有延迟，属正常
    return f"https://cdn.jsdelivr.net/gh/{CDN_REPO}/{CDN_PATH}"


# ---------- 主流程 ----------
def send_card(card, dry):
    if dry or not WEBHOOK:
        if not WEBHOOK:
            log("未设置 WECHAT_WEBHOOK，仅打印 JSON（dry-run）")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return
    try:
        resp = http_post_json(WEBHOOK, card, timeout=10)
        log("推送结果：", resp)
    except Exception as e:
        log("推送失败：", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-image", action="store_true", help="发送 news_notice 卡片（顶部放官方概率预报图）")
    ap.add_argument("--screenshot", action="store_true", help="--with-image 时改用 playwright 截官方交互地图（需装 playwright+chromium）")
    ap.add_argument("--dry", action="store_true", help="只打印 JSON 不推送")
    ap.add_argument("--force", action="store_true", help="测试用：跳过海南判定强制推送（当前台风会标为「测试推送」）")
    ap.add_argument("--quiet", type=int, default=1, help="无台风时是否静默(1)或发提示卡(0)")
    args = ap.parse_args()

    ty = fetch_active_typhoon()
    if not ty:
        log("当前无活跃台风")
        if args.quiet == 0:
            send_card(_no_hainan_card("西太平洋暂无编号台风活动，已自动跳过推送。"), args.dry)
        return

    log(f"活跃台风：{ty['cn']}({ty['en']}) 第{ty['num']}号")
    track = fetch_typhoon_track(ty["id"])
    if not track:
        log("台风路径数据获取失败，跳过")
        return

    # —— 仅推送影响海南的台风（--force 测试时跳过判定） ——
    ok, near = affects_hainan(track)
    if not ok and not args.force:
        dist = near[0] if isinstance(near, tuple) and near and isinstance(near[0], (int, float)) else None
        if dist is not None:
            log(f"台风 {ty['cn']} 距海南中心约 {dist:.0f} km，不影响海南，跳过推送")
        else:
            log(f"台风 {ty['cn']} 路径点缺失，无法判定，跳过推送")
        if args.quiet == 0:
            send_card(_no_hainan_card(
                f"活跃台风「{ty['cn']}({ty['en']}) 第{ty['num']}号」目前不影响海南"
                f"（最近约 {dist:.0f} km），已自动跳过。" if dist is not None
                else f"活跃台风「{ty['cn']}」路径数据不足，已跳过。"), args.dry)
        return

    if args.with_image:
        if args.screenshot:
            # 可选：截官方交互地图（canvas），需 playwright+chromium + GITHUB_TOKEN 图床
            img = screenshot_official(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "typhoon_shot.png"))
            url = upload_image_to_cdn(img) if img else None
            if not url:
                log("截图/图床不可用，降级为 text_notice 卡片")
                card = build_text_notice(ty, track)
            else:
                card = build_news_notice(ty, track, url)
        else:
            # 默认：直接取官方概率预报图（公开 image.nmc.cn，零依赖零算力）
            img_url = fetch_official_image()
            if not img_url:
                log("官方图获取失败，降级为 text_notice 卡片")
                card = build_text_notice(ty, track)
            else:
                card = build_news_notice(ty, track, img_url)
    else:
        card = build_text_notice(ty, track)

    # 给卡片打标记：命中海南→「影响海南」；--force 测试→「测试推送」
    tag = "影响海南" if ok else "测试推送"
    tc = card.get("template_card", {})
    if tc.get("card_type") == "text_notice":
        tc["sub_title_text"] = f"点击下方按钮，进入中央气象台台风路径实时图（{tag}）"
    else:  # news_notice
        mt = tc.get("main_title", {})
        mt["desc"] = (mt.get("desc", "") + f" · {tag}").strip()

    send_card(card, args.dry)


def _no_hainan_card(reason):
    """无影响海南的台风时，--quiet=0 才发送的一张说明卡。"""
    return {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "text_notice",
            "source": {"desc": "中央气象台 · 实时"},
            "main_title": {"title": "台风路径实时跟踪（海南）", "desc": "当前无影响海南的台风"},
            "sub_title_text": reason,
            "card_action": {"type": 1, "url": PATH_URL},
        },
    }


if __name__ == "__main__":
    main()
