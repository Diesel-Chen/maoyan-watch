#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猫眼影院排片监控（分时变频版）
- 持续监控所有未来日期的新增 IMAX 场次，通过飞书和钉钉机器人通知
- 所有时段判断均以【北京时间 Asia/Shanghai】为准（本机是 PDT，务必别改成 localtime）
- 触发风控立刻熔断降级，不硬刚
"""
import base64, hashlib, hmac, fcntl, json, os, random, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------------- 基本配置 ----------------
CINEMA_ID    = "37534"                                      # MOViE MOViE 影城（前滩太古里店）
CINEMA_URL   = f"https://www.maoyan.com/cinema/{CINEMA_ID}?poi=1153113439"
MOVIE_NAME   = "奥德赛"
WANT_HALL    = "IMAX"                                       # 厅名或版本包含该关键字；"" = 不限
MOVIE_ID     = 1545360                                      # 用于拼选座页 URL；换片时随片名一起改
SEAT_URL     = "https://www.maoyan.com/xseats/{seq}?movieId={mid}&cinemaId={cid}"

BJ = ZoneInfo("Asia/Shanghai")

# ---------------- 分时频率（北京时间） ----------------
# (间隔秒, 抖动秒)
SPRINT  = (20,   5)      # 冲刺：20±5s     → 平均发现延迟 10s
NORMAL  = (60,  20)      # 常规：60±20s
NIGHT   = (1500, 300)    # 夜间：25±5min   → 20~30 分钟
DEGRADE = (90,  30)      # 熔断降级：退回已验证安全的 90±30s

SPRINT_WINDOWS = [("11:00", "12:00"),
                  ("13:30", "16:30"),
                  ("20:30", "22:30")]
NIGHT_WINDOW   = ("00:00", "08:00")                          # 其余时间为常规档（08:00~24:00）

# ---------------- 本地配置（不提交到版本库） ----------------
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "config.local.json")) as f:
        _CONFIG = json.load(f)
except FileNotFoundError:
    _CONFIG = {}

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", _CONFIG.get("FEISHU_WEBHOOK", ""))
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", _CONFIG.get("DINGTALK_WEBHOOK", ""))
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", _CONFIG.get("DINGTALK_SECRET", ""))
CHANNELS = tuple(name for name, enabled in (
    ("feishu", bool(FEISHU_WEBHOOK)),
    ("dingtalk", bool(DINGTALK_WEBHOOK and DINGTALK_SECRET)),
) if enabled)

# ---------------- 风控与告警 ----------------
DEGRADE_SECONDS     = 1800   # 熔断后维持降级的时长
FAIL_ALERT_AFTER    = 3      # 连续网络错误几次后告警

STATE_FILE = os.path.join(_HERE, "continuous_state.json")
LOG_FILE   = os.path.join(_HERE, "watch.log")

API = f"https://m.maoyan.com/ajax/cinemaDetail?cinemaId={CINEMA_ID}"
UA  = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def now_bj():
    return datetime.now(BJ)


def log(msg):
    line = f"[{now_bj():%m-%d %H:%M:%S} CST] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- 分时档位 ----------------
def _in_window(hm, start, end):
    return start <= hm < end


def current_tier(degraded_until=0.0):
    """返回 (档位名, 间隔, 抖动)，全部按北京时间判断"""
    if time.time() < degraded_until:
        return ("降级", *DEGRADE)
    hm = f"{now_bj():%H:%M}"
    for s, e in SPRINT_WINDOWS:
        if _in_window(hm, s, e):
            return ("冲刺", *SPRINT)
    if _in_window(hm, *NIGHT_WINDOW):
        return ("夜间", *NIGHT)
    return ("常规", *NORMAL)


# ---------------- 抓取 ----------------
class Blocked(Exception):
    """疑似被风控（验证码 / 403 / 429 / 非 JSON）"""


def fetch():
    req = urllib.request.Request(API, headers={
        "User-Agent": UA,
        "Referer": "https://m.maoyan.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 406, 429):
            raise Blocked(f"HTTP {e.code}") from e
        raise
    head = raw.lstrip()[:200]
    if not head.startswith("{"):
        if "verify" in raw[:2000] or "验证" in raw[:2000]:
            raise Blocked("返回验证码页面")
        raise Blocked(f"返回不是 JSON: {head[:80]!r}")
    return json.loads(raw)


def seat_url(seq):
    return SEAT_URL.format(seq=seq, mid=MOVIE_ID, cid=CINEMA_ID)


def extract(data):
    """返回 {日期: [ {tm, tp, th, seq, url}, ... ]}，按开场时间排序"""
    movies = data.get("showData", {}).get("movies")
    if not isinstance(movies, list):
        raise ValueError("接口缺少 showData.movies，保留原有状态")
    out = {}
    for m in movies:
        if MOVIE_NAME not in (m.get("nm") or ""):
            continue
        for s in m.get("shows", []):
            for p in s.get("plist", []):
                if not p.get("dt") or p["dt"] < now_bj().date().isoformat():
                    continue
                hall, tp = p.get("th", ""), p.get("tp", "")
                if WANT_HALL and WANT_HALL not in (hall + tp):
                    continue
                out.setdefault(p["dt"], []).append({
                    "tm": p.get("tm", ""), "tp": tp, "th": hall,
                    "seq": p.get("seqNo", ""),
                    "url": seat_url(p.get("seqNo", "")),
                })
    for v in out.values():
        v.sort(key=lambda x: x["tm"])
    return out


def last_sellable(data):
    ds = sorted({p["dt"] for m in data.get("showData", {}).get("movies", [])
                 if MOVIE_NAME in (m.get("nm") or "")
                 for s in m.get("shows", []) for p in s.get("plist", [])})
    return ds[-1] if ds else "无"


# ---------------- 通知渠道 ----------------
def _post_one_hook(url, text):
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    code = resp.get("code", resp.get("StatusCode"))
    if code != 0:
        raise RuntimeError(f"飞书返回 {resp}")


def dingtalk_signed_url(timestamp=None):
    timestamp = str(int(time.time() * 1000) if timestamp is None else timestamp)
    digest = hmac.new(DINGTALK_SECRET.encode(),
                      f"{timestamp}\n{DINGTALK_SECRET}".encode(), hashlib.sha256).digest()
    query = urllib.parse.urlencode({"timestamp": timestamp,
                                   "sign": base64.b64encode(digest).decode()})
    return DINGTALK_WEBHOOK + "&" + query


def post_dingtalk(text):
    payload = json.dumps({"msgtype": "text", "text": {"content": text},
                          "at": {"isAtAll": False}}, ensure_ascii=False).encode()
    req = urllib.request.Request(dingtalk_signed_url(), data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode())
    if resp.get("errcode") != 0:
        raise RuntimeError(f"钉钉返回错误码 {resp.get('errcode')}")


def format_hit(fresh, detected_at):
    parts = ["影院：MOViE MOViE 前滩太古里"]
    weekdays = "一二三四五六日"
    for d in sorted(fresh):
        day = datetime.strptime(d, "%Y-%m-%d")
        times = "、".join(sh["tm"] for sh in sorted(fresh[d], key=lambda sh: sh["tm"]))
        parts.append(f"观影日期：{day.month} 月 {day.day} 日（周{weekdays[day.weekday()]}）"
                     f"｜新增 {len(fresh[d])} 场：{times}")
    found = datetime.fromisoformat(detected_at).astimezone(BJ)
    parts.append(f"发现时间（北京时间）：{found.month} 月 {found.day} 日 {found:%H:%M}")
    parts.append(f"🎟 前往影院购票：{CINEMA_URL}")
    return "\n\n".join(parts)


# ---------------- 状态 ----------------
def scope():
    return [CINEMA_ID, MOVIE_NAME, WANT_HALL, MOVIE_ID]


def show_key(day, show):
    # seqNo 可能变化；用实际日期、时间、厅和版本识别场次，避免重复通知。
    return json.dumps([day, show["tm"], show["th"], show["tp"]], ensure_ascii=False)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("持续监控状态文件格式不正确，请检查后再启动")
    if state.get("scope") != scope():
        return None
    if not isinstance(state.get("seen"), dict):
        raise ValueError("持续监控状态缺少 seen")
    return state["seen"]


def save_state(seen):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w") as f:
        json.dump({"version": 1, "scope": scope(), "seen": seen}, f,
                  ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def process_snapshot(data, seen):
    """首次建立基线；后续按场次通知，发送失败不记账，下轮重试。"""
    current = extract(data)
    detected_at = now_bj().isoformat(timespec="seconds")
    if seen is None:
        seen = {show_key(d, sh): {"baseline": True, "first_seen": detected_at}
                for d, shows in current.items() for sh in shows}
        save_state(seen)
        log(f"首次基线已建立：{len(seen)} 场已有排片，不补发历史通知")
        return seen
    # 历史记录没有 delivered 字段，视为完成，不向新增渠道补发旧票。
    current_items = {show_key(d, sh): (d, sh) for d, shows in current.items() for sh in shows}
    errors = []
    for channel in CHANNELS:
        items = sorted((key, value) for key, value in current_items.items()
                       if key not in seen or channel not in seen[key].get("delivered", CHANNELS))
        for start in range(0, len(items), 10):
            chunk = items[start:start + 10]
            fresh = {}
            for key, (d, sh) in chunk:
                fresh.setdefault(d, []).append(sh)
            body = format_hit(fresh, detected_at)
            text = f"🎬《{MOVIE_NAME}》{WANT_HALL} 新增放票\n\n{body}"
            try:
                if channel == "feishu":
                    _post_one_hook(FEISHU_WEBHOOK, text)
                else:
                    post_dingtalk(text)
            except Exception as exc:
                # 不记录请求 URL，避免访问令牌进入日志。
                log(f"{channel} 放票通知失败：{type(exc).__name__}，下轮重试")
                errors.append(channel)
                continue
            updated = dict(seen)
            for key, _ in chunk:
                record = dict(seen.get(key, {"baseline": False, "first_seen": detected_at,
                                             "delivered": []}))
                record["delivered"] = sorted(set(record["delivered"]) | {channel})
                updated[key] = record
            save_state(updated)
            seen.update(updated)
            log(f"{channel} 放票通知送达：{len(chunk)} 场，日期 {'、'.join(sorted(fresh))}")
    if errors:
        raise OSError("通知渠道未全部送达：" + ", ".join(sorted(set(errors))))
    return seen


# ---------------- 主循环 ----------------
def main():
    if DINGTALK_WEBHOOK and not DINGTALK_SECRET:
        raise ValueError("配置钉钉 webhook 时必须同时填写 DINGTALK_SECRET")
    if not CHANNELS:
        raise ValueError("请在 config.local.json 或环境变量中配置至少一个通知渠道")
    seen = load_state()
    tier = current_tier()
    log(f"启动：{MOVIE_NAME} @ cinema {CINEMA_ID}｜持续监控所有日期新增场次｜厅={WANT_HALL or '不限'}")
    fails = ok_cnt = err_cnt = blocked_cnt = 0
    degraded_until = 0.0
    last_tier = tier[0]

    while True:
        try:
            data = fetch()
            seen = process_snapshot(data, seen)
            fails = 0
            ok_cnt += 1
            if ok_cnt % 10 == 1 or last_tier == "冲刺":
                log(f"ok#{ok_cnt} [{last_tier}] 可售到 {last_sellable(data)}｜持续监控新增场次")
        except Blocked as e:
            blocked_cnt += 1
            err_cnt += 1
            degraded_until = time.time() + DEGRADE_SECONDS
            log(f"⚠️ 疑似风控: {e} → 熔断降级至 {DEGRADE[0]}±{DEGRADE[1]}s，持续 {DEGRADE_SECONDS//60} 分钟")
        except Exception as e:                               # noqa: BLE001
            fails += 1
            err_cnt += 1
            log(f"抓取或通知错误({fails}): {e}")
            if fails >= FAIL_ALERT_AFTER:
                time.sleep(min(600, 60 * fails))

        # 档位切换（仅写日志，不发送运行状态提醒）
        tier = current_tier(degraded_until)
        if tier[0] != last_tier:
            log(f"档位切换 {last_tier} → {tier[0]}（{tier[1]}±{tier[2]}s）")
            last_tier = tier[0]
        time.sleep(max(10, tier[1] + random.randint(-tier[2], tier[2])))


if __name__ == "__main__":
    try:
        # 持有文件锁，防止误启动多个进程重复发通知。
        with open(os.path.join(_HERE, "watch.lock"), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit("监控已在运行")
            main()
    except KeyboardInterrupt:
        log("已停止")
        sys.exit(0)
