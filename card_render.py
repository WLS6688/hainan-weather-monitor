# -*- coding: utf-8 -*-
"""海南预警监控 · 统一卡片渲染模块（iOS 天气风格）

把原本散在 scripts/ 两套样张生成器里的逻辑收敛为可直接被 monitor.py 调用的
生产渲染函数，全部返回 PNG bytes（喂给企业微信 image 消息）。

包含：
  - render_daily_card(...)        日报卡（天气自适应背景 + 预警状态 + 台风行
                                  + 逐小时 + 5日报 + 今日指南，紧凑一屏）
  - render_alert_card(...)        生效预警卡（样式2：路径图内嵌 + 灾种分级防御指引）
  - render_transition_card(...)   升级 / 降级 / 解除 三类变更卡（iOS 风格，统一色系）

字体优先用打包的 NotoSansSC（与已批准样张一致），缺失时回退 SimHei。
所有文字均按卡片宽度折行，卡高由内容行数动态推导，杜绝溢出。
"""
import io
import math
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------- 字体（打包进仓库，优先 NotoSansSC，回退 SimHei） ----------------
_ASSETS = Path(__file__).resolve().parent / "assets"
F_BOLD = _ASSETS / "fonts" / "NotoSansSC-Bold.ttf"
F_REG = _ASSETS / "fonts" / "NotoSansSC-Regular.ttf"
SIMHEI = _ASSETS / "SimHei.ttf"

_FB, _FR = {}, {}


def _fb(size):
    if size not in _FB:
        for p in (F_BOLD, SIMHEI):
            if p.exists():
                try:
                    _FB[size] = ImageFont.truetype(str(p), size)
                    break
                except Exception:
                    pass
    return _FB.get(size, ImageFont.load_default())


def _fr(size):
    if size not in _FR:
        for p in (F_REG, SIMHEI):
            if p.exists():
                try:
                    _FR[size] = ImageFont.truetype(str(p), size)
                    break
                except Exception:
                    pass
    return _FR.get(size, ImageFont.load_default())


W = 960

# ---------------- 主题 ----------------
THEME = {
    "bg_top": (56, 116, 200), "bg_mid": (96, 156, 226), "bg_bot": (168, 205, 237),
    "text": (255, 255, 255), "text_sub": (225, 238, 252),
    "card_fill": (255, 255, 255, 46), "card_line": (255, 255, 255, 60),
    "divider": (255, 255, 255, 40),
    "ok_green": (110, 231, 167), "warn_text": (255, 255, 255),
    "cloud": (255, 255, 255), "sun": (255, 214, 90), "rain": (168, 214, 255),
    "pop": (140, 200, 255), "bar_bg": (255, 255, 255, 70),
    "footer": (210, 228, 248),
}

# 告警/变更卡深蓝背景（白字清晰，比日报更"严肃"）
ALERT_BG = ((46, 82, 138), (78, 116, 170), (120, 156, 200))

LEVEL_COLOR = {"红色": (255, 69, 58), "橙色": (255, 149, 0),
               "黄色": (255, 204, 0), "蓝色": (50, 173, 230)}
LEVEL_ORDER = ["蓝色", "黄色", "橙色", "红色"]

# ---------------- 天气自适应背景（仅日报卡用） ----------------
WEATHER_BG = {
    "clear":   ((40, 112, 205), (92, 154, 224), (178, 212, 240)),
    "partly":  ((74, 128, 198), (126, 168, 220), (192, 216, 240)),
    "cloud":   ((108, 124, 146), (148, 162, 182), (194, 204, 217)),
    "fog":     ((120, 130, 142), (158, 166, 174), (198, 204, 210)),
    "rain":    ((64, 84, 114), (94, 120, 152), (148, 174, 200)),
    "thunder": ((50, 50, 94), (72, 68, 122), (118, 114, 162)),
    "snow":    ((100, 118, 144), (146, 164, 186), (198, 212, 228)),
}
WEATHER_TEXTURE = {"clear": "sun", "partly": "sun", "cloud": "cloud", "fog": "cloud",
                   "rain": "rain", "thunder": "thunder", "snow": "snow"}
WEATHER_DESC = {"clear": "晴", "partly": "少云", "cloud": "多云", "fog": "雾",
                "rain": "小雨", "thunder": "雷阵雨", "snow": "小雪"}


def wmo_to_weather(code):
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 95 <= code <= 99:
        return "thunder"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    return "cloud"


# ---------------- 基础绘制 ----------------
def vgrad(w, h, c1, c2, c3):
    img = Image.new("RGB", (w, h))
    px = img.load()
    half = h // 2
    for y in range(h):
        if y <= half:
            t = y / max(half, 1)
            a, b = c1, c2
        else:
            t = (y - half) / max(h - half, 1)
            a, b = c2, c3
        px_row = (int(a[0] + (b[0] - a[0]) * t),
                  int(a[1] + (b[1] - a[1]) * t),
                  int(a[2] + (b[2] - a[2]) * t))
        for x in range(0, w, 1):
            px[x, y] = px_row
    return img


def card(draw, x, y, w, h, th, radius=34):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                           fill=th["card_fill"], outline=th["card_line"], width=2)


def capsule_gradient(width, height, c1, c2):
    bar = Image.new("RGBA", (width, height))
    d = ImageDraw.Draw(bar)
    for x in range(width):
        t = x / max(width - 1, 1)
        col = (int(c1[0] + (c2[0] - c1[0]) * t),
               int(c1[1] + (c2[1] - c1[1]) * t),
               int(c1[2] + (c2[2] - c1[2]) * t), 255)
        d.line([(x, 0), (x, height)], fill=col)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width, height],
                                           radius=height // 2, fill=255)
    out = Image.new("RGBA", (width, height))
    out.paste(bar, (0, 0), mask)
    return out


def draw_icon(img, kind, cx, cy, s, th):
    d = ImageDraw.Draw(img, "RGBA")
    cloud_col, sun_col, rain_col = th["cloud"], th["sun"], th["rain"]

    def cloud(x, y, sc, col):
        r = int(sc * 0.30)
        d.ellipse([x - sc * 0.42, y - sc * 0.10, x - sc * 0.42 + 2 * r, y - sc * 0.10 + 2 * r], fill=col)
        d.ellipse([x - sc * 0.10, y - sc * 0.32, x - sc * 0.10 + 2 * int(sc * 0.38), y - sc * 0.32 + 2 * int(sc * 0.38)], fill=col)
        d.rounded_rectangle([x - sc * 0.42, y + r - sc * 0.10, x + sc * 0.42, y + r + sc * 0.14],
                            radius=int(sc * 0.22), fill=col)

    if kind == "sun":
        r = s * 0.26
        for ang in range(8):
            a = math.pi / 4 * ang
            x1, y1 = cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45
            x2, y2 = cx + math.cos(a) * r * 1.95, cy + math.sin(a) * r * 1.95
            d.line([(x1, y1), (x2, y2)], fill=sun_col, width=max(int(s * 0.05), 3))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=sun_col)
    elif kind == "cloud":
        cloud(cx, cy, s, cloud_col)
    elif kind == "overcast":
        cloud(cx - s * 0.16, cy - s * 0.16, s * 0.66, tuple(min(255, c + 25) for c in th["cloud"]) if isinstance(th["cloud"], tuple) and len(th["cloud"]) == 3 else th["cloud"])
        cloud(cx + s * 0.12, cy + s * 0.10, s * 0.72, cloud_col)
    elif kind == "rain":
        cloud(cx, cy - s * 0.12, s * 0.8, cloud_col)
        for i in range(3):
            x = cx - s * 0.26 + i * s * 0.26
            d.line([(x, cy + s * 0.22), (x - s * 0.06, cy + s * 0.40)],
                   fill=rain_col, width=max(int(s * 0.055), 3))
    elif kind == "thunder":
        cloud(cx, cy - s * 0.12, s * 0.8, cloud_col)
        pts = [(cx - s * 0.05, cy + s * 0.16), (cx + s * 0.10, cy + s * 0.16),
               (cx - s * 0.02, cy + s * 0.30), (cx + s * 0.08, cy + s * 0.30),
               (cx - s * 0.12, cy + s * 0.48)]
        d.polygon(pts, fill=sun_col)
    elif kind == "snow":
        cloud(cx, cy - s * 0.12, s * 0.8, cloud_col)
        for i in range(4):
            x = cx - s * 0.30 + i * s * 0.20
            d.ellipse([x - 4, cy + s * 0.28, x + 4, cy + s * 0.28 + 9],
                      fill=(255, 255, 255, 235))


def apply_texture(img, weather, w, h):
    random.seed(20261030)
    d = ImageDraw.Draw(img, "RGBA")
    if weather == "sun":
        cx, cy = int(w * 0.82), int(h * 0.16)
        for r in range(420, 0, -24):
            a = int(26 * (1 - r / 420))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 226, 150, a))
    elif weather == "rain":
        for _ in range(260):
            x = random.randint(0, w); y = random.randint(0, h)
            d.line([(x, y), (x - 7, y + 30)], fill=(200, 226, 255, 38), width=2)
    elif weather == "cloud":
        for cx, cy, sc in [(w * 0.18, h * 0.13, 120), (w * 0.72, h * 0.09, 90),
                          (w * 0.45, h * 0.20, 80)]:
            d.ellipse([cx - sc, cy - sc * 0.6, cx + sc, cy + sc * 0.6],
                     fill=(255, 255, 255, 26))
    elif weather == "thunder":
        for r in range(min(w, h) // 2, 0, -30):
            d.ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r],
                      fill=(20, 20, 45, 6))
        bx, by = int(w * 0.5), int(h * 0.05)
        d.polygon([(bx, by), (bx - 16, by + 120), (bx + 6, by + 120),
                   (bx - 12, by + 260), (bx + 22, by + 90), (bx, by + 90)],
                  fill=(255, 240, 160, 30))
    elif weather == "snow":
        for _ in range(300):
            x = random.randint(0, w); y = random.randint(0, h)
            r = random.randint(2, 5)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 50))


def wrap_cn(text, n):
    if not text:
        return [""]
    return [text[i:i + n] for i in range(0, len(text), n)]


# NotoSansSC/SimHei 均无彩色 emoji 字形，直接画会成"☒"豆腐块，统一剥掉
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def _strip_emoji(s):
    return _EMOJI_RE.sub("", s or "").strip()


# ---------------- 今日指南（物流中心口径） ----------------
def gen_guides(weather, alerts, pop_max):
    sev = any(a.get("level") in ("红色", "橙色") for a in alerts)
    travel, life = [], []
    if sev:
        travel.append("红/橙预警生效，暂停户外吊装，非必要不外出")
        life.append("遵从预警指令，备好应急照明与物资")
    if weather in ("rain", "thunder") or pop_max >= 50:
        travel.append("降水集中，驾车减速、留意积水与过海停航")
        life.append("雷暴拔电器关窗，防潮防滑")
    if weather == "thunder":
        life.append("雷暴时远离空旷高地与金属物")
    if weather in ("clear", "partly"):
        life.append("天气晴好，注意午间防暑补水")
    if weather in ("cloud", "fog"):
        travel.append("能见度偏低，行车开雾灯、控车速")
    if weather == "snow":
        travel.append("道路湿滑，通行缓慢注意保暖")
    if not travel:
        travel.append("天气平稳，通行与户外作业正常")
    if not life:
        life.append("体感舒适，适宜户外活动")
    return "；".join(travel), "；".join(life)


# ---------------- 物流园区防御指引（按灾种+等级） ----------------
PARK_ADVICE = {
    "台风": {
        "红色": "仓库地堆货转高位、备挡水板沙袋；物业切户外电固广告牌；室外设备入棚断电；立即撤人停作业",
        "橙色": "仓库垫高货物、清排水口；物业查门窗固高空物；叉车充电桩入棚；暂停户外吊装",
        "黄色": "关注台风动态；收好易坠物；提前备应急物资",
        "蓝色": "关注台风动向；固定临边堆放；检查排涝设备",
    },
    "暴雨": {
        "红色": "低洼库区立即转移货物；切地堆电源；物业封门窗防倒灌；危房人员撤离",
        "橙色": "低洼库区垫高货物、清排水口；物业查门窗密封；确保排水泵可用",
        "黄色": "提前清排水口；避积水路段；勿在树下停留",
        "蓝色": "关注雨情；清理排水口；户外作业注意积水",
    },
    "大风": {
        "红色": "立即停户外及高空作业；固或拆易吹落物；船舶回港",
        "橙色": "停露天吊装；固门窗与高空悬挂物；船舶回港",
        "黄色": "减少外出；固定易吹落物品；注意防风",
        "蓝色": "关注大风；收好阳台与临边物品",
    },
    "雷电": {
        "红色": "立即撤空旷高地水域；远金属电力设施；停户外作业",
        "橙色": "远树木金属水体；暂停户外活动",
        "黄色": "减少外出；避户外电子设备与金属",
        "蓝色": "关注雷电；雷雨尽量在室内",
    },
    "地质灾害": {
        "红色": "危险区人员立即转移；远边坡陡崖与泥石流沟",
        "橙色": "危险区人员提前撤离；加强边坡陡崖巡查监测",
        "黄色": "留意险情；避边坡陡崖下停留",
        "蓝色": "关注地灾风险；远隐患点",
    },
}


def level_advice_park(a):
    d = PARK_ADVICE.get(a.get("type"), PARK_ADVICE["台风"])
    return d.get(a.get("level"), d.get("橙色", ""))


def _save_bytes(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", quality=92)
    return buf.getvalue()


# =====================================================================
#  日报卡
# =====================================================================
def render_daily_card(place, temp, tmax, tmin, desc, weather, alerts,
                      hourly, daily, typhoon_text=None, compact=True):
    """返回日报卡 PNG bytes。

    alerts : [{level,type,time,note}, ...]
    hourly : [(label, kind, temp, pop_or_None), ...]   通常 6 个
    daily  : [(label, kind, lo, hi, pop_or_None), ...]  通常 5 个
    """
    th = THEME
    bg_top, bg_mid, bg_bot = WEATHER_BG.get(weather, WEATHER_BG["clear"])
    img = vgrad(W, 3600, bg_top, bg_mid, bg_bot)  # 足够高，避免多预警时 crop 越界出黑边
    apply_texture(img, WEATHER_TEXTURE.get(weather, "cloud"), W, 2400)
    d = ImageDraw.Draw(img, "RGBA")

    f150 = _fb(150); f64 = _fb(64); f40 = _fb(40); f34 = _fb(34)
    f32 = _fr(32); f30 = _fr(30); f27 = _fr(27); f26 = _fr(26)
    text, sub = th["text"], th["text_sub"]
    y = 70
    if not daily:
        daily = [("今天", "cloud", 20, 28, 0)]
    GAP = 16 if compact else 26
    DROW = 68 if compact else 92
    LH = 34 if compact else 40
    MX = 64
    LX = MX + 36
    RX = W - MX - 28

    # ---- header ----
    d.text((W // 2, y), place, font=f40, fill=text, anchor="ma"); y += 64
    d.text((W // 2, y), f"{temp}°", font=f150, fill=text, anchor="ma"); y += 170 if compact else 190
    d.text((W // 2, y), f"最高 {tmax}°   最低 {tmin}°", font=f34, fill=text, anchor="ma"); y += 48 if compact else 56
    d.text((W // 2, y), desc or WEATHER_DESC.get(weather, ""), font=f40, fill=text, anchor="ma"); y += 72 if compact else 86

    # ---- 卡片1：预警状态 ----
    has_alert = bool(alerts)
    NOTE_WRAP = 22
    row_hs = []
    if has_alert:
        for a in alerts:
            nl = len(wrap_cn(a.get("note", ""), NOTE_WRAP))
            row_hs.append(96 + (nl - 1) * 32)
    ch = ((164 if not has_alert else 104 + sum(row_hs) + 24) if compact
          else (168 if not has_alert else 104 + sum(row_hs) + 28))
    card(d, MX, y, W - 2 * MX, ch, th)
    d.text((LX, y + 30), "⚠ 预警状态", font=f40, fill=text, anchor="la")
    if has_alert:
        d.text((RX, y + 36), f"{len(alerts)} 条生效", font=f34,
               fill=(255, 190, 120), anchor="ra")
        yy = y + 104
        for k, a in enumerate(alerts):
            c = LEVEL_COLOR.get(a["level"], (150, 150, 150))
            d.rounded_rectangle([LX, yy + 10, LX + 12, yy + 66], radius=6, fill=c)
            d.text((LX + 32, yy + 12), f"{a['level']}", font=f34, fill=c, anchor="la")
            d.text((LX + 128, yy + 12), f"{a['type']}预警", font=f34, fill=th["warn_text"], anchor="la")
            d.text((RX, yy + 18), a.get("time", ""), font=f26, fill=sub, anchor="ra")
            note_lines = wrap_cn(a.get("note", ""), NOTE_WRAP)
            d.text((LX + 32, yy + 58), note_lines[0], font=f26, fill=sub, anchor="la")
            for j, ln in enumerate(note_lines[1:], 1):
                d.text((LX + 32, yy + 58 + j * 32), ln, font=f26, fill=sub, anchor="la")
            yy += row_hs[k]
    else:
        d.ellipse([LX + 20, y + 88, LX + 76, y + 144], outline=th["ok_green"], width=5)
        d.line([(LX + 33, y + 116), (LX + 46, y + 130)], fill=th["ok_green"], width=5)
        d.line([(LX + 46, y + 130), (LX + 65, y + 100)], fill=th["ok_green"], width=5)
        d.text((LX + 84, y + 94), "当前无生效预警", font=f34, fill=text, anchor="la")
    y += ch + GAP

    # ---- 台风趋势（红字无框，紧跟预警状态卡下方；过长自动折行） ----
    # 注：NotoSansSC 无 🌀 等 emoji 字形，直接用纯文字，避免豆腐块
    t_text = ("台风：" + typhoon_text) if typhoon_text else ("未来数日无预报路径直接影响海南的台风")
    t_lines = wrap_cn(t_text, 20)
    t_fill = (255, 70, 70) if typhoon_text else (150, 222, 255)
    for i, ln in enumerate(t_lines):
        d.text((LX, y + 8 + i * 46), ln, font=f34, fill=t_fill, anchor="la")
    y += 8 + len(t_lines) * 46 + 6 + GAP

    # ---- 卡片2：逐小时预报 ----
    ch = 292 if compact else 308
    card(d, MX, y, W - 2 * MX, ch, th)
    d.text((LX, y + 26), "逐小时预报", font=f34, fill=sub, anchor="la")
    d.text((RX, y + 30), "降水概率", font=f26, fill=sub, anchor="ra")
    cols = len(hourly)
    cw = (W - 2 * MX - 80) / cols if cols else 0
    for i, (lab, kind, t, pop) in enumerate(hourly):
        cx = MX + 40 + cw * (i + 0.5)
        d.text((cx, y + (86 if compact else 92)), lab, font=f32, fill=text, anchor="ma")
        draw_icon(img, kind, cx, y + (165 if compact else 172), 64, th)
        d.text((cx, y + (210 if compact else 222)), f"{t}°", font=f34, fill=text, anchor="ma")
        if pop:
            d.text((cx, y + (246 if compact else 262)), f"{pop}%", font=f26, fill=th["pop"], anchor="ma")
    y += ch + GAP

    # ---- 卡片3：未来5日报 ----
    rows = len(daily)
    ch = (80 + rows * DROW + 18) if compact else (96 + rows * 92 + 22)
    card(d, MX, y, W - 2 * MX, ch, th)
    d.text((LX, y + (20 if compact else 26)), "未来5日报", font=f34, fill=sub, anchor="la")
    all_min = min(r[2] for r in daily)
    all_max = max(r[3] for r in daily)
    span = max(all_max - all_min, 1)
    yy = y + (84 if compact else 100)
    blue, orange = (74, 144, 217), (245, 130, 32)
    for lab, kind, lo, hi, pop in daily:
        d.text((LX, yy), lab, font=f34, fill=text, anchor="lm")
        draw_icon(img, kind, 250, yy, 52, th)
        if pop:
            d.text((250, yy + 34), f"{pop}%", font=f26, fill=th["pop"], anchor="mm")
        d.text((330, yy), f"{lo}°", font=f34, fill=sub, anchor="lm")
        d.text((RX, yy), f"{hi}°", font=f34, fill=text, anchor="rm")
        bx0, bx1 = 400, W - 170
        d.rounded_rectangle([bx0, yy - 7, bx1, yy + 7], radius=7, fill=th["bar_bg"])
        x0 = bx0 + (lo - all_min) / span * (bx1 - bx0)
        x1 = bx0 + (hi - all_min) / span * (bx1 - bx0)
        bar = capsule_gradient(int(x1 - x0), 14, blue, orange)
        img.paste(bar, (int(x0), int(yy - 7)), bar)
        yy += DROW
    y += ch + GAP

    # ---- 今日指南卡 ----
    travel, life = gen_guides(weather, alerts, max((r[4] or 0) for r in daily))
    tlines = wrap_cn(travel, 18)
    llines = wrap_cn(life, 18)
    lh = LH + 2
    ch = (84 + len(tlines) * lh + 14 + len(llines) * lh + 12) if compact else (96 + len(tlines) * lh + 16 + len(llines) * lh + 14)
    card(d, MX, y, W - 2 * MX, ch, th)
    d.text((LX, y + (20 if compact else 26)), "今日指南", font=f34, fill=sub, anchor="la")
    yy = y + (84 if compact else 96)
    d.text((LX, yy), "出行", font=f32, fill=text, anchor="lm")
    for i, ln in enumerate(tlines):
        d.text((220, yy + i * lh), ln, font=f27, fill=text, anchor="lm")
    yy2 = yy + len(tlines) * lh + (12 if compact else 14)
    d.text((LX, yy2), "生活", font=f32, fill=text, anchor="lm")
    for i, ln in enumerate(llines):
        d.text((220, yy2 + i * lh), ln, font=f27, fill=text, anchor="lm")
    y += ch + GAP

    # ---- 落款 ----
    from datetime import datetime
    d.text((W // 2, y), f"数据：中央气象台 · Open-Meteo ｜ 澄迈预警日报 · {datetime.now().strftime('%H:%M')}",
            font=f26, fill=th["footer"], anchor="ma")
    y += 54 if compact else 70

    img = img.crop((0, 0, W, int(y)))
    return _save_bytes(img)


# =====================================================================
#  生效预警卡（样式2：路径图内嵌 + 灾种分级防御指引）
# =====================================================================
def _alert_block_height(a):
    core_lines = wrap_cn(a.get("core", ""), 21)
    meta = f"{a.get('issuer','')} · 生效{a.get('time','')} · 预计至{a.get('to','')}"
    meta_lines = wrap_cn(meta, 30)
    return 78 + len(core_lines) * 38 + 8 + len(meta_lines) * 32 + 14


def _draw_alert_block(d, x, y, w, al, th):
    lc = LEVEL_COLOR.get(al["level"], (150, 150, 150))
    core_lines = wrap_cn(al.get("core", ""), 21)
    meta = f"{al.get('issuer','')} · 生效{al.get('time','')} · 预计至{al.get('to','')}"
    meta_lines = wrap_cn(meta, 30)
    h = 78 + len(core_lines) * 38 + 8 + len(meta_lines) * 32 + 14
    card(d, x, y, w, h, th, radius=28)
    d.rounded_rectangle([x + 4, y + 4, x + 16, y + h - 4], radius=8, fill=lc)
    d.text((x + 44, y + 22), f"【{al['level']}】{al['type']}预警", font=_fb(36), fill=lc, anchor="la")
    for i, ln in enumerate(core_lines):
        d.text((x + 44, y + 74 + i * 38), ln, font=_fr(30), fill=th["text"], anchor="la")
    my = y + 74 + len(core_lines) * 38 + 10
    for i, ln in enumerate(meta_lines):
        d.text((x + 44, my + i * 32), ln, font=_fr(24), fill=th["text_sub"], anchor="la")
    d.rounded_rectangle([x + w - 176, y + 18, x + w - 24, y + 58], radius=18, fill=(255, 255, 255, 40))
    d.text((x + w - 100, y + 38), _strip_emoji(al.get("area", "")), font=_fr(28), fill=th["text"], anchor="mm")
    return h


def _defense_height(alerts):
    groups = {}
    for a in alerts:
        if a["type"] not in groups or LEVEL_ORDER.index(a["level"]) > LEVEL_ORDER.index(groups[a["type"]]["level"]):
            groups[a["type"]] = a
    items = sorted(groups.values(), key=lambda a: -LEVEL_ORDER.index(a["level"]))
    ch = 96
    for a in items:
        wrapped = wrap_cn(level_advice_park(a), 18)
        ch += 38 + len(wrapped) * 36 + 8
    return ch + 12


def _draw_defense(d, x, y, w, alerts, th):
    groups = {}
    for a in alerts:
        if a["type"] not in groups or LEVEL_ORDER.index(a["level"]) > LEVEL_ORDER.index(groups[a["type"]]["level"]):
            groups[a["type"]] = a
    items = sorted(groups.values(), key=lambda a: -LEVEL_ORDER.index(a["level"]))
    ch = _defense_height(alerts)
    card(d, x, y, w, ch, th, radius=28)
    d.text((80, y + 24), "防御指引 · 物流园区", font=_fb(34), fill=th["text_sub"], anchor="la")
    yy = y + 92
    for a in items:
        lc = LEVEL_COLOR[a["level"]]
        d.text((96, yy), f"【{a['type']}·{a['level']}】", font=_fr(30), fill=lc, anchor="la")
        yy += 38
        for ln in wrap_cn(level_advice_park(a), 18):
            d.text((120, yy), ln, font=_fr(28), fill=th["text"], anchor="la")
            yy += 36
        yy += 8
    return ch


def render_alert_card(alerts, track=None, track_mode="compact"):
    """返回生效预警卡 PNG bytes。
    alerts: [{level,type,issuer,time,core,to,area}, ...]
    track : 台风路径图，接受 PIL Image 或 PNG bytes（由 monitor.fetch_official_track_bytes 返回的中央气象台官方概率预报图），可空。
    """
    th = THEME
    # track 为官方路径图 bytes（源自 monitor.fetch_official_track_bytes），这里统一成 PIL Image
    if track is not None and isinstance(track, (bytes, bytearray)):
        try:
            track = Image.open(io.BytesIO(track)).convert("RGB")
        except Exception:
            track = None
    blocks_h = sum(_alert_block_height(a) for a in alerts) + 24 * len(alerts) + 2
    track_h = 0
    if track is not None:
        tw = 900 if track_mode == "full" else 620
        track_h = 50 + tw + 34 + 20 + 26
    def_h = _defense_height(alerts) + 26
    H = 56 + 130 + blocks_h + track_h + def_h + 80 + 60
    img = vgrad(W, H, *ALERT_BG)
    d = ImageDraw.Draw(img, "RGBA")
    y = 56
    top_level = max(alerts, key=lambda a: LEVEL_ORDER.index(a["level"]))
    lc = LEVEL_COLOR[top_level["level"]]
    d.rounded_rectangle([40, y - 18, W - 40, y + 64], radius=22, fill=(0, 0, 0, 35))
    d.rectangle([40, y - 18, 64, y + 64], fill=lc)
    # 标题垂直居中于色带（色带 y-18..y+64，中心 y+23），避免大字下缘出框
    d.text((110, y + 23), "⚠ 预警生效", font=_fb(44), fill=(255, 255, 255), anchor="lm")
    y += 130
    for a in alerts:
        h = _draw_alert_block(d, 40, y, W - 80, a, th)
        y += h + 24
    y += 2
    if track is not None:
        tw = 900 if track_mode == "full" else 620
        th_img = track.resize((tw, tw))
        d.text((80, y), "台风路径预报", font=_fb(34), fill=th["text_sub"], anchor="la")
        y += 50
        if track_mode == "full":
            img.paste(th_img, (40, y)); y += tw
        else:
            fx, fy, fw, fh = int((W - tw) / 2) - 10, y - 10, tw + 20, tw + 20
            d.rounded_rectangle([fx, fy, fx + fw, fy + fh], radius=24,
                                fill=(255, 255, 255, 26), outline=(255, 255, 255, 60), width=2)
            img.paste(th_img, (int((W - tw) / 2), y)); y += tw
        d.text((W // 2, y + 2), "预报路径仅供参考 · 以中央气象台最新发布为准",
               font=_fr(26), fill=(200, 210, 230), anchor="ma")
        y += 54
    y += 20
    ch = _draw_defense(d, 40, y, W - 80, alerts, th)
    y += ch + 26
    d.text((W // 2, y), "数据来源：海南省气象局 / 中央气象台",
           font=_fr(26), fill=th["footer"], anchor="ma")
    y += 60
    img = img.crop((0, 0, W, int(y)))
    return _save_bytes(img)


# =====================================================================
#  变更卡：升级 / 降级 / 解除
# =====================================================================
def render_transition_card(kind, alert, prev_level=None, track=None):
    """返回变更卡 PNG bytes。

    kind       : "upgrade" | "downgrade" | "lift"
    alert      : {level,type,issuer,time,headline,core,area}
    prev_level : 升级/降级时的旧等级（如 "蓝色"）
    track      : 可选路径图（仅台风升级时传入）
    """
    th = THEME
    if kind == "upgrade":
        bar, title = LEVEL_COLOR["红色"], "⚠ 预警升级"
    elif kind == "downgrade":
        bar, title = LEVEL_COLOR["橙色"], "预警降级"
    else:
        bar, title = (90, 210, 140), "预警解除"

    # ---- 内容行预计算 ----
    core_lines = wrap_cn(alert.get("core", alert.get("headline", "")), 21)
    chg_lines = []
    if kind in ("upgrade", "downgrade") and prev_level:
        line = f"{prev_level} → {alert['level']} ｜ {alert['type']}预警"
        chg_lines = wrap_cn(line, 18)
    adv_lines = []
    if kind in ("upgrade", "downgrade"):
        adv_lines = wrap_cn(level_advice_park(alert), 18)
    elif kind == "lift":
        adv_lines = wrap_cn("本次预警已解除，请关注后续天气变化与可能的新预警。", 20)

    meta = f"{alert.get('issuer','')} ｜ {alert.get('time','')}"

    # ---- 高度逐段计算（必须与下方绘制顺序完全一致，否则 crop 越界出黑边） ----
    ADV_H = 92 + len(adv_lines) * 38          # 提示/防御指引卡：标题区 64 + 正文 n*38 + 底部余量 28
    seg_header = 110                          # 色带 header 段
    seg_chg = (len(chg_lines) * 50 + 10) if chg_lines else (60 if kind == "lift" else 0)
    seg_meta = 50
    seg_core = len(core_lines) * 40 + 20
    seg_adv = (ADV_H + 28) if adv_lines else 0
    seg_footer = 52
    H = 56 + seg_header + seg_chg + seg_meta + seg_core + seg_adv + seg_footer

    img = vgrad(W, H, *ALERT_BG)
    d = ImageDraw.Draw(img, "RGBA")
    y = 56

    # 顶部色条 header（标题垂直居中于色带 y-18..y+56，中心 y+19）
    d.rounded_rectangle([40, y - 18, W - 40, y + 56], radius=22, fill=(0, 0, 0, 35))
    d.rectangle([40, y - 18, 64, y + 56], fill=bar)
    d.text((110, y + 19), title, font=_fb(44), fill=(255, 255, 255), anchor="lm")
    y += seg_header

    # 变更主行（升级/降级）
    if chg_lines:
        for i, ln in enumerate(chg_lines):
            d.text((80, y + i * 50), ln, font=_fb(40), fill=bar, anchor="la")
        y += seg_chg
    elif kind == "lift":
        d.text((80, y), f"【{alert.get('level','')}】{alert.get('type','')}预警",
               font=_fb(40), fill=bar, anchor="la")
        y += seg_chg

    # 元信息
    d.text((80, y), meta, font=_fr(28), fill=th["text_sub"], anchor="la")
    y += seg_meta

    # 核心内容
    for i, ln in enumerate(core_lines):
        d.text((80, y + i * 40), ln, font=_fr(30), fill=th["text"], anchor="la")
    y += seg_core

    # 防御/提示卡（卡高 ADV_H：标题 y+20 起、正文 y+64 起、底部余量 28px）
    if adv_lines:
        card(d, 40, y, W - 80, ADV_H, th, radius=24)
        d.text((72, y + 20), "提示" if kind == "lift" else "防御指引 · 物流园区",
               font=_fb(30), fill=th["text_sub"], anchor="la")
        for i, ln in enumerate(adv_lines):
            d.text((96, y + 64 + i * 38), ln, font=_fr(28), fill=th["text"], anchor="la")
        y += seg_adv

    d.text((W // 2, y + 6), "数据来源：海南省气象局 / 中央气象台",
           font=_fr(26), fill=th["footer"], anchor="ma")

    img = img.crop((0, 0, W, int(y + seg_footer)))
    return _save_bytes(img)


if __name__ == "__main__":
    # 仅供本地快速自测（非生产路径）
    a = [{"level": "橙色", "type": "台风", "issuer": "海南省气象台", "time": "10-30 14:20",
          "core": "台风“海燕”将于48小时内逼近海南岛东部沿海，风力逐渐增强至12级以上",
          "to": "11-01 20:00", "area": "沿海"}]
    b = render_alert_card(a)
    print("alert card bytes:", len(b))
    t = render_transition_card("upgrade", a[0], prev_level="蓝色")
    print("upgrade card bytes:", len(t))
    dg = render_transition_card("downgrade", {"level": "黄色", "type": "雷电", "issuer": "澄迈县气象台",
                                             "time": "10-30 18:00", "core": "雷电预警由橙色降为黄色，强度减弱",
                                             "area": "老城"}, prev_level="橙色")
    print("downgrade card bytes:", len(dg))
    lf = render_transition_card("lift", {"level": "橙色", "type": "暴雨", "issuer": "澄迈县气象台",
                                         "time": "10-30 20:00", "core": "暴雨橙色预警已解除", "headline": "暴雨橙色预警"})
    print("lift card bytes:", len(lf))
