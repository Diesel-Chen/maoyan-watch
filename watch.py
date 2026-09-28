#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猫眼影院排片监控（分时变频版）
- 持续监控所有未来日期的新增 IMAX 场次，通过飞书和钉钉机器人通知
- 所有时段判断均以【北京时间 Asia/Shanghai】为准（本机是 PDT，务必别改成 localtime）
- 触发风控立刻熔断降级，不硬刚
"""
import re
import monitor_store

import base64, hashlib, hmac, fcntl, json, os, random, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------- 基本配置 ----------------
CINEMA_ID    = "37534"                                      # MOViE MOViE 影城（前滩太古里店）
CINEMA_URL   = f"https://www.maoyan.com/cinema/{CINEMA_ID}?poi=1153113439"
CINEMA_NAME = "MOViE MOViE 前滩太古里"
WANT_HALL = "IMAX"  # 厅名或版本包含 IMAX，包括在 IMAX 厅放映的普通 2D 电影

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

DB_FILE = os.path.join(_HERE, "monitor.sqlite3")
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


def extract(data):
    """All matching films, keyed by stable movie ID, never by title."""
    movies = data.get("showData", {}).get("movies")
    if not isinstance(movies, list):
        raise ValueError("接口缺少 showData.movies，保留原有状态")
    out = {}
    for movie in movies:
        shows = {}
        for group in movie.get("shows", []):
            for p in group.get("plist", []):
                day, tm = p.get("dt", ""), p.get("tm", "")
                hall, version = p.get("th", ""), p.get("tp", "")
                if WANT_HALL.upper() not in (hall + version).upper():
                    continue
                try:
                    start = datetime.fromisoformat(f"{day}T{tm}").replace(tzinfo=BJ)
                except ValueError:
                    continue
                if start <= now_bj():
                    continue
                key = (day, tm, hall, version)
                shows[key] = {"day": day, "tm": tm, "th": hall, "tp": version}
        if shows:
            if movie.get("id") is None or not movie.get("nm"):
                raise ValueError("IMAX 影片缺少 id 或片名")
            mid = str(movie["id"])
            existing = out.setdefault(mid, {"name": movie["nm"], "shows": {}})
            existing["shows"].update(shows)
    return out


def last_sellable(data):
    dates = [key[0] for movie in extract(data).values() for key in movie["shows"]]
    return max(dates, default="无")


def parse_release(detail):
    """Prefer explicitly mainland release/re-release info; never infer from first show."""
    desc = detail.get("pubDesc") or ""
    match = re.search(r"(\d{4}-\d{2}-\d{2}).*中国大陆.*(上映|重映)", desc)
    if match:
        date, kind = match.group(1), ("rerelease" if "重映" in desc else "release")
        source = "猫眼 pubDesc: " + desc
    elif not desc and re.fullmatch(r"\d{4}-\d{2}-\d{2}", detail.get("rt") or ""):
        date, kind, source = detail["rt"], "release", "猫眼 rt（地区未注明）"
    else:
        return None, None, "上映日期未知"
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None, None, "上映日期未知"
    return date, kind, source


def fetch_release(mid):
    url = "https://m.maoyan.com/ajax/detailmovie?" + urllib.parse.urlencode({"movieId": mid})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://m.maoyan.com/"})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.load(response)
    detail = data.get("detailMovie")
    if not isinstance(detail, dict) or str(detail.get("id")) != str(mid):
        raise ValueError("影片详情无效")
    return parse_release(detail)


def release_label(release_date, kind, day):
    if not release_date:
        return "上映日期未知"
    delta = (datetime.strptime(day, "%Y-%m-%d").date() -
             datetime.strptime(release_date, "%Y-%m-%d").date()).days
    prefix = "重映" if kind == "rerelease" else "上映"
    if -7 <= delta < 0:
        return f"{prefix}前 {-delta} 天"
    if delta == 0:
        return "重映首日" if kind == "rerelease" else "首映日"
    if 0 < delta <= 6:
        return f"{prefix}首周·第 {delta + 1} 天"
    return ""


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


def format_hit(name, fresh, detected_at, release_date=None, kind=None, first=False):
    found = datetime.fromisoformat(detected_at).astimezone(BJ)
    days = sorted(fresh)
    labels = {release_label(release_date, kind, day) for day in days}
    label_now = release_label(release_date, kind, found.date().isoformat())
    if kind == "rerelease":
        title = "🎞 重映关注"
    elif "首映日" in labels or label_now == "首映日":
        title = "🚨 新片首映放票"
    elif release_date and release_date > found.date().isoformat():
        title = "🌟 新片预售放票"
    elif any("首周" in x for x in labels | {label_now}):
        title = "🆕 新片首周放票"
    elif first:
        title = "🆕 首次监测到影片"
    else:
        title = "🎬 新增放票"
    parts = [f"{title}｜《{name}》IMAX 厅", f"影院：{CINEMA_NAME}"]
    if first:
        parts.append("本监控首次发现该影片的 IMAX 排片（不代表影片首次上映）")
    if release_date:
        parts.append(f"{'重映' if kind == 'rerelease' else '上映'}日期：{release_date}"
                     + (f"｜{label_now}" if label_now else ""))
    else:
        parts.append("上映日期暂未查到")
    weekdays = "一二三四五六日"
    for day in days:
        date = datetime.strptime(day, "%Y-%m-%d")
        times = "、".join(sh["tm"] for sh in sorted(fresh[day], key=lambda sh: sh["tm"]))
        flag = release_label(release_date, kind, day) if release_date else ""
        parts.append(f"{date.month} 月 {date.day} 日（周{weekdays[date.weekday()]}）"
                     f"{'【' + flag + '】' if flag else ''}｜新增 {len(fresh[day])} 场：{times}")
    parts.extend([f"发现时间（北京时间）：{found.month} 月 {found.day} 日 {found:%H:%M}",
                  f"🎟 前往影院购票：{CINEMA_URL}"])
    return "\n\n".join(parts)


def scope():
    return json.dumps([CINEMA_ID, WANT_HALL], ensure_ascii=False)


def open_database():
    db = monitor_store.connect(DB_FILE)
    monitor_store.migrate_legacy(db, STATE_FILE)
    return db


def refresh_metadata(db, movies):
    """At most once daily per film, including unsuccessful lookups."""
    stamp = now_bj().isoformat(timespec="seconds")
    for mid, movie in movies.items():
        row = db.execute('SELECT * FROM movies WHERE scope=? AND id=?', (scope(), mid)).fetchone()
        if row and row['checked_at'] and datetime.fromisoformat(row['checked_at']) > now_bj() - timedelta(days=1):
            continue
        try:
            release, kind, source = fetch_release(mid)
            movie['metadata'] = (release, kind, source, stamp)
        except Exception as exc:
            log(f"影片 {mid} 上映日期查询失败：{type(exc).__name__}")
            movie['metadata'] = (row['release_date'] if row else None,
                                 row['release_kind'] if row else None,
                                 row['release_source'] if row else None, stamp)


def process_snapshot(data, db):
    movies = extract(data)  # Validate before writing baseline or any state.
    refresh_metadata(db, movies)
    stamp = now_bj().isoformat(timespec="seconds")
    marker = 'baseline:' + scope()
    baseline = not db.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone()
    with db:
        for mid, movie in movies.items():
            old = db.execute('SELECT * FROM movies WHERE scope=? AND id=?', (scope(), mid)).fetchone()
            first = old is None
            db.execute('INSERT OR IGNORE INTO movies(scope,id,name,first_seen) VALUES(?,?,?,?)',
                       (scope(), mid, movie['name'], stamp))
            db.execute('UPDATE movies SET name=? WHERE scope=? AND id=?', (movie['name'], scope(), mid))
            if 'metadata' in movie:
                db.execute('UPDATE movies SET release_date=?,release_kind=?,release_source=?,checked_at=? WHERE scope=? AND id=?',
                           (*movie['metadata'], scope(), mid))
            row = db.execute('SELECT * FROM movies WHERE scope=? AND id=?', (scope(), mid)).fetchone()
            fresh = []
            for key, sh in sorted(movie['shows'].items()):
                inserted = db.execute('INSERT OR IGNORE INTO shows VALUES(?,?,?,?,?,?,?,?)',
                                      (scope(), mid, *key, stamp, int(baseline))).rowcount
                if inserted and not baseline:
                    fresh.append(sh)
            for start in range(0, len(fresh), 10):
                grouped = {}
                for sh in fresh[start:start + 10]:
                    grouped.setdefault(sh['day'], []).append(sh)
                text = format_hit(movie['name'], grouped, stamp, row['release_date'], row['release_kind'], first)
                for channel in CHANNELS:
                    db.execute('INSERT INTO outbox(scope,movie_id,detected_at,text,channel) VALUES(?,?,?,?,?)',
                               (scope(), mid, stamp, text, channel))
            if fresh:
                log(f"发现《{movie['name']}》新增 {len(fresh)} 场，已存入通知队列")
        db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', (marker, stamp))
    if baseline:
        log(f"所有 IMAX 影片基线已建立：{len(movies)} 部，不补发已有排片")
    flush_outbox(db)


def flush_outbox(db):
    errors = []
    for row in db.execute('SELECT * FROM outbox WHERE scope=? AND sent_at IS NULL ORDER BY id', (scope(),)).fetchall():
        if row['channel'] not in CHANNELS:
            continue
        with db:
            db.execute('UPDATE outbox SET attempts=attempts+1 WHERE id=?', (row['id'],))
        try:
            if row['channel'] == 'feishu':
                _post_one_hook(FEISHU_WEBHOOK, row['text'])
            else:
                post_dingtalk(row['text'])
        except Exception as exc:
            errors.append(row['channel'])
            log(f"{row['channel']} 放票通知失败：{type(exc).__name__}，保留队列待重试")
            continue
        with db:
            db.execute('UPDATE outbox SET sent_at=? WHERE id=?', (now_bj().isoformat(), row['id']))
        log(f"{row['channel']} 放票通知送达：影片 {row['movie_id']}，批次 {row['id']}")
    if errors:
        raise OSError('通知渠道未全部送达：' + ', '.join(sorted(set(errors))))


# ---------------- 主循环 ----------------
def main():
    if DINGTALK_WEBHOOK and not DINGTALK_SECRET:
        raise ValueError("配置钉钉 webhook 时必须同时填写 DINGTALK_SECRET")
    if not CHANNELS:
        raise ValueError("请在 config.local.json 或环境变量中配置至少一个通知渠道")
    db = open_database()
    tier = current_tier()
    log(f"启动：全部 IMAX 影片 @ cinema {CINEMA_ID}｜持续监控所有日期新增场次｜厅={WANT_HALL or '不限'}")
    fails = ok_cnt = err_cnt = blocked_cnt = 0
    degraded_until = 0.0
    last_tier = tier[0]

    while True:
        try:
            data = fetch()
            process_snapshot(data, db)
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
