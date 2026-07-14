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

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE  = os.path.join(os.path.dirname(__file__), "game_state.json")

POLL_INTERVAL  = 60      # 秒
ODDS_CACHE_TTL = 6 * 3600  # 賠率快取 6 小時

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
    home_odds:  Optional[int] = None
    away_odds:  Optional[int] = None
    game_time:  Optional[str] = None   # 台灣時間，格式 "HH:MM"


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
    prices_map: dict[int, dict] = {}  # matchupId → {home_odds, away_odds}
    try:
        r2 = await client.get(f"{PINNACLE_BASE}/leagues/{lid}/markets/straight",
                              headers=PINNACLE_HEADERS, timeout=10)
        if r2.status_code == 200:
            for mkt in r2.json():
                if mkt.get("key") != "s;0;m":
                    continue
                mid = mkt["matchupId"]
                h_price = a_price = None
                for p in mkt.get("prices", []):
                    d = p.get("designation", "")
                    if d == "home":
                        h_price = p["price"]
                    elif d == "away":
                        a_price = p["price"]
                if h_price is not None and a_price is not None:
                    prices_map[mid] = {"home_odds": h_price, "away_odds": a_price}
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
            h_odds, a_odds = pm["home_odds"], pm["away_odds"]

        games.append(Game(
            game_id    = str(mid),
            league     = league,
            home_team  = h_name,
            away_team  = a_name,
            home_score = h_score,
            away_score = a_score,
            inning     = inning,
            status     = status,
            home_odds  = h_odds,
            away_odds  = a_odds,
            game_time  = game_time_str,
        ))

    if games:
        log.info(f"[{league.upper()}] Pinnacle：{len(games)} 場賽事")
    else:
        log.debug(f"[{league.upper()}] Pinnacle：目前無個別賽事（可能還未開盤）")

    return games


# ─── 強弱判斷 ────────────────────────────────────────────────────────────────
def determine_sides(game: Game, odds: Optional[dict]) -> Optional[tuple]:
    """
    回傳 (underdog_name, favorite_name, ud_odds, fav_odds, underdog_is_home)
    若無賠率或賠率相同回傳 None。
    """
    # 從 odds dict（MLB 路徑）或 game.home_odds/away_odds（Pinnacle 路徑）取賠率
    if odds:
        h_o, a_o = odds["home_odds"], odds["away_odds"]
    elif game.home_odds is not None and game.away_odds is not None:
        h_o, a_o = game.home_odds, game.away_odds
    else:
        return None

    if h_o == a_o:
        return None

    if h_o < a_o:   # 主隊賠率較低 = 主隊是熱門
        return (game.away_team, game.home_team, a_o, h_o, False)
    else:           # 客隊是熱門
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
            f"⚾ [{league_tag}] {underdog}({fmt_odds(ud_odds)}) "
            f"{ud_score}:{fav_score} "
            f"{favorite}({fmt_odds(fav_odds)}) "
            f"| 第{inning}局 🔥 弱隊先得分！"
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
            f"⚾ [{league_tag}] {underdog}({fmt_odds(ud_odds)}) "
            f"{ud_score}:{fav_score} "
            f"{favorite}({fmt_odds(fav_odds)}) "
            f"| 第{inning}局 🚀 弱隊反超！"
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

        # KBO / NPB（Pinnacle）
        for lg in ["kbo", "npb"]:
            if config.get(lg):
                all_games.extend(await fetch_pinnacle_games(client, lg))

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
                log.info(f"[{game.league.upper()}] 比賽結束：{game.away_team} {game.away_score} @ {game.home_team} {game.home_score}")
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
    if not context.args:
        await update.message.reply_text("用法：/on mlb|kbo|npb")
        return
    lg = context.args[0].lower()
    if lg not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{lg}")
        return
    cfg = load_config()
    cfg[lg] = True
    save_config(cfg)
    await update.message.reply_text(f"✅ {lg.upper()} 監控已開啟")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/off mlb|kbo|npb")
        return
    lg = context.args[0].lower()
    if lg not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{lg}")
        return
    cfg = load_config()
    cfg[lg] = False
    save_config(cfg)
    await update.message.reply_text(f"⛔ {lg.upper()} 監控已關閉")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg   = load_config()
    state = load_state()

    lines = ["📊 棒球追蹤器狀態\n"]
    for lg in ["mlb", "kbo", "npb"]:
        icon = "✅" if cfg.get(lg) else "⛔"
        src  = "MLB Stats + Odds API" if lg == "mlb" else "Pinnacle Guest API"
        lines.append(f"{icon} {lg.upper()} ({src})")

    live = [gs for gs in state.values() if gs.get("status") == "live"]
    lines.append(f"\n🎮 進行中比賽：{len(live)} 場")
    for gs in live[:5]:
        ud  = gs.get("underdog", "")
        fav = gs.get("favorite", "")
        lg  = gs.get("league", "").upper()
        lines.append(f"  • [{lg}] {ud} vs {fav}")

    await update.message.reply_text("\n".join(lines))


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

        # KBO / NPB
        kbo_games: list[Game] = []
        npb_games: list[Game] = []
        if cfg.get("kbo"):
            kbo_games = await fetch_pinnacle_games(client, "kbo")
        if cfg.get("npb"):
            npb_games = await fetch_pinnacle_games(client, "npb")

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
            lines.append(f"  {time_str} {away_zh} @ {home_zh}{score_str} [{status_str}]{odds_str}")
        lines.append("")
        total += len(games)

    if total == 0:
        lines.append("今日暫無賽事資料")

    await update.message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手動查詢各聯盟目前比賽狀況，顯示是否已觸發條件。"""
    # 可指定聯盟：/check npb  或  /check（全部）
    cfg = load_config()
    target = context.args[0].lower() if context.args else None
    if target and target not in VALID_LEAGUES:
        await update.message.reply_text(f"❌ 未知聯盟：{target}，請用 mlb/kbo/npb")
        return

    state  = load_state()
    today  = date.today().isoformat()
    lines  = [f"🔍 即時觸發檢查（{today}）\n"]

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
        for lg in ["kbo", "npb"]:
            if lg in leagues:
                all_games.extend(await fetch_pinnacle_games(client, lg))

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
            lines.append(f"[{lg_tag}] {away_zh} {score_str} {home_zh} [{status_str}] — 無賠率，跳過")
            continue

        underdog, favorite, ud_odds, fav_odds, ud_is_home = sides
        ud_score  = game.home_score if ud_is_home else game.away_score
        fav_score = game.home_score if not ud_is_home else game.away_score

        gs = state.get(game.game_id, {})
        t1 = "✅" if gs.get("first_score_notified") else ("🔥" if ud_score > 0 and fav_score == 0 else "⬜")
        t2 = "✅" if gs.get("overtake_notified") else ("🚀" if gs.get("was_favorite_leading") and ud_score > fav_score else "⬜")

        ud_zh  = team_zh(underdog)
        fav_zh = team_zh(favorite)
        lines.append(
            f"[{lg_tag}] {away_zh} {score_str} {home_zh} [{status_str}]\n"
            f"  弱隊：{ud_zh}({fmt_odds(ud_odds)}) 強隊：{fav_zh}({fmt_odds(fav_odds)})\n"
            f"  {t1} 弱隊先得分  {t2} 弱隊反超"
        )

    if live_count == 0:
        lines.append("目前無進行中或已結束賽事")

    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚾ 棒球追蹤器指令\n\n"
        "/on mlb|kbo|npb  — 開啟聯盟監控\n"
        "/off mlb|kbo|npb — 關閉聯盟監控\n"
        "/status          — 目前狀態\n"
        "/today           — 今日全部賽程\n"
        "/check [聯盟]    — 即時查觸發狀況\n"
        "/help            — 顯示說明\n\n"
        "觸發條件：\n"
        "🔥 弱隊先得分（正號賠率隊率先得分）\n"
        "🚀 弱隊反超（強隊領先後被弱隊超越）\n\n"
        "資料來源：\n"
        "MLB → MLB Stats API + The Odds API\n"
        "KBO/NPB → Pinnacle Guest API（免費，無需帳號）"
    )
    await update.message.reply_text(text)


# ─── 啟動 ────────────────────────────────────────────────────────────────────
def main():
    log.info("⚾ 棒球追蹤器啟動中...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("on",     cmd_on))
    app.add_handler(CommandHandler("off",    cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("today",  cmd_today))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("help",   cmd_help))

    app.job_queue.run_repeating(monitor_cycle, interval=POLL_INTERVAL, first=10)

    log.info(f"Bot 啟動，每 {POLL_INTERVAL} 秒監控一次")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
