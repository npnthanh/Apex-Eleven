"""Persistent game rules. Simulation inputs are generated once, results are never invented for display."""
import json
import math
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from contextlib import closing
from pathlib import Path

from flask import g

SKILLS = ("finishing", "passing", "pace", "tackling", "marking", "stamina_skill")
START_CASH = 2_000_000
START_TOKENS = 20
SEASON_DAYS = 273
LEAGUE_START = 4
GROUP_DAYS = (15, 36, 64, 85, 106, 134)
CUP_DAYS = {1: (8, 22), 2: (43, 57), 3: (78, 92), 4: (113, 127), 5: (155, 169), 6: (225, 246)}
INTERNATIONAL_DAYS = {7: (148, 162), 8: (183, 197), 9: (211, 232), 10: (253,)}
MAX_FACILITY_LEVEL = 10
MAX_ROSTER = 25
MAX_SKILL = 200
MAX_DAILY_SKILL_GAIN = 1.2
RECOVERY_INTERVAL = 8 * 60 * 60
RECOVERY_AMOUNT = 10
REST_PACK_RECOVERY = 10
MIN_TRAINING_CONDITION = 20


class GameConnection(sqlite3.Connection):
    deferred = False

    def commit(self):
        if not self.deferred:
            super().commit()

    def close(self):
        if not self.deferred:
            super().close()


def atomic_action(view, connect):
    """Serialize purchases/rewards and commit each action as one database transaction."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        conn = connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.deferred = True
        g.action_db = conn
        try:
            result = view(*args, **kwargs)
            status = result[1] if isinstance(result, tuple) and isinstance(result[1], int) else getattr(result, "status_code", 200)
            if status < 400:
                sqlite3.Connection.commit(conn)
            else:
                conn.rollback()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            g.pop("action_db", None)
            conn.deferred = False
            conn.close()
    return wrapped


def facility_values(name, level):
    n = max(0, min(MAX_FACILITY_LEVEL, int(level)) - 1)
    values = {
        "stadium": {"capacity": 2000 + 600 * n, "match_injury_mult": max(.85, 1 - .015 * n)},
        "parking": {"parking_capacity": 700 + 200 * n, "parking_fee": 15},
        "food_court": {"food_income_per_fan": 8 + 2 * n},
        "youth_academy": {"youth_base_cltt": 50 + 1.5 * n, "youth_quality_bonus": 1.5 * n},
        "training_ground": {"rest_packs_daily": 1 + n // 4, "train_gain_mult": 1 + .03 * n, "train_injury_mult": max(.7, 1 - .03 * n)},
        "medical_center": {"heal_packs_daily": 1 + n // 3, "injury_days_mult": max(.55, 1 - .05 * n)},
        "fan_club": {"fan_revenue_daily": 5000 + 1500 * n},
    }
    return values[name]


def facility_description(name, level):
    v = facility_values(name, level)
    return {
        "stadium": lambda: f"Sức chứa: {v['capacity']:,} chỗ ngồi",
        "parking": lambda: f"Chứa {v['parking_capacity']:,} xe; phí {v['parking_fee']}/xe khi đá sân nhà",
        "food_court": lambda: f"+{v['food_income_per_fan']}/CĐV khi đá sân nhà",
        "youth_academy": lambda: f"Chất lượng khởi điểm lứa trẻ khoảng {v['youth_base_cltt']:.1f}%; có thể tập lên 200%",
        "training_ground": lambda: f"+{v['rest_packs_daily']} thẻ thể lực/ngày; hiệu quả tập +{round((v['train_gain_mult']-1)*100)}%",
        "medical_center": lambda: f"+{v['heal_packs_daily']} thẻ y tế/tuần; thời gian chấn thương -{round((1-v['injury_days_mult'])*100)}%",
        "fan_club": lambda: f"+{v['fan_revenue_daily']:,}/ngày từ cửa hàng CLB",
    }[name]()


def player_value(p):
    # One price model for display, purchases and sales. Selling returns 45%.
    mean = sum(float(p[k]) for k in SKILLS) / 6
    age = float(p["age"])
    age_factor = max(.45, 1.1 - max(0, age - 23) * .04)
    return int(max(60_000, 350_000 * (mean / 55) ** 3 * age_factor))


def migrate(conn, ensure_column, new_save=False):
    for column, definition in {
        "rules_version": "INTEGER NOT NULL DEFAULT 0", "season_number": "INTEGER NOT NULL DEFAULT 1",
        "season_length": f"INTEGER NOT NULL DEFAULT {SEASON_DAYS}", "last_season_rank": "INTEGER",
        "last_wage_day": "INTEGER NOT NULL DEFAULT 0", "cash_debt": "INTEGER NOT NULL DEFAULT 0",
        "friendly_opponent": "TEXT", "friendly_strength": "REAL", "friendly_offer_day": "INTEGER NOT NULL DEFAULT -1",
        "clock_offset_seconds": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        ensure_column(conn, "club", column, definition)
    ensure_column(conn, "facility", "pending_level", "INTEGER")
    ensure_column(conn, "fixture", "attendance", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "fixture", "winner", "TEXT")
    for column, definition in {
        "player_id": "INTEGER", "appearances": "INTEGER NOT NULL DEFAULT 0",
        "yellow_cards": "INTEGER NOT NULL DEFAULT 0", "motm": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        ensure_column(conn, "player_match_stat", column, definition)
    conn.execute("""CREATE TABLE IF NOT EXISTS season_record (
        season_number INTEGER PRIMARY KEY, rank INTEGER NOT NULL, level_before INTEGER NOT NULL,
        level_after INTEGER NOT NULL, summary TEXT NOT NULL, matches TEXT NOT NULL, titles TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS player_stat_archive AS
        SELECT *, 0 AS season_number FROM player_match_stat WHERE 0""")
    conn.execute("""CREATE TABLE IF NOT EXISTS player_training_day (
        season_number INTEGER, day INTEGER, source TEXT, player_id INTEGER,
        sessions INTEGER NOT NULL DEFAULT 0, skill_gain REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(season_number, day, source, player_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS game_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, season_number INTEGER, day INTEGER,
        category TEXT NOT NULL, cash_delta INTEGER NOT NULL DEFAULT 0,
        token_delta INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_player_offer (
        season_number INTEGER, day INTEGER, player_json TEXT NOT NULL, price INTEGER NOT NULL,
        PRIMARY KEY(season_number, day))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_player (
        id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT NOT NULL, name TEXT NOT NULL,
        position TEXT NOT NULL, overall REAL NOT NULL, shirt_number INTEGER NOT NULL)""")
    ensure_column(conn, "youth_player", "cohort", "INTEGER NOT NULL DEFAULT 1")
    for table in ("squad_player", "youth_player"):
        for column, definition in {
            "condition_clock": "REAL NOT NULL DEFAULT 0",
            "retirement_age": "INTEGER NOT NULL DEFAULT 0",
            "experience_matches": "INTEGER NOT NULL DEFAULT 0",
            "seasons_played": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            ensure_column(conn, table, column, definition)
    ensure_column(conn, "youth_player", "condition", "REAL NOT NULL DEFAULT 100")
    conn.execute("""CREATE TABLE IF NOT EXISTS retired_player (
        player_id INTEGER PRIMARY KEY, name TEXT NOT NULL, retired_season INTEGER NOT NULL,
        age INTEGER NOT NULL, player_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS friendly_match (
        id INTEGER PRIMARY KEY AUTOINCREMENT, season_number INTEGER, day INTEGER,
        opponent TEXT NOT NULL, opponent_strength REAL NOT NULL, goals_for INTEGER, goals_against INTEGER,
        UNIQUE(season_number,day))""")
    club = conn.execute("SELECT * FROM club WHERE id=1").fetchone()
    if club["rules_version"] < 1:
        if not new_save:
            conn.commit()
            source = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            with closing(sqlite3.connect(source.with_suffix(".before-rules.sqlite3"))) as backup:
                conn.backup(backup)
        # Only known fabricated history is removed. Owned players and balances are retained.
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(club)") if r["name"].startswith("hist_")]
        for column in columns:
            conn.execute(f"UPDATE club SET {column}=? WHERE id=1", ("" if column in {
                "hist_biggest_win", "hist_most_appearances", "hist_top_scorer", "hist_top_assists", "hist_most_goals_match"
            } else 0,))
        conn.execute("UPDATE club SET club_level=1, rules_version=1 WHERE id=1")
        for key, sample, replacement in (
            ("manager_name", "Thành Nhật", g.user["username"]),
            ("stadium_name", "Allianz Arena", "Sân " + club["name"]),
            ("fanclub_name", "Mia San Mia", "CĐV " + club["name"]), ("city", "TP HaNoi", ""),
            ("founded_date", "09/01/2025", datetime.fromtimestamp(g.user["created_at"], timezone(timedelta(hours=7))).strftime("%d/%m/%Y")),
        ):
            conn.execute(f"UPDATE club SET {key}=? WHERE id=1 AND ({key}=? OR {key}='')", (replacement, sample))
        conn.execute("DELETE FROM player_match_stat WHERE fixture_id=0")
        # Legacy event records did not store appearances. Recover only verifiable identities.
        conn.execute("""UPDATE player_match_stat SET player_id=(SELECT id FROM squad_player
            WHERE name=player_match_stat.player_name LIMIT 1) WHERE is_user_player=1 AND player_id IS NULL
            AND (SELECT COUNT(*) FROM squad_player WHERE name=player_match_stat.player_name)=1""")
        # Upgrades formerly granted their benefits before completion; keep the paid target pending.
        conn.execute("UPDATE facility SET pending_level=level, level=MAX(1,level-1) WHERE build_complete_at IS NOT NULL")
        if not new_save:
            reschedule_legacy(conn)
    conn.commit()


def reschedule_legacy(conn):
    club = conn.execute("SELECT * FROM club").fetchone()
    fixtures = conn.execute("SELECT * FROM fixture ORDER BY day,id").fetchall()
    for f in fixtures:
        if f["played"]:
            continue
        if f["competition"] == "League":
            day = LEAGUE_START + 7 * (f["round"] - 1)
        elif f["competition"] == "ApexCup":
            day = CUP_DAYS.get(f["round"], (225, 246))[min(f["leg"]-1, 1)]
        elif f["competition"] in ("Champions", "Europa"):
            day = GROUP_DAYS[f["round"]-1] if f["round"] <= 6 else INTERNATIONAL_DAYS.get(f["round"], (253,))[min(f["leg"]-1, len(INTERNATIONAL_DAYS.get(f["round"], (253,)))-1)]
        else:
            day = max(f["day"], 1)
        # Retain completed results; future matches must also respect their rest windows.
        day = max(day, club["season_day"] + 3)
        while conn.execute("""SELECT 1 FROM fixture WHERE id!=? AND ABS(day-?)<3
            AND (home IN (?,?) OR away IN (?,?)) LIMIT 1""",
            (f["id"], day, f["home"], f["away"], f["home"], f["away"])).fetchone():
            day += 1
        conn.execute("UPDATE fixture SET day=? WHERE id=?", (day, f["id"]))
    maximum = conn.execute("SELECT MAX(day) FROM fixture").fetchone()[0] or 0
    conn.execute("UPDATE club SET season_length=MAX(?,?)", (SEASON_DAYS, maximum + 7))


def ensure_ai_rosters(conn, generate_name):
    club = conn.execute("SELECT name FROM club").fetchone()
    for t in conn.execute("SELECT * FROM team WHERE name!=?", (club["name"],)).fetchall():
        if conn.execute("SELECT 1 FROM ai_player WHERE team_name=?", (t["name"],)).fetchone():
            continue
        for number, pos in enumerate(("GK", "DL", "DC", "DC", "DR", "ML", "MC", "MC", "MR", "ST", "ST"), 1):
            conn.execute("INSERT INTO ai_player(team_name,name,position,overall,shirt_number) VALUES (?,?,?,?,?)",
                         (t["name"], generate_name(), pos, round(max(20, t["base_strength"] + random.uniform(-4,4)), 2), number))
        names = [r[0] for r in conn.execute("SELECT name FROM ai_player WHERE team_name=?", (t["name"],))]
        conn.execute("UPDATE team SET star_players=? WHERE name=?", (json.dumps(names), t["name"]))
    conn.commit()


def event(conn, category, detail, cash=0, tokens=0):
    club = conn.execute("SELECT season_number,season_day FROM club").fetchone()
    conn.execute("INSERT INTO game_event(season_number,day,category,cash_delta,token_delta,detail) VALUES (?,?,?,?,?,?)",
                 (club["season_number"], club["season_day"], category, cash, tokens, detail))


def train_player(conn, p, skills, effort, multiplier=1, source="senior", condition=None):
    club = conn.execute("SELECT season_number,season_day FROM club").fetchone()
    identity = (club["season_number"], club["season_day"], source, p["id"])
    record = conn.execute("SELECT * FROM player_training_day WHERE season_number=? AND day=? AND source=? AND player_id=?", identity).fetchone()
    used = record["skill_gain"] if record else 0
    if used >= MAX_DAILY_SKILL_GAIN - .001:
        return None
    current_condition = float(p["condition"])
    if current_condition < MIN_TRAINING_CONDITION or current_condition < effort:
        return None
    # Legacy saves retain potential for compatibility; it no longer limits growth.
    keys = [k for k in SKILLS if k in skills and p[k] < MAX_SKILL]
    if not keys:
        return None
    age_factor = max(.35, min(1.15, 1.15 - max(0, p["age"]-21) * .045))
    headroom = sum(MAX_SKILL - p[k] for k in keys) / len(keys)
    # Keep gains gradual near 200%, with a floor so the cap remains reachable.
    growth_factor = max(.1, headroom/(headroom+15))
    budget = min(MAX_DAILY_SKILL_GAIN-used, .65 * min(2, effort/20) * multiplier * age_factor * growth_factor)
    updated = {k: float(p[k]) for k in SKILLS}
    for k in keys:
        updated[k] = round(min(MAX_SKILL, p[k] + budget/len(keys)), 4)
    actual = sum(updated[k] - p[k] for k in SKILLS)
    if actual < .0001:
        return None
    conn.execute("""INSERT INTO player_training_day VALUES (?,?,?,?,1,?)
        ON CONFLICT(season_number,day,source,player_id) DO UPDATE SET
        sessions=sessions+1, skill_gain=skill_gain+excluded.skill_gain""", (*identity, actual))
    table = "squad_player" if source == "senior" else "youth_player"
    conn.execute(f"UPDATE {table} SET " + ",".join(k+"=?" for k in SKILLS) + " WHERE id=?",
                 (*[updated[k] for k in SKILLS], p["id"]))
    conn.execute(f"UPDATE {table} SET condition=? WHERE id=?", (max(0, current_condition-effort), p["id"]))
    return {"gain": actual, "values": updated, "new_condition": max(0, current_condition-effort)}


def game_clock(conn, timestamp=None):
    offset = conn.execute("SELECT clock_offset_seconds FROM club").fetchone()[0]
    return (time.time() if timestamp is None else timestamp) + offset


def initialize_lifecycle(conn, timestamp=None):
    clock = game_clock(conn, timestamp)
    for table in ("squad_player", "youth_player"):
        for p in conn.execute(f"SELECT * FROM {table} WHERE retirement_age=0 OR condition_clock=0").fetchall():
            if not p["retirement_age"]:
                # A personal retirement plan, persisted once. Goalkeepers generally play longer.
                low, high = (35, 42) if p["position"] == "GK" else (32, 39)
                retirement = max(p["age"]+1, round(random.triangular(low, high, (low+high)/2)))
                conn.execute(f"UPDATE {table} SET retirement_age=? WHERE id=?", (retirement, p["id"]))
            if not p["condition_clock"]:
                conn.execute(f"UPDATE {table} SET condition_clock=? WHERE id=?", (clock, p["id"]))
    conn.commit()


def recover_condition(conn, timestamp=None):
    """Recover offline and in simulated time; page refreshes cannot create stamina."""
    clock = game_clock(conn, timestamp)
    for table in ("squad_player", "youth_player"):
        for p in conn.execute(f"SELECT id,condition,condition_clock FROM {table}").fetchall():
            anchor = p["condition_clock"] or clock
            ticks = max(0, int((clock-anchor)//RECOVERY_INTERVAL))
            if p["condition"] >= 100:
                anchor = clock  # Do not bank recovery while already fully rested.
            elif ticks:
                condition = min(100, p["condition"]+ticks*RECOVERY_AMOUNT)
                anchor = clock if condition==100 else anchor+ticks*RECOVERY_INTERVAL
                conn.execute(f"UPDATE {table} SET condition=? WHERE id=?", (condition,p["id"]))
            if anchor != p["condition_clock"]:
                conn.execute(f"UPDATE {table} SET condition_clock=? WHERE id=?", (anchor,p["id"]))
    conn.commit()


def experience_multiplier(p):
    row = dict(p)
    matches = max(0, row.get("experience_matches", 0))
    seasons = max(0, row.get("seasons_played", 0))
    maturity = min(12, max(0, row["age"]-20))*.004
    return 1 + min(.20, maturity + matches*.0006 + seasons*.004)


def age_squad_for_new_season(conn):
    """Called once inside the season rollover transaction; keep facts for retired players."""
    season = conn.execute("SELECT season_number FROM club").fetchone()[0]
    conn.execute("UPDATE squad_player SET age=age+1,seasons_played=seasons_played+1," +
                 ",".join(f"{skill}={skill}*0.8" for skill in SKILLS))
    retired = conn.execute("SELECT * FROM squad_player WHERE age>=retirement_age AND retirement_age>0").fetchall()
    for p in retired:
        conn.execute("INSERT INTO retired_player VALUES (?,?,?,?,?)",
                     (p["id"],p["name"],season,p["age"],json.dumps(dict(p),ensure_ascii=False)))
        conn.execute("UPDATE lineup_slot SET player_id=NULL,is_captain=0 WHERE player_id=?",(p["id"],))
        for key in ("captain_pid","penalty_pid","freekick_r_pid","freekick_l_pid","corner_r_pid","corner_l_pid"):
            conn.execute(f"UPDATE club SET {key}=NULL WHERE {key}=?",(p["id"],))
        conn.execute("DELETE FROM squad_player WHERE id=?",(p["id"],))
    return [p["name"] for p in retired]


def career_stats(conn, pid):
    def rows(current_only=False):
        sql = "SELECT * FROM player_match_stat WHERE is_user_player=1 AND player_id=?"
        values = list(conn.execute(sql, (pid,)))
        if not current_only:
            values += list(conn.execute("SELECT * FROM player_stat_archive WHERE is_user_player=1 AND player_id=?", (pid,)))
        return values
    all_rows, current = rows(), rows(True)
    result = {}
    for key, label in (("career", "SỰ NGHIỆP"), ("league", "GIẢI VÔ ĐỊCH"), ("cup", "CÁC CÚP")):
        chosen = lambda r: key == "career" or ((r["competition"] == "League") == (key == "league"))
        total = [r for r in all_rows if chosen(r)]
        season = [r for r in current if chosen(r)]
        result[key] = {
            "title": label, "apps_season": sum(r["appearances"] for r in season),
            "goals_season": sum(r["goals"] for r in season), "assists_season": sum(r["assists"] for r in season),
            "yellows_season": sum(r["yellow_cards"] for r in season), "motm": sum(r["motm"] for r in season),
            "goals_total": sum(r["goals"] for r in total), "apps_total": sum(r["appearances"] for r in total),
            "form": "-".join(str(round(r["rating"],1)) for r in season[-5:]) or "—",
        }
    return result


def summary(matches):
    result = dict(wins=0, draws=0, losses=0, goals_for=0, goals_against=0,
                  avg_attendance=0, max_win_streak=0, max_unbeaten=0, biggest_win="—")
    win_streak = unbeaten = 0
    biggest_margin = 0
    attendances = []
    for m in matches:
        gf, ga = m["goals_for"], m["goals_against"]
        result["goals_for"] += gf
        result["goals_against"] += ga
        result["wins" if gf > ga else "draws" if gf == ga else "losses"] += 1
        win_streak = win_streak+1 if gf > ga else 0
        unbeaten = unbeaten+1 if gf >= ga else 0
        result["max_win_streak"] = max(result["max_win_streak"], win_streak)
        result["max_unbeaten"] = max(result["max_unbeaten"], unbeaten)
        if gf-ga > biggest_margin:
            biggest_margin = gf-ga
            result["biggest_win"] = f"{m['opponent']} ({gf}:{ga})"
        if m["is_home"]:
            attendances.append(m["attendance"])
    result["avg_attendance"] = round(sum(attendances)/len(attendances)) if attendances else 0
    return result


def club_matches(conn):
    name = conn.execute("SELECT name FROM club").fetchone()[0]
    matches = []
    for f in conn.execute("SELECT * FROM fixture WHERE played=1 AND (home=? OR away=?) ORDER BY day,id", (name,name)):
        home = f["home"] == name
        matches.append(dict(opponent=f["away"] if home else f["home"], is_home=home,
                            goals_for=f["home_goals"] if home else f["away_goals"],
                            goals_against=f["away_goals"] if home else f["home_goals"], attendance=f["attendance"]))
    return matches


def player_records(conn, archived=False):
    sql = "SELECT * FROM player_match_stat WHERE is_user_player=1"
    if archived:
        sql += " UNION ALL SELECT " + ",".join(r["name"] for r in conn.execute("PRAGMA table_info(player_match_stat)")) + " FROM player_stat_archive WHERE is_user_player=1"
    stats = [dict(r) for r in conn.execute(sql)]
    result = {k: "—" for k in ("most_apps", "top_scorer", "top_assister", "most_goals_one_match")}
    totals = {}
    for s in stats:
        key = s["player_id"] if s["player_id"] is not None else s["player_name"]
        totals.setdefault(key, dict(name=s["player_name"], apps=0, goals=0, assists=0, maximum=0))
        entry = totals[key]
        entry["apps"] += s["appearances"]
        entry["goals"] += s["goals"]
        entry["assists"] += s["assists"]
        entry["maximum"] = max(entry["maximum"], s["goals"])
    for key, field in (("most_apps","apps"),("top_scorer","goals"),("top_assister","assists"),("most_goals_one_match","maximum")):
        top = max(totals.values(), key=lambda p:p[field], default=None)
        if top and top[field] > 0:
            result[key] = f"{top['name']} ({top[field]})"
    return result
