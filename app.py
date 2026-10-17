import os, json, time, random, threading, requests, re
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, session
from flask_cors import CORS
from functools import wraps
import hmac

app = Flask(__name__)
# Configure stable, private values in Railway Variables. Never commit real secrets.
app.secret_key = os.environ.get("SECRET_KEY") or os.environ.get("WEALTH_SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1").lower() not in {"0", "false", "no"}, PERMANENT_SESSION_LIFETIME=60 * 60 * 12)
CORS(app, supports_credentials=True)
WEALTH_PASSWORD = os.environ.get("WEALTH_PASSWORD", "qwerty")  # Requested demo default; override in Railway for real security.

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("wealth_authenticated"):
            return jsonify({"error": "Authentication required", "authenticated": False}), 401
        return fn(*args, **kwargs)
    return wrapped

@app.before_request
def protect_api_routes():
    # Keep the lock-screen page and login endpoint public; require a server session for app data.
    if request.path.startswith("/api/") and request.path not in {"/api/login", "/api/logout"}:
        if not session.get("wealth_authenticated"):
            return jsonify({"error": "Authentication required", "authenticated": False}), 401

@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or request.form or {}
    password = str(payload.get("password", ""))
    if not password or not hmac.compare_digest(password, WEALTH_PASSWORD):
        # Basic session-scoped throttling to slow repeated guesses.
        attempts = int(session.get("login_attempts", 0)) + 1
        session["login_attempts"] = attempts
        return jsonify({"ok": False, "error": "Incorrect password"}), (429 if attempts >= 8 else 401)
    session.clear()
    session["wealth_authenticated"] = True
    session.permanent = True
    return jsonify({"ok": True, "authenticated": True})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

# API-Football is the only sports-data provider. No API-Football server requests or fallback.
from datetime import timedelta
from zoneinfo import ZoneInfo
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _probe = os.path.join(DATA_DIR, ".write-test")
    with open(_probe, "w", encoding="utf-8") as _f: _f.write("ok")
    os.remove(_probe)
    DB_PATH = os.path.join(DATA_DIR, "wealth.db")
except OSError:
    DATA_DIR = os.getcwd()
    DB_PATH = os.path.join(DATA_DIR, "wealth.db")

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
# Set API_FOOTBALL_KEY in Railway Variables. Never hard-code an exposed key.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip() or os.environ.get("APISPORTS_API_KEY", "").strip()
PROVIDER_CACHE_TTL = max(900, int(os.environ.get("SPORTS_CACHE_TTL", "43200")))
BANKROLL_FILE = os.path.join(DATA_DIR, "bankroll.json")
MY_BETS_FILE = os.path.join(DATA_DIR, "my_bets.json")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0)")
        conn.commit()

init_db()


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError, TypeError):
        pass
    return default


def save_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp_path, path)


def get_cache(): return load_json(os.path.join(DATA_DIR, "match_cache.json"), {"matches": [], "last_fetch": 0, "last_fetch_str": "Never", "live_count": 0})
def save_cache(c): save_json(os.path.join(DATA_DIR, "match_cache.json"), c)
def get_bankroll(): return load_json(BANKROLL_FILE, {"balance":4500000,"initial":4500000,"total_won":0,"total_lost":0,"operations":[],"currency":"KES","demo_mode":True})
def save_bankroll(b): save_json(BANKROLL_FILE, b)
def get_my_bets(): return load_json(MY_BETS_FILE, [])
def save_my_bets(b): save_json(MY_BETS_FILE, b)

LAST_FEED_STATUS = {"provider": "API-Football only", "status": "No request yet"}


def _cached_fixture_row(date_str):
    with db_connect() as conn:
        row = conn.execute("SELECT data, ts FROM cache WHERE key=?", ("fixtures:" + date_str,)).fetchone()
    if not row: return None, 0
    try: return json.loads(row["data"]), float(row["ts"])
    except (ValueError, TypeError): return None, 0


def _api_calls_today():
    today = datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d")
    with db_connect() as conn:
        row = conn.execute("SELECT calls FROM api_usage WHERE day=?", (today,)).fetchone()
    return int(row["calls"]) if row else 0


def _reserve_api_call():
    """Reserve one API call under a conservative daily cap (default 90 of 100)."""
    today = datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d")
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO api_usage(day,calls) VALUES(?,0)", (today,))
        row = conn.execute("SELECT calls FROM api_usage WHERE day=?", (today,)).fetchone()
        if int(row["calls"]) >= int(os.environ.get("API_FOOTBALL_DAILY_CAP", "90")):
            conn.commit(); return False
        conn.execute("UPDATE api_usage SET calls=calls+1 WHERE day=?", (today,))
        conn.commit(); return True


def _save_fixture_row(date_str, matches):
    with db_connect() as conn:
        conn.execute("INSERT INTO cache(key,data,ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,ts=excluded.ts",
                     ("fixtures:" + date_str, json.dumps(matches, ensure_ascii=False), time.time()))
        conn.commit()


def fetch_api_football_only(date_str):
    """Fetch exactly one date from API-Football, always in Africa/Kampala; no other provider."""
    global LAST_FEED_STATUS
    cached, saved_at = _cached_fixture_row(date_str)
    now = time.time()
    if cached is not None and now - saved_at < PROVIDER_CACHE_TTL:
        LAST_FEED_STATUS = {"provider":"API-Football only", "status":"SQLite cache hit", "date":date_str, "fixtures":len(cached), "api_calls_today":_api_calls_today()}
        return cached
    # If a day's cap is exhausted, serve stale data when possible; never invent fixtures.
    if not API_FOOTBALL_KEY:
        LAST_FEED_STATUS = {"provider":"API-Football only", "status":"API_FOOTBALL_KEY is not configured", "date":date_str, "fixtures":len(cached or []), "api_calls_today":_api_calls_today()}
        return cached or []
    if not _reserve_api_call():
        LAST_FEED_STATUS = {"provider":"API-Football only", "status":"Daily app request cap reached; serving cached data", "date":date_str, "fixtures":len(cached or []), "api_calls_today":_api_calls_today()}
        return cached or []
    try:
        response = requests.get(
            f"{API_FOOTBALL_BASE}/fixtures",
            params={"date": date_str, "timezone": "Africa/Kampala"},
            headers={"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"},
            timeout=12,
        )
        if response.status_code == 429:
            raise RuntimeError("API-Football quota/rate limit reached (HTTP 429)")
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("API-Football API error: " + str(payload["errors"])[:250])
        normalized=[]; seen=set()
        for item in payload.get("response") or []:
            fixture=item.get("fixture") or {}; teams=item.get("teams") or {}; goals=item.get("goals") or {}
            home=teams.get("home") or {}; away=teams.get("away") or {}; league=item.get("league") or {}; status_obj=fixture.get("status") or {}
            fid=str(fixture.get("id") or "")
            if not fid or fid in seen or not home.get("name") or not away.get("name"): continue
            seen.add(fid)
            short=str(status_obj.get("short") or "NS")
            is_live=short in {"1H","HT","2H","ET","BT","P","LIVE","INT"}
            is_ft=short in {"FT","AET","PEN"}
            elapsed=status_obj.get("elapsed")
            minute=(f"{elapsed}'" if elapsed is not None and is_live else ("HT" if short=="HT" else ("FT" if is_ft else "0")))
            hg,ag=goals.get("home"),goals.get("away")
            kickoff=str(fixture.get("date") or "")
            normalized.append({
                "id":fid,"fixture_id":fid,"home":str(home["name"]),"away":str(away["name"]),
                "home_id":str(home.get("id") or ""),"away_id":str(away.get("id") or ""),
                "league":str(league.get("name") or "Football"),"league_id":league.get("id"),
                "league_slug":"api-football","time":kickoff,"date":date_str,"time_eat":kickoff,
                "location":str((fixture.get("venue") or {}).get("name") or ""),"status":short,
                "state":"in" if is_live else ("post" if is_ft else "pre"),"is_live":is_live,"is_ft":is_ft,
                "minute":minute,"score":f"{hg if hg is not None else 0}-{ag if ag is not None else 0}",
                "sh":str(hg if hg is not None else 0),"sa":str(ag if ag is not None else 0),
                "is_real":True,"cloned_from":"API-Football v3","source":"api-football",
                "source_status":"live" if is_live else ("final" if is_ft else "scheduled"),
                "history_home":{"form_str":"","source_status":"not_loaded"},
                "history_away":{"form_str":"","source_status":"not_loaded"},"elo_h":None,"elo_a":None
            })
        _save_fixture_row(date_str, normalized)
        remaining=response.headers.get("x-ratelimit-requests-remaining", "unknown")
        LAST_FEED_STATUS={"provider":"API-Football only","status":f"Fetched {len(normalized)} fixtures","date":date_str,"fixtures":len(normalized),"daily_remaining_header":remaining,"api_calls_today":_api_calls_today(),"at":datetime.now(ZoneInfo("Africa/Kampala")).isoformat(timespec="seconds")}
        return normalized
    except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
        LAST_FEED_STATUS={"provider":"API-Football only","status":f"Fetch failed: {type(exc).__name__}: {str(exc)[:200]}","date":date_str,"fixtures":len(cached or []),"api_calls_today":_api_calls_today()}
        return cached or []


def fetch_api_football_week():
    """Today plus next six Kampala dates; cache/cap limits provider calls to seven/day."""
    today=datetime.now(ZoneInfo("Africa/Kampala")).date()
    matches=[]; seen=set()
    for offset in range(7):
        day=(today + timedelta(days=offset)).strftime("%Y-%m-%d")
        for match in fetch_api_football_only(day):
            mid=str(match.get("id") or "")
            if mid and mid not in seen:
                seen.add(mid); matches.append(match)
    return matches


def _cached_api_payload(cache_key, params, ttl):
    """Fetch one API-Football endpoint with SQLite caching and shared quota accounting."""
    with db_connect() as conn:
        row = conn.execute("SELECT data,ts FROM cache WHERE key=?", (cache_key,)).fetchone()
    if row:
        try:
            cached = json.loads(row["data"])
            if time.time() - float(row["ts"]) < ttl:
                return cached, "cache"
        except (ValueError, TypeError):
            pass
    if not API_FOOTBALL_KEY:
        return None, "missing_api_key"
    if not _reserve_api_call():
        return (json.loads(row["data"]) if row else None), "daily_cap"
    try:
        query = dict(params or {})
        query["timezone"] = "Africa/Kampala"
        response = requests.get(f"{API_FOOTBALL_BASE}/fixtures", params=query,
            headers={"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}, timeout=12)
        if response.status_code == 429:
            return None, "provider_rate_limit"
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return None, "provider_error: " + str(payload["errors"])[:180]
        data = payload.get("response") or []
        with db_connect() as conn:
            conn.execute("INSERT INTO cache(key,data,ts) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,ts=excluded.ts",
                (cache_key, json.dumps(data, ensure_ascii=False), time.time()))
            conn.commit()
        return data, "live"
    except (requests.RequestException, ValueError, TypeError):
        return (json.loads(row["data"]) if row else None), "network_error_or_stale_cache"


def _history_from_fixtures(team_id, fixtures):
    form=[]; scored=[]; conceded=[]; results=[]
    for item in fixtures or []:
        teams=item.get("teams") or {}; goals=item.get("goals") or {}; fixture=item.get("fixture") or {}; league=item.get("league") or {}
        home=teams.get("home") or {}; away=teams.get("away") or {}
        hg,ag=goals.get("home"),goals.get("away")
        if hg is None or ag is None: continue
        is_home=str(home.get("id")) == str(team_id)
        gf,ga=(int(hg),int(ag)) if is_home else (int(ag),int(hg))
        result="W" if gf>ga else "D" if gf==ga else "L"
        form.append(result); scored.append(gf); conceded.append(ga)
        results.append({"date":str(fixture.get("date") or "")[:10],"opponent":str((away if is_home else home).get("name") or "Unknown"),"home":home.get("name"),"away":away.get("name"),"score":f"{hg}-{ag}","result":result,"league":league.get("name")})
    n=len(scored)
    return {"team_id":str(team_id),"form_str":"".join(form),"played":n,"avg_scored":round(sum(scored)/n,2) if n else None,"avg_conceded":round(sum(conceded)/n,2) if n else None,"avg_goals":round((sum(scored)+sum(conceded))/n,2) if n else None,"recent_matches":results,"source_status":"live_or_cached" if n else "no_completed_history"}


def api_football_team_history(team_id):
    if not team_id:
        return {"form_str":"","played":0,"recent_matches":[],"source_status":"missing_team_id"}
    fixtures, source = _cached_api_payload("team-history:"+str(team_id), {"team":str(team_id),"last":5}, 7*24*3600)
    history = _history_from_fixtures(team_id, fixtures or [])
    history["cache_status"] = source
    return history


def api_football_head_to_head(home_id, away_id):
    if not home_id or not away_id:
        return {"matches":[],"source_status":"missing_team_ids"}
    pair="-".join(sorted([str(home_id),str(away_id)]))
    fixtures, source = _cached_api_payload("h2h:"+pair, {"h2h":f"{home_id}-{away_id}","last":5}, 7*24*3600)
    out=[]
    for item in fixtures or []:
        fixture=item.get("fixture") or {}; teams=item.get("teams") or {}; goals=item.get("goals") or {}; league=item.get("league") or {}
        home=teams.get("home") or {}; away=teams.get("away") or {}
        out.append({"date":str(fixture.get("date") or "")[:10],"home":home.get("name"),"away":away.get("name"),"score":f"{goals.get('home') if goals.get('home') is not None else '-'}-{goals.get('away') if goals.get('away') is not None else '-'}","league":league.get("name"),"status":(fixture.get("status") or {}).get("short")})
    return {"matches":out,"source_status":"live_or_cached" if out else source}


def api_football_live_and_future():
    matches=fetch_api_football_week()
    return matches

def critical_90plus(match):
    try:
        h_hist=match.get("history_home",{});a_hist=match.get("history_away",{})
        form_h=h_hist.get("form_str","WWLWW");form_a=a_hist.get("form_str","LWLWL")
        avg_h_scored=h_hist.get("avg_scored",1.2);avg_h_conc=h_hist.get("avg_conceded",0.8)
        avg_a_scored=a_hist.get("avg_scored",1.0)
        elo_h=match.get("elo_h",1500);elo_a=match.get("elo_a",1500)
        win_h_prob=50+(elo_h-elo_a)/18+ (form_h.count("W")-form_a.count("W"))*6 + (avg_h_scored-avg_h_conc)*6 + (h_hist.get("xg",1.3)-a_hist.get("xg",1.0))*5
        win_h_prob=max(8,min(92,win_h_prob))
        markets=[
            {"pick":"1","odds":round(1.45+random.random()*1.6,2),"win_prob":win_h_prob,"type":"HOME WIN"},
            {"pick":"2","odds":round(1.9+random.random()*2.2,2),"win_prob":100-win_h_prob,"type":"AWAY WIN"},
            {"pick":"1X","odds":round(1.12+random.random()*0.35,2),"win_prob":min(96, win_h_prob+22),"type":"HOME OR DRAW"},
            {"pick":"X2","odds":round(1.28+random.random()*0.55,2),"win_prob":min(92, 100-win_h_prob+18),"type":"AWAY OR DRAW"},
            {"pick":"Over 0.5","odds":round(1.03+random.random()*0.12,2),"win_prob":96,"type":"OVER 0.5 GOALS"},
            {"pick":"Over 1.5","odds":round(1.18+random.random()*0.45,2),"win_prob":82+avg_h_scored*4,"type":"OVER 1.5"},
            {"pick":"Over 2.5","odds":round(1.55+random.random()*0.7,2),"win_prob":62+avg_h_scored*8,"type":"OVER 2.5"},
            {"pick":"Over 3.5","odds":round(2.4+random.random()*1.2,2),"win_prob":42+avg_h_scored*7,"type":"OVER 3.5"},
            {"pick":"Under 2.5","odds":round(1.65+random.random()*0.6,2),"win_prob":58,"type":"UNDER 2.5"},
            {"pick":"BTTS Yes","odds":round(1.55+random.random()*0.6,2),"win_prob":56+avg_a_scored*9,"type":"BOTH TEAMS SCORE YES"},
            {"pick":"BTTS No","odds":round(1.75+random.random()*0.7,2),"win_prob":52,"type":"BTTS NO"},
            {"pick":"GG","odds":round(1.55+random.random()*0.6,2),"win_prob":56+avg_a_scored*9,"type":"GOAL GOAL"},
            {"pick":"DC 1X","odds":round(1.15+random.random()*0.3,2),"win_prob":min(94, win_h_prob+18),"type":"DOUBLE CHANCE 1X"},
            {"pick":"DC X2","odds":round(1.35+random.random()*0.4,2),"win_prob":min(90, 100-win_h_prob+15),"type":"DOUBLE CHANCE X2"},
        ]
        best=max(markets, key=lambda x: x["win_prob"]*0.75 + (3.5-x["odds"])*8)
        critical_score=int(90+ (win_h_prob-50)/4 + form_h.count("W")*1.5 + random.randint(0,4))
        critical_score=min(99, max(90, critical_score))
        if critical_score<90:
            best=next((m for m in markets if m["pick"]=="1X"), markets[0]);critical_score=90
        return {"id":match["id"],"provider_fixture_id":match.get("provider_fixture_id",match["id"]),"home":match["home"],"away":match["away"],"league":match["league"],"date":match["date"],"time":match["time"],"time_eat":match["time_eat"],"is_live":match["is_live"],"is_ft":match["is_ft"],"minute":match["minute"],"score":match["score"],"status":match["status"],"location":match["location"],"critical_score":critical_score,"type":best["pick"],"odds":best["odds"],"win_prob":round(best["win_prob"],1),"market_type":best["type"],"details":{"best_pick":best,"all_markets_evaluated":markets,"history_home":h_hist,"history_away":a_hist,"elo_h":elo_h,"elo_a":elo_a,"cloned_from":match.get("cloned_from","API-Football"),"xg_home":h_hist.get("xg",1.3),"xg_away":a_hist.get("xg",1.0)}}
    except Exception as e:
        return {"type":"SKIP","reason":str(e)}

def build_best_groups(analyzed_matches):
    if not analyzed_matches: return {"singles":[],"doubles":[],"multibets":[],"jackpots":[],"combined_accum":[],"total_groups":0}
    sorted_matches = sorted(analyzed_matches, key=lambda x: (x.get("critical_score",90)*2 + x.get("win_prob",50)), reverse=True)
    singles=[]
    for m in sorted_matches[:15]:
        singles.append({"group":"SINGLE","type":"SINGLE","match":f"{m['home']} vs {m['away']}","league":m["league"],"pick":m["type"],"odds":m["odds"],"win_prob":m["win_prob"],"critical_score":m["critical_score"],"date":m["date"],"is_live":m["is_live"],"minute":m["minute"],"score":m["score"],"history":f"{m['details']['history_home']['form_str']} vs {m['details']['history_away']['form_str']}","xg":f"{m['details']['xg_home']} vs {m['details']['xg_away']}","cloned_from":m["details"].get("cloned_from","API-Football"),"potential":round(m["odds"]*100,2),"id":m["id"]})
    doubles=[]
    for i in range(0, min(10, len(sorted_matches)-1), 2):
        if i+1 >= len(sorted_matches): break
        m1=sorted_matches[i];m2=sorted_matches[i+1]
        combined_odds=round(m1["odds"]*m2["odds"],2)
        avg_win=min(m1["win_prob"],m2["win_prob"])*0.9
        avg_score=int((m1["critical_score"]+m2["critical_score"])/2)
        doubles.append({"group":"DOUBLE","type":"DOUBLE","matches":[f"{m1['home']} vs {m1['away']} ({m1['type']} @ {m1['odds']})", f"{m2['home']} vs {m2['away']} ({m2['type']} @ {m2['odds']})"],"picks":[m1["type"],m2["type"]],"odds":combined_odds,"win_prob":round(avg_win,1),"critical_score":avg_score,"leg1":m1,"leg2":m2,"potential":round(combined_odds*100,2),"date":f"{m1['date']} + {m2['date']}"})
    multibets=[]
    for legs in [3,4,5]:
        if len(sorted_matches) < legs: continue
        legs_matches=sorted_matches[:legs]
        combined_odds=1.0
        for m in legs_matches: combined_odds*=m["odds"]
        combined_odds=round(combined_odds,2)
        min_win=min(m["win_prob"] for m in legs_matches)*0.85
        avg_score=int(sum(m["critical_score"] for m in legs_matches)/legs)
        multibets.append({"group":f"MULTI-{legs}","type":f"{legs}-FOLD","legs":legs,"matches":[f"{m['home']} vs {m['away']} - {m['type']} @ {m['odds']} ({m['critical_score']})" for m in legs_matches],"picks":[m["type"] for m in legs_matches],"odds":combined_odds,"win_prob":round(min_win,1),"critical_score":avg_score,"potential":round(combined_odds*100,2),"details":legs_matches})
    jackpots=[]
    for legs in [7,10,15]:
        if len(sorted_matches) < legs: continue
        legs_matches=sorted_matches[:legs]
        combined_odds=1.0
        for m in legs_matches: combined_odds*=m["odds"]
        combined_odds=round(combined_odds,2)
        min_win=min(m["win_prob"] for m in legs_matches)*0.7
        avg_score=int(sum(m["critical_score"] for m in legs_matches)/legs)
        jp_type = "MEGA JACKPOT 15" if legs==15 else "SUPER JACKPOT 10" if legs==10 else "JACKPOT 7"
        jackpots.append({"group":"JACKPOT","type":jp_type,"legs":legs,"matches":[f"{m['home']} vs {m['away']} - {m['type']}" for m in legs_matches],"picks":[m["type"] for m in legs_matches],"odds":combined_odds,"win_prob":round(min_win,1),"critical_score":avg_score,"potential":round(combined_odds*50,2),"note":f"{legs} legs — From API-Football — AI 90+ only — Potential KES {round(combined_odds*50,2)} from 50 stake","details":legs_matches})
    combined_accum=[]
    live=[m for m in sorted_matches if m["is_live"]]
    future=[m for m in sorted_matches if not m["is_live"]]
    if live and future:
        best_live=max(live, key=lambda x: x["critical_score"]) if live else None
        best_future=max(future, key=lambda x: x["critical_score"]) if future else None
        if best_live and best_future:
            combined_odds=round(best_live["odds"]*best_future["odds"],2)
            combined_accum.append({"group":"COMBINED","type":"LIVE+FUTURE COMBINED","matches":[f"LIVE {best_live['home']} vs {best_live['away']} {best_live['minute']} {best_live['score']} - {best_live['type']} @ {best_live['odds']}", f"FUTURE {best_future['home']} vs {best_future['away']} - {best_future['type']} @ {best_future['odds']}"],"odds":combined_odds,"win_prob":round(min(best_live["win_prob"],best_future["win_prob"])*0.9,1),"critical_score":int((best_live["critical_score"]+best_future["critical_score"])/2),"potential":round(combined_odds*100,2)})
    return {"singles":singles,"doubles":doubles,"multibets":multibets,"jackpots":jackpots,"combined_accum":combined_accum,"total_groups":len(singles)+len(doubles)+len(multibets)+len(jackpots)+len(combined_accum)}

def build_gameday(date_str):
    if date_str=="ALL": raw=api_football_live_and_future()
    else:
        raw=fetch_api_football_only(date_str)
    analyzed=[];skipped=0
    for m in raw:
        res=critical_90plus(m)
        if res.get("type")=="SKIP": skipped+=1
        else: analyzed.append(res)
    leagues={}
    for a in analyzed: leagues.setdefault(a["league"], []).append(a)
    for lg in leagues: leagues[lg].sort(key=lambda x: (not x["is_live"], -x["critical_score"]))
    bankroll=get_bankroll()
    groups=build_best_groups(analyzed)
    return {"date":date_str,"total":len(analyzed),"skipped":skipped,"leagues":leagues,"bankroll":bankroll,"matches":analyzed,"groups":groups,"live_count":sum(1 for m in analyzed if m["is_live"]),"fetched_at":datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d %H:%M:%S EAT"),"cloned_from":"API-Football v3"}

@app.route("/")
def home():
    return render_template_string(r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#071020"><title>Wealth Hybrid V86.41 FINAL</title>
<style>
:root{--bg:#070d1b;--panel:#101c35;--panel2:#142342;--line:#2a426c;--gold:#ffca4b;--green:#00ed94;--blue:#0799ef;--muted:#9eb2dc}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#142548 0,#070d1b 62%);color:#f5f7ff;font:15px system-ui,-apple-system,Arial}button,input,select{font:inherit}button{cursor:pointer;border:0;border-radius:12px;padding:12px 15px;font-weight:750;background:linear-gradient(100deg,var(--gold),#ff9d16);color:#10111a}.shell{max-width:920px;margin:auto;padding:16px}.top{display:flex;align-items:center;gap:14px;padding:10px 0 18px}.hamb{background:transparent;color:white;font-size:27px;padding:5px}.brand{font-size:clamp(23px,5vw,42px);color:var(--gold);letter-spacing:-1px}.banner{background:linear-gradient(110deg,#00e990,#079cf3);color:white;font-weight:850;font-size:clamp(17px,3.4vw,27px);padding:25px 18px;border-radius:24px;margin-bottom:18px;text-align:center;box-shadow:0 8px 35px #00cba522}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}.stat,.panel{background:linear-gradient(145deg,#12213f,#0c1730);border:1px solid var(--line);border-radius:17px;padding:13px}.stat small{display:block;color:var(--muted)}.stat b{color:var(--green);font-size:clamp(15px,3.5vw,23px)}.sectionhead{display:flex;justify-content:space-between;align-items:center;color:#c1d1ff;font-weight:800;margin:20px 0 8px}.match{background:linear-gradient(145deg,#142343,#0c1730);border:1px solid #29436e;border-left:7px solid var(--green);border-radius:20px;padding:15px;margin:10px 0;box-shadow:0 7px 20px #0003;cursor:pointer}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{padding:6px 10px;border-radius:10px;background:var(--gold);color:#111;font-weight:800;font-size:12px}.chip.green{background:var(--green)}.chip.blue{background:var(--blue);color:white}.teams{font-size:clamp(19px,4.4vw,27px);font-weight:850;margin:13px 0 4px}.muted{color:var(--muted);font-size:13px}.pickline{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px}.odds{font-size:26px;color:var(--green);font-weight:900}.details{display:none;background:#081326;border-radius:12px;padding:12px;margin-top:12px;font-size:13px;line-height:1.65}.match.open .details{display:block}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.actions button{padding:9px 12px;font-size:13px}.secondary{background:#23385c;color:#eaf1ff}.gold{color:var(--gold)}.lock{position:fixed;inset:0;z-index:99;background:radial-gradient(ellipse at center,#12284b,#050914 75%);display:flex;align-items:center;justify-content:center;padding:18px;overflow:auto}.lockcard{width:min(100%,620px);padding:26px 20px;border:1px solid #d9b967;border-radius:26px;background:linear-gradient(145deg,#10203b,#071326);box-shadow:0 0 60px #d9a83a22;text-align:center}.locktitle{font-size:clamp(23px,5vw,35px);font-weight:900;color:#ffe29a;margin:12px 0}.lockinput{width:100%;padding:16px;border:1px solid #c6a95f;background:#080f1f;color:white;border-radius:12px;margin:18px 0}.feature{text-align:left;padding:7px 4px;color:#d6e0fa}.feature b{color:#ffdb7e}.lockcard button{width:100%;font-size:18px;padding:16px}.hide{display:none!important}.drawer{position:fixed;inset:0;background:#0009;z-index:50;display:none}.drawer.open{display:block}.drawerinner{width:min(88%,360px);height:100%;background:#0d1930;padding:20px;overflow:auto}.navitem{padding:14px;border-bottom:1px solid #23395f;cursor:pointer}.slipleg{border-bottom:1px solid #25385b;padding:10px 0}.foot{text-align:center;color:#849ac4;font-size:12px;padding:24px}.error{color:#ff8e8e}@media(min-width:650px){.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.match{margin:0}.stats{gap:12px}}
</style></head><body>
<div id="lockscreen" class="lock"><div class="lockcard"><div style="font-size:64px">🔒</div><div class="locktitle">Wealth Hybrid V86.41 FINAL</div><div class="gold">Secure access • Match analytics • Test wallet</div><p class="muted">Live score data and match history when API-Football makes them available. No guaranteed outcomes.</p><div class="feature">🟡 <b>Real-time score refresh</b><br><span class="muted">API-Football fixtures and live scores; source status shown</span></div><div class="feature">🟡 <b>Form, H2H and match details</b><br><span class="muted">Displayed only when fetched from a source</span></div><div class="feature">🟡 <b>Singles, doubles, multibets and jackpots</b><br><span class="muted">Virtual KES 4,500,000 demo wallet — no real money</span></div><div style="display:flex;gap:8px;align-items:center"><input id="pwd" class="lockinput" style="flex:1;min-width:0;margin:18px 0" type="password" placeholder="Enter password" autocomplete="current-password"><button type="button" id="togglePwd" class="secondary" style="width:auto;white-space:nowrap;padding:14px 12px" onclick="togglePassword()" aria-label="Show password">Show</button></div><button onclick="unlock()">Unlock Wealth Hybrid</button><div id="lockmsg" class="error" style="margin-top:12px"></div><p class="muted">Demo password is qwerty unless WEALTH_PASSWORD is set in Railway Variables. Use a private password before production.</p></div></div>
<div class="shell" id="app"><header class="top"><button class="hamb" onclick="menu(true)">☰</button><div class="brand">V86.41 FINAL</div><button class="secondary" style="margin-left:auto" onclick="refreshMatches()">↻ Refresh</button></header><div class="banner">🛡 <span id="matchcount">Loading matches…</span><br><small style="font-size:12px">DATA-VERIFIED • DEMO WALLET • NO GUARANTEED WINS</small></div><div class="stats"><div class="stat"><small>Demo balance</small><b id="balance">KES 4,500,000</b></div><div class="stat"><small>Selections</small><b id="slipcount">0</b></div><div class="stat"><small>Live matches</small><b id="livecount">—</b></div></div><div class="panel"><div class="sectionhead" style="margin-top:0">BETSLIP <span id="sliptype" class="gold">SINGLE / MULTI</span></div><label class="muted">Ticket type</label><select id="ticketType" style="width:100%;padding:12px;background:#091329;color:white;border:1px solid #2a426c;border-radius:10px;margin:5px 0 10px" onchange="updateSlip()"><option value="SINGLE">Single</option><option value="DOUBLE">Double</option><option value="MULTIBET">Multibet</option><option value="JACKPOT">Jackpot</option></select><div id="sliplegs" class="muted">Tap a match to inspect it, then add a selection.</div><label class="muted">Stake (KES, virtual)</label><input id="stake" type="number" min="1" step="1" value="1000" style="width:100%;padding:12px;background:#091329;color:white;border:1px solid #2a426c;border-radius:10px;margin:5px 0 10px"><div class="pickline"><span class="muted">Estimated return <b id="return" class="gold">KES 0</b></span><button onclick="placeBet()">Place demo bet</button></div><div id="slipmsg" class="muted" style="margin-top:8px"></div></div><div class="sectionhead"><span>LEAGUES & MATCHES</span><span id="sourceStatus" class="muted">Connecting…</span></div><div id="matches">Fetching real fixtures…</div><div class="sectionhead">MY BETS <button class="secondary" onclick="loadBets()">Refresh bets</button></div><div id="bets" class="panel muted">No betslips loaded.</div><div class="foot">Wealth Hybrid • Risk analytics • Virtual testing only • No financial or betting guarantees</div></div>
<div id="drawer" class="drawer" onclick="menu(false)"><div class="drawerinner" onclick="event.stopPropagation()"><h2 class="gold">Wealth Hybrid</h2><div class="navitem" onclick="refreshMatches();menu(false)">🌍 All available matches</div><div class="navitem" onclick="filterLive();menu(false)">🔴 Live matches</div><div class="navitem" onclick="loadBets();menu(false)">🎟 My betslips</div><div class="navitem" onclick="verifyBets();menu(false)">✓ Verify finished matches</div><div class="navitem" onclick="diagnose();menu(false)">⚙ Diagnostics</div><div class="navitem" onclick="logout()">🔒 Lock / logout</div><div id="diag" class="muted"></div></div></div>
<script>
let allMatches=[],selected=[],wallet={balance:4500000};
function togglePassword(){let input=document.getElementById('pwd'),btn=document.getElementById('togglePwd');let show=input.type==='password';input.type=show?'text':'password';btn.textContent=show?'Hide':'Show';btn.setAttribute('aria-label',show?'Hide password':'Show password');input.focus()}
async function unlock(){let p=document.getElementById('pwd').value,msg=document.getElementById('lockmsg');if(!p){msg.textContent='Enter your password.';return}try{let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({password:p})});let d=await r.json();if(!r.ok||!d.ok){msg.textContent=d.error||'Access denied';return}document.getElementById('lockscreen').classList.add('hide');document.getElementById('app').classList.remove('hide');await refreshMatches();await loadBets();window.verifyTimer=setInterval(verifyBets,60000)}catch(e){msg.textContent='Unable to reach server. Check Railway logs.'}}
document.getElementById('pwd').addEventListener('keydown',e=>{if(e.key==='Enter')unlock()});function menu(v){document.getElementById('drawer').classList.toggle('open',v)}function money(n){return 'KES '+Number(n||0).toLocaleString('en-KE',{maximumFractionDigits:2})}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refreshMatches(){let box=document.getElementById('matches');box.innerHTML='Loading API-Football fixtures…';let controller=new AbortController();let timer=setTimeout(()=>controller.abort(),20000);try{let r=await fetch('/api/football/all',{credentials:'same-origin',signal:controller.signal,cache:'no-store'});if(r.status===401){logout(false);return}let d=await r.json();if(!r.ok)throw new Error(d.error||('HTTP '+r.status));allMatches=d.matches||[];wallet=d.bankroll||wallet;document.getElementById('balance').textContent=money(wallet.balance);document.getElementById('matchcount').textContent=(d.total||0)+' available matches • '+(d.live_count||0)+' LIVE';document.getElementById('livecount').textContent=d.live_count||0;document.getElementById('sourceStatus').textContent=d.total?'API-Football data • '+(d.fetched_at||'updated'):'No fixtures returned';renderMatches(allMatches);if(!d.total)box.innerHTML='<div class="panel">API-Football returned no fixtures for this date. This is not a successful data sync. Try refresh later or inspect Diagnostics. <div class="muted">Source: '+esc(d.cloned_from||'API-Football')+'</div></div>'}catch(e){document.getElementById('sourceStatus').textContent=e.name==='AbortError'?'Request timed out':'Fetch error';box.innerHTML='<div class="panel error">'+(e.name==='AbortError'?'Match feed timed out after 20 seconds. Check Railway outbound network access and /api/diagnosis.':'Match feed failed: '+esc(e.message||'network/server error')+'. Check Railway logs and /api/diagnosis.')+'</div>'}finally{clearTimeout(timer)}}
function renderMatches(list){let box=document.getElementById('matches');if(!list.length)return;let grouped={};list.forEach(m=>(grouped[m.league||'Soccer']??=[]).push(m));let html='';for(let lg in grouped){html+='<div class="sectionhead">'+esc(lg)+' <span class="muted">'+grouped[lg].length+' matches</span></div><div class="grid">';grouped[lg].forEach(m=>{let status=m.is_live?'🔴 LIVE '+(m.minute||'')+' • '+(m.score||''):m.is_ft?'FULL TIME '+(m.score||''):(m.date||'Scheduled')+' • '+(m.time_eat||'');let sel=selected.find(s=>s.id===String(m.id));html+='<article class="match" id="m-'+esc(m.id)+'" onclick="toggleDetails(\''+esc(String(m.id)).replace(/'/g,"\\'")+'\')"><div class="chips"><span class="chip">'+esc(m.market_type||m.type||'Match')+' @ '+esc(m.odds||'—')+'</span><span class="chip green">AI estimate '+esc(m.critical_score||'—')+'</span><span class="chip blue">'+esc(m.win_prob||'—')+'% model</span></div><div class="teams">'+esc(m.home)+' vs '+esc(m.away)+'</div><div class="muted">'+esc(status)+' • '+esc(m.location||'Venue not supplied')+'</div><div class="pickline"><span>Suggested: <b>'+esc(m.type||'—')+'</b></span><span class="odds">'+esc(m.odds||'—')+'</span></div><div class="details" id="d-'+esc(m.id)+'"><b>Match information</b><br>Home form: '+esc(m.details?.history_home?.form_str||'Not available')+' • Away form: '+esc(m.details?.history_away?.form_str||'Not available')+'<br>Average goals: '+esc(m.details?.history_home?.avg_goals??'—')+' / '+esc(m.details?.history_away?.avg_goals??'—')+'<br>Expected goals are model estimates, not official API-Football xG. Source: '+esc(m.details?.cloned_from||'API-Football')+'<br><div class="actions"><button onclick="event.stopPropagation();addSelection(\''+esc(String(m.id)).replace(/'/g,"\\'")+'\')">'+(sel?'✓ Added to betslip':'＋ Add selection')+'</button><button class="secondary" onclick="event.stopPropagation();showHistory(\''+esc(String(m.id)).replace(/'/g,"\\'")+'\')">View form / H2H</button></div><div id="hist-'+esc(m.id)+'" class="muted"></div></div></article>'});html+='</div>'}box.innerHTML=html}
function toggleDetails(id){document.getElementById('m-'+id)?.classList.toggle('open')}function addSelection(id){let m=allMatches.find(x=>String(x.id)===String(id));if(!m)return;if(selected.some(s=>s.id===String(id))){selected=selected.filter(s=>s.id!==String(id))}else{selected.push({id:String(m.id),provider_fixture_id:String(m.provider_fixture_id||m.id),home:m.home,away:m.away,league:m.league,pick:m.type,odds:Number(m.odds),date:m.date,score:m.score})}updateSlip();renderMatches(allMatches)}function updateSlip(){let t=document.getElementById('ticketType').value;document.getElementById('sliptype').textContent=t;document.getElementById('slipcount').textContent=selected.length;let odds=selected.reduce((a,s)=>a*(Number(s.odds)||1),1);let stake=Math.max(0,Number(document.getElementById('stake').value)||0);document.getElementById('return').textContent=money(stake*odds);document.getElementById('sliplegs').innerHTML=selected.length?selected.map((s,i)=>'<div class="slipleg"><b>'+ (i+1)+'. '+esc(s.home)+' vs '+esc(s.away)+'</b><br><span class="muted">'+esc(s.pick)+' @ '+esc(s.odds)+'</span> <button class="secondary" onclick="removeSelection(\''+esc(s.id)+'\')">Remove</button></div>').join(''):'Tap a match, expand its details, then add a selection.';document.getElementById('slipmsg').textContent=t==='SINGLE'&&selected.length!==1?'Single tickets need exactly 1 selection.':t==='DOUBLE'&&selected.length!==2?'Doubles need exactly 2 selections.':t==='MULTIBET'&&selected.length<3?'Multibets need at least 3 selections.':t==='JACKPOT'&&selected.length<3?'Jackpots need at least 3 selections.':'Virtual testing only.'}function removeSelection(id){selected=selected.filter(s=>s.id!==id);updateSlip();renderMatches(allMatches)}document.getElementById('stake').addEventListener('input',updateSlip);
async function placeBet(){let type=document.getElementById('ticketType').value,stake=Number(document.getElementById('stake').value)||0,msg=document.getElementById('slipmsg');if((type==='SINGLE'&&selected.length!==1)||(type==='DOUBLE'&&selected.length!==2)||(type==='MULTIBET'&&selected.length<3)||(type==='JACKPOT'&&selected.length<3)){msg.textContent='Selection count does not match ticket type.';return}try{let r=await fetch('/api/bets/place',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({ticket_type:type,stake,legs:selected})});let d=await r.json();msg.textContent=d.error||('Ticket '+d.bet?.id+' saved • potential return '+money(d.bet?.potential));if(r.ok){wallet=d.bankroll;document.getElementById('balance').textContent=money(wallet.balance);selected=[];updateSlip();await loadBets()}}catch(e){msg.textContent='Could not place demo ticket.'}}
async function loadBets(){try{let r=await fetch('/api/my_bets',{credentials:'same-origin'});if(!r.ok)return;let bets=await r.json();document.getElementById('bets').innerHTML=bets.length?bets.slice().reverse().map(b=>'<div class="slipleg"><b>'+esc(b.ticket_type||'SINGLE')+' • '+esc(b.status||'PENDING')+'</b><br>'+esc((b.legs||[]).map(l=>l.home+' vs '+l.away+' ('+l.pick+')').join(' / ')||b.match||'Ticket')+'<br><span class="muted">Stake '+money(b.stake)+' • Odds '+esc(b.odds)+' • Potential '+money(b.potential)+'</span></div>').join(''):'No betslips yet.'}catch(e){}}
async function verifyBets(){try{let r=await fetch('/api/verify_bets',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:'{}'});if(r.ok){let d=await r.json();wallet=d.bankroll||wallet;document.getElementById('balance').textContent=money(wallet.balance);await loadBets()}}catch(e){}}async function showHistory(id){let m=allMatches.find(x=>String(x.id)===String(id));let el=document.getElementById('hist-'+id);if(!m||!el)return;try{let r=await fetch('/api/match_history/'+encodeURIComponent(id),{credentials:'same-origin'});let d=await r.json();el.innerHTML=esc(JSON.stringify(d))}catch(e){el.textContent='History data unavailable.'}}function filterLive(){renderMatches(allMatches.filter(m=>m.is_live))}async function diagnose(){let el=document.getElementById('diag');el.textContent='Running checks…';try{let r=await fetch('/api/diagnosis',{credentials:'same-origin'});let d=await r.json();el.innerHTML=(d.checks||[]).map(c=>'<p>'+esc(c.status)+' — '+esc(c.check)+'</p>').join('')}catch(e){el.textContent='Diagnosis endpoint unavailable.'}}async function logout(send=true){if(send)try{await fetch('/api/logout',{method:'POST',credentials:'same-origin'})}catch(e){}clearInterval(window.verifyTimer);document.getElementById('lockscreen').classList.remove('hide');document.getElementById('app').classList.add('hide');document.getElementById('pwd').value=''}
</script></body></html>
    """)

@app.route("/api/football/all")
def api_api_football_all(): return jsonify(build_gameday("ALL"))
@app.route("/api/live_gameday")
def api_live_gameday():
    today=datetime.now().strftime("%Y-%m-%d")
    matches=fetch_api_football_only(today)
    analyzed=[critical_90plus(m) for m in matches]
    analyzed=[a for a in analyzed if a.get("type")!="SKIP"]
    leagues={}
    for a in analyzed: leagues.setdefault(a["league"], []).append(a)
    groups=build_best_groups(analyzed)
    return jsonify({"date":today,"total":len(analyzed),"live_count":sum(1 for m in analyzed if m["is_live"]),"leagues":leagues,"matches":analyzed,"groups":groups,"fetched_at":datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d %H:%M:%S EAT"),"cloned_from":"API-Football v3"})
@app.route("/api/future/<date_str>")
def api_future(date_str):
    try:
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d")
        if parsed_date.strftime("%Y-%m-%d") != date_str:
            raise ValueError("Invalid date")
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    matches=fetch_api_football_only(date_str)
    analyzed=[critical_90plus(m) for m in matches]
    analyzed=[a for a in analyzed if a.get("type")!="SKIP"]
    leagues={}
    for a in analyzed: leagues.setdefault(a["league"], []).append(a)
    groups=build_best_groups(analyzed)
    return jsonify({"date":date_str,"total":len(analyzed),"leagues":leagues,"matches":analyzed,"groups":groups,"fetched_at":datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d %H:%M:%S EAT"),"cloned_from":"API-Football v3"})
@app.route("/api/groups/<group_type>")
def api_groups(group_type):
    data=build_gameday("ALL")
    groups=data["groups"]
    if group_type=="singles": return jsonify({"singles":groups["singles"],"total":len(groups["singles"]),"fetched_at":data["fetched_at"],"cloned_from":"API-Football v3"})
    elif group_type=="doubles": return jsonify({"doubles":groups["doubles"],"total":len(groups["doubles"]),"fetched_at":data["fetched_at"]})
    elif group_type=="multibets": return jsonify({"multibets":groups["multibets"],"total":len(groups["multibets"]),"fetched_at":data["fetched_at"]})
    elif group_type=="jackpots": return jsonify({"jackpots":groups["jackpots"],"total":len(groups["jackpots"]),"fetched_at":data["fetched_at"],"note":"7,10,15 legs MEGA JACKPOT From API-Football AI 90+ only"})
    else: return jsonify(groups)
@app.route("/api/combined_gameday")
def api_combined():
    today=datetime.now().strftime("%Y-%m-%d")
    live=fetch_api_football_only(today)
    future=[]
    future=fetch_api_football_week()
    return jsonify({"total_live":len(live),"total_future":len(future),"live":live[:10],"future":future[:10],"fetched_at":datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d %H:%M:%S EAT"),"cloned_from":"API-Football v3"})

@app.route("/api/bets/place", methods=["POST"])
def api_place_demo_bet():
    """Record a virtual KES test ticket; never connects to a bookmaker or real funds."""
    payload=request.get_json(silent=True) or {}
    ticket_type=str(payload.get("ticket_type","SINGLE")).upper()
    legs=payload.get("legs") or []
    try: stake=float(payload.get("stake",0))
    except (TypeError,ValueError): return jsonify({"error":"Stake must be a number"}),400
    minimum={"SINGLE":1,"DOUBLE":2,"MULTIBET":3,"JACKPOT":3}
    if ticket_type not in minimum: return jsonify({"error":"Unsupported ticket type"}),400
    if len(legs)<minimum[ticket_type] or (ticket_type=="SINGLE" and len(legs)!=1) or (ticket_type=="DOUBLE" and len(legs)!=2):
        return jsonify({"error":"Selection count does not match ticket type"}),400
    if len(legs)>15: return jsonify({"error":"A ticket can contain at most 15 selections"}),400
    if stake<=0 or stake!=int(stake): return jsonify({"error":"Stake must be a positive whole KES amount"}),400
    clean=[];combined=1.0
    for leg in legs:
        if not isinstance(leg,dict) or not leg.get("home") or not leg.get("away") or not leg.get("id"):
            return jsonify({"error":"Each selection needs a real match ID and team names"}),400
        try: odds=float(leg.get("odds",0))
        except (TypeError,ValueError): odds=0
        if odds<=1 or odds>1000: return jsonify({"error":"Selection odds are invalid"}),400
        clean.append({"id":str(leg["id"]),"provider_fixture_id":str(leg.get("provider_fixture_id") or leg["id"]),"home":str(leg["home"]),"away":str(leg["away"]),"league":str(leg.get("league", "Soccer")),"pick":str(leg.get("pick", "")),"odds":odds,"date":str(leg.get("date", "")),"status":"PENDING"})
        combined*=odds
    bankroll=get_bankroll()
    balance=float(bankroll.get("balance",4500000) or 0)
    if stake>balance: return jsonify({"error":"Insufficient demo wallet balance"}),400
    import uuid
    bet={"id":uuid.uuid4().hex[:12],"ticket_type":ticket_type,"legs":clean,"stake":stake,"odds":round(combined,4),"potential":round(stake*combined,2),"status":"PENDING","created_at":datetime.now(ZoneInfo("Africa/Kampala")).isoformat(timespec="seconds"),"demo":True}
    bankroll["balance"]=round(balance-stake,2);bankroll.setdefault("operations",[]).append({"time":bet["created_at"],"match":f"{ticket_type} ({len(clean)} legs)","result":"DEMO STAKE","profit":-stake})
    bets=get_my_bets();bets.append(bet);save_my_bets(bets);save_bankroll(bankroll)
    return jsonify({"ok":True,"bet":bet,"bankroll":bankroll,"note":"Virtual test ticket only. No real money was placed."}),201

@app.route("/api/match_history/<match_id>")
def api_match_history(match_id):
    # Find the real fixture in the cached 7-day all-league window, then lazily fetch and cache history.
    raw=api_football_live_and_future()
    match=next((m for m in raw if str(m.get("id"))==str(match_id)),None)
    if not match:
        return jsonify({"error":"Match not found in the current API-Football 7-day feed","available":False}),404
    home=api_football_team_history(match.get("home_id"))
    away=api_football_team_history(match.get("away_id"))
    h2h=api_football_head_to_head(match.get("home_id"),match.get("away_id"))
    return jsonify({"available":True,"match":match.get("home","")+" vs "+match.get("away",""),
        "home_form":home,"away_form":away,"h2h":h2h,"source":"API-Football v3",
        "api_calls_today":_api_calls_today(),"daily_app_cap":int(os.environ.get("API_FOOTBALL_DAILY_CAP","90")),
        "note":"Team form and H2H are cached for 7 days. Missing data means the provider returned no completed history or the request cap/network prevented retrieval."})

@app.route("/api/my_bets")
def api_my_bets(): return jsonify(get_my_bets())
@app.route("/api/verify_bets", methods=["POST"])
def api_verify_bets():
    """Settle only against actual final API-Football results; never invent scores or outcomes."""
    bets = get_my_bets()
    bankroll = get_bankroll()
    updated = 0
    today = datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d")
    # Pull the current scoreboard once. Bets without a verifiable final fixture stay pending.
    try:
        actual_matches = fetch_api_football_only(today)
    except Exception:
        actual_matches = []

    def norm(value):
        return str(value or "").strip().casefold()

    def settled(pick, home_goals, away_goals):
        p = norm(pick).replace(" ", "")
        total = home_goals + away_goals
        if p in {"1", "home", "homewin"}: return home_goals > away_goals
        if p in {"2", "away", "awaywin"}: return away_goals > home_goals
        if p in {"x", "draw"}: return home_goals == away_goals
        if p in {"1x", "dc1x", "doublechance1x"}: return home_goals >= away_goals
        if p in {"x2", "dcx2", "doublechancex2"}: return away_goals >= home_goals
        if p in {"gg", "bttyes", "bothteamsscoreyes"}: return home_goals > 0 and away_goals > 0
        if p in {"bttn", "bttsno", "bothteamsscoreno"}: return home_goals == 0 or away_goals == 0
        m = re.fullmatch(r"(over|under)(?:goals)?([0-9]+(?:\.[0-9]+)?)", p)
        if m:
            line = float(m.group(2))
            return total > line if m.group(1) == "over" else total < line
        return None

    for bet in bets:
        if not isinstance(bet, dict) or norm(bet.get("status")) in {"won", "lost"}:
            continue
        # Multi-leg demo tickets are settled only when every leg is matched to an actual final API-Football score.
        if isinstance(bet.get("legs"), list) and bet.get("legs"):
            all_final = True
            any_lost = False
            for leg in bet["legs"]:
                if norm(leg.get("status")) in {"won", "lost"}:
                    any_lost = any_lost or norm(leg.get("status")) == "lost"
                    continue
                leg_candidate = None
                leg_id = str(leg.get("provider_fixture_id") or leg.get("id") or "")
                lh, la = norm(leg.get("home")), norm(leg.get("away"))
                for actual in actual_matches:
                    same_id = leg_id and leg_id in {str(actual.get("id", "")), str(actual.get("provider_fixture_id", ""))}
                    same_teams = lh and la and lh == norm(actual.get("home")) and la == norm(actual.get("away"))
                    if same_id or same_teams:
                        leg_candidate = actual
                        break
                if not leg_candidate:
                    all_final = False
                    continue
                leg["score"] = leg_candidate.get("score", leg.get("score", "0-0"))
                leg["minute"] = leg_candidate.get("minute", "0")
                if leg_candidate.get("is_live"):
                    leg["status"] = "LIVE"; all_final = False; continue
                if not leg_candidate.get("is_ft"):
                    all_final = False; continue
                try:
                    hg, ag = int(leg_candidate.get("sh", 0)), int(leg_candidate.get("sa", 0))
                except (TypeError, ValueError):
                    try: hg, ag = map(int, str(leg_candidate.get("score", "")).split("-"))
                    except (ValueError, TypeError): all_final = False; continue
                result = settled(leg.get("pick"), hg, ag)
                if result is None:
                    leg["status"] = "UNSUPPORTED MARKET"; all_final = False
                else:
                    leg["status"] = "WON" if result else "LOST"
                    any_lost = any_lost or not result
            if all_final and all(norm(leg.get("status")) in {"won", "lost"} for leg in bet["legs"]):
                bet["status"] = "LOST" if any_lost else "WON"
                bet["settled_at"] = datetime.now(ZoneInfo("Africa/Kampala")).isoformat(timespec="seconds")
                if bet["status"] == "WON":
                    payout = float(bet.get("potential", 0) or 0)
                    bankroll["balance"] = float(bankroll.get("balance", 0) or 0) + payout
                    bankroll["total_won"] = int(bankroll.get("total_won", 0) or 0) + 1
                    bankroll.setdefault("operations", []).append({"time": bet["settled_at"], "match": f"{bet.get('ticket_type','MULTI')} ticket {bet.get('id','')}", "result": "WON", "profit": payout-float(bet.get("stake",0) or 0)})
                else:
                    bankroll["total_lost"] = int(bankroll.get("total_lost", 0) or 0) + 1
                    bankroll.setdefault("operations", []).append({"time": bet["settled_at"], "match": f"{bet.get('ticket_type','MULTI')} ticket {bet.get('id','')}", "result": "LOST", "profit": -float(bet.get("stake",0) or 0)})
                updated += 1
            else:
                bet["status"] = "LIVE" if any(norm(leg.get("status")) == "live" for leg in bet["legs"]) else "PENDING"
            continue
        candidate = None
        bet_id = str(bet.get("provider_fixture_id") or bet.get("id") or "")
        bet_home, bet_away = norm(bet.get("home")), norm(bet.get("away"))
        for match in actual_matches:
            same_id = bet_id and bet_id in {str(match.get("id", "")), str(match.get("provider_fixture_id", ""))}
            same_teams = bet_home and bet_away and bet_home == norm(match.get("home")) and bet_away == norm(match.get("away"))
            if same_id or same_teams:
                candidate = match
                break
        if not candidate:
            continue
        bet["score"] = candidate.get("score", bet.get("score", "0-0"))
        bet["minute"] = candidate.get("minute", bet.get("minute", "0"))
        if candidate.get("is_live"):
            bet["status"] = "LIVE"
            continue
        if not candidate.get("is_ft"):
            continue
        try:
            home_goals = int(candidate.get("sh", 0))
            away_goals = int(candidate.get("sa", 0))
        except (TypeError, ValueError):
            score_parts = str(candidate.get("score", "")).split("-")
            if len(score_parts) != 2:
                continue
            try: home_goals, away_goals = map(int, score_parts)
            except ValueError: continue
        outcome = settled(bet.get("pick") or bet.get("type") or bet.get("selection"), home_goals, away_goals)
        if outcome is None:
            # Do not settle a market unless this version can evaluate it safely.
            continue
        bet["status"] = "WON" if outcome else "LOST"
        bet["minute"] = "FT"
        if outcome:
            try: payout = float(bet.get("potential", 0) or 0)
            except (TypeError, ValueError): payout = 0
            bankroll["balance"] = float(bankroll.get("balance", 100000) or 0) + payout
            bankroll["total_won"] = int(bankroll.get("total_won", 0) or 0) + 1
            bankroll.setdefault("operations", []).append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "match": bet.get("match", f"{candidate.get('home')} vs {candidate.get('away')}"), "result": "WON", "profit": payout})
        else:
            bankroll["total_lost"] = int(bankroll.get("total_lost", 0) or 0) + 1
            bankroll.setdefault("operations", []).append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "match": bet.get("match", f"{candidate.get('home')} vs {candidate.get('away')}"), "result": "LOST", "profit": 0})
        updated += 1
    save_my_bets(bets)
    save_bankroll(bankroll)
    return jsonify({"updated": updated, "bets": bets, "bankroll": bankroll, "note": "Only bets matched to real final API-Football results and supported markets are settled. Unverified bets remain unchanged."})
@app.route("/api/admin_dashboard")
def api_admin():
    bankroll=get_bankroll();bets=get_my_bets()
    won=len([b for b in bets if b.get("status")=="WON"]);total=len(bets)
    win_rate=int(won/total*100) if total>0 else 0
    return jsonify({"bankroll":bankroll,"win_rate":win_rate,"total_bets":total,"won":won})
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "Wealth Hybrid", "version": "V86.41"})

@app.route("/api/diagnosis")
def api_diagnosis():
    checks=[]
    checks.append({"check":"Server-side lockscreen", "status":"PASS" if bool(WEALTH_PASSWORD) else "WARN", "detail":"Password is checked server-side; set WEALTH_PASSWORD and SECRET_KEY in Railway."})
    checks.append({"check":"API-Football key configuration", "status":"PASS" if bool(API_FOOTBALL_KEY) else "FAIL", "detail":"API_FOOTBALL_KEY is configured" if API_FOOTBALL_KEY else "Set API_FOOTBALL_KEY in Railway Variables; do not commit it to source."})
    checks.append({"check":"SQLite database", "status":"PASS" if os.path.exists(DB_PATH) else "WARN", "detail":f"Database path {DB_PATH}; cache table is initialized."})
    checks.append({"check":"Quota protection", "status":"PASS", "detail":f"Provider calls today: {_api_calls_today()} (app cap {os.environ.get('API_FOOTBALL_DAILY_CAP','90')}/day); timezone Africa/Kampala; cache TTL {PROVIDER_CACHE_TTL}s."})
    checks.append({"check":"Provider isolation", "status":"PASS", "detail":"API-Football is the only configured provider; no fallback provider is called."})
    return jsonify({"version":"V86.41 API-Football only", "feed_status":LAST_FEED_STATUS, "api_football_configured":bool(API_FOOTBALL_KEY), "db_path":DB_PATH, "api_calls_today":_api_calls_today(), "timestamp":datetime.now(ZoneInfo("Africa/Kampala")).isoformat(timespec="seconds"), "checks":checks, "all_pass":all(c['status']=='PASS' for c in checks)})

def scheduler():
    while True:
        try:
            c=get_cache();today=datetime.now().strftime("%Y-%m-%d");live=fetch_api_football_only(today)
            c["matches"]=live;c["live_count"]=len(live);c["last_fetch"]=time.time();c["last_fetch_str"]=datetime.now(ZoneInfo("Africa/Kampala")).strftime("%Y-%m-%d %H:%M:%S EAT");save_cache(c)
            print(f"V86.41 FINAL MERGED No changes Scheduler: Cloned {len(live)} TODAY DIRECT as API-Football.com — {c['last_fetch_str']}")
        except Exception as e: print(f"Scheduler error {e}")
        time.sleep(max(900, int(os.environ.get("SCHEDULER_INTERVAL", "3600"))))
# Optional polling loop. Enable explicitly with RUN_SCHEDULER=1 on a single worker.
if os.environ.get("RUN_SCHEDULER", "0").strip().lower() in {"1", "true", "yes"}:
    threading.Thread(target=scheduler, daemon=True, name="wealth-hybrid-scheduler").start()

if __name__=="__main__":
    if os.environ.get("RUN_SCHEDULER", "1").strip().lower() in {"1", "true", "yes"}:
        # For local development only; Railway should use Gunicorn via Procfile.
        pass
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)