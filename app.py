
import os
import math
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flask import Flask, flash, has_request_context, redirect, render_template, request, url_for

APP_NAME = "Băng Ghế Chỉ Đạo (Prototype)"
DB_PATH = os.path.join(os.path.dirname(__file__), "game.db")

MARKET_BATCH_SECONDS = 180  # 3 minutes
MARKET_LISTINGS_PER_BATCH = 20

# -----------------------------
# Utilities
# -----------------------------
def now_ts() -> int:
    return int(time.time())

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_exec(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> None:
    conn.execute(sql, params)
    conn.commit()

def db_insert(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> int:
    cur = conn.execute(sql, params)
    conn.commit()
    return int(cur.lastrowid)

def db_one(conn: sqlite3.Connection, sql: str, params: Tuple = ()):
    cur = conn.execute(sql, params)
    return cur.fetchone()

def db_all(conn: sqlite3.Connection, sql: str, params: Tuple = ()):
    cur = conn.execute(sql, params)
    return cur.fetchall()

def ensure_column(conn: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
    """Add a column if it does not exist (lightweight migration helper)."""
    try:
        cols = [r["name"] for r in db_all(conn, f"PRAGMA table_info({table})")]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            conn.commit()
    except sqlite3.OperationalError:
        # Table may not exist yet; ignore here.
        pass

def fmt_money(x: int) -> str:
    # Vietnamese-ish compact formatting
    if x >= 1_000_000_000:
        return f"{x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x/1_000:.1f}K"
    return str(x)

def clean_name(s: str, max_len: int = 28) -> str:
    s = (s or "").strip()
    s = " ".join(s.split())
    return s[:max_len]

# -----------------------------
# Game math
# -----------------------------
def player_overall(p: sqlite3.Row) -> float:
    # Weighted overall: tweak as you like
    fin = p["finishing"]
    pas = p["passing"]
    pac = p["pace"]
    tac = p["tackling"]
    mar = p["marking"]
    sta = p["stamina_skill"]
    # positions: GK is not modeled; we treat GK as defensive+passing+stamina
    if p["position"] == "GK":
        return 0.20 * pas + 0.35 * mar + 0.35 * tac + 0.10 * sta
    if p["position"] in ("DC", "DL", "DR"):
        return 0.10 * fin + 0.15 * pas + 0.15 * pac + 0.30 * tac + 0.20 * mar + 0.10 * sta
    if p["position"] in ("DMC",):
        return 0.15 * fin + 0.20 * pas + 0.15 * pac + 0.20 * tac + 0.15 * mar + 0.15 * sta
    if p["position"] in ("MC", "AMC", "ML", "MR", "AML", "AMR"):
        return 0.20 * fin + 0.25 * pas + 0.20 * pac + 0.10 * tac + 0.10 * mar + 0.15 * sta
    # ST
    return 0.40 * fin + 0.15 * pas + 0.25 * pac + 0.05 * tac + 0.05 * mar + 0.10 * sta

def player_effective_rating(p: sqlite3.Row, club_day: int) -> float:
    base = player_overall(p)
    cond = p["condition"] / 100.0
    morale = p["morale"] / 100.0
    form = p["form"] / 100.0
    injured = (p["injured_until_day"] or 0) > club_day
    if injured:
        return 0.0
    # Condition is a strong factor; morale/form moderate
    return base * (0.55 + 0.45 * cond) * (0.85 + 0.15 * morale) * (0.85 + 0.15 * form)


SLOT_SIMILAR: Dict[str, set] = {
    "GK": {"GK"},
    "DL": {"DL", "ML"},
    "DR": {"DR", "MR"},
    "DC": {"DC", "DMC"},
    "DMC": {"DMC", "MC", "DC"},
    "MC": {"MC", "DMC", "AMC", "ML", "MR"},
    "ML": {"ML", "AML", "MC", "DL"},
    "MR": {"MR", "AMR", "MC", "DR"},
    "AMC": {"AMC", "MC", "ST"},
    "AML": {"AML", "ML", "ST"},
    "AMR": {"AMR", "MR", "ST"},
    "ST": {"ST", "AMC", "AML", "AMR"},
}


def slot_fit_multiplier(player_pos: str, slot_pos: str) -> float:
    """Penalty when using a player out of his natural position (prototype)."""
    if player_pos == slot_pos:
        return 1.0
    if player_pos in SLOT_SIMILAR.get(slot_pos, set()):
        return 0.92
    return 0.80


def player_effective_in_slot(p: sqlite3.Row, slot_pos: str, club_day: int) -> float:
    return player_effective_rating(p, club_day) * slot_fit_multiplier(p["position"], slot_pos)

def formation_slots(formation: str) -> List[str]:
    """
    Return 11 position slots (not strict; used for auto-picking).
    """
    if formation == "4-4-2":
        return ["GK", "DL", "DC", "DC", "DR", "ML", "MC", "MC", "MR", "ST", "ST"]
    if formation == "4-3-3":
        return ["GK", "DL", "DC", "DC", "DR", "MC", "MC", "MC", "AML", "ST", "AMR"]
    if formation == "4-2-3-1":
        return ["GK", "DL", "DC", "DC", "DR", "DMC", "DMC", "AML", "AMC", "AMR", "ST"]
    if formation == "3-5-2":
        return ["GK", "DC", "DC", "DC", "ML", "MC", "MC", "MR", "AMC", "ST", "ST"]
    if formation == "5-3-2":
        return ["GK", "DL", "DC", "DC", "DC", "DR", "MC", "MC", "MC", "ST", "ST"]
    # default
    return ["GK", "DL", "DC", "DC", "DR", "MC", "MC", "MC", "AML", "ST", "AMR"]


# Pitch layout coordinates (percent) for each formation slot index.
# x: 0..100 (left->right), y: 0..100 (top->bottom)
FORMATION_LAYOUT: Dict[str, List[Tuple[int, int]]] = {
    "4-4-2": [(50, 88), (18, 72), (40, 74), (60, 74), (82, 72), (18, 52), (45, 55), (55, 55), (82, 52), (45, 28), (55, 28)],
    "4-3-3": [(50, 88), (18, 72), (40, 74), (60, 74), (82, 72), (40, 56), (50, 54), (60, 56), (22, 32), (50, 26), (78, 32)],
    "4-2-3-1": [(50, 88), (18, 72), (40, 74), (60, 74), (82, 72), (42, 60), (58, 60), (22, 38), (50, 38), (78, 38), (50, 22)],
    "3-5-2": [(50, 88), (35, 74), (50, 76), (65, 74), (18, 54), (42, 56), (58, 56), (82, 54), (50, 38), (45, 24), (55, 24)],
    "5-3-2": [(50, 88), (14, 74), (35, 76), (50, 78), (65, 76), (86, 74), (40, 54), (50, 52), (60, 54), (45, 26), (55, 26)],
}


def ensure_lineup_slots(conn: sqlite3.Connection, formation: str) -> None:
    """Ensure lineup slots exist (and are aligned) for a given formation."""
    slots = formation_slots(formation)
    rows = db_all(conn, "SELECT * FROM lineup_slot WHERE formation=? ORDER BY slot_index", (formation,))
    if len(rows) != len(slots):
        db_exec(conn, "DELETE FROM lineup_slot WHERE formation=?", (formation,))
        for idx, pos in enumerate(slots):
            db_exec(
                conn,
                "INSERT OR REPLACE INTO lineup_slot (formation, slot_index, position, player_id, is_captain) VALUES (?, ?, ?, NULL, 0)",
                (formation, idx, pos),
            )


def get_lineup_slots(conn: sqlite3.Connection, formation: str) -> List[sqlite3.Row]:
    ensure_lineup_slots(conn, formation)
    return db_all(conn, "SELECT * FROM lineup_slot WHERE formation=? ORDER BY slot_index", (formation,))


def set_lineup(conn: sqlite3.Connection, formation: str, slot_to_player: Dict[int, Optional[int]], captain_slot: Optional[int]) -> None:
    ensure_lineup_slots(conn, formation)

    # Clear captain flags
    db_exec(conn, "UPDATE lineup_slot SET is_captain=0 WHERE formation=?", (formation,))

    for slot_idx, pid in slot_to_player.items():
        if pid is None:
            db_exec(conn, "UPDATE lineup_slot SET player_id=NULL WHERE formation=? AND slot_index=?", (formation, slot_idx))
        else:
            db_exec(conn, "UPDATE lineup_slot SET player_id=? WHERE formation=? AND slot_index=?", (pid, formation, slot_idx))

    if captain_slot is not None:
        db_exec(conn, "UPDATE lineup_slot SET is_captain=1 WHERE formation=? AND slot_index=?", (formation, captain_slot))


def starting_xi_from_saved(conn: sqlite3.Connection, club_day: int, formation: str) -> List[Tuple[sqlite3.Row, str]]:
    """Return list of (player_row, slot_position) in slot order using saved lineup where possible."""
    slots = formation_slots(formation)
    slot_rows = get_lineup_slots(conn, formation)
    players = db_all(conn, "SELECT * FROM squad_player")
    by_id = {p["id"]: p for p in players}

    picked_ids: set[int] = set()
    missing_slots: List[Tuple[int, str]] = []

    # 1) Place saved players first
    for i, slot_pos in enumerate(slots):
        sr = slot_rows[i]
        pid = sr["player_id"]
        if pid is None:
            missing_slots.append((i, slot_pos))
            continue
        p = by_id.get(pid)
        if not p:
            missing_slots.append((i, slot_pos))
            continue
        # must be healthy and unique
        if (p["injured_until_day"] or 0) > club_day:
            missing_slots.append((i, slot_pos))
            continue
        if pid in picked_ids:
            missing_slots.append((i, slot_pos))
            continue
        picked_ids.add(pid)

    # 2) Fill missing slots by best fit
    remaining = [p for p in players if (p["injured_until_day"] or 0) <= club_day and p["id"] not in picked_ids]

    # We'll build a dict first then output in slot order.
    # (We rebuild assignments from lineup_slot to keep slot indexes.)
    assigned: Dict[int, sqlite3.Row] = {}
    for i, slot_pos in enumerate(slots):
        pid = slot_rows[i]["player_id"]
        if pid is not None and pid in picked_ids and pid in by_id:
            assigned[i] = by_id[pid]

    for slot_idx, slot_pos in missing_slots:
        if not remaining:
            break
        best = max(remaining, key=lambda p: player_effective_in_slot(p, slot_pos, club_day))
        assigned[slot_idx] = best
        remaining.remove(best)

    # 3) If still not 11 (e.g., too many injuries), fill with any remaining
    for slot_idx, slot_pos in enumerate(slots):
        if slot_idx in assigned:
            continue
        if not remaining:
            break
        best_any = max(remaining, key=lambda p: player_effective_rating(p, club_day))
        assigned[slot_idx] = best_any
        remaining.remove(best_any)

    # Output in slot order
    out: List[Tuple[sqlite3.Row, str]] = []
    for i, slot_pos in enumerate(slots):
        if i in assigned:
            out.append((assigned[i], slot_pos))
    return out


def auto_pick_and_save_lineup(conn: sqlite3.Connection, club_day: int, formation: str) -> None:
    players = db_all(conn, "SELECT * FROM squad_player")
    slots = formation_slots(formation)
    picked: List[sqlite3.Row] = []
    remaining = [p for p in players if (p["injured_until_day"] or 0) <= club_day]
    for slot_pos in slots:
        if not remaining:
            break
        best = max(remaining, key=lambda p: player_effective_in_slot(p, slot_pos, club_day))
        picked.append(best)
        remaining.remove(best)

    slot_to_player: Dict[int, Optional[int]] = {}
    for idx, _ in enumerate(slots):
        slot_to_player[idx] = picked[idx]["id"] if idx < len(picked) else None
    # captain: choose best morale among picked
    captain_slot: Optional[int] = None
    if picked:
        best_cap = max(range(len(picked)), key=lambda i: (picked[i]["morale"], player_overall(picked[i])))
        captain_slot = best_cap
    set_lineup(conn, formation, slot_to_player, captain_slot)

def pick_starting_11(players: List[sqlite3.Row], formation: str, club_day: int) -> List[sqlite3.Row]:
    slots = formation_slots(formation)
    remaining = players[:]
    picked: List[sqlite3.Row] = []

    def best_for(pos: str) -> Optional[sqlite3.Row]:
        # First pass: exact position
        candidates = [p for p in remaining if p["position"] == pos and (p["injured_until_day"] or 0) <= club_day]
        if candidates:
            return max(candidates, key=lambda p: player_effective_rating(p, club_day))
        # Second pass: similar roles
        similars = {
            "DC": ["DC", "DMC"],
            "DL": ["DL", "ML"],
            "DR": ["DR", "MR"],
            "ML": ["ML", "AML", "MC"],
            "MR": ["MR", "AMR", "MC"],
            "MC": ["MC", "DMC", "AMC"],
            "DMC": ["DMC", "MC"],
            "AMC": ["AMC", "MC"],
            "AML": ["AML", "ML", "ST"],
            "AMR": ["AMR", "MR", "ST"],
            "ST": ["ST", "AML", "AMR", "AMC"],
            "GK": ["GK"],
        }
        for alt in similars.get(pos, [pos]):
            candidates = [p for p in remaining if p["position"] == alt and (p["injured_until_day"] or 0) <= club_day]
            if candidates:
                return max(candidates, key=lambda p: player_effective_rating(p, club_day))
        # fallback: anyone not injured
        candidates = [p for p in remaining if (p["injured_until_day"] or 0) <= club_day]
        if not candidates:
            return None
        return max(candidates, key=lambda p: player_effective_rating(p, club_day))

    for slot in slots:
        p = best_for(slot)
        if p is None:
            break
        picked.append(p)
        remaining.remove(p)

    # If we still have less than 11, fill with best remaining healthy players
    while len(picked) < 11:
        healthy = [p for p in remaining if (p["injured_until_day"] or 0) <= club_day]
        if not healthy:
            break
        p = max(healthy, key=lambda p: player_effective_rating(p, club_day))
        picked.append(p)
        remaining.remove(p)
    return picked

def facility_effects(conn: sqlite3.Connection) -> Dict[str, float]:
    facs = {r["name"]: r["level"] for r in db_all(conn, "SELECT name, level FROM facility")}
    stadium = facs.get("stadium", 1)
    training = facs.get("training_ground", 1)
    medical = facs.get("medical_center", 1)
    youth = facs.get("youth_academy", 1)

    # multipliers / bonuses
    return {
        "match_injury_mult": clamp(1.0 - 0.01 * stadium, 0.70, 1.0),         # up to -30%
        "train_injury_mult": clamp(1.0 - 0.012 * training, 0.60, 1.0),       # up to -40%
        "train_gain_mult": 1.0 + 0.02 * training,                             # +2% per lvl
        "injury_days_mult": clamp(1.0 - 0.03 * medical, 0.40, 1.0),           # up to -60%
        "capacity": 1000 + 600 * stadium,                                     # tickets
        "youth_quality_bonus": 0.5 * youth,                                   # bonus to youth stats
    }

def tactics_modifiers(formation: str, mentality: str, pressing: str, passing: str) -> Dict[str, float]:
    # Very simplified – tweak anytime
    atk = 1.0
    dfn = 1.0
    stamina = 1.0

    if mentality == "defensive":
        atk *= 0.92
        dfn *= 1.08
    elif mentality == "attacking":
        atk *= 1.08
        dfn *= 0.94

    if pressing == "low":
        dfn *= 0.98
        stamina *= 1.06
    elif pressing == "high":
        dfn *= 1.05
        stamina *= 0.94

    if passing == "short":
        atk *= 1.02
    elif passing == "long":
        atk *= 1.00  # could be better vs certain opponents; kept neutral

    # formation small bias
    if formation in ("4-3-3", "4-2-3-1"):
        atk *= 1.03
    if formation in ("5-3-2",):
        dfn *= 1.04

    return {"atk": atk, "dfn": dfn, "stamina": stamina}


def compute_user_strength(conn: sqlite3.Connection, club: sqlite3.Row) -> Tuple[float, List[Tuple[sqlite3.Row, str]]]:
    """Compute user's strength based on saved lineup + tactics. Returns (strength, xi_pairs)."""
    day = club["season_day"]
    formation = club["formation"]
    xi_pairs = starting_xi_from_saved(conn, day, formation)
    if not xi_pairs:
        return 1.0, []

    base = sum(player_effective_in_slot(p, slot_pos, day) for p, slot_pos in xi_pairs) / max(1, len(xi_pairs))
    tmods = tactics_modifiers(club["formation"], club["mentality"], club["pressing"], club["passing"])
    strength = base * tmods["atk"] * tmods["dfn"]

    # Captain small bonus if captain is in XI (+ tiny bonus if captain has trait "Thủ lĩnh")
    cap = db_one(conn, "SELECT player_id FROM lineup_slot WHERE formation=? AND is_captain=1", (formation,))
    if cap and cap["player_id"]:
        cap_id = cap["player_id"]
        for p, _ in xi_pairs:
            if p["id"] != cap_id:
                continue
            traits = parse_traits(p["traits"] if "traits" in p.keys() else "")
            mods = trait_effect_mods(traits)
            strength *= 1.02 * mods["captain"]
            break
    return strength, xi_pairs

def poisson(lam: float) -> int:
    # Knuth algorithm
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while p > L:
        k += 1
        p *= random.random()
        if k > 20:
            break
    return max(0, k - 1)

def simulate_match(user_strength: float, opp_strength: float, home_adv: float = 1.05) -> Tuple[int, int]:
    # Convert strengths to expected goals
    us = user_strength * home_adv
    os = opp_strength

    # Prevent weird extremes
    us = max(1.0, us)
    os = max(1.0, os)

    share = us / (us + os)  # 0..1
    # Baseline goals per team
    lam_user = clamp(0.6 + 2.6 * share, 0.2, 4.2)
    lam_opp = clamp(0.6 + 2.6 * (1 - share), 0.2, 4.2)

    return poisson(lam_user), poisson(lam_opp)


def expected_goals(user_strength: float, opp_strength: float, home_adv: float = 1.05) -> Tuple[float, float]:
    """Return (xG_user, xG_opp) using the same mapping as simulate_match."""
    us = max(1.0, user_strength * home_adv)
    os = max(1.0, opp_strength)
    share = us / (us + os)
    lam_user = clamp(0.6 + 2.6 * share, 0.2, 4.2)
    lam_opp = clamp(0.6 + 2.6 * (1 - share), 0.2, 4.2)
    return lam_user, lam_opp


def match_odds(user_strength: float, opp_strength: float, home_adv: float = 1.05) -> Dict[str, float]:
    """Rough win/draw/loss odds for UI preview (not used for match simulation)."""
    us = max(1.0, user_strength * home_adv)
    os = max(1.0, opp_strength)
    d = (us - os) / 10.0

    # win probability via logistic on strength diff
    p_win_raw = 1.0 / (1.0 + math.exp(-d))
    # draw higher when teams are close
    p_draw = clamp(0.08 + 0.18 * math.exp(-abs(d)), 0.08, 0.26)
    p_win = p_win_raw * (1.0 - p_draw)
    p_loss = (1.0 - p_win_raw) * (1.0 - p_draw)

    # normalize
    s = p_win + p_draw + p_loss
    if s <= 0:
        return {"win": 0.33, "draw": 0.34, "loss": 0.33}
    return {"win": p_win / s, "draw": p_draw / s, "loss": p_loss / s}

def bid_step(current_bid: int) -> int:
    if current_bid < 1_000_000:
        return 50_000
    if current_bid < 5_000_000:
        return 100_000
    return 250_000

# -----------------------------
# Content generation
# -----------------------------
FIRST = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Hồ"]
LAST = ["Minh", "Huy", "Nam", "Khang", "Đức", "Phúc", "An", "Tùng", "Khôi", "Sơn", "Long", "Duy", "Vinh", "Thắng"]
NICK = ["Sấm Sét", "Bão Táp", "Sư Tử", "Đại Bàng", "Rồng", "Hổ", "Sói", "Cá Mập", "Báo Đen", "Phượng Hoàng"]
CITY = ["Hà Nội", "Sài Gòn", "Đà Nẵng", "Hải Phòng", "Cần Thơ", "Nha Trang", "Huế", "Vũng Tàu", "Bangkok", "Chiang Mai"]
TEAM_SUFFIX = ["FC", "United", "City", "Stars", "Warriors"]

POSITIONS = ["GK", "DL", "DC", "DR", "DMC", "MC", "ML", "MR", "AMC", "AML", "AMR", "ST"]

TRAIT_CATALOG = [
    "Tốc độ",
    "Nhạc trưởng",
    "Sát thủ",
    "Máy quét",
    "Bền bỉ",
    "Học nhanh",
    "Thủ lĩnh",
    "Dễ chấn thương",
    "Chuyên gia đá phạt",
]


def parse_traits(s: Optional[str]) -> List[str]:
    if not s:
        return []
    parts = [p.strip() for p in str(s).split("|")]
    return [p for p in parts if p]


def traits_to_str(traits: List[str]) -> str:
    clean = [t.strip() for t in traits if t and t.strip()]
    # keep unique while preserving order
    out = []
    seen = set()
    for t in clean:
        if t in seen:
            continue
        out.append(t)
        seen.add(t)
    return "|".join(out)


def gen_traits(position: str, finishing: int, passing: int, pace: int, tackling: int, marking: int, stamina_skill: int) -> List[str]:
    """Lightweight traits for fun + small gameplay modifiers."""
    t: List[str] = []

    # stat-driven
    if pace >= 78 and random.random() < 0.45:
        t.append("Tốc độ")
    if passing >= 78 and random.random() < 0.45:
        t.append("Nhạc trưởng")
    if finishing >= 80 and position in ("ST", "AMC", "AML", "AMR") and random.random() < 0.45:
        t.append("Sát thủ")
    if (tackling + marking) >= 155 and position in ("DC", "DL", "DR", "DMC") and random.random() < 0.45:
        t.append("Máy quét")
    if stamina_skill >= 80 and random.random() < 0.45:
        t.append("Bền bỉ")

    # flavor / economy
    if random.random() < 0.18:
        t.append("Học nhanh")
    if random.random() < 0.14:
        t.append("Thủ lĩnh")
    if random.random() < 0.12:
        t.append("Chuyên gia đá phạt")
    if random.random() < 0.10:
        t.append("Dễ chấn thương")

    # cap to 2 traits (prototype)
    random.shuffle(t)
    return t[:2]


def trait_effect_mods(traits: List[str]) -> Dict[str, float]:
    """Return per-player multipliers."""
    gain = 1.0
    injury = 1.0
    fatigue = 1.0
    captain = 1.0
    if "Học nhanh" in traits:
        gain *= 1.12
    if "Dễ chấn thương" in traits:
        injury *= 1.25
    if "Bền bỉ" in traits:
        fatigue *= 0.88
    if "Thủ lĩnh" in traits:
        captain *= 1.01
    return {"gain": gain, "injury": injury, "fatigue": fatigue, "captain": captain}


def compute_role(p: sqlite3.Row) -> str:
    """Derive a simple role label from stats & position."""
    pos = p["position"]
    fin, pas, pac = p["finishing"], p["passing"], p["pace"]
    tac, mar, sta = p["tackling"], p["marking"], p["stamina_skill"]

    if pos == "GK":
        return "Sweeper Keeper" if pas >= 66 else "Shot Stopper"
    if pos in ("DC",):
        if pas >= 72:
            return "Ball-playing Defender"
        if pac >= 72:
            return "Cover Defender"
        if tac >= 75 or mar >= 75:
            return "Stopper"
        return "Defender"
    if pos in ("DL", "DR"):
        if pac >= 75 and sta >= 70:
            return "Wingback"
        if tac >= 75 or mar >= 75:
            return "Defensive Fullback"
        return "Fullback"
    if pos == "DMC":
        if pas >= 72 and tac >= 70:
            return "Deep-lying Playmaker"
        if (tac + mar) >= 150:
            return "Destroyer"
        return "Holding Mid"
    if pos in ("MC",):
        if pas >= 78:
            return "Playmaker"
        if sta >= 78 and tac >= 66:
            return "Box-to-box"
        if (tac + mar) >= 145:
            return "Ball Winner"
        return "Central Mid"
    if pos in ("AMC",):
        if pas >= 78:
            return "Advanced Playmaker"
        if fin >= 76:
            return "Shadow Striker"
        return "Attacking Mid"
    if pos in ("ML", "MR", "AML", "AMR"):
        if pac >= 78:
            return "Winger"
        if pas >= 75:
            return "Wide Playmaker"
        return "Wide Mid"
    if pos == "ST":
        if pas >= 76 and pas >= fin - 4:
            return "False 9"
        if pac >= 80 and pac > fin:
            return "Speed Striker"
        if sta >= 80 and fin >= 70:
            return "Pressing Forward"
        return "Poacher"
    return "Utility"


def backfill_player_traits(conn: sqlite3.Connection) -> int:
    """Add random traits for players that don't have any yet (for migrated DBs)."""
    players = db_all(
        conn,
        "SELECT id, position, finishing, passing, pace, tackling, marking, stamina_skill, traits FROM squad_player",
    )
    n = 0
    for p in players:
        cur = (p["traits"] or "").strip() if "traits" in p.keys() else ""
        if cur:
            continue
        traits = traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"]))
        db_exec(conn, "UPDATE squad_player SET traits=? WHERE id=?", (traits, p["id"]))
        n += 1
    return n

def gen_name() -> str:
    return f"{random.choice(FIRST)} {random.choice(LAST)}"

def gen_team_name(existing: set) -> str:
    for _ in range(50):
        name = f"{random.choice(CITY)} {random.choice(NICK)} {random.choice(TEAM_SUFFIX)}"
        if name not in existing:
            return name
    return f"{random.choice(CITY)} {random.choice(NICK)} {random.randint(10,99)}"

def gen_player(position: str, base: int, spread: int, age_range=(18, 32)) -> Dict:
    age = random.randint(*age_range)
    # Base stats: position-biased
    def stat(mu: int, sd: int) -> int:
        return int(clamp(random.gauss(mu, sd), 1, 100))

    if position == "GK":
        finishing = stat(base-25, spread)
        passing = stat(base-10, spread)
        pace = stat(base-20, spread)
        tackling = stat(base+10, spread)
        marking = stat(base+12, spread)
        stamina_skill = stat(base, spread)
    elif position in ("DC", "DL", "DR"):
        finishing = stat(base-15, spread)
        passing = stat(base-5, spread)
        pace = stat(base, spread)
        tackling = stat(base+10, spread)
        marking = stat(base+8, spread)
        stamina_skill = stat(base+3, spread)
    elif position in ("DMC",):
        finishing = stat(base-8, spread)
        passing = stat(base+3, spread)
        pace = stat(base, spread)
        tackling = stat(base+6, spread)
        marking = stat(base+3, spread)
        stamina_skill = stat(base+5, spread)
    elif position in ("MC", "AMC", "ML", "MR", "AML", "AMR"):
        finishing = stat(base+2, spread)
        passing = stat(base+8, spread)
        pace = stat(base+6, spread)
        tackling = stat(base-2, spread)
        marking = stat(base-4, spread)
        stamina_skill = stat(base+3, spread)
    else:  # ST
        finishing = stat(base+14, spread)
        passing = stat(base, spread)
        pace = stat(base+10, spread)
        tackling = stat(base-12, spread)
        marking = stat(base-12, spread)
        stamina_skill = stat(base+2, spread)

    potential = int(clamp(random.gauss(base + 10, spread * 1.1), 20, 100))
    salary = int(2_000 + base * 220 + random.randint(-800, 1200))
    return {
        "name": gen_name(),
        "position": position,
        "age": age,
        "finishing": finishing,
        "passing": passing,
        "pace": pace,
        "tackling": tackling,
        "marking": marking,
        "stamina_skill": stamina_skill,
        "potential": potential,
        "condition": random.randint(75, 95),
        "morale": random.randint(70, 90),
        "form": random.randint(65, 90),
        "injured_until_day": 0,
        "salary": salary,
    }

# -----------------------------
# DB init & season generation
# -----------------------------
def init_db() -> None:
    conn = get_db()
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS club (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        cash INTEGER NOT NULL,
        tokens INTEGER NOT NULL,
        formation TEXT NOT NULL,
        mentality TEXT NOT NULL,
        pressing TEXT NOT NULL,
        passing TEXT NOT NULL,
        season_day INTEGER NOT NULL,
        market_reset_at INTEGER NOT NULL
    )
    """)

    # lightweight migrations for existing DBs
    ensure_column(conn, "club", "last_friendly_day", "INTEGER NOT NULL DEFAULT -1")
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS facility (
        name TEXT PRIMARY KEY,
        level INTEGER NOT NULL,
        build_complete_at INTEGER
    )
    """)
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS squad_player (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        position TEXT NOT NULL,
        age INTEGER NOT NULL,
        finishing INTEGER NOT NULL,
        passing INTEGER NOT NULL,
        pace INTEGER NOT NULL,
        tackling INTEGER NOT NULL,
        marking INTEGER NOT NULL,
        stamina_skill INTEGER NOT NULL,
        traits TEXT,
        potential INTEGER NOT NULL,
        condition INTEGER NOT NULL,
        morale INTEGER NOT NULL,
        form INTEGER NOT NULL,
        injured_until_day INTEGER NOT NULL,
        salary INTEGER NOT NULL
    )
    """)

    # lightweight migrations for existing DBs
    ensure_column(conn, "squad_player", "traits", "TEXT")

    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS player_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        day INTEGER NOT NULL,
        condition INTEGER NOT NULL,
        morale INTEGER NOT NULL,
        form INTEGER NOT NULL,
        note TEXT,
        created_at INTEGER NOT NULL
    )
    """)
    db_exec(conn, "CREATE INDEX IF NOT EXISTS idx_player_log_pid_day ON player_log(player_id, day)")
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS market_listing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        position TEXT NOT NULL,
        age INTEGER NOT NULL,
        finishing INTEGER NOT NULL,
        passing INTEGER NOT NULL,
        pace INTEGER NOT NULL,
        tackling INTEGER NOT NULL,
        marking INTEGER NOT NULL,
        stamina_skill INTEGER NOT NULL,
        potential INTEGER NOT NULL,
        currency TEXT NOT NULL,         -- 'cash' or 'token'
        start_price INTEGER NOT NULL,
        current_bid INTEGER NOT NULL,
        highest_bidder TEXT NOT NULL,   -- 'none', 'you', 'ai'
        user_has_bid INTEGER NOT NULL DEFAULT 0,
        batch_ends_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_ai_update INTEGER NOT NULL
    )
    """)

    # Ensure column exists for older DBs
    ensure_column(conn, "market_listing", "user_has_bid", "INTEGER NOT NULL DEFAULT 0")

    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS lineup_slot (
        formation TEXT NOT NULL,
        slot_index INTEGER NOT NULL,
        position TEXT NOT NULL,
        player_id INTEGER,
        is_captain INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (formation, slot_index)
    )
    """)
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS team (
        name TEXT PRIMARY KEY,
        base_strength INTEGER NOT NULL
    )
    """)
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS fixture (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competition TEXT NOT NULL,
        day INTEGER NOT NULL,
        round INTEGER NOT NULL,
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        played INTEGER NOT NULL DEFAULT 0,
        home_goals INTEGER,
        away_goals INTEGER
    )
    """)

    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    if club is None:
        db_exec(conn, """
            INSERT INTO club (id, name, cash, tokens, formation, mentality, pressing, passing, season_day, market_reset_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("My FC", 5_000_000, 40, "4-4-2", "balanced", "normal", "short", 0, now_ts() + MARKET_BATCH_SECONDS))
        # facilities
        for name in ["stadium", "training_ground", "medical_center", "youth_academy"]:
            db_exec(conn, "INSERT OR REPLACE INTO facility (name, level, build_complete_at) VALUES (?, ?, NULL)", (name, 1))
        # initial squad
        seed_initial_squad(conn)
        # log initial squad state (day 0)
        log_all_players_missing(conn, 0, note="init")
        # season teams & fixtures
        generate_season(conn)

    # Backfill traits for older saves (if missing)
    backfill_player_traits(conn)

    # Ensure we have at least one log row for current day (for migrated saves)
    club2 = db_one(conn, "SELECT season_day FROM club WHERE id=1")
    if club2 is not None:
        log_all_players_missing(conn, int(club2["season_day"]), note="daily")

    conn.close()

def seed_initial_squad(conn: sqlite3.Connection) -> None:
    random.seed(42)
    # 18 players
    roster_positions = ["GK","GK","DL","DC","DC","DR","DMC","MC","MC","ML","MR","AMC","AML","AMR","ST","ST","DC","MC"]
    for pos in roster_positions:
        p = gen_player(pos, base=55, spread=10, age_range=(19, 30))
        db_exec(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential, condition, morale, form, injured_until_day, salary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"], p["position"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"])),
              p["potential"], p["condition"], p["morale"], p["form"], p["injured_until_day"], p["salary"]))

    # reset RNG seed so the rest of the game is not deterministic
    random.seed()

def round_robin_schedule(teams: List[str]) -> List[List[Tuple[str, str]]]:
    """
    Circle method for even number of teams.
    Returns rounds, each round list of (home, away).
    """
    n = len(teams)
    assert n % 2 == 0
    arr = teams[:]
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            t1 = arr[i]
            t2 = arr[n - 1 - i]
            # alternate home/away
            if r % 2 == 0:
                pairs.append((t1, t2))
            else:
                pairs.append((t2, t1))
        rounds.append(pairs)
        # rotate (keep first fixed)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return rounds

def generate_season(conn: sqlite3.Connection) -> None:
    # Clear previous season data (if any)
    db_exec(conn, "DELETE FROM team")
    db_exec(conn, "DELETE FROM fixture")

    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    user_team = club["name"]

    existing = {user_team}
    ai_teams = [gen_team_name(existing) for _ in range(7)]
    for t in ai_teams:
        existing.add(t)

    teams = [user_team] + ai_teams

    # Save teams with base strengths
    for t in teams:
        if t == user_team:
            base_strength = 60
        else:
            base_strength = random.randint(52, 68)
        db_exec(conn, "INSERT INTO team (name, base_strength) VALUES (?, ?)", (t, base_strength))

    # League fixtures on odd days: 1,3,5,7,9,11,13 (7 rounds)
    rr = round_robin_schedule(teams)
    day = 1
    round_no = 1
    for pairs in rr:
        for home, away in pairs:
            db_exec(conn, "INSERT INTO fixture (competition, day, round, home, away) VALUES (?, ?, ?, ?, ?)",
                    ("League", day, round_no, home, away))
        day += 2
        round_no += 1

    # Cup (knockout) — quarter day 2, semi day 6, final day 10
    # Random seeding
    seeded = teams[:]
    random.shuffle(seeded)
    # quarterfinals
    q_pairs = list(zip(seeded[0::2], seeded[1::2]))
    for i, (home, away) in enumerate(q_pairs, start=1):
        db_exec(conn, "INSERT INTO fixture (competition, day, round, home, away) VALUES (?, ?, ?, ?, ?)",
                ("Cup", 2, 1, home, away))

    # Champions Cup (knockout) — quarter day 4, semi day 8, final day 12
    seeded2 = teams[:]
    random.shuffle(seeded2)
    q2_pairs = list(zip(seeded2[0::2], seeded2[1::2]))
    for i, (home, away) in enumerate(q2_pairs, start=1):
        db_exec(conn, "INSERT INTO fixture (competition, day, round, home, away) VALUES (?, ?, ?, ?, ?)",
                ("Champions", 4, 1, home, away))

    # Super Cup (one-off) — day 0: user vs strongest AI
    strongest_ai = max(ai_teams, key=lambda t: db_one(conn, "SELECT base_strength FROM team WHERE name=?", (t,))["base_strength"])
    db_exec(conn, "INSERT INTO fixture (competition, day, round, home, away) VALUES (?, ?, ?, ?, ?)",
            ("SuperCup", 0, 1, user_team, strongest_ai))

# -----------------------------
# Market mechanics
# -----------------------------
def regen_market_batch(conn: sqlite3.Connection, reset_at: int) -> None:
    # Wipe listings and create new batch
    db_exec(conn, "DELETE FROM market_listing")
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = club["season_day"]
    # As you progress in season, average market improves slightly
    base = int(clamp(52 + day * 0.6, 45, 80))
    spread = 10

    for _ in range(MARKET_LISTINGS_PER_BATCH):
        # Decide currency and quality band
        if random.random() < 0.30:
            currency = "token"
            q_base = base + random.randint(6, 14)
            start_price = random.randint(2, 10)  # tokens
        else:
            currency = "cash"
            q_base = base + random.randint(-4, 10)
            start_price = random.randint(150_000, 1_200_000)

        pos = random.choice(POSITIONS)
        p = gen_player(pos, base=q_base, spread=spread, age_range=(18, 31))

        db_exec(conn, """
            INSERT INTO market_listing
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, potential,
             currency, start_price, current_bid, highest_bidder, user_has_bid, batch_ends_at, created_at, last_ai_update)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"], p["position"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], p["potential"], currency, start_price, start_price, "none", 0, reset_at, now_ts(), now_ts()))

def update_market(conn: sqlite3.Connection) -> None:
    club = db_one(conn, "SELECT market_reset_at FROM club WHERE id=1")
    reset_at = club["market_reset_at"]
    ts = now_ts()

    # If batch expired or empty, finalize & regenerate
    listings = db_all(conn, "SELECT * FROM market_listing")
    if ts >= reset_at or len(listings) == 0:
        finalize_market_batch(conn)
        new_reset = ts + MARKET_BATCH_SECONDS
        db_exec(conn, "UPDATE club SET market_reset_at=? WHERE id=1", (new_reset,))
        regen_market_batch(conn, new_reset)
        return

    # Otherwise, simulate AI bids a bit (deterministic-ish per request)
    simulate_ai_bids(conn, ts)

def finalize_market_batch(conn: sqlite3.Connection) -> None:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    cash = club["cash"]
    tokens = club["tokens"]

    purchased: List[Tuple[str, str, int]] = []  # (name, currency, price)
    skipped: List[Tuple[str, str, int]] = []

    listings = db_all(conn, "SELECT * FROM market_listing")
    for l in listings:
        if l["highest_bidder"] != "you":
            continue

        # Pay (if possible). If you oversubscribed your funds by bidding multiple auctions,
        # the extra wins will be skipped.
        if l["currency"] == "cash":
            if cash < l["current_bid"]:
                skipped.append((l["name"], "cash", int(l["current_bid"])))
                continue
            cash -= l["current_bid"]
        else:
            if tokens < l["current_bid"]:
                skipped.append((l["name"], "token", int(l["current_bid"])))
                continue
            tokens -= l["current_bid"]

        # Add player to squad
        traits = traits_to_str(gen_traits(l["position"], l["finishing"], l["passing"], l["pace"], l["tackling"], l["marking"], l["stamina_skill"]))
        pid = db_insert(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential,
             condition, morale, form, injured_until_day, salary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (l["name"], l["position"], l["age"], l["finishing"], l["passing"], l["pace"], l["tackling"], l["marking"],
              l["stamina_skill"], traits, l["potential"], random.randint(75, 95), random.randint(70, 90), random.randint(65, 90), 0,
              int(2_000 + l["current_bid"] * 0.002)))

        log_player_state(conn, pid, int(club["season_day"]), note="transfer")
        purchased.append((l["name"], l["currency"], int(l["current_bid"])))

    # Persist wallet updates once
    db_exec(conn, "UPDATE club SET cash=?, tokens=? WHERE id=1", (cash, tokens))

    # remove listings
    db_exec(conn, "DELETE FROM market_listing")

    # Notify user (only if inside request)
    if has_request_context():
        if purchased:
            parts = []
            for name, cur, price in purchased:
                if cur == "cash":
                    parts.append(f"{name} (💵 {fmt_money(price)})")
                else:
                    parts.append(f"{name} (🪙 {price})")
            flash("✅ Mua thành công: " + ", ".join(parts))
        if skipped:
            parts = []
            for name, cur, price in skipped:
                if cur == "cash":
                    parts.append(f"{name} (💵 {fmt_money(price)})")
                else:
                    parts.append(f"{name} (🪙 {price})")
            flash("⚠️ Bạn đang dẫn ở một số phiên nhưng không đủ tiền/token nên bị bỏ qua: " + ", ".join(parts))

def simulate_ai_bids(conn: sqlite3.Connection, ts: int) -> None:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    reset_at = club["market_reset_at"]
    listings = db_all(conn, "SELECT * FROM market_listing")
    time_left = max(0, reset_at - ts)

    for l in listings:
        # Only simulate if auction has at least 10 seconds left
        if time_left < 10:
            continue
        last_upd = l["last_ai_update"]
        if ts <= last_upd:
            continue

        # Interest depends on quality & currency
        fake_row = l  # for overall calc
        quality = player_overall(fake_row)
        base_rate = 0.0025  # bids per second baseline
        if l["currency"] == "token":
            base_rate *= 1.4
        # More quality -> more interest
        base_rate *= clamp(quality / 65.0, 0.6, 1.8)
        # More near the end -> more aggressive
        base_rate *= clamp(1.2 - (time_left / MARKET_BATCH_SECONDS), 0.6, 1.4)

        dt = ts - last_upd
        expected = base_rate * dt
        n_bids = poisson(expected)

        current_bid = l["current_bid"]
        highest = l["highest_bidder"]

        for _ in range(n_bids):
            # AI doesn't always outbid you; sometimes stops
            if highest == "you" and random.random() < 0.45:
                break
            # Raise
            if l["currency"] == "cash":
                current_bid += bid_step(current_bid)
            else:
                current_bid += 1
            highest = "ai"

        if n_bids > 0:
            db_exec(conn, "UPDATE market_listing SET current_bid=?, highest_bidder=?, last_ai_update=? WHERE id=?",
                    (current_bid, highest, ts, l["id"]))
        else:
            # just update last_ai_update to avoid huge dt
            db_exec(conn, "UPDATE market_listing SET last_ai_update=? WHERE id=?", (ts, l["id"]))

# -----------------------------
# Day progression & recovery
# -----------------------------


def log_player_state(conn: sqlite3.Connection, player_id: int, day: int, note: str = "daily") -> None:
    p = db_one(conn, "SELECT condition, morale, form FROM squad_player WHERE id=?", (player_id,))
    if not p:
        return
    db_exec(
        conn,
        "INSERT INTO player_log (player_id, day, condition, morale, form, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (player_id, day, int(p["condition"]), int(p["morale"]), int(p["form"]), note, now_ts()),
    )


def log_all_players_missing(conn: sqlite3.Connection, day: int, note: str = "daily") -> int:
    """Ensure each player has at least one log row for a given day."""
    ids = [r["id"] for r in db_all(conn, "SELECT id FROM squad_player")]
    created = 0
    for pid in ids:
        exists = db_one(conn, "SELECT 1 FROM player_log WHERE player_id=? AND day=?", (pid, day))
        if exists:
            continue
        log_player_state(conn, pid, day, note=note)
        created += 1
    return created
def advance_day(conn: sqlite3.Connection) -> None:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = club["season_day"] + 1
    db_exec(conn, "UPDATE club SET season_day=? WHERE id=1", (day,))

    eff = facility_effects(conn)

    # Recover players & reduce injury durations by day progression
    players = db_all(conn, "SELECT * FROM squad_player")
    for p in players:
        # condition recovery
        cond = p["condition"]
        cond = int(clamp(cond + 10, 0, 100))

        # morale drift upward a bit
        morale = int(clamp(p["morale"] + 2, 0, 100))
        form = int(clamp(p["form"] + 1, 0, 100))

        db_exec(conn, "UPDATE squad_player SET condition=?, morale=?, form=? WHERE id=?",
                (cond, morale, form, p["id"]))

    # Youth intake every 7 days
    if day % 7 == 0:
        youth_intake(conn)

    # Let other teams play their matches up to this day (world keeps moving)
    auto_play_ai_fixtures(conn, up_to_day=day, user_team=club["name"])

    # Log daily snapshot for all players
    log_all_players_missing(conn, day, note="daily")

def youth_intake(conn: sqlite3.Connection) -> None:
    eff = facility_effects(conn)
    bonus = eff["youth_quality_bonus"]
    # 1-2 youth players
    k = 1 if random.random() < 0.6 else 2
    for _ in range(k):
        pos = random.choice(POSITIONS)
        base = int(clamp(45 + bonus + random.randint(-2, 6), 35, 80))
        p = gen_player(pos, base=base, spread=9, age_range=(16, 18))
        p["salary"] = int(1200 + base * 120)
        pid = db_insert(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential,
             condition, morale, form, injured_until_day, salary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"] + " (U)", p["position"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"])),
              p["potential"], 85, 80, 70, 0, p["salary"]))
        log_player_state(conn, pid, db_one(conn, "SELECT season_day FROM club WHERE id=1")["season_day"], note="youth")

# -----------------------------
# Flask app
# -----------------------------
app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "static"))
app.secret_key = "touchline-boss-prototype"

@app.before_request
def _ensure_db():
    init_db()
    # Keep market ticking even if the user is not on the Market page
    conn = get_db()
    try:
        update_market(conn)
        club = db_one(conn, "SELECT name, season_day FROM club WHERE id=1")
        if club is not None:
            auto_play_ai_fixtures(conn, up_to_day=int(club["season_day"]), user_team=club["name"])
    finally:
        conn.close()

def load_context(conn: sqlite3.Connection) -> Dict:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    return {
        "club": club,
        "fmt_money": fmt_money,
        "now_ts": now_ts,
    }

@app.route("/")
def home():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    # Simulate AI vs AI fixtures up to today so the tables feel alive
    auto_play_ai_fixtures(conn, up_to_day=int(club["season_day"]), user_team=club["name"])
    eff = facility_effects(conn)
    # upcoming fixtures for today
    fixtures = db_all(conn, "SELECT * FROM fixture WHERE day=? ORDER BY competition, round", (club["season_day"],))
    # show some squad summary
    players = db_all(conn, "SELECT * FROM squad_player")
    avg = 0.0
    if players:
        avg = sum(player_effective_rating(p, club["season_day"]) for p in players) / len(players)
    ctx = load_context(conn)
    conn.close()
    return render_template("home.html", **ctx, effects=eff, fixtures=fixtures, avg_rating=avg)

@app.post("/advance_day")
def do_advance_day():
    conn = get_db()
    advance_day(conn)
    conn.close()
    flash("Đã sang ngày mới.")
    return redirect(url_for("home"))

@app.route("/squad")
def squad():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    players = db_all(conn, "SELECT * FROM squad_player ORDER BY position, id")
    # compute overall
    squad_list = []
    for p in players:
        squad_list.append({
            **dict(p),
            "overall": round(player_overall(p), 1),
            "effective": round(player_effective_rating(p, club["season_day"]), 1),
            "injured": (p["injured_until_day"] or 0) > club["season_day"],
        })
    ctx = load_context(conn)
    conn.close()
    return render_template("squad.html", **ctx, players=squad_list)


@app.route("/player/<int:pid>")
def player_detail(pid: int):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    p = db_one(conn, "SELECT * FROM squad_player WHERE id=?", (pid,))
    if p is None:
        conn.close()
        flash("Không tìm thấy cầu thủ.")
        return redirect(url_for("squad"))

    # Backfill traits if missing
    if not (p["traits"] or "").strip():
        traits = traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"]))
        db_exec(conn, "UPDATE squad_player SET traits=? WHERE id=?", (traits, pid))
        p = db_one(conn, "SELECT * FROM squad_player WHERE id=?", (pid,))

    role = compute_role(p)
    traits_list = parse_traits(p["traits"])

    day = int(club["season_day"])
    view = {
        **dict(p),
        "overall": round(player_overall(p), 1),
        "effective": round(player_effective_rating(p, day), 1),
        "injured": (p["injured_until_day"] or 0) > day,
    }

    logs = db_all(
        conn,
        "SELECT day, condition, morale, form, note FROM player_log WHERE player_id=? ORDER BY day DESC, id DESC LIMIT 20",
        (pid,),
    )
    logs = list(reversed([dict(r) for r in logs]))

    ctx = load_context(conn)
    conn.close()
    return render_template("player_detail.html", **ctx, player=view, role=role, traits=traits_list, logs=logs)


@app.post("/player/<int:pid>/rename")
def player_rename(pid: int):
    new_name = clean_name(request.form.get("new_name", ""), max_len=28)
    if len(new_name) < 2:
        flash("Tên cầu thủ quá ngắn.")
        return redirect(url_for("player_detail", pid=pid))

    conn = get_db()
    p = db_one(conn, "SELECT name FROM squad_player WHERE id=?", (pid,))
    if p is None:
        conn.close()
        flash("Không tìm thấy cầu thủ.")
        return redirect(url_for("squad"))

    db_exec(conn, "UPDATE squad_player SET name=? WHERE id=?", (new_name, pid))
    conn.close()
    flash(f"Đã đổi tên cầu thủ: {p['name']} → {new_name}")
    return redirect(url_for("player_detail", pid=pid))


@app.post("/club/rename")
def club_rename():
    new_name = clean_name(request.form.get("new_name", ""), max_len=28)
    if len(new_name) < 3:
        flash("Tên CLB quá ngắn (tối thiểu 3 ký tự).")
        return redirect(url_for("home"))

    conn = get_db()
    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    if club is None:
        conn.close()
        flash("Không tìm thấy CLB.")
        return redirect(url_for("home"))
    old = club["name"]
    if new_name == old:
        conn.close()
        flash("Tên CLB không thay đổi.")
        return redirect(url_for("home"))

    exists = db_one(conn, "SELECT 1 FROM team WHERE name=?", (new_name,))
    if exists is not None:
        conn.close()
        flash("Tên này đã tồn tại trong giải. Hãy chọn tên khác.")
        return redirect(url_for("home"))

    # Update everywhere
    db_exec(conn, "UPDATE club SET name=? WHERE id=1", (new_name,))
    db_exec(conn, "UPDATE team SET name=? WHERE name=?", (new_name, old))
    db_exec(conn, "UPDATE fixture SET home=? WHERE home=?", (new_name, old))
    db_exec(conn, "UPDATE fixture SET away=? WHERE away=?", (new_name, old))
    conn.close()
    flash(f"Đã đổi tên CLB: {old} → {new_name}")
    return redirect(url_for("home"))

@app.post("/sell/<int:pid>")
def sell_player(pid: int):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    p = db_one(conn, "SELECT * FROM squad_player WHERE id=?", (pid,))
    if p is None:
        conn.close()
        flash("Không tìm thấy cầu thủ.")
        return redirect(url_for("squad"))

    # Value based on overall & age
    ov = player_overall(p)
    age = p["age"]
    age_factor = 1.0 - max(0, age - 26) * 0.02  # older reduces value a bit
    value = int(clamp(ov * 85_000 * age_factor, 50_000, 15_000_000))

    db_exec(conn, "DELETE FROM squad_player WHERE id=?", (pid,))
    db_exec(conn, "UPDATE club SET cash=cash+? WHERE id=1", (value,))
    conn.close()
    flash(f"Đã bán {p['name']} nhận {fmt_money(value)} tiền mặt.")
    return redirect(url_for("squad"))

@app.route("/market")
def market():
    conn = get_db()
    update_market(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    listings = db_all(conn, "SELECT * FROM market_listing")
    # decorate
    rows: List[Dict] = []
    for l in listings:
        ov = player_overall(l)
        rows.append({
            **dict(l),
            "overall": round(ov, 1),
            "bid_step": bid_step(l["current_bid"]) if l["currency"] == "cash" else 1,
        })

    # Default ordering: your watched items first, then currently leading, then quality/price
    rows.sort(
        key=lambda r: (
            int(r.get("user_has_bid", 0)),
            1 if r.get("highest_bidder") == "you" else 0,
            1 if r.get("currency") == "token" else 0,
            float(r.get("overall", 0.0)),
            int(r.get("current_bid", 0)),
        ),
        reverse=True,
    )

    watchlist = [r for r in rows if int(r.get("user_has_bid", 0)) == 1]
    seconds_left = max(0, club["market_reset_at"] - now_ts())
    ctx = load_context(conn)
    conn.close()
    return render_template("market.html", **ctx, listings=rows, watchlist=watchlist, seconds_left=seconds_left)

@app.post("/bid/<int:lid>")
def bid(lid: int):
    conn = get_db()
    update_market(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    l = db_one(conn, "SELECT * FROM market_listing WHERE id=?", (lid,))
    if l is None:
        conn.close()
        flash("Phiên đấu giá không còn tồn tại (đã reset).")
        return redirect(url_for("market"))

    # Determine next bid
    if l["currency"] == "cash":
        inc = bid_step(l["current_bid"])
        next_bid = l["current_bid"] + inc
        if club["cash"] < next_bid:
            conn.close()
            flash("Bạn không đủ tiền mặt để trả giá mức này.")
            return redirect(url_for("market"))
    else:
        next_bid = l["current_bid"] + 1
        if club["tokens"] < next_bid:
            conn.close()
            flash("Bạn không đủ token để trả giá mức này.")
            return redirect(url_for("market"))

    db_exec(conn, "UPDATE market_listing SET current_bid=?, highest_bidder=?, user_has_bid=1 WHERE id=?", (next_bid, "you", lid))
    conn.close()
    flash("Đã trả giá!")
    return redirect(url_for("market"))

@app.route("/training")
def training():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    players = db_all(conn, "SELECT * FROM squad_player ORDER BY position, id")
    healthy = [p for p in players if (p["injured_until_day"] or 0) <= club["season_day"]]
    injured = [p for p in players if (p["injured_until_day"] or 0) > club["season_day"]]
    ctx = load_context(conn)
    eff = facility_effects(conn)
    conn.close()
    return render_template("training.html", **ctx, healthy=len(healthy), injured=len(injured), effects=eff)

@app.post("/training")
def do_training():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    focus = request.form.get("focus", "attack")
    intensity = request.form.get("intensity", "normal")

    eff = facility_effects(conn)
    gain_mult = eff["train_gain_mult"]
    injury_mult = eff["train_injury_mult"]

    # Intensity setup
    if intensity == "light":
        gain = 0.8
        fatigue = 6
        inj_base = 0.008
    elif intensity == "hard":
        gain = 1.25
        fatigue = 14
        inj_base = 0.020
    else:
        gain = 1.0
        fatigue = 10
        inj_base = 0.013

    gain *= gain_mult

    players = db_all(conn, "SELECT * FROM squad_player")
    trained = 0
    injuries = 0
    for p in players:
        if (p["injured_until_day"] or 0) > club["season_day"]:
            continue

        traits = parse_traits(p["traits"] if "traits" in p.keys() else "")
        mods = trait_effect_mods(traits)

        # apply fatigue
        new_cond = int(clamp(p["condition"] - int(round(fatigue * mods["fatigue"])), 0, 100))
        # stat gains
        delta = max(1, int(random.randint(1, 3) * gain * mods["gain"]))
        fin, pas, pac, tac, mar, sta = p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"]

        if focus == "attack":
            fin += delta + random.randint(0, 1)
            pac += random.randint(0, 1)
        elif focus == "defense":
            tac += delta
            mar += delta
        elif focus == "fitness":
            sta += delta
            pac += random.randint(0, 1)
        else:  # tactics
            pas += delta
            mar += random.randint(0, 1)

        # cap stats
        fin = int(clamp(fin, 1, 100))
        pas = int(clamp(pas, 1, 100))
        pac = int(clamp(pac, 1, 100))
        tac = int(clamp(tac, 1, 100))
        mar = int(clamp(mar, 1, 100))
        sta = int(clamp(sta, 1, 100))

        # injury check
        # risk increases when condition low
        cond_factor = clamp(1.0 + (0.8 - new_cond/100.0), 0.8, 1.8)
        risk = inj_base * cond_factor * injury_mult * mods["injury"]
        if random.random() < risk:
            injuries += 1
            # injury duration in days, reduced by medical
            days = random.randint(1, 5)
            days = max(1, int(math.ceil(days * eff["injury_days_mult"])))
            injured_until = club["season_day"] + days
        else:
            injured_until = p["injured_until_day"]

        db_exec(conn, """
            UPDATE squad_player
            SET finishing=?, passing=?, pace=?, tackling=?, marking=?, stamina_skill=?,
                condition=?, injured_until_day=?
            WHERE id=?
        """, (fin, pas, pac, tac, mar, sta, new_cond, injured_until, p["id"]))
        trained += 1

    conn.close()
    flash(f"Đã tập luyện cho {trained} cầu thủ. Chấn thương mới: {injuries}.")
    return redirect(url_for("training"))

@app.route("/facilities")
def facilities():
    conn = get_db()
    finalize_builds(conn)
    facs = db_all(conn, "SELECT * FROM facility ORDER BY name")
    facilities_view = []
    ts = now_ts()
    for f in facs:
        lvl = f["level"]
        next_lvl = lvl + 1
        cost = upgrade_cost(f["name"], next_lvl)
        tsec = upgrade_time_seconds(f["name"], next_lvl)
        remaining = 0
        if f["build_complete_at"] is not None and ts < f["build_complete_at"]:
            remaining = f["build_complete_at"] - ts
        facilities_view.append({
            **dict(f),
            "next_level": next_lvl,
            "next_cost": cost,
            "next_time": tsec,
            "remaining": remaining,
        })
    ctx = load_context(conn)
    eff = facility_effects(conn)
    conn.close()
    return render_template("facilities.html", **ctx, facilities=facilities_view, effects=eff)

def upgrade_cost(name: str, level: int) -> int:
    # next level cost
    base = {
        "stadium": 250_000,
        "training_ground": 220_000,
        "medical_center": 180_000,
        "youth_academy": 200_000,
    }.get(name, 200_000)
    return int(base * (1.18 ** (level - 1)))

def upgrade_time_seconds(name: str, level: int) -> int:
    # short for prototype
    return int(20 + level * 10)

@app.post("/upgrade/<name>")
def upgrade(name: str):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    fac = db_one(conn, "SELECT * FROM facility WHERE name=?", (name,))
    if fac is None:
        conn.close()
        flash("Không tìm thấy công trình.")
        return redirect(url_for("facilities"))
    if fac["build_complete_at"] is not None and now_ts() < fac["build_complete_at"]:
        conn.close()
        flash("Công trình đang nâng cấp, chưa xong.")
        return redirect(url_for("facilities"))

    next_level = fac["level"] + 1
    cost = upgrade_cost(name, next_level)
    if club["cash"] < cost:
        conn.close()
        flash("Không đủ tiền mặt để nâng cấp.")
        return redirect(url_for("facilities"))

    done_at = now_ts() + upgrade_time_seconds(name, next_level)
    db_exec(conn, "UPDATE club SET cash=cash-? WHERE id=1", (cost,))
    db_exec(conn, "UPDATE facility SET level=?, build_complete_at=? WHERE name=?", (next_level, done_at, name))
    conn.close()
    flash("Đã bắt đầu nâng cấp. Chờ một chút rồi tải lại trang.")
    return redirect(url_for("facilities"))

def finalize_builds(conn: sqlite3.Connection) -> None:
    ts = now_ts()
    facs = db_all(conn, "SELECT * FROM facility WHERE build_complete_at IS NOT NULL")
    for f in facs:
        if ts >= f["build_complete_at"]:
            db_exec(conn, "UPDATE facility SET build_complete_at=NULL WHERE name=?", (f["name"],))

@app.route("/competitions")
def competitions():
    conn = get_db()
    finalize_builds(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = club["season_day"]

    # AI teams also play their fixtures (league/cup/champions)
    auto_play_ai_fixtures(conn, up_to_day=int(day), user_team=club["name"])

    # fixtures pending for user (allow playing missed matches too)
    my = club["name"]
    pending = db_all(conn, """
        SELECT * FROM fixture
        WHERE played=0 AND day<=? AND (home=? OR away=?)
        ORDER BY day ASC, competition ASC, round ASC
    """, (day, my, my))

    # fixtures today (for the home page style card)
    todays = db_all(conn, """
        SELECT * FROM fixture
        WHERE day=? AND (home=? OR away=?)
        ORDER BY competition
    """, (day, my, my))

    # standings league
    standings = compute_league_table(conn)
    # recent results
    recent = db_all(conn, "SELECT * FROM fixture WHERE played=1 ORDER BY day DESC, id DESC LIMIT 12")

    # Previews for pending matches (user only)
    previews: Dict[int, Dict] = {}
    if pending:
        user_strength, _ = compute_user_strength(conn, club)
        for f in pending:
            my = club["name"]
            opp_name = f["away"] if f["home"] == my else f["home"]
            opp_base = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (opp_name,))
            opp_strength = float((opp_base["base_strength"] if opp_base else 60))
            home_adv = 1.05 if f["home"] == my else 1.00
            odds = match_odds(user_strength, opp_strength, home_adv=home_adv)
            xg_u, xg_o = expected_goals(user_strength, opp_strength, home_adv=home_adv)
            previews[f["id"]] = {
                "win": int(round(odds["win"] * 100)),
                "draw": int(round(odds["draw"] * 100)),
                "loss": int(round(odds["loss"] * 100)),
                "xg_u": round(xg_u, 2),
                "xg_o": round(xg_o, 2),
            }

    ctx = load_context(conn)
    conn.close()
    return render_template("competitions.html", **ctx, pending=pending, todays=todays, standings=standings, recent=recent, previews=previews)


@app.route("/friendly")
def friendly():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    strength, _ = compute_user_strength(conn, club)
    can_play = club["last_friendly_day"] != club["season_day"]

    # pick a suggested opponent strength band
    ai_strength = int(clamp(55 + club["season_day"] * 0.5 + random.randint(-5, 6), 45, 80))
    odds = match_odds(strength, ai_strength, home_adv=1.02)

    ctx = load_context(conn)
    conn.close()
    return render_template(
        "friendly.html",
        **ctx,
        can_play=can_play,
        est_strength=round(strength, 1),
        opp_strength=ai_strength,
        win=int(round(odds["win"] * 100)),
        draw=int(round(odds["draw"] * 100)),
        loss=int(round(odds["loss"] * 100)),
    )


@app.post("/friendly/play")
def friendly_play():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    if club["last_friendly_day"] == club["season_day"]:
        conn.close()
        flash("Bạn đã đá giao hữu hôm nay rồi. Hãy sang ngày để đá tiếp.")
        return redirect(url_for("friendly"))

    eff = facility_effects(conn)
    user_strength, xi_pairs = compute_user_strength(conn, club)
    opp_strength = float(int(clamp(55 + club["season_day"] * 0.5 + random.randint(-5, 6), 45, 80)))

    hg, ag = simulate_match(user_strength, opp_strength, home_adv=1.02)

    # Reward: small cash + morale/form
    base_cash = 90_000
    if hg > ag:
        base_cash = int(base_cash * 1.25)
    elif hg == ag:
        base_cash = int(base_cash * 1.05)
    else:
        base_cash = int(base_cash * 0.90)

    db_exec(conn, "UPDATE club SET cash=cash+?, last_friendly_day=? WHERE id=1", (base_cash, club["season_day"]))

    # Apply morale/form changes for XI
    delta_morale = 3 if hg > ag else (1 if hg == ag else -1)
    delta_form = 2 if hg > ag else (1 if hg == ag else -1)
    for p, _ in xi_pairs:
        morale = int(clamp(p["morale"] + delta_morale, 0, 100))
        form = int(clamp(p["form"] + delta_form, 0, 100))
        db_exec(conn, "UPDATE squad_player SET morale=?, form=? WHERE id=?", (morale, form, p["id"]))

    # Apply fatigue & chance of injuries (lighter than official match)
    fatigue = 7
    match_injury_base = 0.014 * eff["match_injury_mult"]
    inj_mults: List[float] = []
    for p, _ in xi_pairs:
        traits = parse_traits(p["traits"] if "traits" in p.keys() else "")
        mods = trait_effect_mods(traits)
        inj_mults.append(mods["injury"])
        new_cond = int(clamp(p["condition"] - int(round(fatigue * mods["fatigue"])), 0, 100))
        db_exec(conn, "UPDATE squad_player SET condition=? WHERE id=?", (new_cond, p["id"]))

    injured_name = None
    avg_inj_mult = sum(inj_mults) / max(1, len(inj_mults))
    if random.random() < match_injury_base * avg_inj_mult and xi_pairs:
        cand = [p for p, _ in xi_pairs]
        weights = []
        for cp in cand:
            traits = parse_traits(cp["traits"] if "traits" in cp.keys() else "")
            weights.append(trait_effect_mods(traits)["injury"])
        victim = random.choices(cand, weights=weights, k=1)[0]
        days = random.randint(1, 4)
        days = max(1, int(math.ceil(days * eff["injury_days_mult"])))
        injured_until = club["season_day"] + days
        db_exec(conn, "UPDATE squad_player SET injured_until_day=? WHERE id=?", (injured_until, victim["id"]))
        injured_name = victim["name"]

    conn.close()
    msg = f"Giao hữu: Bạn {hg} - {ag} Đối thủ. +{fmt_money(base_cash)} tiền"
    if injured_name:
        msg += f". Chấn thương: {injured_name}"
    flash(msg)
    return redirect(url_for("friendly"))


def winner_of_fixture(conn: sqlite3.Connection, fix: sqlite3.Row) -> str:
    if fix["home_goals"] > fix["away_goals"]:
        return fix["home"]
    if fix["home_goals"] < fix["away_goals"]:
        return fix["away"]
    # Draw -> penalties: bias by base_strength
    h = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (fix["home"],))
    a = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (fix["away"],))
    hs = (h["base_strength"] if h else 60) + random.uniform(-2, 2)
    as_ = (a["base_strength"] if a else 60) + random.uniform(-2, 2)
    return fix["home"] if hs >= as_ else fix["away"]

def progress_knockout(conn: sqlite3.Connection, competition: str) -> None:
    """
    Create next round fixtures when a round is completed.
    Cup: QF->SF->Final
    Champions: QF->SF->Final
    """
    day_map = {
        "Cup": {1: 2, 2: 6, 3: 10},
        "Champions": {1: 4, 2: 8, 3: 12},
    }.get(competition)
    if not day_map:
        return

    # Find rounds that exist for this competition
    rounds = [r["round"] for r in db_all(conn, "SELECT DISTINCT round FROM fixture WHERE competition=? ORDER BY round", (competition,))]
    if not rounds:
        return

    for r in rounds:
        # If next round already exists, skip this r
        next_round = r + 1
        if next_round in rounds:
            continue
        if next_round > 3:
            continue

        # check if all fixtures in round r are played
        fixes = db_all(conn, "SELECT * FROM fixture WHERE competition=? AND round=? ORDER BY id", (competition, r))
        if not fixes:
            continue
        if any(f["played"] == 0 for f in fixes):
            continue

        winners = [winner_of_fixture(conn, f) for f in fixes]
        # Pair winners sequentially
        pairs = list(zip(winners[0::2], winners[1::2]))
        day = day_map[next_round]
        for home, away in pairs:
            db_exec(conn, "INSERT INTO fixture (competition, day, round, home, away) VALUES (?, ?, ?, ?, ?)",
                    (competition, day, next_round, home, away))


def auto_play_ai_fixtures(conn: sqlite3.Connection, up_to_day: int, user_team: str) -> int:
    """Simulate fixtures for AI vs AI teams up to a given day (keeps the world moving)."""
    total = 0
    # There may be newly created knockout fixtures after playing a round.
    # Loop until nothing left to simulate in the time window.
    while True:
        fixes = db_all(
            conn,
            """
            SELECT * FROM fixture
            WHERE played=0 AND day<=? AND home!=? AND away!=?
            ORDER BY day, id
            """,
            (up_to_day, user_team, user_team),
        )
        if not fixes:
            break

        for fix in fixes:
            h = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (fix["home"],))
            a = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (fix["away"],))
            hs = float((h["base_strength"] if h else 60)) * random.uniform(0.90, 1.10)
            as_ = float((a["base_strength"] if a else 60)) * random.uniform(0.90, 1.10)

            hg, ag = simulate_match(hs, as_, home_adv=1.05)
            db_exec(conn, "UPDATE fixture SET played=1, home_goals=?, away_goals=? WHERE id=?", (hg, ag, fix["id"]))
            total += 1

            if fix["competition"] in ("Cup", "Champions"):
                progress_knockout(conn, fix["competition"])

    return total

def compute_league_table(conn: sqlite3.Connection) -> List[Dict]:
    teams = [r["name"] for r in db_all(conn, "SELECT name FROM team")]
    table = {t: {"team": t, "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "PTS": 0} for t in teams}
    matches = db_all(conn, "SELECT * FROM fixture WHERE competition='League' AND played=1")
    for m in matches:
        h, a = m["home"], m["away"]
        hg, ag = m["home_goals"], m["away_goals"]
        table[h]["P"] += 1
        table[a]["P"] += 1
        table[h]["GF"] += hg
        table[h]["GA"] += ag
        table[a]["GF"] += ag
        table[a]["GA"] += hg
        if hg > ag:
            table[h]["W"] += 1
            table[a]["L"] += 1
            table[h]["PTS"] += 3
        elif hg < ag:
            table[a]["W"] += 1
            table[h]["L"] += 1
            table[a]["PTS"] += 3
        else:
            table[h]["D"] += 1
            table[a]["D"] += 1
            table[h]["PTS"] += 1
            table[a]["PTS"] += 1
    # sort by PTS, GD, GF
    rows = list(table.values())
    rows.sort(key=lambda r: (r["PTS"], (r["GF"] - r["GA"]), r["GF"]), reverse=True)
    # add rank
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r["GD"] = r["GF"] - r["GA"]
    return rows

@app.route("/tactics")
def tactics():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    ctx = load_context(conn)
    conn.close()
    return render_template("tactics.html", **ctx)

@app.post("/tactics")
def save_tactics():
    formation = request.form.get("formation", "4-4-2")
    mentality = request.form.get("mentality", "balanced")
    pressing = request.form.get("pressing", "normal")
    passing = request.form.get("passing", "short")
    conn = get_db()
    db_exec(conn, "UPDATE club SET formation=?, mentality=?, pressing=?, passing=? WHERE id=1",
            (formation, mentality, pressing, passing))
    # Ensure lineup slots exist for the selected formation
    ensure_lineup_slots(conn, formation)
    conn.close()
    flash("Đã lưu đội hình & chiến thuật.")
    return redirect(url_for("tactics"))


@app.route("/lineup")
def lineup():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    formation = club["formation"]
    day = club["season_day"]

    slot_positions = formation_slots(formation)
    coords = FORMATION_LAYOUT.get(formation, FORMATION_LAYOUT.get("4-4-2"))
    slot_rows = get_lineup_slots(conn, formation)

    players = db_all(conn, "SELECT * FROM squad_player ORDER BY position, id")
    decorated_players: List[Dict] = []
    for p in players:
        decorated_players.append({
            **dict(p),
            "overall": round(player_overall(p), 1),
            "effective": round(player_effective_rating(p, day), 1),
            "injured": (p["injured_until_day"] or 0) > day,
        })

    by_id = {p["id"]: p for p in decorated_players}

    slots_view = []
    captain_slot = None
    for i, pos in enumerate(slot_positions):
        sr = slot_rows[i]
        if sr["is_captain"] == 1:
            captain_slot = i
        pid = sr["player_id"]
        assigned = by_id.get(pid) if pid else None
        x, y = coords[i] if coords and i < len(coords) else (50, 50)
        slots_view.append({
            "slot_index": i,
            "slot_pos": pos,
            "x": x,
            "y": y,
            "player": assigned,
            "is_captain": sr["is_captain"] == 1,
        })

    strength, xi_pairs = compute_user_strength(conn, club)
    xg_u, xg_o = expected_goals(strength, 60.0, home_adv=1.0)

    ctx = load_context(conn)
    conn.close()
    return render_template(
        "lineup.html",
        **ctx,
        formation=formation,
        slots=slots_view,
        players=decorated_players,
        captain_slot=captain_slot,
        est_strength=round(strength, 1),
        est_xg=round(xg_u, 2),
        est_xg_opp=round(xg_o, 2),
    )


@app.post("/lineup/save")
def save_lineup():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    formation = club["formation"]
    slots = formation_slots(formation)

    slot_to_player: Dict[int, Optional[int]] = {}
    for i in range(len(slots)):
        raw = request.form.get(f"slot_{i}", "").strip()
        if raw.isdigit():
            slot_to_player[i] = int(raw)
        else:
            slot_to_player[i] = None

    cap_raw = request.form.get("captain_slot", "").strip()
    captain_slot = int(cap_raw) if cap_raw.isdigit() else None

    set_lineup(conn, formation, slot_to_player, captain_slot)
    conn.close()
    flash("Đã lưu đội hình ra sân (kéo thả).")
    return redirect(url_for("lineup"))


@app.post("/lineup/autopick")
def lineup_autopick():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    auto_pick_and_save_lineup(conn, club["season_day"], club["formation"])
    conn.close()
    flash("Đã auto-pick đội hình mạnh nhất theo sơ đồ.")
    return redirect(url_for("lineup"))


@app.post("/lineup/clear")
def lineup_clear():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    formation = club["formation"]
    slots = formation_slots(formation)
    slot_to_player = {i: None for i in range(len(slots))}
    set_lineup(conn, formation, slot_to_player, None)
    conn.close()
    flash("Đã xóa đội hình đá chính.")
    return redirect(url_for("lineup"))

@app.post("/play/<int:fid>")
def play_fixture(fid: int):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    my = club["name"]
    fix = db_one(conn, "SELECT * FROM fixture WHERE id=?", (fid,))
    if fix is None:
        conn.close()
        flash("Không tìm thấy trận.")
        return redirect(url_for("competitions"))

    # Safety: user can only play their own fixtures
    if not (fix["home"] == my or fix["away"] == my):
        conn.close()
        flash("Bạn không tham gia trận này.")
        return redirect(url_for("competitions"))
    if fix["played"] == 1:
        conn.close()
        flash("Trận này đã đá rồi.")
        return redirect(url_for("competitions"))
    if fix["day"] > club["season_day"]:
        conn.close()
        flash("Chưa đến ngày thi đấu của trận này.")
        return redirect(url_for("competitions"))

    eff = facility_effects(conn)

    # Compute user strength from saved XI (drag & drop) + tactics
    players = db_all(conn, "SELECT * FROM squad_player")
    user_strength, xi_pairs = compute_user_strength(conn, club)
    xi = [p for p, _ in xi_pairs]

    # Opponent strength
    opp_name = fix["away"] if fix["home"] == my else fix["home"]
    opp_base = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (opp_name,))["base_strength"]
    # add small randomness
    opp_strength = opp_base * random.uniform(0.90, 1.10)

    # Home advantage if user at home
    home_adv = 1.05 if fix["home"] == my else 1.00
    hg, ag = simulate_match(user_strength, opp_strength, home_adv=home_adv)
    if fix["home"] == my:
        home_goals, away_goals = hg, ag
    else:
        home_goals, away_goals = ag, hg

    db_exec(conn, "UPDATE fixture SET played=1, home_goals=?, away_goals=? WHERE id=?",
            (home_goals, away_goals, fid))

    # If knockout, generate next round when ready
    comp = fix["competition"]
    progress_knockout(conn, comp)

    # Rewards
    # ticket revenue (only if user home)
    ticket_revenue = 0
    if fix["home"] == my:
        cap = eff["capacity"]
        # occupancy depends on morale and results; use avg morale
        avg_morale = sum(p["morale"] for p in players) / max(1, len(players))
        occ = clamp(0.55 + 0.004 * (avg_morale - 70), 0.40, 0.95)
        price = 12  # simple
        ticket_revenue = int(cap * occ * price)

    comp = fix["competition"]
    base_cash = {"League": 120_000, "Cup": 160_000, "Champions": 220_000, "SuperCup": 260_000}.get(comp, 100_000)
    token_reward = {"League": 0, "Cup": 1, "Champions": 2, "SuperCup": 1}.get(comp, 0)

    # match result bonus
    my_goals = home_goals if fix["home"] == my else away_goals
    opp_goals = away_goals if fix["home"] == my else home_goals
    if my_goals > opp_goals:
        base_cash = int(base_cash * 1.25)
    elif my_goals == opp_goals:
        base_cash = int(base_cash * 1.05)
    else:
        base_cash = int(base_cash * 0.95)

    cash_gain = base_cash + ticket_revenue
    db_exec(conn, "UPDATE club SET cash=cash+?, tokens=tokens+? WHERE id=1", (cash_gain, token_reward))

    # Apply fatigue & chance of injuries (traits may affect both)
    pressing = club["pressing"]
    fatigue = 8 if pressing == "low" else (14 if pressing == "high" else 11)
    match_injury_base = 0.015 if pressing == "low" else (0.030 if pressing == "high" else 0.022)
    match_injury_base *= eff["match_injury_mult"]

    injured_name = None
    inj_mults: List[float] = []
    for p in xi:
        traits = parse_traits(p["traits"] if "traits" in p.keys() else "")
        mods = trait_effect_mods(traits)
        inj_mults.append(mods["injury"])
        new_cond = int(clamp(p["condition"] - int(round(fatigue * mods["fatigue"])), 0, 100))
        db_exec(conn, "UPDATE squad_player SET condition=? WHERE id=?", (new_cond, p["id"]))

    # maybe injure one player
    # maybe injure one player
    avg_inj_mult = sum(inj_mults) / max(1, len(inj_mults))
    if random.random() < match_injury_base * avg_inj_mult:
        cand = [p for p in xi if (p["injured_until_day"] or 0) <= club["season_day"]]
        if cand:
            weights = []
            for cp in cand:
                traits = parse_traits(cp["traits"] if "traits" in cp.keys() else "")
                weights.append(trait_effect_mods(traits)["injury"])
            victim = random.choices(cand, weights=weights, k=1)[0]
            days = random.randint(1, 6)
            days = max(1, int(math.ceil(days * eff["injury_days_mult"])))
            injured_until = club["season_day"] + days
            db_exec(conn, "UPDATE squad_player SET injured_until_day=? WHERE id=?", (injured_until, victim["id"]))
            injured_name = victim["name"]

    conn.close()

    msg = f"Kết quả: {fix['home']} {home_goals} - {away_goals} {fix['away']}. +{fmt_money(cash_gain)} tiền"
    if token_reward:
        msg += f", +{token_reward} token"
    if injured_name:
        msg += f". Chấn thương: {injured_name}"
    flash(msg)
    return redirect(url_for("competitions"))

@app.post("/reset_season")
def reset_season():
    conn = get_db()
    # Reset club day and regenerate season
    db_exec(conn, "UPDATE club SET season_day=0, cash=5000000, tokens=40 WHERE id=1")
    generate_season(conn)
    # reset logs (prototype)
    db_exec(conn, "DELETE FROM player_log")
    log_all_players_missing(conn, 0, note="reset")
    # reset market
    db_exec(conn, "UPDATE club SET market_reset_at=? WHERE id=1", (now_ts() + MARKET_BATCH_SECONDS,))
    regen_market_batch(conn, now_ts() + MARKET_BATCH_SECONDS)
    conn.close()
    flash("Đã reset mùa giải (prototype).")
    return redirect(url_for("home"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
