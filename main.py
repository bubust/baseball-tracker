#!/usr/bin/env python3
"""
Baseball Tracker
監控 MLB / KBO / NPB 賽前賠率，即時偵測：
  1. 弱隊先得分
  2. 強隊先領先後弱隊反超
觸發時發送 Telegram 通知

資料來源：
  MLB  — MLB Stats API（比分）+ The Odds API（賠率）
  KBO  — Pinnacle Sports 公開 Guest API（比分 + 賠率）
  NPB  — Pinnacle Sports 公開 Guest API（比分 + 賠率）
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Optional

TW_TZ = timezone(timedelta(hours=8))

# ─── 球隊中文名對照表 ─────────────────────────────────────────────────────────
TEAM_ZH: dict[str, str] = {
    # MLB
    "Arizona Diamondbacks":        "亞利桑那響尾蛇",
    "Atlanta Braves":              "亞特蘭大勇士",
    "Baltimore Orioles":           "巴爾的摩金鶯",
    "Boston Red Sox":              "波士頓紅襪",
    "Chicago White Sox":           "芝加哥白襪",
    "Chicago Cubs":                "芝加哥小熊",
    "Cincinnati Reds":             "辛辛那提紅人",
    "Cleveland Guardians":         "克里夫蘭守護者",
    "Colorado Rockies":            "科羅拉多洛磯",
    "Detroit Tigers":              "底特律老虎",
    "Houston Astros":              "休士頓太空人",
    "Kansas City Royals":          "堪薩斯市皇家",
    "Los Angeles Angels":          "洛杉磯天使",
    "Los Angeles Dodgers":         "洛杉磯道奇",
    "Miami Marlins":               "邁阿密馬林魚",
    "Milwaukee Brewers":           "密爾瓦基釀酒人",
    "Minnesota Twins":             "明尼蘇達雙城",
    "New York Yankees":            "紐約洋基",
    "New York Mets":               "紐約大都會",
    "Athletics":                   "奧克蘭運動家",
    "Oakland Athletics":           "奧克蘭運動家",
    "Sacramento Athletics":        "沙加緬度運動家",
    "Philadelphia Phillies":       "費城費城人",
    "Pittsburgh Pirates":          "匹茲堡海盜",
    "San Diego Padres":            "聖地牙哥教士",
    "San Francisco Giants":        "舊金山巨人",
    "Seattle Mariners":            "西雅圖水手",
    "St. Louis Cardinals":         "聖路易紅雀",
    "Tampa Bay Rays":              "坦帕灣光芒",
    "Texas Rangers":               "德州遊騎兵",
    "Toronto Blue Jays":           "多倫多藍鳥",
    "Washington Nationals":        "華盛頓國民",
    # KBO
    "Doosan Bears":                "斗山熊",
    "LG Twins":                    "LG雙子",
    "Samsung Lions":               "三星獅",
    "NC Dinos":                    "NC恐龍",
    "KT Wiz":                      "KT巫師",
    "SSG Landers":                 "SSG登陸者",
    "Lotte Giants":                "樂天巨人",
    "Kia Tigers":                  "起亞老虎",
    "KIA Tigers":                  "起亞老虎",
    "Hanwha Eagles":               "韓華鷹",
    "Kiwoom Heroes":               "奇蒙英雄",
    # NPB
    "Yomiuri Giants":              "讀賣巨人",
    "Hanshin Tigers":              "阪神虎",
    "Hiroshima Toyo Carp":         "廣島東洋鯉魚",
    "Hiroshima Carp":              "廣島鯉魚",
    "Yokohama DeNA BayStars":      "橫濱DeNA海灣之星",
    "DeNA BayStars":               "DeNA海灣之星",
    "Tokyo Yakult Swallows":       "東京養樂多燕子",
    "Yakult Swallows":             "養樂多燕子",
    "Chunichi Dragons":            "中日龍",
    "Hokkaido Nippon-Ham Fighters":"北海道日本火腿鬥士",
    "Nippon-Ham Fighters":         "日本火腿鬥士",
    "Tohoku Rakuten Golden Eagles":"東北樂天金鷹",
    "Rakuten Eagles":              "樂天金鷹",
    "Chiba Lotte Marines":         "千葉羅德水手",
    "Lotte Marines":               "羅德水手",
    "Orix Buffaloes":              "歐力士野牛",
    "Fukuoka SoftBank Hawks":      "福岡軟銀鷹",
    "SoftBank Hawks":              "軟銀鷹",
    "Seibu Lions":                 "西武獅",
    "Saitama Seibu Lions":         "埼玉西武獅",
    "Yokohama Bay Stars":          "橫濱海灣之星",
    "Fukuoka Softbank Hawks":      "福岡軟銀鷹",
}


def team_zh(name: str) -> str:
    """回傳中文隊名，精確比對優先，其次部分包含比對。"""
    if name in TEAM_ZH:
        return TEAM_ZH[name]
    name_lower = name.lower()
    for en, zh in TEAM_ZH.items():
        if en.lower() in name_lower or name_lower in en.lower():
            return zh
    return name

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── 常數設定 ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8827160247:AAHPYR2hfHFr_g2HUwzLiQfhAHyRvyuyjBE"
CHAT_ID        = 1002700617
ODDS_API_KEY   = "0a5e9cd5cacdd220692bd42f3884426a"

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MLB_API_BASE  = "https://statsapi.mlb.com/api/v1"
PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

PINNACLE_LEAGUE_IDS = {
    "kbo": 6227,
    "npb": 187703,
}

CONFIG_FILE    = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE     = os.path.join(os.path.dirname(__file__), "game_state.json")
RESULTS_FILE   = os.path.join(os.path.dirname(__file__), "results.json")
PRE_ODDS_FILE  = os.path.join(os.path.dirname(__file__), "pre_odds_cache.json")

POLL_INTERVAL  = 60      # 秒
ODDS_CACHE_TTL = 6 * 3600  # 賠率快取 6 小時

PLAYSPORT_BASE = "https://ls.playsport.cc/ls_json.php"
PLAYSPORT_ALLIANCE_IDS = {"mlb": 1, "npb": 2, "kbo": 9}

# playsport official_id 短碼 → 中文隊名
PLAYSPORT_NPB_CODES: dict[str, str] = {
    "Hawks":    "福岡軟銀鷹",
    "Fighters": "北海道日本火腿鬥士",
    "Giants":   "讀賣巨人",
    "Swallows": "東京養樂多燕子",
    "Tigers":   "阪神虎",
    "Dragons":  "中日龍",
    "DeNA":     "橫濱海灣之星",
    "Carp":     "廣島東洋鯉魚",
    "Orix":     "歐力士野牛",
    "Rakuten":  "東北樂天金鷹",
    "Marines":  "千葉羅德水手",
    "Lions":    "西武獅",
    "Buffaloes":"歐力士野牛",
}
PLAYSPORT_KBO_CODES: dict[str, str] = {
    "Bears":    "斗山熊",
    "Twins":    "LG雙子",
    "Lions":    "三星獅",
    "Dinos":    "NC恐龍",
    "Wiz":      "KT巫師",
    "Landers":  "SSG登陸者",
    "Giants":   "樂天巨人",
    "Tigers":   "起亞老虎",
    "Eagles":   "韓華鷹",
    "Heroes":   "奇蒙英雄",
}
PLAYSPORT_CODES: dict[str, dict] = {
    "npb": PLAYSPORT_NPB_CODES,
    "kbo": PLAYSPORT_KBO_CODES,
}

YUNSAI_URL       = "https://www.sportslottery.com.tw/sports/baseball"
YUNSAI_CACHE_TTL = 3600   # 1 小時快取
_yunsai_cache: dict = {"_ts": 0, "data": {}}

PINNACLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     "https://www.pinnacle.com",
    "Referer":    "https://www.pinnacle.com/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "tracker.log"),
            encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)


# ─── 資料結構 ────────────────────────────────────────────────────────────────
@dataclass
class Game:
    game_id:    str
    league:     str
    home_team:  str
    away_team:  str
    home_score: int
    away_score: int
    inning:     int
    status:     str          # "live" | "final" | "scheduled"
    home_odds:   Optional[int]   = None
    away_odds:   Optional[int]   = None
    game_time:   Optional[str]   = None   # 台灣時間，格式 "HH:MM"
    home_spread: Optional[float] = None   # 主隊讓分：負=讓(強隊)，正=受讓(弱隊)


# ─── 設定 & 狀態 I/O ─────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    default = {"mlb": True, "kbo": True, "npb": True}
    save_config(default)
    return default


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_results() -> list:
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def append_result(record: dict):
    results = load_results()
    results.append(record)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def load_pre_odds() -> dict:
    if os.path.exists(PRE_ODDS_FILE):
        with open(PRE_ODDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_pre_odds(data: dict):
    with open(PRE_ODDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── 工具函式 ────────────────────────────────────────────────────────────────
def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fmt_odds(o: int) -> str:
    return f"+{o}" if o > 0 else str(o)


# ─── MLB Stats API ───────────────────────────────────────────────────────────
async def fetch_mlb_games(client: httpx.AsyncClient) -> list[Game]:
    today = date.today().isoformat()
    try:
        r = await client.get(
            f"{MLB_API_BASE}/schedule",
            params={"sportId": 1, "hydrate": "linescore", "date": today},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"[MLB] 比分 API 錯誤：{e}")
        return []

    games: list[Game] = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            code = g.get("status", {}).get("codedGameState", "")
            if code in ("S", "P"):
                continue
            status = "final" if code in ("F", "O", "U", "C", "D") else "live"

            ls    = g.get("linescore", {})
            teams = g.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})
            h_name = home.get("team", {}).get("name", "")
            a_name = away.get("team", {}).get("name", "")
            if not h_name or not a_name:
                continue

            games.append(Game(
                game_id    = str(g["gamePk"]),
                league     = "mlb",
                home_team  = h_name,
                away_team  = a_name,
                home_score = int(home.get("score", 0) or 0),
                away_score = int(away.get("score", 0) or 0),
                inning     = int(ls.get("currentInning", 0) or 0),
                status     = status,
            ))
    return games


# ─── MLB Stats API（全部場次，含預定）──────────────────────────────────────
async def fetch_mlb_all_games(client: httpx.AsyncClient) -> list[Game]:
    """同 fetch_mlb_games，但包含 Scheduled/Pre-Game 狀態，並附遊戲時間（台灣時間）。"""
    today = date.today().isoformat()
    try:
        r = await client.get(
            f"{MLB_API_BASE}/schedule",
            params={"sportId": 1, "hydrate": "linescore", "date": today},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"[MLB] 全場次 API 錯誤：{e}")
        return []

    games: list[Game] = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            code = g.get("status", {}).get("codedGameState", "")
            if code in ("S", "P"):
                status = "scheduled"
            elif code in ("F", "O", "U", "C", "D"):
                status = "final"
            else:
                status = "live"

            ls    = g.get("linescore", {})
            teams = g.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})
            h_name = home.get("team", {}).get("name", "")
            a_name = away.get("team", {}).get("name", "")
            if not h_name or not a_name:
                continue

            # 解析 UTC 時間 → 台灣時間
            game_time_str = None
            raw_time = g.get("gameDate", "")
            if raw_time:
                try:
                    dt_utc = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    dt_tw  = dt_utc.astimezone(TW_TZ)
                    game_time_str = dt_tw.strftime("%H:%M")
                except Exception:
                    pass

            games.append(Game(
                game_id    = str(g["gamePk"]),
                league     = "mlb",
                home_team  = h_name,
                away_team  = a_name,
                home_score = int(home.get("score", 0) or 0),
                away_score = int(away.get("score", 0) or 0),
                inning     = int(ls.get("currentInning", 0) or 0),
                status     = status,
                game_time  = game_time_str,
            ))
    return games


# ─── The Odds API（MLB 賠率）────────────────────────────────────────────────
_odds_cache: dict[str, dict]    = {}
_odds_last_fetch: dict[str, float] = {}


async def refresh_mlb_odds(client: httpx.AsyncClient) -> dict:
    now = time.monotonic()
    if now - _odds_last_fetch.get("mlb", 0) < ODDS_CACHE_TTL and "mlb" in _odds_cache:
        return _odds_cache["mlb"]

    try:
        r = await client.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "h2h", "oddsFormat": "american"},
            timeout=10,
        )
        r.raise_for_status()
        events = r.json()
        log.info(f"[MLB] 賠率已更新，剩餘配額：{r.headers.get('x-requests-remaining','?')}")
    except Exception as e:
        log.error(f"[MLB] Odds API 錯誤：{e}")
        return _odds_cache.get("mlb", {})

    cache: dict = {}
    for ev in events:
        h, a = ev.get("home_team", ""), ev.get("away_team", "")
        h_o = a_o = None
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    if normalize(outcome["name"]) == normalize(h):
                        h_o = outcome["price"]
                    elif normalize(outcome["name"]) == normalize(a):
                        a_o = outcome["price"]
                if h_o is not None and a_o is not None:
                    break
            if h_o is not None:
                break
        if h_o is not None and a_o is not None:
            key = f"{normalize(h)}_{normalize(a)}"
            cache[key] = {"home_team": h, "away_team": a,
                          "home_odds": int(h_o), "away_odds": int(a_o)}

    _odds_cache["mlb"] = cache
    _odds_last_fetch["mlb"] = now
    return cache


async def refresh_kbo_npb_odds(client: httpx.AsyncClient, league: str) -> dict:
    """從 The Odds API 取得 KBO/NPB 賠率，作為 Pinnacle 無盤時的備用。"""
    sport_key = f"baseball_{league}"  # baseball_kbo / baseball_npb
    now = time.monotonic()
    if now - _odds_last_fetch.get(league, 0) < ODDS_CACHE_TTL and league in _odds_cache:
        return _odds_cache[league]

    try:
        r = await client.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "h2h", "oddsFormat": "american"},
            timeout=10,
        )
        r.raise_for_status()
        events = r.json()
        log.info(f"[{league.upper()}] Odds API 賠率更新，剩餘配額：{r.headers.get('x-requests-remaining','?')}")
    except Exception as e:
        log.error(f"[{league.upper()}] Odds API 錯誤：{e}")
        return _odds_cache.get(league, {})

    cache: dict = {}
    for ev in events:
        h, a = ev.get("home_team", ""), ev.get("away_team", "")
        h_o = a_o = None
        for bm in ev.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    if normalize(outcome["name"]) == normalize(h):
                        h_o = outcome["price"]
                    elif normalize(outcome["name"]) == normalize(a):
                        a_o = outcome["price"]
                if h_o is not None and a_o is not None:
                    break
            if h_o is not None:
                break
        if h_o is not None and a_o is not None:
            h_zh = team_zh(h)
            a_zh = team_zh(a)
            cache[f"{h_zh}|{a_zh}"] = {"home_team": h, "away_team": a,
                                        "home_odds": int(h_o), "away_odds": int(a_o)}

    _odds_cache[league] = cache
    _odds_last_fetch[league] = now
    return cache


def _apply_odds_api_to_games(games: list[Game], odds_cache: dict) -> None:
    """把 Odds API 賠率貼到 game 物件（僅在 home_odds 仍為 None 時填入）。"""
    for g in games:
        if g.home_odds is not None:
            continue
        h_zh = team_zh(g.home_team)
        a_zh = team_zh(g.away_team)
        entry = odds_cache.get(f"{h_zh}|{a_zh}") or odds_cache.get(f"{a_zh}|{h_zh}")
        if entry:
            if f"{h_zh}|{a_zh}" in odds_cache:
                g.home_odds = entry["home_odds"]
                g.away_odds = entry["away_odds"]
            else:
                # 主客顛倒 → 對調
                g.home_odds = entry["away_odds"]
                g.away_odds = entry["home_odds"]
            log.info(f"[Odds API 補充] {h_zh} vs {a_zh} 賠率 {g.home_odds}/{g.away_odds}")


def find_mlb_odds(game: Game, cache: dict) -> Optional[dict]:
    norm_h, norm_a = normalize(game.home_team), normalize(game.away_team)
    key = f"{norm_h}_{norm_a}"
    if key in cache:
        return cache[key]
    for v in cache.values():
        if (norm_h in normalize(v["home_team"]) or normalize(v["home_team"]) in norm_h) and \
           (norm_a in normalize(v["away_team"]) or normalize(v["away_team"]) in norm_a):
            return v
    return None


# ─── Pinnacle Guest API（KBO / NPB 比分 + 賠率）───────────────────────────
async def fetch_pinnacle_games(client: httpx.AsyncClient, league: str) -> list[Game]:
    """
    從 Pinnacle 取得 KBO/NPB 個別賽事（賽前 + 即時）。

    Pinnacle 個別賽事結構：
      matchup.type == "matchup"
      matchup.participants 恰好 2 人，alignment 為 "home" / "away"
      participant.stats[i].period == 0  → 總得分
      participant.stats[i].period == n  → 第 n 局
    賠率由 markets/straight 端點取得，key 為 "s;0;m"（全場 moneyline）。
    """
    lid = PINNACLE_LEAGUE_IDS.get(league)
    if not lid:
        return []

    # 取得賽事列表
    try:
        r = await client.get(f"{PINNACLE_BASE}/leagues/{lid}/matchups",
                             headers=PINNACLE_HEADERS, timeout=10)
        r.raise_for_status()
        matchups = r.json()
    except Exception as e:
        log.error(f"[{league.upper()}] Pinnacle matchups 錯誤：{e}")
        return []

    # 取得賠率
    prices_map: dict[int, dict] = {}  # matchupId → {home_odds, away_odds, home_spread}
    try:
        r2 = await client.get(f"{PINNACLE_BASE}/leagues/{lid}/markets/straight",
                              headers=PINNACLE_HEADERS, timeout=10)
        if r2.status_code == 200:
            raw_mkts = r2.json()
            # ── Moneyline ──
            for mkt in raw_mkts:
                if mkt.get("key") != "s;0;m":
                    continue
                mid = mkt["matchupId"]
                h_price = a_price = None
                for p in mkt.get("prices", []):
                    d = p.get("designation", "")
                    if d == "home":   h_price = p["price"]
                    elif d == "away": a_price = p["price"]
                if h_price is not None and a_price is not None:
                    prices_map.setdefault(mid, {})
                    prices_map[mid]["home_odds"] = h_price
                    prices_map[mid]["away_odds"] = a_price

            # ── Spread（讓分盤）：取最小絕對值那條線 ──
            spread_best: dict[int, float] = {}  # mid → 最小 |handicap|
            for mkt in raw_mkts:
                key = mkt.get("key", "")
                if not (key.startswith("s;0;s;") or key == "s;0;s"):
                    continue
                mid = mkt["matchupId"]
                for p in mkt.get("prices", []):
                    if p.get("designation") == "home" and "points" in p:
                        h_pts = float(p["points"])
                        if mid not in spread_best or abs(h_pts) < abs(spread_best[mid]):
                            spread_best[mid] = h_pts
            for mid, h_pts in spread_best.items():
                prices_map.setdefault(mid, {})
                prices_map[mid]["home_spread"] = h_pts

    except Exception as e:
        log.warning(f"[{league.upper()}] Pinnacle 賠率錯誤：{e}")

    games: list[Game] = []
    for ev in matchups:
        # 只處理個別賽事（2 participants, home/away alignment）
        if ev.get("type") != "matchup":
            continue
        parts = ev.get("participants", [])
        home_p = next((p for p in parts if p.get("alignment") == "home"), None)
        away_p = next((p for p in parts if p.get("alignment") == "away"), None)
        if not home_p or not away_p:
            continue

        h_name = home_p.get("name", "")
        a_name = away_p.get("name", "")

        # 從 stats 解析比分（period=0 為總得分）
        def get_score(participant: dict) -> int:
            for s in participant.get("stats", []):
                if s.get("period") == 0 and "score" in s:
                    return int(s["score"])
            return 0

        def get_inning(participant: dict) -> int:
            inning = 0
            for s in participant.get("stats", []):
                p = s.get("period", 0)
                if p > inning and "score" in s:
                    inning = p
            return inning

        h_score = get_score(home_p)
        a_score = get_score(away_p)
        inning  = get_inning(home_p)

        is_live    = ev.get("isLive", False)
        is_settled = ev.get("isSettled", False)
        status = "final" if is_settled else ("live" if is_live else "scheduled")

        # 開賽時間 → 台灣時間
        game_time_str = None
        raw_start = ev.get("startTime", "")
        if raw_start:
            try:
                dt_utc = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                dt_tw  = dt_utc.astimezone(TW_TZ)
                game_time_str = dt_tw.strftime("%H:%M")
            except Exception:
                pass

        # 賠率
        h_odds = a_odds = None
        mid = ev["id"]
        if mid in prices_map:
            pm = prices_map[mid]
            h_odds = pm.get("home_odds")
            a_odds = pm.get("away_odds")

        games.append(Game(
            game_id     = str(mid),
            league      = league,
            home_team   = h_name,
            away_team   = a_name,
            home_score  = h_score,
            away_score  = a_score,
            inning      = inning,
            status      = status,
            home_odds   = h_odds,
            away_odds   = a_odds,
            game_time   = game_time_str,
            home_spread = prices_map.get(mid, {}).get("home_spread"),
        ))

    if games:
        log.info(f"[{league.upper()}] Pinnacle：{len(games)} 場賽事")
    else:
        log.debug(f"[{league.upper()}] Pinnacle：目前無個別賽事（可能還未開盤）")

    return games


# ─── Playsport.cc（補充比分，Pinnacle 結算後仍可查）──────────────────────────
async def fetch_playsport_scores(
    client: httpx.AsyncClient, league: str, gamedate: Optional[str] = None
) -> list[Game]:
    """
    從 playsport.cc JSON API 取得 NPB/KBO 比分（含已結束場次）。
    gamedate 格式：'YYYYMMDD'，預設為今天。
    """
    aid = PLAYSPORT_ALLIANCE_IDS.get(league)
    if not aid:
        return []
    if not gamedate:
        gamedate = date.today().strftime("%Y%m%d")

    try:
        r = await client.get(
            PLAYSPORT_BASE,
            params={"alliance": aid, "gamedate": gamedate},
            headers={"Referer": f"https://www.playsport.cc/livescore/{aid}",
                     "User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"[{league.upper()}] Playsport API 錯誤：{e}")
        return []

    code_map = PLAYSPORT_CODES.get(league, {})
    games: list[Game] = []

    for gid_str, gdata in data.items():
        if gid_str in ("use_memcache", "timestamp"):
            continue
        if not isinstance(gdata, dict):
            continue

        official_id = gdata.get("official_id", "")
        # 格式：NPB_YYYYMMDD_Away@Home_HHMM
        try:
            parts    = official_id.split("_")
            teams_str = parts[2]            # "Away@Home"
            away_code, home_code = teams_str.split("@")
        except Exception:
            continue

        away_name = code_map.get(away_code, away_code)
        home_name = code_map.get(home_code, home_code)

        scores = gdata.get("r", ["0", "0"])
        try:
            away_score = int(scores[0])
            home_score = int(scores[1])
        except Exception:
            away_score = home_score = 0

        ss = str(gdata.get("ss", "0"))
        if ss == "2":
            status = "final"
        elif ss == "1":
            status = "live"
        else:
            status = "scheduled"

        gs    = gdata.get("gs", {})
        inning = int(gs.get("i", 0) or 0)

        # 開賽時間 → 台灣時間（dateon 已是 UTC+8？直接取 HH:MM）
        game_time_str = None
        dateon = gdata.get("dateon", "")
        if dateon and len(dateon) >= 16:
            game_time_str = dateon[11:16]

        games.append(Game(
            game_id    = f"ps_{league}_{gid_str}",
            league     = league,
            home_team  = home_name,
            away_team  = away_name,
            home_score = home_score,
            away_score = away_score,
            inning     = inning,
            status     = status,
            game_time  = game_time_str,
        ))

    log.info(f"[{league.upper()}] Playsport：{len(games)} 場賽事")
    return games


async def fetch_yunsai_handicap() -> dict[str, str]:
    """
    用 Playwright 從運彩棒球頁抓今日讓分盤。
    回傳 {球隊中文名: "受讓" | "讓"} 字典。
    有 1 小時快取避免重複開啟瀏覽器。
    """
    global _yunsai_cache
    now = time.monotonic()
    if now - _yunsai_cache["_ts"] < YUNSAI_CACHE_TTL and _yunsai_cache["data"]:
        return _yunsai_cache["data"]

    result: dict[str, str] = {}
    try:
        from playwright.async_api import async_playwright
        from bs4 import BeautifulSoup

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                viewport={"width": 1280, "height": 800},
            )
            # 隱藏自動化特徵，繞過 Cloudflare 偵測
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-TW','zh','en-US','en']});
                window.chrome = {runtime: {}};
            """)
            page = await ctx.new_page()
            try:
                await page.goto(YUNSAI_URL, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(5000)
                html = await page.content()
                log.info(f"[運彩] 頁面載入完成，大小：{len(html)} bytes")
            except Exception as e:
                log.error(f"[運彩] 頁面載入失敗：{e}")
                html = await page.content()
            finally:
                await browser.close()

        # 解析 受讓 / 讓
        soup = BeautifulSoup(html, "lxml")
        page_text = soup.get_text(separator="\n")
        # 輸出前 2000 字供 debug
        log.info(f"[運彩] 頁面文字前2000字：\n{page_text[:2000]}")

        # 策略：找含「受讓」或「讓」的文字區塊，往上找隊名
        found_handicap = False
        lines = [l.strip() for l in page_text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "受讓" in line or ("讓" in line and "受讓" not in line and len(line) < 20):
                # 往前找隊名（通常在 1-3 行內）
                for j in range(max(0, i - 3), i):
                    team_candidate = lines[j]
                    if 2 <= len(team_candidate) <= 10 and not any(
                        c.isdigit() for c in team_candidate
                    ):
                        handicap_type = "受讓" if "受讓" in line else "讓"
                        result[team_candidate] = handicap_type
                        log.info(f"[運彩] {team_candidate} → {handicap_type}  (原文：{line})")
                        found_handicap = True

        if not found_handicap:
            log.warning("[運彩] 未找到任何受讓/讓資料，可能是頁面結構不同或尚未開盤")

    except ImportError:
        log.error("[運彩] playwright 或 beautifulsoup4 未安裝")
    except Exception as e:
        log.error(f"[運彩] 爬蟲錯誤：{e}")

    _yunsai_cache["data"] = result
    _yunsai_cache["_ts"] = time.monotonic()
    return result


def determine_underdog_from_yunsai(
    home_team: str, away_team: str, yunsai: dict[str, str]
) -> Optional[tuple]:
    """
    用運彩讓分盤判斷強弱隊。
    受讓方 = 弱隊（underdog）。
    回傳 (underdog_name, favorite_name, underdog_is_home) 或 None。
    """
    def match(name: str) -> Optional[str]:
        if name in yunsai:
            return name
        for k in yunsai:
            if k in name or name in k:
                return k
        return None

    h_key = match(home_team)
    a_key = match(away_team)

    if h_key and yunsai[h_key] == "受讓":
        return (home_team, away_team, True)
    if a_key and yunsai[a_key] == "受讓":
        return (away_team, home_team, False)
    if h_key and yunsai[h_key] == "讓":
        return (away_team, home_team, False)
    if a_key and yunsai[a_key] == "讓":
        return (home_team, away_team, True)
    return None


def _zh_match(a: str, b: str) -> bool:
    """模糊比對中文隊名：完全相同 或 互相包含。"""
    return a == b or a in b or b in a


def _update_pre_odds_cache(pin_games: list[Game], pre_odds: dict) -> None:
    """把 Pinnacle 即時有效賠率存入預快取（key = 主隊中文|客隊中文）。"""
    today = date.today().isoformat()
    for pp in pin_games:
        if pp.home_odds is None or pp.away_odds is None:
            continue
        h_zh = team_zh(pp.home_team)
        a_zh = team_zh(pp.away_team)
        pre_odds[f"{h_zh}|{a_zh}"] = {
            "home_odds":   pp.home_odds,
            "away_odds":   pp.away_odds,
            "home_spread": pp.home_spread,
            "date":        today,
        }


def _attach_pinnacle_odds(
    ps_games: list[Game],
    pin_games: list[Game],
    pre_odds: Optional[dict] = None,
):
    """
    將 Pinnacle 的賠率對應貼到 playsport 場次。
    策略：
      1. Pinnacle 即時資料（模糊中文隊名比對，同時支援主客場順序相反）
      2. Pinnacle 盤口暫停時，從預快取（pre_odds）撈賽前賠率（同樣支援逆向）
    """
    today = date.today().isoformat()

    for pg in ps_games:
        if pg.home_odds is not None:
            continue
        pg_h = pg.home_team   # playsport 已是中文
        pg_a = pg.away_team

        # 1️⃣ Pinnacle 即時資料（模糊比對，支援主客場逆向）
        matched = False
        for pp in pin_games:
            pp_h = team_zh(pp.home_team)
            pp_a = team_zh(pp.away_team)
            if _zh_match(pg_h, pp_h) and _zh_match(pg_a, pp_a):
                pg.home_odds   = pp.home_odds
                pg.away_odds   = pp.away_odds
                pg.home_spread = pp.home_spread
                matched = True
                break
            elif _zh_match(pg_h, pp_a) and _zh_match(pg_a, pp_h):
                # 主客場相反 → 對調賠率，讓分值取反
                pg.home_odds   = pp.away_odds
                pg.away_odds   = pp.home_odds
                pg.home_spread = (-pp.home_spread if pp.home_spread is not None else None)
                matched = True
                break

        if matched or pre_odds is None:
            continue

        # 2️⃣ Pinnacle 盤口暫停 → 從預快取補賠率（同樣支援逆向）
        cache_found = False
        for cache_key, pm in pre_odds.items():
            if pm.get("date") != today:
                continue
            parts = cache_key.split("|", 1)
            if len(parts) != 2:
                continue
            ch, ca = parts
            if _zh_match(pg_h, ch) and _zh_match(pg_a, ca):
                pg.home_odds   = pm.get("home_odds")
                pg.away_odds   = pm.get("away_odds")
                pg.home_spread = pm.get("home_spread")
                log.info(f"[預快取] {pg_h} vs {pg_a} 使用賽前快取賠率")
                cache_found = True
                break
            elif _zh_match(pg_h, ca) and _zh_match(pg_a, ch):
                # 快取方向與 Playsport 相反 → 對調
                pg.home_odds   = pm.get("away_odds")
                pg.away_odds   = pm.get("home_odds")
                h_sp = pm.get("home_spread")
                pg.home_spread = (-h_sp if h_sp is not None else None)
                log.info(f"[預快取逆向] {pg_h} vs {pg_a} 使用賽前快取賠率（主客對調）")
                cache_found = True
                break
        if not cache_found:
            log.warning(f"[無賠率] {pg_h} vs {pg_a} — Pinnacle 無即時賠率，快取亦無今日資料")


# ─── 回測工具 ────────────────────────────────────────────────────────────────

def _parse_pinnacle_innings(participant: dict) -> list[int]:
    """從 Pinnacle participant.stats 取出逐局得分（period 1..N，排除 period=0 總分）。"""
    rows = [
        (s["period"], int(s["score"]))
        for s in participant.get("stats", [])
        if s.get("period", 0) > 0 and "score" in s
    ]
    rows.sort()
    return [runs for _, runs in rows]


async def fetch_pinnacle_game_innings(
    client: httpx.AsyncClient, league: str
) -> dict[str, dict]:
    """
    從 Pinnacle 取得已結算場次的逐局比分。
    key = "主隊中文|客隊中文"
    val = {"home_innings": [r1,r2,...], "away_innings": [r1,r2,...]}
    """
    lid = PINNACLE_LEAGUE_IDS.get(league)
    if not lid:
        return {}
    try:
        r = await client.get(
            f"{PINNACLE_BASE}/leagues/{lid}/matchups",
            headers=PINNACLE_HEADERS, timeout=10
        )
        r.raise_for_status()
        matchups = r.json()
    except Exception as e:
        log.error(f"[{league.upper()}] Pinnacle innings 錯誤：{e}")
        return {}

    result: dict[str, dict] = {}
    for ev in matchups:
        if ev.get("type") != "matchup" or not ev.get("isSettled", False):
            continue
        parts = ev.get("participants", [])
        home_p = next((p for p in parts if p.get("alignment") == "home"), None)
        away_p = next((p for p in parts if p.get("alignment") == "away"), None)
        if not home_p or not away_p:
            continue

        h_zh = team_zh(home_p.get("name", ""))
        a_zh = team_zh(away_p.get("name", ""))
        h_inn = _parse_pinnacle_innings(home_p)
        a_inn = _parse_pinnacle_innings(away_p)
        if not h_inn and not a_inn:
            continue

        result[f"{h_zh}|{a_zh}"] = {
            "home_innings": h_inn,
            "away_innings": a_inn,
        }
    return result


def simulate_triggers(
    ud_is_home: bool,
    home_innings: list[int],
    away_innings: list[int],
) -> tuple[bool, bool, Optional[int], Optional[int]]:
    """
    從逐局比分模擬觸發條件，完全對應 check_triggers 邏輯。
    回傳 (first_score_trig, overtake_trig, first_score_inning, overtake_inning)
    """
    total_inn = max(len(home_innings), len(away_innings))
    ud_total = fav_total = 0
    first_score_checked = False
    first_scorer = None
    was_fav_leading = False
    t1 = t2 = False
    t1_inn = t2_inn = None

    for i in range(total_inn):
        h_runs = home_innings[i] if i < len(home_innings) else 0
        a_runs = away_innings[i] if i < len(away_innings) else 0
        ud_runs  = h_runs if ud_is_home else a_runs
        fav_runs = a_runs if ud_is_home else h_runs
        ud_total  += ud_runs
        fav_total += fav_runs
        inn = i + 1

        if not first_score_checked and (ud_total > 0 or fav_total > 0):
            first_score_checked = True
            if ud_total > 0 and fav_total == 0:
                first_scorer = "underdog"
            elif fav_total > 0 and ud_total == 0:
                first_scorer = "favorite"

        if first_scorer == "underdog" and not t1:
            t1 = True
            t1_inn = inn

        if fav_total > ud_total:
            was_fav_leading = True

        if was_fav_leading and ud_total > fav_total and not t2:
            t2 = True
            t2_inn = inn

    return t1, t2, t1_inn, t2_inn


# ─── 強弱判斷 ────────────────────────────────────────────────────────────────
def determine_sides(game: Game, odds: Optional[dict]) -> Optional[tuple]:
    """
    回傳 (underdog_name, favorite_name, ud_odds, fav_odds, underdog_is_home)
    若無法判斷回傳 None。

    判斷優先順序：
    1. Pinnacle spread（讓分盤）：主隊 home_spread > 0 = 主隊受讓 = 主隊弱
    2. Moneyline 賠率：正號賠率方為弱隊
    """
    # ── 優先：spread 讓分盤 ──
    if game.home_spread is not None and game.home_spread != 0:
        if game.home_spread > 0:
            # 主隊受讓（正數）= 主隊是弱隊
            h_o = game.home_odds or 0
            a_o = game.away_odds or 0
            return (game.home_team, game.away_team, h_o, a_o, True)
        else:
            # 主隊讓分（負數）= 主隊是強隊
            h_o = game.home_odds or 0
            a_o = game.away_odds or 0
            return (game.away_team, game.home_team, a_o, h_o, False)

    # ── 備用：moneyline 賠率 ──
    if odds:
        h_o, a_o = odds["home_odds"], odds["away_odds"]
    elif game.home_odds is not None and game.away_odds is not None:
        h_o, a_o = game.home_odds, game.away_odds
    else:
        return None

    if h_o == a_o:
        return None

    if h_o < a_o:   # 主隊賠率較低 = 主隊是強隊
        return (game.away_team, game.home_team, a_o, h_o, False)
    else:           # 客隊是強隊
        return (game.home_team, game.away_team, h_o, a_o, True)


# ─── 觸發邏輯 ────────────────────────────────────────────────────────────────
def check_triggers(game: Game, gs: dict) -> list[str]:
    ud_is_home = gs["underdog_is_home"]
    ud_score   = game.home_score if ud_is_home  else game.away_score
    fav_score  = game.home_score if not ud_is_home else game.away_score
    underdog   = gs["underdog"]
    favorite   = gs["favorite"]
    ud_odds    = gs["underdog_odds"]
    fav_odds   = gs["favorite_odds"]
    league_tag = game.league.upper()
    inning     = game.inning

    # 讓分顯示（若有 spread 資料）
    spread_val = gs.get("home_spread")
    if spread_val is not None:
        pts = abs(spread_val)
        spread_str = f"受讓{pts:.1f}分" if spread_val != 0 else ""
    else:
        spread_str = fmt_odds(ud_odds) if ud_odds else ""

    msgs: list[str] = []

    # ── 觸發 1：弱隊先得分 ──
    if not gs["first_score_checked"] and (ud_score > 0 or fav_score > 0):
        gs["first_score_checked"] = True
        if ud_score > 0 and fav_score == 0:
            gs["first_scorer"] = "underdog"
        elif fav_score > 0 and ud_score == 0:
            gs["first_scorer"] = "favorite"
        else:
            gs["first_scorer"] = "unknown"

    if gs.get("first_scorer") == "underdog" and not gs["first_score_notified"]:
        gs["first_score_notified"] = True
        msgs.append(
            f"⚾ [{league_tag}] 🔥 弱隊先得分！第{inning}局\n"
            f"🐣弱隊（{spread_str}）：{underdog}  {ud_score}分\n"
            f"💪強隊：{favorite}  {fav_score}分"
        )

    # ── 追蹤強隊是否曾領先 ──
    if fav_score > ud_score:
        gs["was_favorite_leading"] = True

    # ── 觸發 2：弱隊反超 ──
    if (gs["was_favorite_leading"]
            and ud_score > fav_score
            and not gs["overtake_notified"]):
        gs["overtake_notified"] = True
        msgs.append(
            f"⚾ [{league_tag}] 🚀 弱隊反超！第{inning}局\n"
            f"🐣弱隊（{spread_str}）：{underdog}  {ud_score}分\n"
            f"💪強隊：{favorite}  {fav_score}分"
        )

    return msgs


# ─── 監控主迴圈 ──────────────────────────────────────────────────────────────
async def monitor_cycle(context: ContextTypes.DEFAULT_TYPE):
    config  = load_config()
    state   = load_state()
    changed = False
    today   = date.today().isoformat()

    async with httpx.AsyncClient() as client:
        all_games: list[Game] = []

        # MLB
        if config.get("mlb"):
            mlb_games = await fetch_mlb_games(client)
            mlb_odds  = await refresh_mlb_odds(client)
            # 把賠率附加到 game 物件上（方便統一處理）
            for g in mlb_games:
                od = find_mlb_odds(g, mlb_odds)
                if od:
                    g.home_odds = od["home_odds"]
                    g.away_odds = od["away_odds"]
            all_games.extend(mlb_games)

        # KBO / NPB：Playsport 提供完整比分（含已結束），Pinnacle 補賠率
        pre_odds = load_pre_odds()
        for lg in ["kbo", "npb"]:
            if config.get(lg):
                pin_games = await fetch_pinnacle_games(client, lg)
                # 不論 Playsport 是否有資料，先把 Pinnacle 賠率存進快取
                _update_pre_odds_cache(pin_games, pre_odds)
                ps_games = await fetch_playsport_scores(client, lg)
                if ps_games:
                    _attach_pinnacle_odds(ps_games, pin_games, pre_odds)
                    # Pinnacle 無盤時，用 Odds API 補賠率
                    if any(g.home_odds is None for g in ps_games):
                        oa_cache = await refresh_kbo_npb_odds(client, lg)
                        _apply_odds_api_to_games(ps_games, oa_cache)
                    all_games.extend(ps_games)
                elif pin_games:
                    all_games.extend(pin_games)
        save_pre_odds(pre_odds)

        # 逐場處理
        for game in all_games:
            if game.status == "scheduled":
                continue

            sides = determine_sides(game, None)   # 賠率已在 game 物件內
            if sides is None:
                continue

            underdog, favorite, ud_odds, fav_odds, ud_is_home = sides
            gid = game.game_id

            if gid not in state:
                state[gid] = {
                    "underdog":             team_zh(underdog),
                    "favorite":             team_zh(favorite),
                    "underdog_is_home":     ud_is_home,
                    "underdog_odds":        ud_odds,
                    "favorite_odds":        fav_odds,
                    "home_spread":          game.home_spread,
                    "first_score_checked":  False,
                    "first_scorer":         None,
                    "first_score_notified": False,
                    "was_favorite_leading": False,
                    "overtake_notified":    False,
                    "status":               "live",
                    "league":               game.league,
                    "date":                 today,
                }
                changed = True
                log.info(
                    f"[{game.league.upper()}] 新比賽：{game.away_team} @ {game.home_team} "
                    f"| 弱隊：{underdog}({fmt_odds(ud_odds)})"
                )

            gs = state[gid]

            if game.status == "final" and gs.get("status") != "final":
                gs["status"] = "final"
                changed = True
                ud_is_home = gs.get("underdog_is_home")
                ud_score   = game.home_score if ud_is_home else game.away_score
                fav_score  = game.home_score if not ud_is_home else game.away_score
                ud_won = ud_score > fav_score
                append_result({
                    "date":          today,
                    "league":        game.league,
                    "away_team":     team_zh(game.away_team),
                    "home_team":     team_zh(game.home_team),
                    "away_score":    game.away_score,
                    "home_score":    game.home_score,
                    "underdog":      gs.get("underdog"),
                    "favorite":      gs.get("favorite"),
                    "underdog_odds": gs.get("underdog_odds"),
                    "favorite_odds": gs.get("favorite_odds"),
                    "underdog_won":  ud_won,
                    "ud_triggered":  gs.get("first_score_notified", False),
                    "overtake":      gs.get("overtake_notified", False),
                })
                log.info(
                    f"[{game.league.upper()}] 比賽結束：{game.away_team} {game.away_score} "
                    f"@ {game.home_team} {game.home_score} | 弱隊{'贏' if ud_won else '輸'}"
                )
                continue

            if gs.get("status") == "final":
                continue

            msgs = check_triggers(game, gs)
            if msgs:
                changed = True
                for msg in msgs:
                    log.info(f"TRIGGER → {msg}")
                    try:
                        await context.bot.send_message(chat_id=CHAT_ID, text=msg)
                    except Exception as e:
                        log.error(f"Telegram 發送失敗：{e}")

        # 清理昨天的結束比賽
        to_del = [gid for gid, gs in state.items()
                  if gs.get("status") == "final" and gs.get("date", today) < today]
        for gid in to_del:
            del state[gid]
            changed = True

    if changed:
        save_state(state)


# ─── Telegram Bot 指令 ───────────────────────────────────────────────────────
VALID_LEAGUES = {"mlb", "kbo", "npb"}


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not context.args:
        for lg in VALID_LEAGUES:
            cfg[lg] = True
        save_config(cfg)
        await update.message.reply_text("✅ 全部聯盟（MLB/KBO/NPB）監控已開啟")
        return
    lg = context.args[0].lower()
    if lg not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{lg}")
        return
    cfg[lg] = True
    save_config(cfg)
    await update.message.reply_text(f"✅ {lg.upper()} 監控已開啟")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not context.args:
        for lg in VALID_LEAGUES:
            cfg[lg] = False
        save_config(cfg)
        await update.message.reply_text("⛔ 全部聯盟（MLB/KBO/NPB）監控已關閉")
        return
    lg = context.args[0].lower()
    if lg not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{lg}")
        return
    cfg[lg] = False
    save_config(cfg)
    await update.message.reply_text(f"⛔ {lg.upper()} 監控已關閉")




async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    lines = [f"📅 今日賽程（{today}，台灣時間）\n"]

    cfg = load_config()

    LEAGUE_FLAG = {"mlb": "🇺🇸 MLB", "kbo": "🇰🇷 KBO", "npb": "🇯🇵 NPB"}

    async with httpx.AsyncClient() as client:
        # MLB
        mlb_games: list[Game] = []
        if cfg.get("mlb"):
            mlb_games = await fetch_mlb_all_games(client)
            mlb_odds  = await refresh_mlb_odds(client)
            for g in mlb_games:
                od = find_mlb_odds(g, mlb_odds)
                if od:
                    g.home_odds = od["home_odds"]
                    g.away_odds = od["away_odds"]

        # KBO / NPB：Playsport 提供完整比分（含已結束），Pinnacle 補賠率
        pre_odds  = load_pre_odds()
        kbo_games: list[Game] = []
        npb_games: list[Game] = []
        if cfg.get("kbo"):
            pin_kbo   = await fetch_pinnacle_games(client, "kbo")
            _update_pre_odds_cache(pin_kbo, pre_odds)
            kbo_games = await fetch_playsport_scores(client, "kbo")
            if kbo_games:
                _attach_pinnacle_odds(kbo_games, pin_kbo, pre_odds)
                if any(g.home_odds is None for g in kbo_games):
                    oa_kbo = await refresh_kbo_npb_odds(client, "kbo")
                    _apply_odds_api_to_games(kbo_games, oa_kbo)
            elif pin_kbo:
                kbo_games = pin_kbo
        if cfg.get("npb"):
            pin_npb   = await fetch_pinnacle_games(client, "npb")
            _update_pre_odds_cache(pin_npb, pre_odds)
            npb_games = await fetch_playsport_scores(client, "npb")
            if npb_games:
                _attach_pinnacle_odds(npb_games, pin_npb, pre_odds)
                if any(g.home_odds is None for g in npb_games):
                    oa_npb = await refresh_kbo_npb_odds(client, "npb")
                    _apply_odds_api_to_games(npb_games, oa_npb)
            elif pin_npb:
                npb_games = pin_npb

    league_groups = [
        ("mlb", mlb_games),
        ("kbo", kbo_games),
        ("npb", npb_games),
    ]

    total = 0
    for lg, games in league_groups:
        if not games:
            continue
        # 依時間排序（無時間的排後面）
        games_sorted = sorted(games, key=lambda g: g.game_time or "99:99")
        lines.append(f"{LEAGUE_FLAG[lg]}（{len(games)} 場）")
        for g in games_sorted:
            time_str = g.game_time or "--:--"

            if g.status == "scheduled":
                status_str = "預定"
            elif g.status == "live":
                status_str = f"進行中 第{g.inning}局" if g.inning else "進行中"
            else:
                status_str = "已結束"

            score_str = ""
            if g.status in ("live", "final"):
                score_str = f" {g.away_score}:{g.home_score}"

            odds_str = ""
            if g.home_odds is not None and g.away_odds is not None:
                odds_str = f"  {fmt_odds(g.away_odds)}/{fmt_odds(g.home_odds)}"

            away_zh = team_zh(g.away_team)
            home_zh = team_zh(g.home_team)
            if away_zh == g.away_team:
                log.debug(f"[未翻譯] {g.league.upper()} 客隊：{g.away_team!r}")
            if home_zh == g.home_team:
                log.debug(f"[未翻譯] {g.league.upper()} 主隊：{g.home_team!r}")

            # 標記弱隊（🐣）
            sides = determine_sides(g, None)
            if sides is not None:
                _, _, _, _, ud_is_home = sides
                if ud_is_home:
                    home_zh = f"🐣{home_zh}"
                else:
                    away_zh = f"🐣{away_zh}"

            lines.append(f"  {time_str} {away_zh} @ {home_zh}{score_str} [{status_str}]{odds_str}")
        lines.append("")
        total += len(games)

    if total == 0:
        lines.append("今日暫無賽事資料")

    await update.message.reply_text("\n".join(lines))


async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示累積弱隊買贏/輸戰績。用法：/record [today|mlb|kbo|npb]"""
    results = load_results()
    if not results:
        await update.message.reply_text("📭 尚無任何完賽記錄（Bot 記錄從現在開始累積）")
        return

    arg = context.args[0].lower() if context.args else None
    today = date.today().isoformat()

    # 篩選
    if arg == "today":
        subset = [r for r in results if r.get("date") == today]
        title = f"📊 今日弱隊戰績（{today}）"
    elif arg in ("mlb", "kbo", "npb"):
        subset = [r for r in results if r.get("league") == arg]
        title = f"📊 {arg.upper()} 弱隊累積戰績"
    else:
        subset = results
        title = "📊 全部弱隊累積戰績"

    if not subset:
        await update.message.reply_text(f"{title}\n\n暫無資料")
        return

    wins   = sum(1 for r in subset if r.get("underdog_won"))
    losses = sum(1 for r in subset if r.get("underdog_won") is False)
    total  = wins + losses
    rate   = f"{wins/total*100:.1f}%" if total else "–"

    lines = [title, f"勝負：{wins}勝{losses}敗  勝率：{rate}\n"]

    # 依日期分組顯示詳細（最多顯示最近 20 場）
    for r in subset[-20:]:
        ud  = r.get("underdog", "?")
        fav = r.get("favorite", "?")
        ud_odds  = r.get("underdog_odds")
        result = "✅" if r.get("underdog_won") else "❌"
        a = r.get("away_team","?")
        h = r.get("home_team","?")
        s = f"{r.get('away_score',0)}:{r.get('home_score',0)}"
        lg = r.get("league","?").upper()
        t1 = "🔥" if r.get("ud_triggered") else ""
        t2 = "🚀" if r.get("overtake") else ""
        odds_str = f"({fmt_odds(ud_odds)})" if ud_odds else ""
        lines.append(
            f"{result}[{lg}]{r.get('date','')} {a} {s} {h}\n"
            f"   弱隊：{ud}{odds_str} {t1}{t2}"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查詢監控狀態 + 即時比賽觸發情況。/check [mlb|kbo|npb]"""
    cfg = load_config()
    target = context.args[0].lower() if context.args else None
    if target and target not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{target}，請用 mlb/kbo/npb")
        return

    state  = load_state()
    today  = date.today().isoformat()

    # ── 頂部顯示開關狀態 ──
    status_parts = []
    for lg in ["mlb", "kbo", "npb"]:
        icon = "✅" if cfg.get(lg) else "⛔"
        status_parts.append(f"{icon}{lg.upper()}")
    lines = [f"⚾ 棒球追蹤器  {'  '.join(status_parts)}", f"📅 {today}\n"]

    async with httpx.AsyncClient() as client:
        all_games: list[Game] = []

        leagues = [lg for lg in ["mlb", "kbo", "npb"]
                   if cfg.get(lg) and (target is None or lg == target)]

        if "mlb" in leagues:
            mlb_games = await fetch_mlb_games(client)
            mlb_odds  = await refresh_mlb_odds(client)
            for g in mlb_games:
                od = find_mlb_odds(g, mlb_odds)
                if od:
                    g.home_odds = od["home_odds"]
                    g.away_odds = od["away_odds"]
            all_games.extend(mlb_games)
        pre_odds = load_pre_odds()
        for lg in ["kbo", "npb"]:
            if lg in leagues:
                pin_games = await fetch_pinnacle_games(client, lg)
                _update_pre_odds_cache(pin_games, pre_odds)
                ps_games  = await fetch_playsport_scores(client, lg)
                if ps_games:
                    _attach_pinnacle_odds(ps_games, pin_games, pre_odds)
                    if any(g.home_odds is None for g in ps_games):
                        oa_cache = await refresh_kbo_npb_odds(client, lg)
                        _apply_odds_api_to_games(ps_games, oa_cache)
                    all_games.extend(ps_games)
                elif pin_games:
                    all_games.extend(pin_games)

    if not all_games:
        lines.append("目前無進行中賽事")
        await update.message.reply_text("\n".join(lines))
        return

    live_count = 0
    for game in all_games:
        if game.status == "scheduled":
            continue
        live_count += 1
        sides = determine_sides(game, None)
        lg_tag = game.league.upper()

        away_zh = team_zh(game.away_team)
        home_zh = team_zh(game.home_team)
        score_str = f"{game.away_score}:{game.home_score}"
        inn_str   = f" 第{game.inning}局" if game.inning else ""
        status_str = "已結束" if game.status == "final" else f"進行中{inn_str}"

        if sides is None:
            if game.home_odds is not None and game.away_odds is not None:
                odds_raw = f"客{fmt_odds(game.away_odds)}/主{fmt_odds(game.home_odds)}"
                lines.append(f"[{lg_tag}] {away_zh} {score_str} {home_zh} [{status_str}] — 賠率相等（{odds_raw}），無法判斷強弱")
            else:
                lines.append(f"[{lg_tag}] {away_zh} {score_str} {home_zh} [{status_str}] — 無賠率（Pinnacle 尚未開盤）")
            continue

        underdog, favorite, ud_odds, fav_odds, ud_is_home = sides
        ud_score  = game.home_score if ud_is_home else game.away_score
        fav_score = game.home_score if not ud_is_home else game.away_score

        gs = state.get(game.game_id, {})

        # 讓分顯示
        spread = game.home_spread
        if spread is not None:
            pts = abs(spread)
            ud_label  = f"受讓{pts:.1f}分"
            fav_label = f"讓{pts:.1f}分"
        else:
            ud_label  = fmt_odds(ud_odds) if ud_odds else "弱"
            fav_label = fmt_odds(fav_odds) if fav_odds else "強"

        # 觸發狀態
        t1_done = gs.get("first_score_notified")
        t2_done = gs.get("overtake_notified")
        if game.status == "final":
            t1 = "✅" if t1_done else "❌"
            t2 = "✅" if t2_done else "❌"
        else:
            t1 = "✅" if t1_done else ("🔥" if ud_score > 0 and fav_score == 0 else "⬜")
            t2 = "✅" if t2_done else ("🚀" if gs.get("was_favorite_leading") and ud_score > fav_score else "⬜")

        ud_zh  = team_zh(underdog)
        fav_zh = team_zh(favorite)
        inn_label = f" 第{game.inning}局" if game.inning and game.status == "live" else ""

        lines.append(
            f"\n[{lg_tag}] {status_str}{inn_label}\n"
            f"  💪 強隊（{fav_label}）：{fav_zh}  {fav_score}分\n"
            f"  🐣 弱隊（{ud_label}）：{ud_zh}  {ud_score}分\n"
            f"  🔥先得分：{t1}  |  🚀反超：{t2}"
        )

    if live_count == 0:
        lines.append("目前無進行中或已結束賽事")

    await update.message.reply_text("\n".join(lines))


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /backtest [YYYYMMDD]
    用 Pinnacle 逐局比分回測今日（或指定日期）KBO/NPB 場次，
    驗證弱隊先得分 / 反超觸發條件。
    """
    # ── 解析日期參數 ──────────────────────────────────────────────
    args = context.args or []
    if args:
        raw = args[0].strip()
        try:
            dt = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            await update.message.reply_text("格式錯誤，請用 /backtest YYYYMMDD")
            return
    else:
        dt = date.today()

    date_str   = dt.isoformat()                  # 2025-07-15
    ps_gamedate = dt.strftime("%Y%m%d")          # 20250715

    await update.message.reply_text(f"🔍 回測 {date_str} 中，請稍候…")

    lines = [f"📊 回測結果：{date_str}\n"]
    LEAGUE_FLAG = {"kbo": "🇰🇷 KBO", "npb": "🇯🇵 NPB"}
    g_total = t1_total = t2_total = 0

    async with httpx.AsyncClient() as client:
        for lg in ["kbo", "npb"]:
            # 1. 取 Pinnacle 賠率（也順便更新 pre_odds cache）
            pre_odds = load_pre_odds()
            pin_games = await fetch_pinnacle_games(client, lg)
            _update_pre_odds_cache(pin_games, pre_odds)
            save_pre_odds(pre_odds)

            # 2. 取 Pinnacle 逐局比分（只有 isSettled=True 的場次）
            inning_map = await fetch_pinnacle_game_innings(client, lg)

            # 3. 取 Playsport 歷史比分
            ps_games = await fetch_playsport_scores(client, lg, gamedate=ps_gamedate)
            if not ps_games:
                continue

            # 4. 附加賠率
            _attach_pinnacle_odds(ps_games, pin_games, pre_odds)

            # 5. 只分析已結束場次
            finished = [g for g in ps_games if g.status == "final"]
            if not finished:
                continue

            lines.append(LEAGUE_FLAG.get(lg, lg.upper()))

            for g in finished:
                # 判斷強弱
                sides = determine_sides(g, None)
                if not sides:
                    lines.append(f"  ❔ {g.away_team} vs {g.home_team}  [無賠率，跳過]")
                    continue

                ud_name, fav_name, ud_odds, fav_odds, ud_is_home = sides

                # 在 inning_map 裡找對應逐局比分（雙向模糊比對）
                inn_data = None
                for key, val in inning_map.items():
                    parts = key.split("|")
                    if len(parts) != 2:
                        continue
                    k_home, k_away = parts[0], parts[1]
                    if _zh_match(g.home_team, k_home) and _zh_match(g.away_team, k_away):
                        inn_data = val
                        break
                    if _zh_match(g.home_team, k_away) and _zh_match(g.away_team, k_home):
                        # 方向相反，交換
                        inn_data = {
                            "home_innings": val["away_innings"],
                            "away_innings": val["home_innings"],
                        }
                        break

                if not inn_data:
                    lines.append(
                        f"  ❔ {g.away_team} {g.away_score}:{g.home_score} {g.home_team}"
                        f"  [無逐局資料]  弱:{ud_name}"
                    )
                    continue

                # 模擬觸發
                t1, t2, t1_inn, t2_inn = simulate_triggers(
                    ud_is_home,
                    inn_data["home_innings"],
                    inn_data["away_innings"],
                )

                g_total  += 1
                t1_total += int(t1)
                t2_total += int(t2)

                icon = "✅" if (t1 or t2) else "➖"
                t1_str = f"🔥{t1_inn}局" if t1 else ""
                t2_str = f"🚀{t2_inn}局" if t2 else ""
                trig_str = " ".join(filter(None, [t1_str, t2_str])) or "無觸發"

                # 顯示賠率（美式）
                ud_odds_str  = f"+{ud_odds}"  if ud_odds  >= 0 else str(ud_odds)
                fav_odds_str = f"+{fav_odds}" if fav_odds >= 0 else str(fav_odds)

                lines.append(
                    f"  {icon} {g.away_team} {g.away_score}:{g.home_score} {g.home_team}"
                    f"  弱:{ud_name}({ud_odds_str}) {trig_str}"
                )

            lines.append("")

    # 統計
    lines.append(
        f"共 {g_total} 場有效  /  🔥先得分 {t1_total} 場  /  🚀反超 {t2_total} 場"
    )
    if g_total > 0:
        lines.append(
            f"先得分率 {t1_total/g_total*100:.0f}%  /  反超率 {t2_total/g_total*100:.0f}%"
        )

    # 避免訊息過長（Telegram 上限 4096）
    text = "\n".join(lines)
    if len(text) > 4000:
        chunks = []
        cur = ""
        for line in lines:
            if len(cur) + len(line) + 1 > 3900:
                chunks.append(cur)
                cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚾ 棒球追蹤器指令\n\n"
        "/on [mlb|kbo|npb]  — 開啟監控（不加參數 = 全開）\n"
        "/off [mlb|kbo|npb] — 關閉監控（不加參數 = 全關）\n"
        "/today             — 今日賽程 + 🐣弱隊標示\n"
        "/check [聯盟]      — 監控狀態 + 即時觸發情況\n"
        "/record [聯盟/today] — 弱隊累積戰績\n"
        "/backtest [YYYYMMDD] — 回測觸發條件（KBO/NPB）\n"
        "/help              — 顯示說明\n\n"
        "🐣 = 弱隊（賠率正號/受讓方）\n"
        "💪 = 強隊\n\n"
        "觸發通知：\n"
        "🔥 弱隊先得分\n"
        "🚀 弱隊反超（強隊曾領先後被超越）\n\n"
        "資料來源：\n"
        "MLB → MLB Stats API + The Odds API\n"
        "KBO/NPB → Playsport比分 + Pinnacle/Odds API賠率"
    )
    await update.message.reply_text(text)


# ─── 啟動 ────────────────────────────────────────────────────────────────────
def main():
    log.info("⚾ 棒球追蹤器啟動中...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("on",       cmd_on))
    app.add_handler(CommandHandler("off",      cmd_off))
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("check",    cmd_check))
    app.add_handler(CommandHandler("record",   cmd_record))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("help",     cmd_help))

    app.job_queue.run_repeating(monitor_cycle, interval=POLL_INTERVAL, first=10)

    log.info(f"Bot 啟動，每 {POLL_INTERVAL} 秒監控一次")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
