
import os
import math
import random
import sqlite3
import time
import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, abort, flash, g, has_request_context, redirect, render_template, request, url_for

from auth import game_db_path, init_auth
from player_names import generate_identity
from pitch_positions import POSITION_ZONES, MIN_X, MAX_X, MIN_Y, MAX_Y, position_at
import game_rules as rules

APP_NAME = "Băng Ghế Chỉ Đạo"

MARKET_BATCH_SECONDS = 180  # 3 minutes
MARKET_LISTINGS_PER_BATCH = 15  # 15 random players per 3m batch
SCOUT_RESET_SECONDS = 172800  # 48 hours (2 days)

# -----------------------------
# Utilities
# -----------------------------
def now_ts() -> int:
    return int(time.time())

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def get_db() -> sqlite3.Connection:
    if g.get("action_db") is not None:
        return g.action_db
    conn = sqlite3.connect(game_db_path(), timeout=30, factory=rules.GameConnection)
    conn.row_factory = sqlite3.Row
    g.setdefault("game_connections", []).append(conn)
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

def short_player_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    if len(parts) <= 1:
        return full_name or ""
    return f"{parts[0][0]}. {' '.join(parts[1:])}"

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
    return base * (0.55 + 0.45 * cond) * (0.85 + 0.15 * morale) * (0.85 + 0.15 * form) * rules.experience_multiplier(p)


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

# Horizontal pitch layout coordinates (percent): x: 0..100 (goal left -> goal right), y: 0..100 (top -> bottom)
HORIZONTAL_FORMATION_LAYOUT: Dict[str, List[Tuple[int, int]]] = {
    "4-4-2": [
        (13, 50),  # GK
        (31, 20),  # DL
        (26, 38),  # DC
        (26, 62),  # DC
        (31, 80),  # DR
        (50, 20),  # ML
        (48, 38),  # MC
        (48, 62),  # MC
        (50, 80),  # MR
        (74, 38),  # ST
        (74, 62),  # ST
    ],
    "4-3-3": [
        (13, 50),  # GK
        (31, 20),  # DL
        (26, 38),  # DC
        (26, 62),  # DC
        (31, 80),  # DR
        (48, 28),  # MC
        (46, 50),  # MC
        (48, 72),  # MC
        (70, 22),  # AML
        (75, 50),  # ST
        (70, 78),  # AMR
    ],
    "4-2-3-1": [
        (13, 50),  # GK
        (31, 18),  # DL
        (26, 37),  # DC
        (26, 63),  # DC
        (31, 82),  # DR
        (40, 38),  # DMC
        (40, 62),  # DMC
        (64, 18),  # AML
        (64, 50),  # AMC
        (64, 82),  # AMR
        (76, 50),  # ST
    ],
    "3-5-2": [
        (13, 50),  # GK
        (26, 28),  # DC
        (25, 50),  # DC
        (26, 72),  # DC
        (48, 18),  # ML
        (46, 38),  # MC
        (46, 62),  # MC
        (48, 82),  # MR
        (62, 50),  # AMC
        (75, 38),  # ST
        (75, 62),  # ST
    ],
    "5-3-2": [
        (13, 50),  # GK
        (31, 16),  # DL
        (26, 33),  # DC
        (25, 50),  # DC
        (26, 67),  # DC
        (31, 84),  # DR
        (48, 30),  # MC
        (46, 50),  # MC
        (48, 70),  # MC
        (75, 38),  # ST
        (75, 62),  # ST
    ],
}

NAT_FLAGS: Dict[str, str] = {
    "VN": "🇻🇳",
    "BG": "🇧🇬",
    "TR": "🇹🇷",
    "SE": "🇸🇪",
    "BR": "🇧🇷",
    "FR": "🇫🇷",
    "AR": "🇦🇷",
    "DE": "🇩🇪",
    "ES": "🇪🇸",
    "EN": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "IT": "🇮🇹",
    "PT": "🇵🇹",
    "JP": "🇯🇵",
    "KR": "🇰🇷",
    "NL": "🇳🇱",
}

# Core attributes for each position: highlighted with colored bar; non-core dimmed/greyed out
POSITION_CORE_ATTRIBUTES: Dict[str, set] = {
    "GK": {"can_pha", "chay_cho", "dung_cam", "chuyen", "the_luc", "suc_manh"},
    "DC": {"can_pha", "kem_nguoi", "chay_cho", "danh_dau", "dung_cam", "suc_manh", "the_luc"},
    "DL": {"can_pha", "kem_nguoi", "chay_cho", "chuyen", "dan_bong", "tat_canh", "toc_do", "the_luc"},
    "DR": {"can_pha", "kem_nguoi", "chay_cho", "chuyen", "dan_bong", "tat_canh", "toc_do", "the_luc"},
    "DMC": {"can_pha", "kem_nguoi", "chay_cho", "danh_dau", "dung_cam", "chuyen", "the_luc", "suc_manh", "xong_xao", "sang_tao"},
    "MC": {"chay_cho", "chuyen", "dan_bong", "the_luc", "toc_do", "sang_tao"},
    "ML": {"chay_cho", "chuyen", "dan_bong", "tat_canh", "the_luc", "toc_do", "sang_tao"},
    "MR": {"chay_cho", "chuyen", "dan_bong", "tat_canh", "the_luc", "toc_do", "sang_tao"},
    "AMC": {"chay_cho", "chuyen", "dan_bong", "sut_manh", "dut_diem", "toc_do", "the_luc", "sang_tao"},
    "AML": {"chay_cho", "chuyen", "dan_bong", "tat_canh", "dut_diem", "toc_do", "the_luc", "sang_tao"},
    "AMR": {"chay_cho", "chuyen", "dan_bong", "tat_canh", "dut_diem", "toc_do", "the_luc", "sang_tao"},
    "ST": {"chay_cho", "danh_dau", "dan_bong", "sut_manh", "dut_diem", "toc_do", "the_luc", "suc_manh"},
}

def compute_player_attributes_15(p: Any) -> Dict[str, Any]:
    row = dict(p) if hasattr(p, "keys") else p
    pos = row.get("position", "MC")
    fin = row.get("finishing", 60)
    pas = row.get("passing", 60)
    pac = row.get("pace", 60)
    tac = row.get("tackling", 60)
    mar = row.get("marking", 60)
    sta = row.get("stamina_skill", 60)
    pid = row.get("id", 1) or 1
    h = row.get("height", 182) or 182
    w = row.get("weight", 76) or 76

    # 1. PHÒNG THỦ (5 attributes)
    can_pha = tac
    kem_nguoi = mar
    chay_cho = int(clamp(round(pac * 0.5 + pas * 0.5), 0, 200))
    danh_dau = int(clamp(round(mar * 0.5 + fin * 0.5), 0, 200))
    dung_cam = int(clamp(round(tac * 0.5 + sta * 0.5), 0, 200))
    def_avg = round((can_pha + kem_nguoi + chay_cho + danh_dau + dung_cam) / 5.0)

    # 2. TẤN CÔNG (5 attributes)
    chuyen = pas
    dan_bong = int(clamp(round(pas * 0.5 + pac * 0.5), 0, 200))
    tat_canh = int(clamp(round(pas * 0.7 + pac * 0.3), 0, 200))
    sut_manh = int(clamp(round(fin * 0.65 + sta * 0.35), 0, 200))
    dut_diem = fin
    atk_avg = round((chuyen + dan_bong + tat_canh + sut_manh + dut_diem) / 5.0)

    # 3. THỂ CHẤT (5 attributes)
    the_luc = sta
    suc_manh = int(clamp(round(sta * 0.6 + tac * 0.4), 0, 200))
    xong_xao = int(clamp(round(sta * 0.5 + tac * 0.5), 0, 200))
    toc_do = pac
    sang_tao = int(clamp(round(pas * 0.75 + fin * 0.25), 0, 200))
    phy_avg = round((the_luc + suc_manh + xong_xao + toc_do + sang_tao) / 5.0)

    core_attrs = POSITION_CORE_ATTRIBUTES.get(pos, {"chay_cho", "chuyen", "toc_do", "the_luc"})

    return {
        "def_avg": def_avg,
        "atk_avg": atk_avg,
        "phy_avg": phy_avg,
        "defense": [
            {"key": "can_pha", "name": "Cản phá", "val": can_pha, "is_core": "can_pha" in core_attrs, "cat": "def"},
            {"key": "kem_nguoi", "name": "Kèm người", "val": kem_nguoi, "is_core": "kem_nguoi" in core_attrs, "cat": "def"},
            {"key": "chay_cho", "name": "Chạy chỗ", "val": chay_cho, "is_core": "chay_cho" in core_attrs, "cat": "def"},
            {"key": "danh_dau", "name": "Đánh đầu", "val": danh_dau, "is_core": "danh_dau" in core_attrs, "cat": "def"},
            {"key": "dung_cam", "name": "Dũng cảm", "val": dung_cam, "is_core": "dung_cam" in core_attrs, "cat": "def"},
        ],
        "attack": [
            {"key": "chuyen", "name": "Chuyền", "val": chuyen, "is_core": "chuyen" in core_attrs, "cat": "atk"},
            {"key": "dan_bong", "name": "Dẫn bóng", "val": dan_bong, "is_core": "dan_bong" in core_attrs, "cat": "atk"},
            {"key": "tat_canh", "name": "Tạt cánh", "val": tat_canh, "is_core": "tat_canh" in core_attrs, "cat": "atk"},
            {"key": "sut_manh", "name": "Sút mạnh", "val": sut_manh, "is_core": "sut_manh" in core_attrs, "cat": "atk"},
            {"key": "dut_diem", "name": "Dứt điểm", "val": dut_diem, "is_core": "dut_diem" in core_attrs, "cat": "atk"},
        ],
        "physical": [
            {"key": "the_luc", "name": "Thể lực", "val": the_luc, "is_core": "the_luc" in core_attrs, "cat": "phy"},
            {"key": "suc_manh", "name": "Sức mạnh", "val": suc_manh, "is_core": "suc_manh" in core_attrs, "cat": "phy"},
            {"key": "xong_xao", "name": "Xông xáo", "val": xong_xao, "is_core": "xong_xao" in core_attrs, "cat": "phy"},
            {"key": "toc_do", "name": "Tốc độ", "val": toc_do, "is_core": "toc_do" in core_attrs, "cat": "phy"},
            {"key": "sang_tao", "name": "Sáng tạo", "val": sang_tao, "is_core": "sang_tao" in core_attrs, "cat": "phy"},
        ],
    }

# Hệ thống đánh giá 10 sao: mỗi sao tương ứng 20% chỉ số, tối đa 200% = 10 sao
def cltt_to_stars_10(cltt: float) -> str:
    stars_val = clamp(float(cltt) / 20.0, 0.5, 10.0)
    full_stars = int(stars_val)
    half_star = (stars_val - full_stars) >= 0.5
    return ("★" * full_stars) + ("½" if half_star else "")

def cltt_stars_info(cltt: float) -> Dict[str, Any]:
    val = clamp(float(cltt) / 20.0, 0.5, 10.0)
    full = int(val)
    half = (val - full) >= 0.5
    stars_str = ("★" * full) + ("½" if half else "")
    return {
        "val": round(val, 1),
        "full": full,
        "half": half,
        "stars_str": stars_str,
        "cltt": round(float(cltt), 1),
    }

# Danh sách 7 công trình Khuôn viên
ALL_FACILITIES = [
    ("parking", "Bãi Đỗ Xe", "Chứa được nhiều ô tô & xe máy của CĐV hơn, tăng mạnh doanh thu đỗ xe ngày thi đấu.", 90, "fac-pin-parking"),
    ("food_court", "Khu Ẩm Thực", "Bán thức ăn, nước uống và dịch vụ ăn uống cho CĐV đến sân, tăng doanh thu thương mại.", 90, "fac-pin-food"),
    ("stadium", "Sân Vận Động Chính", "Mở rộng sức chứa khán đài chỗ ngồi, đón nhiều CĐV hơn và tăng mạnh tiền bán vé mỗi trận.", 50, "fac-pin-stadium"),
    ("youth_academy", "Cơ Sở Đào Tạo Trẻ", "Nâng cao chất lượng khởi điểm của lứa 11 cầu thủ trẻ U18 mỗi mùa giải; mọi cầu thủ đều có thể tập lên 200%.", 90, "fac-pin-youth"),
    ("training_ground", "T.Tâm Huấn Luyện", "Gia tăng tốc độ tập luyện và định kỳ tự động cấp Thẻ Hồi Thể Lực (Rest Packs ⚡).", 90, "fac-pin-training"),
    ("medical_center", "Khu Trị Thương & Bệnh Viện", "Giúp cầu thủ hồi phục chấn thương nhanh và định kỳ nhận Thẻ Y Tế chữa chấn thương (➕).", 90, "fac-pin-medical"),
    ("fan_club", "Fan CLB & Store", "Thu hút hội CĐV và bán vật phẩm lưu niệm, tăng tiền tài trợ và bản quyền hình ảnh.", 90, "fac-pin-fan"),
]

# 11 vị trí ngẫu nhiên ở 3 tuyến cho lứa U18 Học viện
YOUTH_POSITIONS_11 = [
    # Hàng thủ (4)
    ("GK", "DEF"),
    ("DL", "DEF"),
    ("DC", "DEF"),
    ("DR", "DEF"),
    # Tuyến giữa (4)
    ("DMC", "MID"),
    ("MC", "MID"),
    ("ML", "MID"),
    ("MR", "MID"),
    # Hàng công (3)
    ("AMC", "ATT"),
    ("AML", "ATT"),
    ("ST", "ATT"),
]

def compute_player_career_stats(p: Any) -> Dict[str, Any]:
    row = dict(p)
    if row.get("career_stats") is not None:
        return row["career_stats"]
    empty = {"apps_season": 0, "motm": 0, "goals_season": 0, "assists_season": 0,
             "yellows_season": 0, "form": "—", "goals_total": 0, "apps_total": 0}
    return {key: {**empty, "title": title} for key, title in
            (("career", "SỰ NGHIỆP"), ("league", "GIẢI VÔ ĐỊCH"), ("cup", "CÁC CÚP"))}


def player_to_modal_dict(p: Any, source: str = "auction", conn=None) -> Dict[str, Any]:
    row = dict(p) if hasattr(p, "keys") else p
    ov = round(player_overall(row), 1)
    attrs = compute_player_attributes_15(row)
    stats = rules.career_stats(conn if conn is not None else get_db(), row["id"]) if source == "squad" else compute_player_career_stats(row)

    recent_bids = []
    if source == "auction" and "recent_bids" in row.keys() and row["recent_bids"]:
        try:
            recent_bids = json.loads(row["recent_bids"])
        except Exception:
            recent_bids = []

    nat = row.get("nationality", "BR") or "BR"
    flag = NAT_FLAGS.get(nat, "🇧🇷")

    h = row.get("height", 182) or 182
    w = row.get("weight", 78) or 78
    foot = row.get("preferred_foot", "Chân phải 🦶") or "Chân phải 🦶"
    skill = row.get("special_skill", "Không có") or "Không có"

    # stars calculation (up to 10 stars, 20% per star)
    s_info = cltt_stars_info(ov)
    stars_val = s_info["val"]
    full_stars = s_info["full"]
    half_star = s_info["half"]
    stars_str = s_info["stars_str"]

    cur_bid = row.get("current_bid", row.get("start_price", 1_000_000))
    currency = row.get("currency", "cash")
    step = 1 if currency == "token" else bid_step(cur_bid)
    next_bid_val = cur_bid + step

    price_cash = row.get("price_cash", cur_bid if currency == "cash" else 0)
    price_tokens = row.get("price_tokens", cur_bid if currency == "token" else 0)

    val = int(clamp(rules.player_value(row), 60_000, 80_000_000))
    sal = row.get("salary", int(2_000 + ov * 180))

    return {
        "id": row.get("id", 1),
        "source": source,
        "name": row.get("name", ""),
        "position": row.get("position", "MC"),
        "age": row.get("age", 22),
        "overall": ov,
        "experience_matches": row.get("experience_matches",0),
        "seasons_played": row.get("seasons_played",0),
        "retirement_age": row.get("retirement_age"),
        "potential": row.get("potential"),
        "condition": row.get("condition"),
        "stars_str": stars_str,
        "stars_val": round(stars_val, 1),
        "nationality": nat,
        "flag": flag,
        "height": h,
        "weight": w,
        "preferred_foot": foot,
        "special_skill": skill,
        "currency": currency,
        "current_bid": cur_bid,
        "next_bid": next_bid_val,
        "bid_step": step,
        "start_price": row.get("start_price", cur_bid),
        "price_cash": price_cash,
        "price_tokens": price_tokens,
        "highest_bidder": row.get("highest_bidder", "none"),
        "bid_count": row.get("bid_count", len(recent_bids)),
        "recent_bids": recent_bids,
        "salary": sal,
        "market_value": val,
        "form_pattern": stats["career"]["form"],
        "def_avg": attrs["def_avg"],
        "atk_avg": attrs["atk_avg"],
        "phy_avg": attrs["phy_avg"],
        "defense": attrs["defense"],
        "attack": attrs["attack"],
        "physical": attrs["physical"],
        "career_stats": stats,
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


def set_lineup(
    conn: sqlite3.Connection,
    formation: str,
    slot_to_player: Dict[int, Optional[int]],
    captain_slot: Optional[int],
    arrow_trends: Optional[Dict[int, int]] = None,
    coordinates: Optional[Dict[int, Tuple[float, float]]] = None,
) -> None:
    ensure_lineup_slots(conn, formation)

    # Clear captain flags
    db_exec(conn, "UPDATE lineup_slot SET is_captain=0 WHERE formation=?", (formation,))

    for slot_idx, pid in slot_to_player.items():
        arr = arrow_trends.get(slot_idx, 0) if arrow_trends else 0
        if pid is None:
            db_exec(conn, "UPDATE lineup_slot SET player_id=NULL, arrow_trend=? WHERE formation=? AND slot_index=?", (arr, formation, slot_idx))
        else:
            db_exec(conn, "UPDATE lineup_slot SET player_id=?, arrow_trend=? WHERE formation=? AND slot_index=?", (pid, arr, formation, slot_idx))
        if coordinates and slot_idx in coordinates:
            x, y = coordinates[slot_idx]
            db_exec(conn, "UPDATE lineup_slot SET pitch_x=?,pitch_y=?,position=? WHERE formation=? AND slot_index=?",
                    (x, y, position_at(x, y), formation, slot_idx))

    if captain_slot is not None:
        db_exec(conn, "UPDATE lineup_slot SET is_captain=1 WHERE formation=? AND slot_index=?", (formation, captain_slot))


def starting_xi_from_saved(conn: sqlite3.Connection, club_day: int, formation: str) -> List[Tuple[sqlite3.Row, str]]:
    """Return list of (player_row, slot_position) in slot order using saved lineup where possible."""
    slot_rows = get_lineup_slots(conn, formation)
    slots = [row["position"] for row in slot_rows]
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
    ensure_lineup_slots(conn, formation)
    for i, position in enumerate(slots):
        db_exec(conn, "UPDATE lineup_slot SET pitch_x=NULL,pitch_y=NULL,position=? WHERE formation=? AND slot_index=?",
                (position, formation, i))
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

def ensure_all_facilities(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in db_all(conn, "SELECT name FROM facility")}
    for name, title, desc, pct, pcls in ALL_FACILITIES:
        if name not in existing:
            db_exec(conn, "INSERT INTO facility (name, level, build_complete_at) VALUES (?, 1, NULL)", (name,))

def facility_effects(conn: sqlite3.Connection) -> Dict[str, Any]:
    ensure_all_facilities(conn)
    finalize_builds(conn)
    effects = {}
    for f in db_all(conn, "SELECT * FROM facility"):
        effects.update(rules.facility_values(f["name"], f["level"]))
        effects[f["name"] + "_level"] = f["level"]
    effects["training_level"] = effects["training_ground_level"]
    effects["medical_level"] = effects["medical_center_level"]
    effects["youth_level"] = effects["youth_academy_level"]
    return effects

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
        if p["traits"] is not None:
            continue
        traits = traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"]))
        db_exec(conn, "UPDATE squad_player SET traits=? WHERE id=?", (traits, p["id"]))
        n += 1
    return n

def gen_name(nationality=None) -> str:
    return generate_identity(nationality)[0]

def gen_team_name(existing: set) -> str:
    for _ in range(50):
        name = f"{random.choice(CITY)} {random.choice(NICK)} {random.choice(TEAM_SUFFIX)}"
        if name not in existing:
            return name
    return f"{random.choice(CITY)} {random.choice(NICK)} {random.randint(10,99)}"

def gen_player(position: str, base: int, spread: int, age_range=(18, 32), nationality=None) -> Dict:
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

    potential = int(clamp(max(base + 8, random.gauss(base + 16, spread)), 35, 160))
    salary = int(1_000 + base * 90 + random.randint(-400, 600))
    name, nationality = generate_identity(nationality)
    return {
        "name": name,
        "nationality": nationality,
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

def backfill_player_attributes(conn: sqlite3.Connection) -> None:
    """Ensure players have realistic shirt numbers, nationalities, contract years, transfer status, height, weight, preferred foot."""
    try:
        players = db_all(conn, "SELECT id, name, position, shirt_number, nationality, contract_years, transfer_status, height, weight, preferred_foot, special_skill FROM squad_player ORDER BY id ASC")
    except Exception:
        return

    used_numbers = set()
    for p in players:
        num = p["shirt_number"] if "shirt_number" in p.keys() else 0
        if num and num > 0:
            used_numbers.add(num)

    # Standard numbers for football positions if available
    pos_preferred_nums = {
        "GK": [1, 12, 22],
        "DL": [3, 4, 13, 23],
        "DC": [2, 3, 5, 14, 15],
        "DR": [2, 5, 16, 25],
        "DMC": [6, 16, 21],
        "MC": [8, 14, 15, 16, 20],
        "ML": [7, 11, 17],
        "MR": [7, 10, 18],
        "AMC": [10, 11, 19],
        "AML": [11, 17, 7],
        "AMR": [7, 18, 10],
        "ST": [9, 18, 19, 27],
    }

    cur_fallback_num = 1
    for p in players:
        pid = p["id"]
        updates = []
        params = []

        num = p["shirt_number"] if "shirt_number" in p.keys() else 0
        if not num or num <= 0:
            assigned_num = None
            for cand in pos_preferred_nums.get(p["position"], []):
                if cand not in used_numbers:
                    assigned_num = cand
                    break
            if not assigned_num:
                while cur_fallback_num in used_numbers:
                    cur_fallback_num += 1
                assigned_num = cur_fallback_num
            used_numbers.add(assigned_num)
            updates.append("shirt_number=?")
            params.append(assigned_num)

        nat = (p["nationality"] or "").strip() if "nationality" in p.keys() else ""
        if not nat:
            name_l = p["name"].lower()
            if "dimitrov" in name_l:
                nat = "BG"
            elif "yildirgan" in name_l:
                nat = "TR"
            elif "lindgren" in name_l:
                nat = "SE"
            elif "gladyshe" in name_l:
                nat = "BG"
            elif any(k in name_l for k in ["ngọc", "thành", "công", "minh", "vũ", "nhật", "đạt", "việt", "hoàng", "thắng", "an", "long"]):
                nat = "VN"
            else:
                nat = random.choice(["VN", "VN", "VN", "BR", "FR", "ES", "DE", "AR", "EN", "TR", "SE", "BG"])
            updates.append("nationality=?")
            params.append(nat)

        cy = p["contract_years"] if "contract_years" in p.keys() else 0
        if not cy or cy <= 0:
            cy = 1 if "thành long" in p["name"].lower() else random.choice([2, 3, 3, 3])
            updates.append("contract_years=?")
            params.append(cy)

        h = p["height"] if "height" in p.keys() else 0
        if not h or h <= 0:
            assigned_h = 188 if p["position"] in ("GK", "DC") else random.randint(174, 186)
            updates.append("height=?")
            params.append(assigned_h)

        w = p["weight"] if "weight" in p.keys() else 0
        if not w or w <= 0:
            assigned_w = random.randint(68, 84)
            updates.append("weight=?")
            params.append(assigned_w)

        foot = (p["preferred_foot"] or "").strip() if "preferred_foot" in p.keys() else ""
        if not foot:
            assigned_foot = "Chân trái 🦶" if p["position"] in ("DL", "ML", "AML") else random.choice(["Chân phải 🦶", "Chân phải 🦶", "Cả hai chân 🦶"])
            updates.append("preferred_foot=?")
            params.append(assigned_foot)

        skill = (p["special_skill"] or "").strip() if "special_skill" in p.keys() else ""
        if not skill:
            assigned_skill = random.choice(["Không có", "Sút xoáy 💫", "Chuyền vượt tuyến 🎯", "Bức tốc ⚡", "Không có", "Không có"])
            updates.append("special_skill=?")
            params.append(assigned_skill)

        if updates:
            params.append(pid)
            db_exec(conn, f"UPDATE squad_player SET {', '.join(updates)} WHERE id=?", tuple(params))

# -----------------------------
# DB init & season generation
# -----------------------------
def init_db(club_name: str = "My FC") -> None:
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
    ensure_column(conn, "club", "rest_packs", "INTEGER NOT NULL DEFAULT 10")
    ensure_column(conn, "club", "morale_packs", "INTEGER NOT NULL DEFAULT 5")
    ensure_column(conn, "club", "injury_packs", "INTEGER NOT NULL DEFAULT 3")
    ensure_column(conn, "club", "daily_gift_claimed_day", "INTEGER NOT NULL DEFAULT -1")
    ensure_column(conn, "club", "tv_contract_signed", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "sponsor_contract_signed", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "daily_offer_bought_day", "INTEGER NOT NULL DEFAULT -1")

    # Team tactics & set pieces columns
    ensure_column(conn, "club", "captain_pid", "INTEGER")
    ensure_column(conn, "club", "penalty_pid", "INTEGER")
    ensure_column(conn, "club", "freekick_r_pid", "INTEGER")
    ensure_column(conn, "club", "freekick_l_pid", "INTEGER")
    ensure_column(conn, "club", "corner_r_pid", "INTEGER")
    ensure_column(conn, "club", "corner_l_pid", "INTEGER")
    ensure_column(conn, "club", "marking_type", "TEXT NOT NULL DEFAULT 'man'")
    ensure_column(conn, "club", "pressing_style", "TEXT NOT NULL DEFAULT 'low'")
    ensure_column(conn, "club", "defensive_line", "TEXT NOT NULL DEFAULT 'tight'")
    ensure_column(conn, "club", "tackling_style", "TEXT NOT NULL DEFAULT 'normal'")
    ensure_column(conn, "club", "attacking_focus", "TEXT NOT NULL DEFAULT 'mixed'")
    ensure_column(conn, "club", "counter_attack", "TEXT NOT NULL DEFAULT 'off'")
    ensure_column(conn, "club", "training_points_today", "INTEGER NOT NULL DEFAULT 0")

    # Club Profile & History Details (Screenshot 1, 2, 3, 4)
    ensure_column(conn, "club", "manager_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "stadium_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "founded_date", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "country", "TEXT NOT NULL DEFAULT 'Việt Nam'")
    ensure_column(conn, "club", "country_flag", "TEXT NOT NULL DEFAULT '🇻🇳'")
    ensure_column(conn, "club", "city", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "fanclub_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "club_level", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "club", "home_kit_color", "TEXT NOT NULL DEFAULT '#dc2626'")
    ensure_column(conn, "club", "away_kit_color", "TEXT NOT NULL DEFAULT '#ffffff'")
    ensure_column(conn, "club", "hist_league_titles", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_champions_titles", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_supercup_titles", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_cup_titles", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_ultimate_titles", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_wins", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_draws", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_losses", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_goals_scored", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_goals_conceded", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_avg_attendance", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_longest_win_streak", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_longest_unbeaten", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "hist_biggest_win", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "hist_most_appearances", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "hist_top_scorer", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "hist_top_assists", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "club", "hist_most_goals_match", "TEXT NOT NULL DEFAULT ''")
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

    ensure_column(conn, "club", "scout_reset_at", "INTEGER NOT NULL DEFAULT 0")

    # lightweight migrations for existing DBs
    ensure_column(conn, "squad_player", "traits", "TEXT")
    ensure_column(conn, "squad_player", "shirt_number", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "squad_player", "nationality", "TEXT NOT NULL DEFAULT 'VN'")
    ensure_column(conn, "squad_player", "contract_years", "INTEGER NOT NULL DEFAULT 3")
    ensure_column(conn, "squad_player", "transfer_status", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "squad_player", "bench_slot", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "club", "bench_initialized", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "squad_player", "height", "INTEGER NOT NULL DEFAULT 180")
    ensure_column(conn, "squad_player", "weight", "INTEGER NOT NULL DEFAULT 75")
    ensure_column(conn, "squad_player", "preferred_foot", "TEXT NOT NULL DEFAULT 'Chân phải 🦶'")
    ensure_column(conn, "squad_player", "special_skill", "TEXT NOT NULL DEFAULT 'Không có'")

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
    ensure_column(conn, "market_listing", "height", "INTEGER NOT NULL DEFAULT 180")
    ensure_column(conn, "market_listing", "weight", "INTEGER NOT NULL DEFAULT 75")
    ensure_column(conn, "market_listing", "preferred_foot", "TEXT NOT NULL DEFAULT 'Chân phải 🦶'")
    ensure_column(conn, "market_listing", "special_skill", "TEXT NOT NULL DEFAULT 'Không có'")
    ensure_column(conn, "market_listing", "nationality", "TEXT NOT NULL DEFAULT 'BR'")
    ensure_column(conn, "market_listing", "bid_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "market_listing", "recent_bids", "TEXT NOT NULL DEFAULT '[]'")

    # Tab 2: High quality scout players (reset every 2 days)
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS scout_listing (
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
        currency TEXT NOT NULL,
        price_cash INTEGER NOT NULL,
        price_tokens INTEGER NOT NULL,
        height INTEGER NOT NULL DEFAULT 180,
        weight INTEGER NOT NULL DEFAULT 75,
        preferred_foot TEXT NOT NULL DEFAULT 'Chân phải 🦶',
        special_skill TEXT NOT NULL DEFAULT 'Không có',
        nationality TEXT NOT NULL DEFAULT 'NL',
        created_at INTEGER NOT NULL
    )
    """)

    # Tab 3: Assistant scout recommendations for weakest position in squad
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS assistant_recommendation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tier INTEGER NOT NULL,
        target_pos TEXT NOT NULL,
        weakest_player_name TEXT NOT NULL,
        weakest_player_ovr REAL NOT NULL,
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
        currency TEXT NOT NULL,
        price_cash INTEGER NOT NULL,
        price_tokens INTEGER NOT NULL,
        height INTEGER NOT NULL DEFAULT 180,
        weight INTEGER NOT NULL DEFAULT 75,
        preferred_foot TEXT NOT NULL DEFAULT 'Chân phải 🦶',
        special_skill TEXT NOT NULL DEFAULT 'Không có',
        nationality TEXT NOT NULL DEFAULT 'BR',
        created_at INTEGER NOT NULL
    )
    """)

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
    ensure_column(conn, "lineup_slot", "arrow_trend", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "lineup_slot", "pitch_x", "REAL")
    ensure_column(conn, "lineup_slot", "pitch_y", "REAL")
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

    # Youth Academy table (11 players aged 18 in 11 positions across 3 lines)
    ensure_youth_table(conn)
    # Competitions, 2-legged ties, and player match statistics tables
    ensure_competitions_schema(conn)

    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    new_save = club is None
    if club is None:
        db_exec(conn, """
            INSERT INTO club (id, name, cash, tokens, formation, mentality, pressing, passing, season_day, market_reset_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (club_name, rules.START_CASH, rules.START_TOKENS, "4-4-2", "balanced", "normal", "short", 0, now_ts() + MARKET_BATCH_SECONDS))
        db_exec(conn, "UPDATE club SET manager_name=?, stadium_name=?, fanclub_name=?, founded_date=?, city='' WHERE id=1",
                (g.user["username"], "Sân " + club_name, "CĐV " + club_name,
                 datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y")))
        # facilities (all 7 campus facilities)
        for name, title, desc, pct, pcls in ALL_FACILITIES:
            db_exec(conn, "INSERT OR REPLACE INTO facility (name, level, build_complete_at) VALUES (?, 1, NULL)", (name,))
        # initial squad
        seed_initial_squad(conn)
        # log initial squad state (day 0)
        log_all_players_missing(conn, 0, note="init")
        # season teams & fixtures
        generate_season(conn)

    # Ensure all 7 facilities exist for existing saves
    ensure_all_facilities(conn)
    rules.migrate(conn, ensure_column, new_save=new_save)
    rules.ensure_ai_rosters(conn, gen_name)
    # Ensure 11 youth players are available in Youth Academy
    get_or_regen_youth_squad(conn)

    # Backfill traits & player attributes for older saves (if missing)
    backfill_player_traits(conn)
    backfill_player_attributes(conn)
    rules.initialize_lifecycle(conn, now_ts())
    rules.recover_condition(conn, now_ts())

    refreshed_club=db_one(conn,"SELECT * FROM club")
    actual_strength,_=compute_user_strength(conn,refreshed_club)
    db_exec(conn,"UPDATE team SET base_strength=? WHERE name=? AND ABS(base_strength-?)>.001",
            (actual_strength,refreshed_club["name"],actual_strength))
    # Ensure we have at least one log row for current day (for migrated saves)
    club2 = db_one(conn, "SELECT season_day FROM club WHERE id=1")
    if club2 is not None:
        log_all_players_missing(conn, int(club2["season_day"]), note="daily")

    conn.close()

def seed_initial_squad(conn: sqlite3.Connection) -> None:
    # 18 players
    roster_positions = ["GK","GK","DL","DC","DC","DR","DMC","MC","MC","ML","MR","AMC","AML","AMR","ST","ST","DC","MC"]
    for pos in roster_positions:
        p = gen_player(pos, base=55, spread=10, age_range=(19, 30))
        db_exec(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential, condition, morale, form, injured_until_day, salary, nationality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"], p["position"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], traits_to_str(gen_traits(p["position"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"], p["stamina_skill"])),
              p["potential"], p["condition"], p["morale"], p["form"], p["injured_until_day"], p["salary"], p["nationality"]))


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

# -----------------------------
# REALISTIC COMPETITIONS DATA POOLS
# -----------------------------
DOMESTIC_EPL_POOL = [
    {"name": "Manchester City", "strength": 86, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Erling Haaland", "Kevin De Bruyne", "Phil Foden", "Bernardo Silva", "Rodri"]},
    {"name": "Arsenal", "strength": 84, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Bukayo Saka", "Martin Odegaard", "Kai Havertz", "Gabriel Martinelli", "Declan Rice"]},
    {"name": "Liverpool FC", "strength": 85, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Blessing Odinakachaku", "Mohamed Salah", "Pablo Lucero", "Luis Diaz", "Kevin Steinsvik"]},
    {"name": "Aston Villa", "strength": 79, "logo": "🟣", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Ollie Watkins", "Leon Bailey", "John McGinn", "Morgan Rogers", "Youri Tielemans"]},
    {"name": "Tottenham Hotspur", "strength": 80, "logo": "⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Son Heung-min", "James Maddison", "Dejan Kulusevski", "Dominic Solanke", "Brennan Johnson"]},
    {"name": "Chelsea", "strength": 81, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Cole Palmer", "Nicolas Jackson", "Noni Madueke", "Enzo Fernandez", "Pedro Neto"]},
    {"name": "Newcastle United", "strength": 78, "logo": "⚫", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Alexander Isak", "Anthony Gordon", "Harvey Barnes", "Bruno Guimaraes", "Joelinton"]},
    {"name": "Manchester United", "strength": 80, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Bruno Fernandes", "Marcus Rashford", "Alejandro Garnacho", "Rasmus Hojlund", "Joshua Zirkzee"]},
    {"name": "West Ham United", "strength": 75, "logo": "⚒️", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jarrod Bowen", "Mohammed Kudus", "Lucas Paqueta", "Michail Antonio", "Niclas Fullkrug"]},
    {"name": "Brighton & Hove", "strength": 76, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Danny Welbeck", "Kaoru Mitoma", "Joao Pedro", "Simon Adingra", "Georginio Rutter"]},
    {"name": "Wolverhampton", "strength": 73, "logo": "🐺", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Matheus Cunha", "Hwang Hee-chan", "Jorgen Strand Larsen", "Mario Lemina", "Pablo Sarabia"]},
    {"name": "Fulham", "strength": 74, "logo": "⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Raul Jimenez", "Alex Iwobi", "Adama Traore", "Emile Smith Rowe", "Andreas Pereira"]},
    {"name": "AFC Bournemouth", "strength": 72, "logo": "🍒", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Antoine Semenyo", "Evanilson", "Justin Kluivert", "Marcus Tavernier", "Ryan Christie"]},
    {"name": "Everton", "strength": 73, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Dominic Calvert-Lewin", "Dwight McNeil", "Iliman Ndiaye", "Abdoulaye Doucoure", "Jack Harrison"]},
    {"name": "Brentford", "strength": 74, "logo": "🐝", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Bryan Mbeumo", "Yoane Wissa", "Mikkel Damsgaard", "Kevin Schade", "Christian Norgaard"]},
    {"name": "Crystal Palace", "strength": 74, "logo": "🦅", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Eberechi Eze", "Jean-Philippe Mateta", "Eddie Nketiah", "Ismaila Sarr", "Adam Wharton"]},
    {"name": "Nottingham Forest", "strength": 72, "logo": "🌲", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Chris Wood", "Callum Hudson-Odoi", "Morgan Gibbs-White", "Anthony Elanga", "Elliot Anderson"]},
    {"name": "Leicester City", "strength": 72, "logo": "🦊", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jamie Vardy", "Stephy Mavididi", "Facundo Buonanotte", "Jordan Ayew", "Harry Winks"]},
    {"name": "Southampton", "strength": 70, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Cameron Archer", "Adam Armstrong", "Tyler Dibling", "Mateus Fernandes", "Flynn Downes"]},
    {"name": "Ipswich Town", "strength": 70, "logo": "🚜", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Liam Delap", "Omari Hutchinson", "Sammie Szmodics", "Wes Burns", "Conor Chaplin"]},
]

CHAMPIONS_LEAGUE_INTL_POOL = [
    {"name": "Real Madrid", "strength": 89, "logo": "👑", "flag": "🇪🇸", "stars": ["Vinicius Jr", "Jude Bellingham", "Kylian Mbappé", "Rodrygo", "Federico Valverde"]},
    {"name": "Bayern München", "strength": 88, "logo": "🔴", "flag": "🇩🇪", "stars": ["Harry Kane", "Jamal Musiala", "Michael Olise", "Leroy Sané", "Serge Gnabry"]},
    {"name": "Barcelona", "strength": 87, "logo": "🔵🔴", "flag": "🇪🇸", "stars": ["Robert Lewandowski", "Lamine Yamal", "Raphinha", "Dani Olmo", "Pedri"]},
    {"name": "Paris Saint-Germain", "strength": 86, "logo": "🔵", "flag": "🇫🇷", "stars": ["Bradley Barcola", "Ousmane Dembélé", "Vitinha", "Marco Asensio", "Achraf Hakimi"]},
    {"name": "Inter Milan", "strength": 85, "logo": "⚫🔵", "flag": "🇮🇹", "stars": ["Lautaro Martinez", "Marcus Thuram", "Hakan Calhanoglu", "Nicolo Barella", "Federico Dimarco"]},
    {"name": "Juventus", "strength": 83, "logo": "⚪⚫", "flag": "🇮🇹", "stars": ["Dusan Vlahovic", "Kenan Yildiz", "Teun Koopmeiners", "Francisco Conceicao", "Manuel Locatelli"]},
    {"name": "Borussia Dortmund", "strength": 83, "logo": "🟡", "flag": "🇩🇪", "stars": ["Serhou Guirassy", "Karim Adeyemi", "Julian Brandt", "Jamie Gittens", "Marcel Sabitzer"]},
    {"name": "Atletico Madrid", "strength": 83, "logo": "🔴⚪", "flag": "🇪🇸", "stars": ["Antoine Griezmann", "Julian Alvarez", "Alexander Sorloth", "Rodrigo De Paul", "Koke"]},
    {"name": "AC Milan", "strength": 82, "logo": "🔴⚫", "flag": "🇮🇹", "stars": ["Rafael Leao", "Christian Pulisic", "Alvaro Morata", "Tijjani Reijnders", "Theo Hernandez"]},
    {"name": "Bayer Leverkusen", "strength": 84, "logo": "⚫🔴", "flag": "🇩🇪", "stars": ["Florian Wirtz", "Victor Boniface", "Jeremie Frimpong", "Granit Xhaka", "Alejandro Grimaldo"]},
    {"name": "Sporting CP", "strength": 81, "logo": "🟢⚪", "flag": "🇵🇹", "stars": ["Viktor Gyökeres", "Francisco Trincao", "Pedro Goncalves", "Morten Hjulmand", "Geovany Quenda"]},
    {"name": "SL Benfica", "strength": 80, "logo": "🔴", "flag": "🇵🇹", "stars": ["Angel Di Maria", "Kerem Akturkoglu", "Vangelis Pavlidis", "Orkun Kokcu", "Florentino Luis"]},
    {"name": "FC Porto", "strength": 80, "logo": "🔵⚪", "flag": "🇵🇹", "stars": ["Samu Omorodion", "Wenderson Galeno", "Pepe Aquino", "Nico Gonzalez", "Alan Varela"]},
    {"name": "PSV Eindhoven", "strength": 78, "logo": "🔴⚪", "flag": "🇳🇱", "stars": ["Luuk de Jong", "Johan Bakayoko", "Guus Til", "Ismael Saibari", "Malik Tillman"]},
    {"name": "Feyenoord", "strength": 77, "logo": "🔴⚪", "flag": "🇳🇱", "stars": ["Santiago Gimenez", "Igor Paixao", "Quinten Timber", "Antoni Milambo", "Calvin Stengs"]},
    {"name": "RB Leipzig", "strength": 81, "logo": "⚪🔴", "flag": "🇩🇪", "stars": ["Benjamin Sesko", "Lois Openda", "Xavi Simons", "Antonio Nusa", "Christoph Baumgartner"]},
    {"name": "SSC Napoli", "strength": 82, "logo": "🔵", "flag": "🇮🇹", "stars": ["Khvicha Kvaratskhelia", "Romelu Lukaku", "Scott McTominay", "Frank Anguissa", "Matteo Politano"]},
    {"name": "SS Lazio", "strength": 78, "logo": "🦅", "flag": "🇮🇹", "stars": ["Valentin Castellanos", "Mattia Zaccagni", "Boulaye Dia", "Matteo Guendouzi", "Gustav Isaksen"]},
    {"name": "AS Monaco", "strength": 79, "logo": "⚪🔴", "flag": "🇫🇷", "stars": ["Breel Embolo", "Eliesse Ben Seghir", "Maghnes Akliouche", "Aleksandr Golovin", "Denis Zakaria"]},
    {"name": "Celtic FC", "strength": 75, "logo": "🟢⚪", "flag": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "stars": ["Kyogo Furuhashi", "Daizen Maeda", "Nicolas Kuhn", "Callum McGregor", "Arne Engels"]},
    {"name": "Shakhtar Donetsk", "strength": 75, "logo": "🟠⚫", "flag": "🇺🇦", "stars": ["Georgiy Sudakov", "Danylo Sikan", "Artem Bondarenko", "Pedrinho", "Kevin Kelsy"]},
    {"name": "Galatasaray", "strength": 78, "logo": "🟡🔴", "flag": "🇹🇷", "stars": ["Victor Osimhen", "Mauro Icardi", "Dries Mertens", "Baris Alper Yilmaz", "Lucas Torreira"]},
    {"name": "Red Bull Salzburg", "strength": 76, "logo": "🔴⚪", "flag": "🇦🇹", "stars": ["Karim Konate", "Oscar Gloukh", "Dorgeles Nene", "Maurits Kjaergaard", "Lucas Gourna-Douath"]},
    {"name": "Ajax Amsterdam", "strength": 77, "logo": "⚪🔴", "flag": "🇳🇱", "stars": ["Brian Brobbey", "Wout Weghorst", "Bertrand Traore", "Mika Godts", "Kenneth Taylor"]},
    {"name": "Atalanta", "strength": 81, "logo": "🔵⚫", "flag": "🇮🇹", "stars": ["Mateo Retegui", "Ademola Lookman", "Charles De Ketelaere", "Ederson", "Mario Pasalic"]},
    {"name": "VfB Stuttgart", "strength": 78, "logo": "⚪🔴", "flag": "🇩🇪", "stars": ["Deniz Undav", "Ermedin Demirovic", "Enzo Millot", "Jamie Leweling", "Angelo Stiller"]},
    {"name": "Girona FC", "strength": 78, "logo": "🔴⚪", "flag": "🇪🇸", "stars": ["Bojan Miovski", "Viktor Tsygankov", "Bryan Gil", "Yangel Herrera", "Ivan Martin"]},
    {"name": "Stade Brestois", "strength": 75, "logo": "🔴⚪", "flag": "🇫🇷", "stars": ["Ludovic Ajorque", "Romain Del Castillo", "Abdallah Sima", "Mahdi Camara", "Hugo Magnetti"]},
]

EUROPA_LEAGUE_INTL_POOL = [
    {"name": "Sevilla FC", "strength": 78, "logo": "⚪🔴", "flag": "🇪🇸", "stars": ["Isaac Romero", "Dodi Lukebakio", "Saul Niguez", "Djibril Sow", "Lucien Agoume"]},
    {"name": "AS Roma", "strength": 80, "logo": "🐺", "flag": "🇮🇹", "stars": ["Artem Dovbyk", "Paulo Dybala", "Lorenzo Pellegrini", "Matias Soule", "Bryan Cristante"]},
    {"name": "Villarreal CF", "strength": 78, "logo": "🟡", "flag": "🇪🇸", "stars": ["Ayoze Perez", "Gerard Moreno", "Alex Baena", "Nicolas Pepe", "Dani Parejo"]},
    {"name": "Athletic Bilbao", "strength": 79, "logo": "🔴⚪", "flag": "🇪🇸", "stars": ["Nico Williams", "Inaki Williams", "Oihan Sancet", "Gorka Guruzeta", "Benat Prados"]},
    {"name": "Real Sociedad", "strength": 78, "logo": "🔵⚪", "flag": "🇪🇸", "stars": ["Mikel Oyarzabal", "Takefusa Kubo", "Brais Mendez", "Martin Zubimendi", "Orri Oskarsson"]},
    {"name": "Fiorentina", "strength": 77, "logo": "🟣", "flag": "🇮🇹", "stars": ["Moise Kean", "Albert Gudmundsson", "Andrea Colpani", "Yacine Adli", "Edoardo Bove"]},
    {"name": "Eintracht Frankfurt", "strength": 78, "logo": "🦅", "flag": "🇩🇪", "stars": ["Omar Marmoush", "Hugo Ekitike", "Mario Götze", "Fares Chaibi", "Ellyes Skhiri"]},
    {"name": "Olympique Marseille", "strength": 78, "logo": "⚪🔵", "flag": "🇫🇷", "stars": ["Mason Greenwood", "Elye Wahi", "Amine Harit", "Adrien Rabiot", "Pierre-Emile Hojbjerg"]},
    {"name": "Olympique Lyonnais", "strength": 77, "logo": "🦁", "flag": "🇫🇷", "stars": ["Alexandre Lacazette", "Georges Mikautadze", "Rayan Cherki", "Said Benrahma", "Corentin Tolisso"]},
    {"name": "LOSC Lille", "strength": 78, "logo": "🔴", "flag": "🇫🇷", "stars": ["Jonathan David", "Edon Zhegrova", "Angel Gomes", "Osame Sahraoui", "Benjamin Andre"]},
    {"name": "Fenerbahce", "strength": 77, "logo": "🟡🔵", "flag": "🇹🇷", "stars": ["Edin Dzeko", "Youssef En-Nesyri", "Dusan Tadic", "Fred", "Sebastian Szymanski"]},
    {"name": "Besiktas JK", "strength": 76, "logo": "🦅", "flag": "🇹🇷", "stars": ["Ciro Immobile", "Rafa Silva", "Semih Kilicsoy", "Gedson Fernandes", "Ernest Muci"]},
]

APEX_CUP_LOWER_POOL = [
    {"name": "Leeds United", "strength": 72, "logo": "⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Joel Piroe", "Willy Gnonto", "Mateo Joseph", "Brenden Aaronson", "Daniel James"]},
    {"name": "Sunderland", "strength": 71, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jobe Bellingham", "Wilson Isidor", "Patrick Roberts", "Romaine Mundle", "Chris Rigg"]},
    {"name": "Burnley", "strength": 72, "logo": "🟣", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Lyle Foster", "Luca Koleosho", "Josh Brownhill", "Zian Flemming", "Jaidon Anthony"]},
    {"name": "Sheffield United", "strength": 71, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Kieffer Moore", "Gustavo Hamer", "Callum O'Hare", "Jesurun Rak-Sakyi", "Vinicius Souza"]},
    {"name": "West Bromwich", "strength": 70, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Josh Maja", "Karlan Grant", "Tom Fellows", "John Swift", "Alex Mowatt"]},
    {"name": "Middlesbrough", "strength": 70, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Emmanuel Latte Lath", "Finn Azaz", "Tommy Conway", "Hayden Hackney", "Riley McGree"]},
    {"name": "Coventry City", "strength": 69, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Haji Wright", "Ellis Simms", "Jack Rudoni", "Tatsuhiro Sakamoto", "Milan van Ewijk"]},
    {"name": "Norwich City", "strength": 70, "logo": "🟡🟢", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Josh Sargent", "Borja Sainz", "Ante Crnac", "Marcelino Nunez", "Kenny McLean"]},
    {"name": "Watford", "strength": 69, "logo": "🟡🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Vakoun Bayo", "Edo Kayembe", "Kwadwo Baah", "Giorgi Chakvetadze", "Moussa Sissoko"]},
    {"name": "Bristol City", "strength": 68, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Sinclair Armstrong", "Fally Mayulu", "Anis Mehmeti", "Max Bird", "Jason Knight"]},
    {"name": "Millwall", "strength": 68, "logo": "🦁", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Macaulay Langstaff", "Duncan Watmore", "Romain Esse", "George Honeyman", "George Saville"]},
    {"name": "Swansea City", "strength": 68, "logo": "🦢", "flag": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "stars": ["Liam Cullen", "Zan Vipotnik", "Ronald Pereira", "Ollie Cooper", "Matt Grimes"]},
    {"name": "Blackburn Rovers", "strength": 69, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Yuki Ohashi", "Makhtar Gueye", "Tyrhys Dolan", "Ryan Hedges", "Sondre Tronstad"]},
    {"name": "Hull City", "strength": 68, "logo": "🐯", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Chris Bedia", "Mohamed Belloumi", "Liam Millar", "Kasey Palmer", "Xavier Simons"]},
    {"name": "QPR", "strength": 67, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Zan Celar", "Michael Frey", "Karamoko Dembele", "Ilias Chair", "Nicolas Madsen"]},
    {"name": "Stoke City", "strength": 68, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Tom Cannon", "Million Manhoef", "Bae Jun-ho", "Lewis Baker", "Wouter Burger"]},
    {"name": "Derby County", "strength": 67, "logo": "🐏", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jerry Yates", "Kayden Jackson", "Nathaniel Mendez-Laing", "Kenzo Goudmijn", "Ebou Adams"]},
    {"name": "Preston North End", "strength": 67, "logo": "⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Emil Riis", "Milutin Osmajic", "Sam Greenwood", "Mads Frokjaer", "Ben Whiteman"]},
    {"name": "Sheffield Wednesday", "strength": 67, "logo": "🦉", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Josh Windass", "Michael Smith", "Anthony Musaba", "Djeidi Gassama", "Barry Bannan"]},
    {"name": "Plymouth Argyle", "strength": 66, "logo": "🟢", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Morgan Whittaker", "Mustapha Bundu", "Rami Al Hajj", "Ibrahim Cissoko", "Adam Randell"]},
    {"name": "Portsmouth", "strength": 66, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Callum Lang", "Christian Saydee", "Mark O'Mahony", "Paddy Lane", "Freddie Potts"]},
    {"name": "Luton Town", "strength": 70, "logo": "🟠", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Elijah Adebayo", "Carlton Morris", "Tahith Chong", "Alfie Doughty", "Marvelous Nakamba"]},
    {"name": "Cardiff City", "strength": 67, "logo": "🐦", "flag": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "stars": ["Callum Robinson", "Rubin Colwill", "Anwar El Ghazi", "Chris Willock", "Alex Robertson"]},
    {"name": "Oxford United", "strength": 66, "logo": "🐂", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Mark Harris", "Dane Scarlett", "Tyler Goodrham", "Siriki Dembele", "Cameron Brannagan"]},
    {"name": "Birmingham City", "strength": 68, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jay Stansfield", "Alfie May", "Willum Willumsson", "Emil Hansson", "Paik Seung-ho"]},
    {"name": "Wrexham AFC", "strength": 66, "logo": "🐉", "flag": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "stars": ["Paul Mullin", "Jack Marriott", "Ollie Palmer", "Elliot Lee", "George Dobson"]},
    {"name": "Bolton Wanderers", "strength": 66, "logo": "⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Dion Charles", "Victor Adeboyejo", "John McAtee", "Aaron Collins", "Josh Sheehan"]},
    {"name": "Charlton Athletic", "strength": 65, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Alfie May", "Matty Godden", "Tyreece Campbell", "Luke Berry", "Greg Docherty"]},
    {"name": "Barnsley", "strength": 65, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Stephen Humphrys", "Sam Cosgrove", "Davis Keillor-Dunn", "Adam Phillips", "Luca Connell"]},
    {"name": "Reading FC", "strength": 65, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Sam Smith", "Kelvin Ehibhatiomhan", "Harvey Knibbs", "Chem Campbell", "Lewis Wing"]},
    {"name": "Blackpool FC", "strength": 65, "logo": "🍊", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Kyle Joseph", "Jake Beesley", "CJ Hamilton", "Rob Apter", "Lee Evans"]},
    {"name": "Wigan Athletic", "strength": 65, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Thelo Aasgaard", "Joe Hugill", "Dale Taylor", "Callum McManaman", "Matt Smith"]},
    {"name": "Huddersfield Town", "strength": 66, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Josh Koroma", "Bojan Radulovic", "Callum Marshall", "Ben Wiles", "Jonathan Hogg"]},
    {"name": "Lincoln City", "strength": 64, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Ben House", "Bailey Cadamarteri", "Jovon Makama", "Tendayi Darikwa", "Ethan Erhahon"]},
    {"name": "Peterborough United", "strength": 65, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Ricky-Jade Jones", "Malik Mothersille", "Kwame Poku", "Abraham Odoh", "Hector Kyprianou"]},
    {"name": "Wycombe Wanderers", "strength": 64, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Richard Kone", "Garath McCleary", "Cameron Humphreys", "Fred Onyedinma", "Luke Leahy"]},
    {"name": "Stockport County", "strength": 65, "logo": "🔵", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Louie Barry", "Kyle Wootton", "Isaac Olaofe", "Fraser Horsfall", "Callum Camps"]},
    {"name": "Rotherham United", "strength": 64, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jonson Clarke-Harris", "Sam Nombe", "Mallik Wilks", "Joe Powell", "Christ Tiehi"]},
    {"name": "Mansfield Town", "strength": 63, "logo": "🟡", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Will Evans", "Lee Gregory", "Stephen Quinn", "Keanu Baccus", "Hiram Boateng"]},
    {"name": "Stevenage FC", "strength": 63, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Jake Young", "Elliott List", "Jamie Reid", "Dan Kemp", "Harvey White"]},
    {"name": "Exeter City", "strength": 63, "logo": "🔴⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Josh Magennis", "Millenic Alli", "Tristan Crama", "Amani Richards", "Ryan Woods"]},
    {"name": "Bristol Rovers", "strength": 63, "logo": "🔵⚪", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Promise Omochere", "Gatlin O'Donkor", "Scott Sinclair", "Isaac Hutchinson", "Kamil Conteh"]},
    {"name": "Leyton Orient", "strength": 63, "logo": "🔴", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Charlie Kelman", "Dan Agyei", "Sonny Perkins", "Ethan Galbraith", "Jordan Brown"]},
    {"name": "Burton Albion", "strength": 62, "logo": "🟡", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "stars": ["Danilo Orsi", "Billy Bodin", "Ben Whitfield", "Tomas Kalinauskas", "Elliot Watt"]},
]

def ensure_competitions_schema(conn: sqlite3.Connection) -> None:
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS player_match_stat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fixture_id INTEGER NOT NULL,
        competition TEXT NOT NULL,
        team_name TEXT NOT NULL,
        player_name TEXT NOT NULL,
        is_user_player INTEGER NOT NULL DEFAULT 0,
        goals INTEGER NOT NULL DEFAULT 0,
        assists INTEGER NOT NULL DEFAULT 0,
        rating REAL NOT NULL DEFAULT 6.5
    )
    """)
    db_exec(conn, "CREATE INDEX IF NOT EXISTS idx_pms_comp ON player_match_stat(competition)")
    db_exec(conn, "CREATE INDEX IF NOT EXISTS idx_pms_team ON player_match_stat(team_name)")
    db_exec(conn, "CREATE INDEX IF NOT EXISTS idx_pms_pname ON player_match_stat(player_name)")

    ensure_column(conn, "fixture", "leg", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "fixture", "tie_id", "TEXT")
    ensure_column(conn, "fixture", "group_name", "TEXT")
    ensure_column(conn, "fixture", "aggregate_score", "TEXT")

    ensure_column(conn, "team", "league", "TEXT NOT NULL DEFAULT 'League'")
    ensure_column(conn, "team", "logo_icon", "TEXT DEFAULT '🛡️'")
    ensure_column(conn, "team", "country_flag", "TEXT DEFAULT '🏴󠁧󠁢󠁥󠁮󠁧󠁿'")
    ensure_column(conn, "team", "star_players", "TEXT DEFAULT '[]'")

def generate_season(conn: sqlite3.Connection) -> None:
    ensure_competitions_schema(conn)
    # Clear previous season data
    db_exec(conn, "DELETE FROM team")
    db_exec(conn, "DELETE FROM fixture")
    db_exec(conn, "DELETE FROM player_match_stat")

    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    user_team = club["name"] if club else "Touchline Boss FC"

    existing = {user_team}
    ai_teams = []
    # Generate 19 AI teams so user + 19 = 20 teams (Luật EPL 20 đội ngoài đời)
    for _ in range(19):
        team_name = gen_team_name(existing)
        existing.add(team_name)
        ai_teams.append(team_name)

    league_teams = [user_team] + ai_teams

    # Save 20 league teams with metadata & stars
    for idx, t in enumerate(league_teams):
        if t == user_team:
            base_strength = 55
            logo = "⚡"
            flag = "🇻🇳"
            stars = ["Tiền Đạo Chủ Lực", "Nhạc Trưởng Tuyến Giữa", "Thủ Môn Thần Thánh", "Trung Vệ Thép", "Hậu Vệ Cánh Tốc Độ"]
        else:
            meta = DOMESTIC_EPL_POOL[(idx - 1) % len(DOMESTIC_EPL_POOL)]
            base_strength = int(clamp(55 + random.randint(-7, 7) + 2 * (db_one(conn, "SELECT club_level FROM club")[0] - 1), 40, 100))
            logo = meta["logo"]
            flag = meta["flag"]
            stars = meta["stars"]
        db_exec(conn, """
            INSERT INTO team (name, base_strength, league, logo_icon, country_flag, star_players)
            VALUES (?, ?, 'League', ?, ?, ?)
        """, (t, base_strength, logo, flag, json.dumps(stars, ensure_ascii=False)))

    # 1. GIẢI VÔ ĐỊCH QUỐC GIA (38 Vòng, lượt đi & lượt về, 20 đội)
    rr1 = round_robin_schedule(league_teams)  # 19 rounds lượt đi
    rr2 = [[(away, home) for home, away in r] for r in rr1]  # 19 rounds lượt về
    all_38_rounds = rr1 + rr2
    for r_idx, pairs in enumerate(all_38_rounds, start=1):
        day = rules.LEAGUE_START + (r_idx - 1) * 7
        for home, away in pairs:
            db_exec(conn, """
                INSERT INTO fixture (competition, day, round, home, away, leg, tie_id, group_name)
                VALUES ('League', ?, ?, ?, ?, 1, NULL, NULL)
            """, (day, r_idx, home, away))

    # 2. GIẢI C1 (UEFA Champions League - 32 Đội): Top 4 giải VĐQG + 28 CLB quốc tế
    previous_rank = db_one(conn,"SELECT last_season_rank FROM club")[0] if "last_season_rank" in db_one(conn,"SELECT * FROM club").keys() else None
    top4_national = [user_team] + ai_teams[:3] if previous_rank and previous_rank<=4 else ai_teams[:4]
    c1_intl = CHAMPIONS_LEAGUE_INTL_POOL[:28]
    for c in c1_intl:
        db_exec(conn, """
            INSERT OR IGNORE INTO team (name, base_strength, league, logo_icon, country_flag, star_players)
            VALUES (?, ?, 'Champions', ?, ?, ?)
        """, (c["name"], int(clamp(58 + random.randint(-7, 7) + 2 * (db_one(conn, "SELECT club_level FROM club")[0] - 1), 40, 110)), c["logo"], c["flag"], "[]"))

    c1_32_teams = top4_national + [c["name"] for c in c1_intl]
    # 8 Bảng A-H (Mỗi bảng 4 đội, User ở Bảng A để trải nghiệm ngay)
    c1_group_days = rules.GROUP_DAYS
    for g_idx in range(8):
        g_name = f"Bảng {chr(65 + g_idx)}"
        g_teams = [c1_32_teams[g_idx], c1_32_teams[8 + g_idx], c1_32_teams[16 + g_idx], c1_32_teams[24 + g_idx]]
        g_rr1 = round_robin_schedule(g_teams)
        g_rr2 = [[(away, home) for home, away in r] for r in g_rr1]
        for m_idx, pairs in enumerate(g_rr1 + g_rr2):
            g_day = c1_group_days[m_idx]
            for home, away in pairs:
                db_exec(conn, """
                    INSERT INTO fixture (competition, day, round, home, away, leg, tie_id, group_name)
                    VALUES ('Champions', ?, ?, ?, ?, 1, NULL, ?)
                """, (g_day, m_idx + 1, home, away, g_name))

    # 3. GIẢI C2 (UEFA Europa League - 16 Đội): Top 5-8 giải VĐQG + 12 CLB quốc tế
    top5_8_national = [user_team] + ai_teams[4:7] if previous_rank and 5<=previous_rank<=8 else ai_teams[4:8]
    c2_intl = EUROPA_LEAGUE_INTL_POOL[:12]
    for c in c2_intl:
        db_exec(conn, """
            INSERT OR IGNORE INTO team (name, base_strength, league, logo_icon, country_flag, star_players)
            VALUES (?, ?, 'Europa', ?, ?, ?)
        """, (c["name"], int(clamp(58 + random.randint(-7, 7) + 2 * (db_one(conn, "SELECT club_level FROM club")[0] - 1), 40, 110)), c["logo"], c["flag"], "[]"))
    c2_16_teams = top5_8_national + [c["name"] for c in c2_intl]
    for g_idx in range(4):
        g_name = f"Bảng {chr(65 + g_idx)}"
        g_teams = [c2_16_teams[g_idx], c2_16_teams[4 + g_idx], c2_16_teams[8 + g_idx], c2_16_teams[12 + g_idx]]
        g_rr1 = round_robin_schedule(g_teams)
        g_rr2 = [[(away, home) for home, away in r] for r in g_rr1]
        for m_idx, pairs in enumerate(g_rr1 + g_rr2):
            g_day = c1_group_days[m_idx]
            for home, away in pairs:
                db_exec(conn, """
                    INSERT INTO fixture (competition, day, round, home, away, leg, tie_id, group_name)
                    VALUES ('Europa', ?, ?, ?, ?, 1, NULL, ?)
                """, (g_day, m_idx + 1, home, away, g_name))

    # 4. GIẢI THỨ 3: CÚP QUỐC GIA APEX (64 Đội - 32 Cặp Đấu Loại Trực Tiếp 2 Lượt Lượt Đi Lượt Về)
    cup_lower = APEX_CUP_LOWER_POOL[:44]
    for c in cup_lower:
        db_exec(conn, """
            INSERT OR IGNORE INTO team (name, base_strength, league, logo_icon, country_flag, star_players)
            VALUES (?, ?, 'ApexCup', ?, ?, ?)
        """, (c["name"], int(clamp(58 + random.randint(-7, 7) + 2 * (db_one(conn, "SELECT club_level FROM club")[0] - 1), 40, 110)), c["logo"], c["flag"], "[]"))

    all_64_cup_teams = league_teams + [c["name"] for c in cup_lower]
    cup_opponents = [t for t in all_64_cup_teams if t != user_team]
    random.shuffle(cup_opponents)
    seeded_64 = [user_team, cup_opponents[0]] + cup_opponents[1:]

    # 32 Cặp đấu loại trực tiếp Vòng 64: Lượt đi Day 2, Lượt về Day 4
    for i in range(32):
        t1 = seeded_64[i * 2]
        t2 = seeded_64[i * 2 + 1]
        tie_id = f"APEX_R64_P{i + 1}"
        # Lượt đi: t1 sân nhà
        db_exec(conn, """
            INSERT INTO fixture (competition, day, round, home, away, leg, tie_id, group_name)
            VALUES ('ApexCup', 8, 1, ?, ?, 1, ?, 'Vòng 64')
        """, (t1, t2, tie_id))
        # Lượt về: t2 sân nhà
        db_exec(conn, """
            INSERT INTO fixture (competition, day, round, home, away, leg, tie_id, group_name)
            VALUES ('ApexCup', 22, 1, ?, ?, 2, ?, 'Vòng 64')
        """, (t2, t1, tie_id))


# -----------------------------
# Market mechanics
# -----------------------------
def regen_market_batch(conn: sqlite3.Connection, reset_at: int) -> None:
    # Wipe listings and create new batch
    db_exec(conn, "DELETE FROM market_listing")
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = club["season_day"]
    # As you progress in season, average market improves slightly
    base = int(clamp(53 + 2 * (club["club_level"] - 1), 48, 82))
    spread = 10

    manager_pool = [
        ("MehmetEminYILDIRIM44", "🦈"),
        ("Mehmet", "👔"),
        ("Dirk S Aus Bo", "⚽"),
        ("Carlos_Silva", "🦁"),
        ("ZlatanFan9", "👑"),
        ("Kenji_Sato", "⚡"),
        ("TouchlineKing", "🔥"),
        ("AlexFerguson_88", "🛡️"),
    ]

    for _ in range(MARKET_LISTINGS_PER_BATCH):
        # Decide currency and quality band
        if random.random() < 0.25:
            currency = "token"
            q_base = base + random.randint(6, 14)
            start_price = random.randint(2, 8)  # tokens
        else:
            currency = "cash"
            q_base = base + random.randint(-4, 10)
            start_price = random.randint(300_000, 2_200_000)

        pos = random.choice(POSITIONS)
        p = gen_player(pos, base=q_base, spread=spread, age_range=(18, 30))
        h = 188 if pos in ("GK", "DC") else random.randint(174, 187)
        w = random.randint(68, 85)
        foot = "Chân trái 🦶" if pos in ("DL", "ML", "AML") else random.choice(["Chân phải 🦶", "Chân phải 🦶", "Cả hai chân 🦶"])
        skill = random.choice(["Không có", "Không có", "Sút xoáy 💫", "Chuyền vượt tuyến 🎯", "Bức tốc ⚡", "Không có"])
        nat = p["nationality"]

        start_price=max(2,math.ceil(rules.player_value(p)/50_000)) if currency=="token" else int(rules.player_value(p)*.85)
        # Decide if this player already has some simulated auction bids (Top Eleven style)
        has_initial_bids = False
        cur_bid = start_price
        bidder = "none"
        bid_list = []

        if has_initial_bids:
            num_bids = random.randint(1, 3)
            sampled_mgrs = random.sample(manager_pool, num_bids)
            for m_idx, (m_name, m_avatar) in enumerate(sampled_mgrs):
                step = 1 if currency == "token" else bid_step(cur_bid)
                cur_bid += step
                bid_list.insert(0, {
                    "user": m_name,
                    "avatar": m_avatar,
                    "tokens": cur_bid if currency == "token" else 0,
                    "cash": cur_bid if currency == "cash" else 0,
                    "time": now_ts() - (num_bids - m_idx) * 25,
                })
            bidder = "ai"

        db_exec(conn, """
            INSERT INTO market_listing
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, potential,
             currency, start_price, current_bid, highest_bidder, user_has_bid, batch_ends_at, created_at, last_ai_update,
             height, weight, preferred_foot, special_skill, nationality, bid_count, recent_bids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"], p["position"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], p["potential"], currency, start_price, cur_bid, bidder, 0, reset_at, now_ts(), now_ts(),
              h, w, foot, skill, nat, len(bid_list), json.dumps(bid_list, ensure_ascii=False)))

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
        if db_one(conn,"SELECT COUNT(*) FROM squad_player")[0]>=rules.MAX_ROSTER or club["cash_debt"]:
            skipped.append((l["name"],l["currency"],int(l["current_bid"])))
            continue

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
        h = l["height"] if "height" in l.keys() else 180
        w = l["weight"] if "weight" in l.keys() else 75
        foot = l["preferred_foot"] if "preferred_foot" in l.keys() else "Chân phải 🦶"
        skill = l["special_skill"] if "special_skill" in l.keys() else "Không có"
        nat = l["nationality"] if "nationality" in l.keys() else "BR"

        pid = db_insert(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential,
             condition, morale, form, injured_until_day, salary, height, weight, preferred_foot, special_skill, nationality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (l["name"], l["position"], l["age"], l["finishing"], l["passing"], l["pace"], l["tackling"], l["marking"],
              l["stamina_skill"], traits, l["potential"], random.randint(75, 95), random.randint(70, 90), random.randint(65, 90), 0,
              int(1_000 + player_overall(l)*90), h, w, foot, skill, nat))

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

    manager_pool = [
        ("MehmetEminYILDIRIM44", "🦈"),
        ("Mehmet", "👔"),
        ("Dirk S Aus Bo", "⚽"),
        ("Carlos_Silva", "🦁"),
        ("ZlatanFan9", "👑"),
        ("Kenji_Sato", "⚡"),
        ("TouchlineKing", "🔥"),
        ("AlexFerguson_88", "🛡️"),
    ]

    for l in listings:
        # Only simulate if auction has at least 8 seconds left
        if time_left < 8:
            continue
        last_upd = l["last_ai_update"]
        if ts <= last_upd:
            continue

        fake_row = l
        quality = player_overall(fake_row)
        base_rate = 0.003
        if l["currency"] == "token":
            base_rate *= 1.3
        base_rate *= clamp(quality / 65.0, 0.6, 1.8)
        base_rate *= clamp(1.2 - (time_left / MARKET_BATCH_SECONDS), 0.6, 1.4)

        dt = ts - last_upd
        expected = base_rate * dt
        n_bids = poisson(expected)

        current_bid = l["current_bid"]
        highest = l["highest_bidder"]
        bid_count = l["bid_count"] if "bid_count" in l.keys() else 0

        recent_bids = []
        if "recent_bids" in l.keys() and l["recent_bids"]:
            try:
                recent_bids = json.loads(l["recent_bids"])
            except Exception:
                recent_bids = []

        added = 0
        for _ in range(n_bids):
            if highest == "you" and random.random() < 0.45:
                break
            if l["currency"] == "cash":
                current_bid += bid_step(current_bid)
            else:
                current_bid += 1
            highest = "ai"
            mgr_name, mgr_avatar = random.choice(manager_pool)
            recent_bids.insert(0, {
                "user": mgr_name,
                "avatar": mgr_avatar,
                "tokens": current_bid if l["currency"] == "token" else 0,
                "cash": current_bid if l["currency"] == "cash" else 0,
                "time": ts,
            })
            recent_bids = recent_bids[:10]
            bid_count += 1
            added += 1

        if added > 0:
            db_exec(conn, """
                UPDATE market_listing
                SET current_bid=?, highest_bidder=?, last_ai_update=?, bid_count=?, recent_bids=?
                WHERE id=?
            """, (current_bid, highest, ts, bid_count, json.dumps(recent_bids, ensure_ascii=False), l["id"]))
        else:
            db_exec(conn, "UPDATE market_listing SET last_ai_update=? WHERE id=?", (ts, l["id"]))

# -----------------------------
# Scout Mechanics (Tab 2: High Quality Players - 2 Days Reset)
# -----------------------------
def update_scout(conn: sqlite3.Connection) -> None:
    club = db_one(conn, "SELECT scout_reset_at FROM club WHERE id=1")
    reset_at = (club["scout_reset_at"] if "scout_reset_at" in club.keys() else 0) or 0
    ts = now_ts()
    listings = db_all(conn, "SELECT * FROM scout_listing")
    if reset_at == 0 or ts >= reset_at or len(listings) == 0:
        new_reset = ts + SCOUT_RESET_SECONDS
        db_exec(conn, "UPDATE club SET scout_reset_at=? WHERE id=1", (new_reset,))
        regen_scout_batch(conn, new_reset)

def regen_scout_batch(conn: sqlite3.Connection, reset_at: int) -> None:
    db_exec(conn, "DELETE FROM scout_listing")
    club=db_one(conn,"SELECT club_level FROM club")
    scout_targets=[]
    for _ in range(8):
        pos=random.choice(POSITIONS)
        base=int(clamp(62+2*(club["club_level"]-1)+random.randint(-4,6),50,105))
        scout_targets.append({"pos":pos,"base":base,
                              "skill":"Không có","cur":"cash","price":0,"tokens":0})

    for t in scout_targets:
        p = gen_player(t["pos"], base=t["base"], spread=5, age_range=(18, 27))
        cur = t.get("cur", "cash")
        price_cash = int(rules.player_value(p)*1.4)
        price_tokens = math.ceil(price_cash/50_000)
        h = 188 if t["pos"] in ("GK", "DC") else random.randint(175, 186)
        w = random.randint(70, 85)
        foot = "Chân trái 🦶" if t["pos"] in ("DL", "ML", "AML") else random.choice(["Chân phải 🦶", "Cả hai chân 🦶"])

        db_exec(conn, """
            INSERT INTO scout_listing
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, potential,
             currency, price_cash, price_tokens, height, weight, preferred_foot, special_skill, nationality, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p["name"], t["pos"], p["age"], p["finishing"], p["passing"], p["pace"], p["tackling"], p["marking"],
              p["stamina_skill"], p["potential"], cur, price_cash, price_tokens,
              h, w, foot, t["skill"], p["nationality"], now_ts()))

# -----------------------------
# Assistant Recommendations (Tab 3: Weakest Position in Squad)
# -----------------------------
def get_or_regen_assistant_recommendations(conn: sqlite3.Connection) -> Tuple[Optional[Dict], List[Dict]]:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = int(club["season_day"])
    formation = club["formation"]
    xi_pairs = starting_xi_from_saved(conn, day, formation)
    players = db_all(conn, "SELECT * FROM squad_player")
    if not players:
        return None, []

    if xi_pairs:
        weakest_player, weakest_pos = min(xi_pairs, key=lambda pair: player_overall(pair[0]))
    else:
        weakest_player = min(players, key=lambda p: player_overall(p))
        weakest_pos = weakest_player["position"]

    weak_name = weakest_player["name"]
    weak_ovr = round(player_overall(weakest_player), 1)

    existing = db_all(conn, "SELECT * FROM assistant_recommendation WHERE target_pos=? ORDER BY tier ASC", (weakest_pos,))
    if len(existing) == 3:
        rows = []
        for r in existing:
            m = player_to_modal_dict(dict(r), source="assistant")
            rows.append({
                **dict(r),
                **m,
                "json_data": json.dumps(m, ensure_ascii=False),
            })
        return {"pos": weakest_pos, "name": weak_name, "ovr": weak_ovr}, rows

    # Regenerate 3 tiered upgrades
    db_exec(conn, "DELETE FROM assistant_recommendation")
    spread = 6

    # Tier 1: Tiềm năng (Good)
    base_1 = int(clamp(weak_ovr + 4, 50, 80))
    p1 = gen_player(weakest_pos, base=base_1, spread=spread, age_range=(19, 23))
    p1_price = int(rules.player_value(p1)*1.3)
    db_insert(conn, """
        INSERT INTO assistant_recommendation
        (tier, target_pos, weakest_player_name, weakest_player_ovr, name, position, age,
         finishing, passing, pace, tackling, marking, stamina_skill, potential, currency, price_cash, price_tokens,
         height, weight, preferred_foot, special_skill, nationality, created_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cash', ?, 0, ?, ?, ?, 'Không có', ?, ?)
    """, (weakest_pos, weak_name, weak_ovr, p1["name"], weakest_pos, p1["age"],
          p1["finishing"], p1["passing"], p1["pace"], p1["tackling"], p1["marking"], p1["stamina_skill"],
          p1["potential"], p1_price, random.randint(176, 188), random.randint(70, 82),
          random.choice(["Chân phải 🦶", "Chân trái 🦶"]), p1["nationality"], now_ts()))

    # Tier 2: Trụ cột (Elite)
    base_2 = int(clamp(weak_ovr + 9, 55, 88))
    p2 = gen_player(weakest_pos, base=base_2, spread=spread, age_range=(23, 27))
    p2_price = int(rules.player_value(p2)*1.4)
    db_insert(conn, """
        INSERT INTO assistant_recommendation
        (tier, target_pos, weakest_player_name, weakest_player_ovr, name, position, age,
         finishing, passing, pace, tackling, marking, stamina_skill, potential, currency, price_cash, price_tokens,
         height, weight, preferred_foot, special_skill, nationality, created_at)
        VALUES (2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cash', ?, 0, ?, ?, ?, ?, ?, ?)
    """, (weakest_pos, weak_name, weak_ovr, p2["name"], weakest_pos, p2["age"],
          p2["finishing"], p2["passing"], p2["pace"], p2["tackling"], p2["marking"], p2["stamina_skill"],
          p2["potential"], p2_price, random.randint(178, 190), random.randint(72, 85),
          random.choice(["Chân phải 🦶", "Chân trái 🦶"]), random.choice(["Bức tốc ⚡", "Chuyền vượt tuyến 🎯"]),
          p2["nationality"], now_ts()))

    # Tier 3: Đẳng cấp thế giới (World Class)
    base_3 = int(clamp(weak_ovr + 14, 60, 96))
    p3 = gen_player(weakest_pos, base=base_3, spread=spread, age_range=(24, 29))
    p3_price = int(rules.player_value(p3)*1.5)
    db_insert(conn, """
        INSERT INTO assistant_recommendation
        (tier, target_pos, weakest_player_name, weakest_player_ovr, name, position, age,
         finishing, passing, pace, tackling, marking, stamina_skill, potential, currency, price_cash, price_tokens,
         height, weight, preferred_foot, special_skill, nationality, created_at)
        VALUES (3, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cash', ?, 0, ?, ?, ?, ?, ?, ?)
    """, (weakest_pos, weak_name, weak_ovr, p3["name"], weakest_pos, p3["age"],
          p3["finishing"], p3["passing"], p3["pace"], p3["tackling"], p3["marking"], p3["stamina_skill"],
          p3["potential"], p3_price, random.randint(180, 192), random.randint(75, 88),
          random.choice(["Chân phải 🦶", "Cả hai chân 🦶"]), random.choice(["Sút xoáy 💫", "Nhạc trưởng 🎹", "Bức tốc ⚡"]),
          p3["nationality"], now_ts()))

    fresh = db_all(conn, "SELECT * FROM assistant_recommendation WHERE target_pos=? ORDER BY tier ASC", (weakest_pos,))
    rows = []
    for r in fresh:
        m = player_to_modal_dict(dict(r), source="assistant")
        rows.append({
            **dict(r),
            **m,
            "json_data": json.dumps(m, ensure_ascii=False),
        })
    return {"pos": weakest_pos, "name": weak_name, "ovr": weak_ovr}, rows

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
def advance_day(conn: sqlite3.Connection) -> bool:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    pending = db_one(conn, "SELECT id FROM fixture WHERE played=0 AND day<=? AND (home=? OR away=?) LIMIT 1",
                     (club["season_day"], club["name"], club["name"]))
    if pending:
        flash("Hãy hoàn thành trận đấu hôm nay trước khi sang ngày.")
        return False
    if club["season_day"] >= club["season_length"]:
        flash("Mùa giải đã kết thúc. Chọn Bắt đầu mùa mới để tổng kết và xét thăng cấp.")
        return False
    day = club["season_day"] + 1
    eff = facility_effects(conn)
    wages = sum(p["salary"] for p in db_all(conn, "SELECT salary FROM squad_player")) if day % 7 == 0 else 0
    tv_gain = 1 if club["tv_contract_signed"] and day % 3 == 0 else 0
    sponsor_gain = 12_000 if club["sponsor_contract_signed"] else 0
    income = sponsor_gain + eff["fan_revenue_daily"]
    debt_payment = min(club["cash_debt"], club["cash"] + income)
    available = club["cash"] + income - debt_payment
    paid = min(available, wages)
    debt = club["cash_debt"] - debt_payment + wages - paid
    medical = eff["heal_packs_daily"] if day % 7 == 0 else 0
    conn.execute("""UPDATE club SET season_day=?, tokens=tokens+?, cash=?, cash_debt=?,
        rest_packs=MAX(rest_packs, MIN(100,rest_packs+?)), injury_packs=MAX(injury_packs,MIN(100,injury_packs+?)),
        training_points_today=0, last_wage_day=?, clock_offset_seconds=clock_offset_seconds+86400 WHERE id=1""",
        (day, tv_gain, available-paid, debt, eff["rest_packs_daily"], medical, day if wages else club["last_wage_day"]))
    rules.recover_condition(conn, now_ts())
    conn.execute("UPDATE squad_player SET morale=MIN(100,morale+1)")
    if debt:
        conn.execute("UPDATE squad_player SET morale=MAX(20,morale-3)")
    rules.event(conn, "daily_finance", f"Thu {income:,}; lương tuần {wages:,}; nợ {debt:,}", income-paid-debt_payment, tv_gain)
    auto_play_ai_fixtures(conn, up_to_day=day, user_team=club["name"])
    log_all_players_missing(conn, day, note="daily")
    conn.commit()
    return True

def ensure_youth_table(conn: sqlite3.Connection) -> None:
    db_exec(conn, """
    CREATE TABLE IF NOT EXISTS youth_player (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        position TEXT NOT NULL,
        line TEXT NOT NULL,
        age INTEGER NOT NULL DEFAULT 18,
        overall REAL NOT NULL,
        potential REAL NOT NULL,
        finishing REAL NOT NULL,
        passing REAL NOT NULL,
        pace REAL NOT NULL,
        tackling REAL NOT NULL,
        marking REAL NOT NULL,
        stamina_skill REAL NOT NULL,
        height INTEGER NOT NULL DEFAULT 180,
        weight INTEGER NOT NULL DEFAULT 74,
        preferred_foot TEXT NOT NULL DEFAULT 'Chân phải 🦶',
        special_skill TEXT NOT NULL DEFAULT 'Không có',
        nationality TEXT NOT NULL DEFAULT 'VN',
        slot_index INTEGER NOT NULL,
        tier INTEGER NOT NULL DEFAULT 1,
        trained_ovr REAL NOT NULL DEFAULT 0,
        is_signed INTEGER NOT NULL DEFAULT 0,
        season_day INTEGER NOT NULL DEFAULT 0
    )
    """)

def format_youth_player_dict(conn: sqlite3.Connection, p: Any) -> Dict[str, Any]:
    row = dict(p) if hasattr(p, "keys") else p
    nationality = row.get("nationality") or "VN"
    flag = NAT_FLAGS.get(nationality, "🏳️")
    ov = round(player_overall(row), 1)
    stars_info = cltt_stars_info(ov)
    attrs = compute_player_attributes_15(row)
    json_data = json.dumps({
        "id": row.get("id"),
        "name": row.get("name"),
        "position": row.get("position"),
        "line": row.get("line"),
        "age": row.get("age", 18),
        "overall": ov,
        "potential": row.get("potential", 80),
        "stars_str": stars_info["stars_str"],
        "stars_val": stars_info["val"],
        "full_stars": stars_info["full"],
        "half_star": stars_info["half"],
        "nationality": nationality,
        "flag": flag,
        "height": row.get("height", 180),
        "weight": row.get("weight", 74),
        "preferred_foot": row.get("preferred_foot", "Chân phải 🦶"),
        "special_skill": row.get("special_skill", "Không có"),
        "tier": row.get("tier", 1),
        "attributes": attrs,
        "cltt": ov,
    }, ensure_ascii=False)
    return {
        **row,
        "overall": ov,
        "cltt": ov,
        "stars_str": stars_info["stars_str"],
        "stars_val": stars_info["val"],
        "full_stars": stars_info["full"],
        "half_star": stars_info["half"],
        "flag": flag,
        "json_data": json_data,
        "attributes": attrs,
    }

def get_or_regen_youth_squad(conn: sqlite3.Connection, force: bool = False) -> List[Dict[str, Any]]:
    ensure_youth_table(conn)
    if force:
        db_exec(conn, "DELETE FROM youth_player WHERE is_signed=0")

    existing = db_all(conn, "SELECT * FROM youth_player WHERE is_signed=0 ORDER BY slot_index ASC")
    if db_one(conn,"SELECT 1 FROM youth_player WHERE cohort=? LIMIT 1",(db_one(conn,"SELECT season_number FROM club")[0],)) and not force:
        return [format_youth_player_dict(conn, p) for p in existing]

    # Generate 11 youth players aged 18 in 11 positions across 3 lines
    ensure_all_facilities(conn)
    fac = db_one(conn, "SELECT level FROM facility WHERE name='youth_academy'")
    youth_lvl = fac["level"] if fac else 1
    club = db_one(conn, "SELECT season_day FROM club WHERE id=1")
    s_day = club["season_day"] if club else 0

    special_skills_pool = [
        "Sút xa sấm sét ⚡", "Chuyên gia cản phá 🧤", "Nhạc trưởng kiến tạo 🎯",
        "Dốc bóng thần tốc 🚀", "Không chiến dũng mãnh ✈️", "Xoạc bóng chuẩn xác 🛡️",
        "Bóng chết điêu luyện ⚽", "Không có"
    ]

    for idx, (pos, line) in enumerate(YOUTH_POSITIONS_11, start=1):
        # Chất lượng cầu thủ trẻ phụ thuộc trực tiếp vào cơ sở đào tạo trẻ
        base_cltt = int(clamp(50 + (youth_lvl - 1) * 1.5 + random.randint(-3, 3), 45, 180))
        potential = int(clamp(base_cltt + random.randint(15, 28), 60, 120))

        p_stats = gen_player(pos, base=base_cltt, spread=8, age_range=(18, 18))
        h = random.randint(176, 189)
        w = random.randint(70, 82)
        foot = random.choice(["Chân phải 🦶", "Chân trái 🦶", "Hai chân như một 👟"])
        skill = random.choice(special_skills_pool)

        db_exec(conn, """
            INSERT INTO youth_player
            (name, position, line, age, overall, potential, finishing, passing, pace,
             tackling, marking, stamina_skill, height, weight, preferred_foot, special_skill,
             nationality, slot_index, tier, trained_ovr, is_signed, season_day, cohort)
            VALUES (?, ?, ?, 18, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?)
        """, (p_stats["name"], pos, line, float(player_overall(p_stats)), float(potential),
              p_stats["finishing"], p_stats["passing"], p_stats["pace"],
              p_stats["tackling"], p_stats["marking"], p_stats["stamina_skill"],
              h, w, foot, skill, p_stats["nationality"], idx, s_day, db_one(conn,"SELECT season_number FROM club")[0]))

    current = db_all(conn, "SELECT * FROM youth_player WHERE is_signed=0 ORDER BY slot_index ASC")
    return [format_youth_player_dict(conn, p) for p in current]

# -----------------------------
# Flask app
# -----------------------------
app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "static"))
init_auth(app, init_db)

@app.teardown_appcontext
def close_game_connections(error=None):
    for conn in g.pop("game_connections", []):
        conn.close()

@app.before_request
def _ensure_db():
    if request.endpoint is None or request.endpoint == "static" or request.endpoint.startswith("auth."):
        return
    init_db()
    # Keep market ticking even if the user is not on the Market page
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.deferred=True
        update_market(conn)
        club=db_one(conn,"SELECT name,season_day FROM club")
        auto_play_ai_fixtures(conn,up_to_day=club["season_day"],user_team=club["name"])
        sqlite3.Connection.commit(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.deferred=False
        conn.close()


def load_context(conn: sqlite3.Connection) -> Dict:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    return {
        "club": club,
        "fmt_money": fmt_money,
        "now_ts": now_ts,
    }

def team_recent_form(conn: sqlite3.Connection, team_name: str, limit: int = 5) -> List[str]:
    rows = db_all(conn, """
        SELECT home, away, home_goals, away_goals
        FROM fixture
        WHERE played=1 AND (home=? OR away=?)
        ORDER BY day DESC, id DESC LIMIT ?
    """, (team_name, team_name, limit))
    form = []
    for r in reversed(rows):
        if r["home"] == team_name:
            if r["home_goals"] > r["away_goals"]: form.append("T")
            elif r["home_goals"] == r["away_goals"]: form.append("H")
            else: form.append("B")
        else:
            if r["away_goals"] > r["home_goals"]: form.append("T")
            elif r["away_goals"] == r["home_goals"]: form.append("H")
            else: form.append("B")
    while len(form) < limit:
        form.insert(0, "—")
    return form

def cltt_to_stars(cltt: float) -> str:
    return cltt_to_stars_10(cltt)


def daily_offer(conn):
    club=db_one(conn,"SELECT * FROM club")
    identity=(club["season_number"],club["season_day"])
    saved=db_one(conn,"SELECT * FROM daily_player_offer WHERE season_number=? AND day=?",identity)
    if saved:
        player=json.loads(saved["player_json"])
        price=saved["price"]
    else:
        squad=db_all(conn,"SELECT * FROM squad_player")
        positions=list(dict.fromkeys(formation_slots(club["formation"])))
        averages={pos:sum(player_overall(p) for p in squad if p["position"]==pos)/max(1,sum(p["position"]==pos for p in squad)) for pos in positions}
        weakest=min(averages,key=averages.get)
        base=int(clamp(averages[weakest]+5,48,95))
        player=gen_player(weakest,base=base,spread=5,age_range=(19,26))
        player["traits"]=traits_to_str(gen_traits(*(player[k] for k in ("position",*rules.SKILLS))))
        price=int(rules.player_value(player)*1.2)
        conn.execute("INSERT INTO daily_player_offer VALUES (?,?,?,?)",(*identity,json.dumps(player,ensure_ascii=False),price))
        conn.commit()
    return {**player,"rating":round(player_overall(player),1),"stars":cltt_to_stars(player_overall(player)),
            "price_cash":price,"price_tokens":0,"is_bought":club["daily_offer_bought_day"]==club["season_day"],
            "current_weakest_ovr":round(sum(player_overall(p) for p in db_all(conn,"SELECT * FROM squad_player WHERE position=?",(player["position"],)))/max(1,db_one(conn,"SELECT COUNT(*) FROM squad_player WHERE position=?",(player["position"],))[0]),1)}


@app.route("/")
def home():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = int(club["season_day"])
    my = club["name"]

    # Simulate AI vs AI fixtures up to today so the tables feel alive
    auto_play_ai_fixtures(conn, up_to_day=day, user_team=my)
    eff = facility_effects(conn)

    # 1. UPCOMING FIXTURE (Information for clash card)
    upcoming_fix = db_one(conn, """
        SELECT * FROM fixture
        WHERE played=0 AND (home=? OR away=?)
        ORDER BY day ASC, id ASC LIMIT 1
    """, (my, my))

    upcoming_match = None
    if upcoming_fix:
        is_user_home = (upcoming_fix["home"] == my)
        opp_name = upcoming_fix["away"] if is_user_home else upcoming_fix["home"]
        opp_row = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (opp_name,))
        opp_base = float(opp_row["base_strength"] if opp_row else 60.0)

        user_strength, _ = compute_user_strength(conn, club)
        user_cltt = round(user_strength, 1)
        opp_cltt = round(opp_base, 1)

        weathers = [
            {"name": "MƯA", "icon": "🌧️"},
            {"name": "NẮNG ĐẸP", "icon": "☀️"},
            {"name": "SƯƠNG MÙ", "icon": "🌫️"},
            {"name": "TUYẾT RƠI", "icon": "❄️"},
            {"name": "GIÓ NHẸ", "icon": "⛅"},
        ]
        weather = weathers[upcoming_fix["id"] % len(weathers)]

        home_name = upcoming_fix["home"]
        away_name = upcoming_fix["away"]

        home_cltt = user_cltt if is_user_home else opp_cltt
        away_cltt = opp_cltt if is_user_home else user_cltt

        upcoming_match = {
            "id": upcoming_fix["id"],
            "competition": upcoming_fix["competition"],
            "comp_label": "C.NGHIỆP" if upcoming_fix["competition"] == "League" else upcoming_fix["competition"].upper(),
            "round": upcoming_fix["round"],
            "day": upcoming_fix["day"],
            "time_label": f"CÒN {upcoming_fix['day']-day} NGÀY" if upcoming_fix["day"]>day else "HÔM NAY",
            "weather": weather,
            "difficulty": "Khó" if opp_cltt > user_cltt*1.15 else "Thuận lợi" if user_cltt > opp_cltt*1.15 else "Cân bằng",
            "is_user_home": is_user_home,
            "home": home_name,
            "away": away_name,
            "home_cltt": home_cltt,
            "away_cltt": away_cltt,
            "home_stars": cltt_to_stars(home_cltt),
            "away_stars": cltt_to_stars(away_cltt),
            "home_form": team_recent_form(conn, home_name),
            "away_form": team_recent_form(conn, away_name),
        }

    # 2. LAST PLAYED FIXTURE REPORT
    last_fix = db_one(conn, """
        SELECT * FROM fixture
        WHERE played=1 AND (home=? OR away=?)
        ORDER BY day DESC, id DESC LIMIT 1
    """, (my, my))

    last_match = None
    if last_fix:
        is_user_home = (last_fix["home"] == my)
        user_goals = last_fix["home_goals"] if is_user_home else last_fix["away_goals"]
        opp_goals = last_fix["away_goals"] if is_user_home else last_fix["home_goals"]
        opp_name = last_fix["away"] if is_user_home else last_fix["home"]
        res_label = "THẮNG" if user_goals > opp_goals else ("HÒA" if user_goals == opp_goals else "THUA")
        res_class = "good" if user_goals > opp_goals else ("warn" if user_goals == opp_goals else "danger")
        last_match = {
            **dict(last_fix),
            "is_user_home": is_user_home,
            "opp_name": opp_name,
            "user_goals": user_goals,
            "opp_goals": opp_goals,
            "result_label": res_label,
            "result_class": res_class,
        }

    # 3. SQUAD RATING & WEAKEST POSITION SCOUT
    players = db_all(conn, "SELECT * FROM squad_player")
    avg = 0.0
    if players:
        avg = sum(player_effective_rating(p, day) for p in players) / len(players)

    daily_scout = daily_offer(conn)

    # 4. COMPETITIONS POSITIONS (Bottom Right Widget)
    standings = compute_league_table(conn)
    user_rank = next((r["rank"] for r in standings if r["team"] == my), 1)
    user_pts = next((r["PTS"] for r in standings if r["team"] == my), 0)

    club_stats=compute_club_season_stats(conn,club)
    comp_summary=[
        {"title":"Giải Vô Địch","subtitle":f"Vị trí {user_rank} ({user_pts} điểm)","trophy":"gold"},
        {"title":"Cúp Quốc Gia","subtitle":club_stats["cup_status"],"trophy":"silver"},
        {"title":"Cúp Châu Lục","subtitle":club_stats["champions_status"],"trophy":"bronze"},
    ]

    # 5. MANAGER & ASSISTANT DATA
    manager_hub = {
        "daily_gift_claimed": bool(club["daily_gift_claimed_day"] == day),
        "tv_contract_signed": bool(club["tv_contract_signed"]),
        "sponsor_contract_signed": bool(club["sponsor_contract_signed"]),
    }

    ctx = load_context(conn)
    conn.close()
    return render_template(
        "home.html",
        **ctx,
        effects=eff,
        avg_rating=avg,
        upcoming_match=upcoming_match,
        last_match=last_match,
        daily_scout=daily_scout,
        comp_summary=comp_summary,
        manager_hub=manager_hub,
    )

@app.post("/claim_daily_gift")
def claim_daily_gift():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club")
    if club["daily_gift_claimed_day"] == club["season_day"]:
        flash("Bạn đã nhận quà hôm nay.")
    else:
        medical = int(club["season_day"] % 3 == 0)
        conn.execute("""UPDATE club SET rest_packs=MAX(rest_packs,MIN(100,rest_packs+1)),
            morale_packs=MAX(morale_packs,MIN(100,morale_packs+1)),
            injury_packs=MAX(injury_packs,MIN(100,injury_packs+?)), daily_gift_claimed_day=season_day""", (medical,))
        rules.event(conn, "daily_gift", f"1 thẻ thể lực, 1 tinh thần, {medical} y tế")
        flash("Đã nhận 1 thẻ thể lực, 1 thẻ tinh thần; thẻ y tế được tặng mỗi 3 ngày. Quà không tăng chỉ số kỹ năng.")
    conn.close()
    return redirect(url_for("home"))

@app.post("/sign_contract/<ctype>")
def sign_contract(ctype: str):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    if ctype == "tv":
        if club["tv_contract_signed"]:
            flash("Hợp đồng Bản quyền TV đã được ký trước đó.")
        else:
            db_exec(conn, "UPDATE club SET tv_contract_signed=1, tokens=tokens+2 WHERE id=1")
            flash("📺 Mày đã ký Hợp đồng Bản quyền TV thành công! Nhận ngay +2 Tiền Vàng và +1 Tiền Vàng mỗi 3 ngày.")
    elif ctype == "sponsor":
        if club["sponsor_contract_signed"]:
            flash("Hợp đồng Nhà tài trợ Jersey đã được ký trước đó.")
        else:
            db_exec(conn, "UPDATE club SET sponsor_contract_signed=1, cash=cash+100000 WHERE id=1")
            flash("👕 Mày đã ký Hợp đồng Nhà tài trợ Jersey thành công! Nhận ngay +100.000$ tiền mặt và +12.000$/ngày.")
    conn.close()
    return redirect(url_for("home"))

@app.post("/buy_daily_offer")
def buy_daily_offer():
    conn=get_db()
    club=db_one(conn,"SELECT * FROM club")
    if club["daily_offer_bought_day"]==club["season_day"]:
        flash("Bạn đã mua ưu đãi hôm nay.")
        return redirect(url_for("home"))
    if not can_recruit(conn):
        return redirect(url_for("home"))
    p=daily_offer(conn)
    if club["cash"]<p["price_cash"]:
        flash("Không đủ tiền mặt để ký hợp đồng.")
        return redirect(url_for("home"))
    pid=db_insert(conn,"""INSERT INTO squad_player(name,position,age,finishing,passing,pace,tackling,marking,stamina_skill,
        traits,potential,condition,morale,form,injured_until_day,salary,nationality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (*[p[k] for k in ("name","position","age",*rules.SKILLS,"traits","potential","condition","morale","form","injured_until_day","salary")],p.get("nationality") or "VN"))
    conn.execute("UPDATE club SET cash=cash-?,daily_offer_bought_day=season_day",(p["price_cash"],))
    rules.event(conn,"transfer",p["name"],cash=-p["price_cash"])
    backfill_player_attributes(conn)
    rules.initialize_lifecycle(conn,now_ts())
    log_player_state(conn,pid,club["season_day"],"daily_offer")
    conn.close()
    flash(f"Đã chiêu mộ {p['name']} với đúng chỉ số trong ưu đãi.")
    return redirect(url_for("home"))


@app.post("/resource/exchange")
def resource_exchange():
    res_type = request.form.get("type", "")
    conn = get_db()
    offers = {"cash": (2, "cash", 100_000), "rest": (1, "rest_packs", 2),
              "morale": (1, "morale_packs", 2), "injury": (1, "injury_packs", 1)}
    if res_type not in offers:
        flash("Token được nhận từ hợp đồng và thành tích thi đấu; không có chức năng cộng Token miễn phí.")
    else:
        cost, column, amount = offers[res_type]
        changed = conn.execute(f"UPDATE club SET tokens=tokens-?, {column}={column}+? WHERE tokens>=?", (cost, amount, cost)).rowcount
        if changed:
            rules.event(conn, "exchange", f"Đổi {cost} Token lấy {amount} {column}", amount if column == "cash" else 0, -cost)
            flash("Đổi tài nguyên thành công.")
        else:
            flash("Không đủ Token để đổi tài nguyên.")
    conn.close()
    return redirect(url_for("home"))

@app.post("/advance_day")
def do_advance_day():
    conn = get_db()
    if advance_day(conn):
        flash("Đã sang ngày mới: phục hồi thể lực, cập nhật thu chi và lịch thi đấu.")
    conn.close()
    return redirect(url_for("home"))

@app.route("/player/<int:pid>")
def player_detail(pid: int):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    p = db_one(conn, "SELECT * FROM squad_player WHERE id=?", (pid,))
    if p is None:
        conn.close()
        flash("Không tìm thấy cầu thủ.")
        return redirect(url_for("squad"))

    role = compute_role(p)
    traits_list = parse_traits(p["traits"])

    day = int(club["season_day"])
    modal_dict = player_to_modal_dict(p, source="squad")
    view = {
        **dict(p),
        **modal_dict,
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
    return render_template(
        "player_detail.html",
        **ctx,
        player=view,
        role=role,
        traits=traits_list,
        logs=logs,
        modal_data=modal_dict,
        json_data=json.dumps(modal_dict, ensure_ascii=False),
    )


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
    db_exec(conn,"UPDATE player_match_stat SET player_name=? WHERE player_id=? AND is_user_player=1",(new_name,pid))
    db_exec(conn,"UPDATE player_stat_archive SET player_name=? WHERE player_id=? AND is_user_player=1",(new_name,pid))
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
    db_exec(conn,"UPDATE player_match_stat SET team_name=? WHERE team_name=? AND is_user_player=1",(new_name,old))
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
    value = int(rules.player_value(p)*.45)

    if db_one(conn,"SELECT COUNT(*) FROM squad_player")[0] <= 18:
        flash("Cần giữ tối thiểu 18 cầu thủ để đủ đội hình và dự bị.")
        return redirect(url_for("squad"))
    db_exec(conn, "DELETE FROM lineup_slot WHERE player_id=?", (pid,))
    db_exec(conn, "DELETE FROM squad_player WHERE id=?", (pid,))
    db_exec(conn, "UPDATE club SET cash=cash+? WHERE id=1", (value,))
    conn.close()
    flash(f"Đã bán {p['name']} nhận {fmt_money(value)} tiền mặt.")
    return redirect(url_for("squad"))

@app.route("/market")
def market():
    conn = get_db()
    update_market(conn)
    update_scout(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")

    active_tab = request.args.get("tab", "auctions")
    if active_tab not in ("auctions", "scout", "assistant"):
        active_tab = "auctions"

    # Tab 1: Auctions (15 players batch)
    listings = db_all(conn, "SELECT * FROM market_listing")
    auction_rows: List[Dict] = []
    for l in listings:
        m = player_to_modal_dict(dict(l), source="auction")
        auction_rows.append({
            **dict(l),
            **m,
            "bid_step": bid_step(l["current_bid"]) if l["currency"] == "cash" else 1,
            "json_data": json.dumps(m, ensure_ascii=False),
        })

    # Default ordering: your watched items first, then currently leading, then quality/price
    auction_rows.sort(
        key=lambda r: (
            int(r.get("user_has_bid", 0)),
            1 if r.get("highest_bidder") == "you" else 0,
            1 if r.get("currency") == "token" else 0,
            float(r.get("overall", 0.0)),
            int(r.get("current_bid", 0)),
        ),
        reverse=True,
    )
    watchlist = [r for r in auction_rows if int(r.get("user_has_bid", 0)) == 1]
    seconds_left = max(0, club["market_reset_at"] - now_ts())

    # Tab 2: Scout listings (High Quality Players, 2-day reset)
    scout_raw = db_all(conn, "SELECT * FROM scout_listing ORDER BY price_cash DESC, price_tokens DESC")
    scout_rows: List[Dict] = []
    for s in scout_raw:
        m = player_to_modal_dict(dict(s), source="scout")
        scout_rows.append({
            **dict(s),
            **m,
            "json_data": json.dumps(m, ensure_ascii=False),
        })
    scout_reset_at = club["scout_reset_at"] if "scout_reset_at" in club.keys() else (now_ts() + SCOUT_RESET_SECONDS)
    scout_seconds_left = max(0, (scout_reset_at or (now_ts() + SCOUT_RESET_SECONDS)) - now_ts())

    # Tab 3: Assistant Recommendations (Weakest position in squad)
    weakest_info, assistant_options = get_or_regen_assistant_recommendations(conn)

    ctx = load_context(conn)
    conn.close()
    return render_template(
        "market.html",
        **ctx,
        active_tab=active_tab,
        listings=auction_rows,
        watchlist=watchlist,
        seconds_left=seconds_left,
        scout_listings=scout_rows,
        scout_seconds_left=scout_seconds_left,
        weakest_info=weakest_info,
        assistant_options=assistant_options,
    )

@app.post("/bid/<int:lid>")
def bid(lid: int):
    conn = get_db()
    update_market(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    l = db_one(conn, "SELECT * FROM market_listing WHERE id=?", (lid,))
    if l is None:
        conn.close()
        flash("Phiên đấu giá không còn tồn tại (đã reset).")
        return redirect(url_for("market", tab="auctions"))

    # Determine next bid
    if l["currency"] == "cash":
        inc = bid_step(l["current_bid"])
        next_bid = l["current_bid"] + inc
        if club["cash"] < next_bid:
            conn.close()
            flash("Mày không đủ tiền mặt để trả giá mức này.")
            return redirect(url_for("market", tab="auctions"))
    else:
        next_bid = l["current_bid"] + 1
        if club["tokens"] < next_bid:
            conn.close()
            flash("Mày không đủ token để trả giá mức này.")
            return redirect(url_for("market", tab="auctions"))

    # Update recent_bids list
    recent_bids = []
    if "recent_bids" in l.keys() and l["recent_bids"]:
        try:
            recent_bids = json.loads(l["recent_bids"])
        except Exception:
            recent_bids = []

    new_bid_entry = {
        "user": club["name"],
        "avatar": "👑",
        "tokens": next_bid if l["currency"] == "token" else 0,
        "cash": next_bid if l["currency"] == "cash" else 0,
        "time": now_ts(),
    }
    recent_bids.insert(0, new_bid_entry)
    recent_bids = recent_bids[:10]
    bid_count = (l["bid_count"] if "bid_count" in l.keys() else len(recent_bids)) + 1

    db_exec(conn, """
        UPDATE market_listing
        SET current_bid=?, highest_bidder='you', user_has_bid=1, bid_count=?, recent_bids=?
        WHERE id=?
    """, (next_bid, bid_count, json.dumps(recent_bids, ensure_ascii=False), lid))
    conn.close()
    flash(f"Mày đã trả giá thành công {fmt_money(next_bid) if l['currency']=='cash' else str(next_bid) + ' Tokens'}!")
    return redirect(url_for("market", tab="auctions"))

def can_recruit(conn, amount=1):
    club=db_one(conn,"SELECT * FROM club")
    count=db_one(conn,"SELECT COUNT(*) FROM squad_player")[0]
    if club["cash_debt"]>0:
        flash("Cần thanh toán nợ lương trước khi ký thêm cầu thủ.")
        return False
    if count+amount>rules.MAX_ROSTER:
        flash(f"Đội một có tối đa {rules.MAX_ROSTER} cầu thủ. Hãy chọn ký từng cầu thủ phù hợp.")
        return False
    return True


@app.post("/market/buy_scout/<int:sid>")
def buy_scout(sid: int):
    conn = get_db()
    if not can_recruit(conn):
        return redirect(url_for("market"))

    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    s = db_one(conn, "SELECT * FROM scout_listing WHERE id=?", (sid,))
    if s is None:
        conn.close()
        flash("Cầu thủ này không còn trong danh sách tuyển trạch.")
        return redirect(url_for("market", tab="scout"))

    cash = club["cash"]
    tokens = club["tokens"]

    if s["currency"] == "cash":
        if cash < s["price_cash"]:
            conn.close()
            flash("Mày không đủ tiền mặt để ký hợp đồng với cầu thủ này.")
            return redirect(url_for("market", tab="scout"))
        cash -= s["price_cash"]
    else:
        if tokens < s["price_tokens"]:
            conn.close()
            flash("Mày không đủ token để ký hợp đồng với cầu thủ này.")
            return redirect(url_for("market", tab="scout"))
        tokens -= s["price_tokens"]

    traits = traits_to_str(gen_traits(s["position"], s["finishing"], s["passing"], s["pace"], s["tackling"], s["marking"], s["stamina_skill"]))
    pid = db_insert(conn, """
        INSERT INTO squad_player
        (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential,
         condition, morale, form, injured_until_day, salary, height, weight, preferred_foot, special_skill, nationality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (s["name"], s["position"], s["age"], s["finishing"], s["passing"], s["pace"], s["tackling"], s["marking"],
          s["stamina_skill"], traits, s["potential"], 95, 90, 85, 0,
          int(5_000 + s["price_cash"] * 0.001), s["height"], s["weight"], s["preferred_foot"], s["special_skill"], s["nationality"]))

    log_player_state(conn, pid, int(club["season_day"]), note="scout_transfer")
    db_exec(conn, "UPDATE club SET cash=?, tokens=? WHERE id=1", (cash, tokens))
    db_exec(conn, "DELETE FROM scout_listing WHERE id=?", (sid,))
    backfill_player_attributes(conn)
    conn.close()
    flash(f"🎉 Chúc mừng! Mày đã chiêu mộ thành công siêu sao {s['name']} (CLTT: {player_overall(s):.0f}) vào đội hình!")
    return redirect(url_for("market", tab="scout"))

@app.post("/market/buy_assistant/<int:aid>")
def buy_assistant(aid: int):
    conn = get_db()
    if not can_recruit(conn):
        return redirect(url_for("market"))

    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    a = db_one(conn, "SELECT * FROM assistant_recommendation WHERE id=?", (aid,))
    if a is None:
        conn.close()
        flash("Đề xuất này đã hết hạn hoặc không còn khả dụng.")
        return redirect(url_for("market", tab="assistant"))

    cash = club["cash"]
    tokens = club["tokens"]

    if a["currency"] == "cash":
        if cash < a["price_cash"]:
            conn.close()
            flash("Mày không đủ tiền mặt để ký hợp đồng theo đề xuất của trợ lý.")
            return redirect(url_for("market", tab="assistant"))
        cash -= a["price_cash"]
    else:
        if tokens < a["price_tokens"]:
            conn.close()
            flash("Mày không đủ token để ký hợp đồng theo đề xuất của trợ lý.")
            return redirect(url_for("market", tab="assistant"))
        tokens -= a["price_tokens"]

    traits = traits_to_str(gen_traits(a["position"], a["finishing"], a["passing"], a["pace"], a["tackling"], a["marking"], a["stamina_skill"]))
    pid = db_insert(conn, """
        INSERT INTO squad_player
        (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill, traits, potential,
         condition, morale, form, injured_until_day, salary, height, weight, preferred_foot, special_skill, nationality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (a["name"], a["position"], a["age"], a["finishing"], a["passing"], a["pace"], a["tackling"], a["marking"],
          a["stamina_skill"], traits, a["potential"], 90, 85, 80, 0,
          int(3_000 + a["price_cash"] * 0.001), a["height"], a["weight"], a["preferred_foot"], a["special_skill"], a["nationality"]))

    log_player_state(conn, pid, int(club["season_day"]), note="assistant_upgrade")
    db_exec(conn, "UPDATE club SET cash=?, tokens=? WHERE id=1", (cash, tokens))
    db_exec(conn, "DELETE FROM assistant_recommendation WHERE id=?", (aid,))
    backfill_player_attributes(conn)
    conn.close()
    flash(f"✅ Mày đã nâng cấp vị trí {a['position']} thành công với cầu thủ {a['name']}!")
    return redirect(url_for("market", tab="assistant"))

@app.post("/market/refresh_assistant")
def refresh_assistant():
    conn = get_db()
    db_exec(conn, "DELETE FROM assistant_recommendation")
    get_or_regen_assistant_recommendations(conn)
    conn.close()
    flash("Đã làm mới danh sách đề xuất từ trợ lý HLV!")
    return redirect(url_for("market", tab="assistant"))

# ==========================================================================
# TRAINING CENTER & DRILLS SYSTEM (TOP ELEVEN STYLE)
# ==========================================================================
DRILLS_CATALOG: Dict[str, Dict] = {
    # 1. TẤN CÔNG (ATTACK)
    "att_1": {
        "id": "att_1",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "DỨT ĐIỂM ĐỐI MẶT",
        "desc": "Dứt điểm, Lao ra cản phá, Cản phá, Đoán bóng, Rê bóng",
        "skills": ["finishing", "pace", "tackling"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.5,
        "icon": "⚽"
    },
    "att_2": {
        "id": "att_2",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "CHUYỀN, CHẠY VÀ SÚT!",
        "desc": "Tốc độ, Sút mạnh, Chuyền bóng, Đoán bóng",
        "skills": ["pace", "finishing", "passing"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.5,
        "icon": "👟"
    },
    "att_3": {
        "id": "att_3",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "PHÂN CÔNG ĐÁ PHẠT",
        "desc": "Sút mạnh, Lao ra cản phá, Kèm người, Đánh đầu, Tạt cánh",
        "skills": ["finishing", "passing", "marking"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 3,
        "fatigue": 2.25,
        "icon": "📐"
    },
    "att_4": {
        "id": "att_4",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "KỸ THUẬT SÚT",
        "desc": "Sút mạnh, Dứt điểm, Phản xạ, Sức mạnh, Nhanh nhẹn",
        "skills": ["finishing", "pace", "stamina_skill"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "🎯"
    },
    "att_5": {
        "id": "att_5",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "RÊ VƯỢT NGẠI VẬT",
        "desc": "Tốc độ, Chuyền bóng, Rê bóng, Thể lực",
        "skills": ["pace", "passing", "stamina_skill"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.0,
        "icon": "🏃"
    },
    "att_6": {
        "id": "att_6",
        "category": "attack",
        "category_name": "TẤN CÔNG",
        "name": "TẤN CÔNG BIÊN VÀ ĐÁNH ĐẦU",
        "desc": "Tạt cánh, Tốc độ, Dứt điểm, Đánh đầu",
        "skills": ["passing", "pace", "finishing"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.0,
        "icon": "🏹"
    },

    # 2. PHÒNG NGỰ (DEFENSE)
    "def_1": {
        "id": "def_1",
        "category": "defense",
        "category_name": "PHÒNG NGỰ",
        "name": "KÈM NGƯỜI 1 KÈM 1",
        "desc": "Kèm người, Tắc bóng, Đoán bóng, Tốc độ",
        "skills": ["marking", "tackling", "pace"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.5,
        "icon": "🛡️"
    },
    "def_2": {
        "id": "def_2",
        "category": "defense",
        "category_name": "PHÒNG NGỰ",
        "name": "PHÒNG NGỰ BÓNG BỔNG",
        "desc": "Đánh đầu, Bật cao, Kèm người, Phản xạ",
        "skills": ["marking", "stamina_skill", "tackling"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "🪂"
    },
    "def_3": {
        "id": "def_3",
        "category": "defense",
        "category_name": "PHÒNG NGỰ",
        "name": "KỸ THUẬT XOẠC BÓNG",
        "desc": "Tắc bóng, Sức mạnh, Đoán bóng, Dũng cảm",
        "skills": ["tackling", "stamina_skill", "marking"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "⚔️"
    },
    "def_4": {
        "id": "def_4",
        "category": "defense",
        "category_name": "PHÒNG NGỰ",
        "name": "BẪY VIỆT VỊ ĐỒNG BỘ",
        "desc": "Đoán bóng, Kèm người, Giao tiếp, Tốc độ",
        "skills": ["marking", "passing", "pace"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.5,
        "icon": "🚩"
    },
    "def_5": {
        "id": "def_5",
        "category": "defense",
        "category_name": "PHÒNG NGỰ",
        "name": "CHỐNG PHẢN CÔNG NHANH",
        "desc": "Đoán bóng, Kèm người, Sức mạnh, Thể lực",
        "skills": ["marking", "tackling", "stamina_skill"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.0,
        "icon": "🧱"
    },

    # 3. KIỂM SOÁT BÓNG (POSSESSION)
    "pos_1": {
        "id": "pos_1",
        "category": "possession",
        "category_name": "KIỂM SOÁT BÓNG",
        "name": "ĐÁ MA MỘT CHẠM (RONDO)",
        "desc": "Chuyền bóng, Đoán bóng, Nhanh nhẹn, Tốc độ",
        "skills": ["passing", "pace", "finishing"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.5,
        "icon": "🌀"
    },
    "pos_2": {
        "id": "pos_2",
        "category": "possession",
        "category_name": "KIỂM SOÁT BÓNG",
        "name": "CHUYỀN DÀI CHUẨN XÁC",
        "desc": "Chuyền bóng, Tạt cánh, Sáng tạo, Đoán bóng",
        "skills": ["passing", "finishing", "stamina_skill"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "🎯"
    },
    "pos_3": {
        "id": "pos_3",
        "category": "possession",
        "category_name": "KIỂM SOÁT BÓNG",
        "name": "KIỂM SOÁT NHỊP ĐỘ",
        "desc": "Chuyền bóng, Rê bóng, Bình tĩnh, Đoán bóng",
        "skills": ["passing", "marking", "pace"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "⏱️"
    },
    "pos_4": {
        "id": "pos_4",
        "category": "possession",
        "category_name": "KIỂM SOÁT BÓNG",
        "name": "PHỐI HỢP TAM GIÁC",
        "desc": "Chuyền bóng, Rê bóng, Tốc độ, Sáng tạo",
        "skills": ["passing", "pace", "finishing"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.0,
        "icon": "🔺"
    },

    # 4. THỂ LỰC VÀ TÂM LÝ (FITNESS & MENTAL)
    "fit_1": {
        "id": "fit_1",
        "category": "fitness",
        "category_name": "THỂ LỰC VÀ TÂM LÝ",
        "name": "KHỞI ĐỘNG LÀM NÓNG",
        "desc": "Thể lực, Nhanh nhẹn, Linh hoạt",
        "skills": ["stamina_skill", "pace"],
        "difficulty": "RẤT DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.0,
        "icon": "🔥"
    },
    "fit_2": {
        "id": "fit_2",
        "category": "fitness",
        "category_name": "THỂ LỰC VÀ TÂM LÝ",
        "name": "CHẠY BIẾN TỐC CƯỜNG ĐỘ CAO",
        "desc": "Tốc độ, Sức mạnh, Thể lực",
        "skills": ["pace", "stamina_skill"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.25,
        "icon": "⚡"
    },
    "fit_3": {
        "id": "fit_3",
        "category": "fitness",
        "category_name": "THỂ LỰC VÀ TÂM LÝ",
        "name": "TẬP TẠ TĂNG SỨC MẠNH",
        "desc": "Sức mạnh, Tranh chấp, Thể lực",
        "skills": ["stamina_skill", "tackling"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.0,
        "icon": "🏋️"
    },
    "fit_4": {
        "id": "fit_4",
        "category": "fitness",
        "category_name": "THỂ LỰC VÀ TÂM LÝ",
        "name": "KÉO GIÃN CƠ & YOGA",
        "desc": "Thể lực, Tinh thần, Bền bỉ",
        "skills": ["stamina_skill", "passing"],
        "difficulty": "DỄ",
        "difficulty_key": "easy",
        "whistles": 1,
        "fatigue": 1.2,
        "icon": "🧘"
    },

    # 5. CHƠI ĐỒNG ĐỘI (TEAM PLAY)
    "team_1": {
        "id": "team_1",
        "category": "teamplay",
        "category_name": "CHƠI ĐỒNG ĐỘI",
        "name": "ĐÁ TẬP NỘI BỘ 11 VS 11",
        "desc": "Toàn diện tất cả chỉ số, Phối hợp đội",
        "skills": ["finishing", "passing", "tackling", "marking", "pace", "stamina_skill"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.5,
        "icon": "👥"
    },
    "team_2": {
        "id": "team_2",
        "category": "teamplay",
        "category_name": "CHƠI ĐỒNG ĐỘI",
        "name": "TÌNH HUỐNG CỐ ĐỊNH PHỐI HỢP",
        "desc": "Tạt cánh, Đánh đầu, Dứt điểm, Đoán bóng",
        "skills": ["finishing", "passing", "marking"],
        "difficulty": "VỪA PHẢI",
        "difficulty_key": "medium",
        "whistles": 2,
        "fatigue": 2.5,
        "icon": "⛳"
    },
    "team_3": {
        "id": "team_3",
        "category": "teamplay",
        "category_name": "CHƠI ĐỒNG ĐỘI",
        "name": "ÁP SÁT TẦM CAO (GEGENPRESS)",
        "desc": "Thể lực, Tắc bóng, Kèm người, Tốc độ",
        "skills": ["tackling", "marking", "pace", "stamina_skill"],
        "difficulty": "KHÓ",
        "difficulty_key": "hard",
        "whistles": 3,
        "fatigue": 3.0,
        "icon": "🌪️"
    },
}

TRAINING_CATEGORIES = [
    {"key": "attack", "name": "TẤN CÔNG", "badge": ""},
    {"key": "defense", "name": "PHÒNG NGỰ", "badge": ""},
    {"key": "possession", "name": "KIỂM SOÁT BÓNG", "badge": ""},
    {"key": "fitness", "name": "THỂ LỰC VÀ TÂM LÝ", "badge": ""},
    {"key": "teamplay", "name": "CHƠI ĐỒNG ĐỘI (3)", "badge": "!"},
]


@app.route("/training")
def training():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    season_day = club["season_day"]

    # Slot map to see who is starting
    formation = club["formation"]
    slots = db_all(conn, "SELECT slot_index, player_id, position FROM lineup_slot WHERE formation=?", (formation,))
    starting_ids = {s["player_id"]: s["position"] for s in slots if s["player_id"]}

    raw_players = db_all(conn, "SELECT * FROM squad_player ORDER BY position, id")
    players_view = []
    total_cond = 0
    healthy_count = 0
    injured_count = 0

    for p in raw_players:
        pid = p["id"]
        is_inj = (p["injured_until_day"] or 0) > season_day
        if is_inj:
            injured_count += 1
        elif p["condition"]>=20:
            healthy_count += 1

        cond = p["condition"]
        total_cond += cond
        ovr = player_overall(p)
        cltt_val = round(ovr, 1)

        assigned_pos = starting_ids.get(pid, "")
        bench_slot = p["bench_slot"] if "bench_slot" in p.keys() and p["bench_slot"] else 0

        players_view.append({
            "id": pid,
            "name": p["name"],
            "short_name": short_player_name(p["name"]),
            "shirt_number": p["shirt_number"] if "shirt_number" in p.keys() and p["shirt_number"] else pid,
            "position": p["position"],
            "assigned_pos": assigned_pos,
            "is_starter": bool(assigned_pos),
            "bench_slot": bench_slot,
            "condition": cond,
            "morale": p["morale"],
            "form": p["form"],
            "cltt": cltt_val,
            "stars": cltt_to_stars(cltt_val),
            "is_injured": is_inj,
            "can_train": not is_inj and cond>=20,
            "injured_days_left": max(0, (p["injured_until_day"] or 0) - season_day) if is_inj else 0,
        })

    avg_team_cond = int(round(total_cond / max(1, len(raw_players))))
    training_pts = club["training_points_today"] or 0
    training_pts = max(0, min(40, training_pts))

    # Drills grouped by category
    drills_by_cat = {}
    for cat in TRAINING_CATEGORIES:
        drills_by_cat[cat["key"]] = [d for d in DRILLS_CATALOG.values() if d["category"] == cat["key"]]

    ctx = load_context(conn)
    eff = facility_effects(conn)
    clock=rules.game_clock(conn,now_ts())
    recovery_seconds=min((max(0, math.ceil(rules.RECOVERY_INTERVAL-(clock-p["condition_clock"]))) for p in raw_players if p["condition"]<100),default=0)
    conn.close()

    from flask import session as fl_session
    last_report = fl_session.get("last_training_report", None)

    return render_template(
        "training.html",
        **ctx,
        players=players_view,
        healthy_count=healthy_count,
        injured_count=injured_count,
        avg_team_cond=avg_team_cond,
        training_pts=training_pts,
        drills_catalog=DRILLS_CATALOG,
        drills_by_cat=drills_by_cat,
        categories=TRAINING_CATEGORIES,
        effects=eff,
        last_report=last_report,
        recovery_seconds=recovery_seconds,
    )


@app.post("/training")
def do_training():
    from flask import session as fl_session
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    season_day = club["season_day"]

    eff = facility_effects(conn)
    gain_mult = eff["train_gain_mult"]
    injury_mult = eff["train_injury_mult"]

    is_json = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    data = request.get_json(silent=True) or request.form
    if not hasattr(data,"get"):
        abort(400,description="Dữ liệu bài tập không hợp lệ.")

    quick_mode = data.get("quick_mode", "")
    if not isinstance(quick_mode,str):
        abort(400)
    quick_mode=quick_mode.strip()
    selected_drill_ids = data.getlist("drill_ids") if hasattr(data, "getlist") else data.get("drill_ids", [])
    if isinstance(selected_drill_ids, str):
        selected_drill_ids = [selected_drill_ids]
    if not isinstance(selected_drill_ids,list) or not all(isinstance(value,str) for value in selected_drill_ids):
        abort(400,description="Danh sách bài tập không hợp lệ.")
    selected_drill_ids = [part.strip() for value in selected_drill_ids for part in value.split(",") if part.strip()]

    selected_player_ids = data.getlist("player_ids") if hasattr(data, "getlist") else data.get("player_ids", [])
    if isinstance(selected_player_ids, str):
        selected_player_ids = [selected_player_ids]
    if not isinstance(selected_player_ids,list) or not all(isinstance(value,(str,int)) for value in selected_player_ids):
        abort(400,description="Danh sách cầu thủ không hợp lệ.")
    parts = [part.strip() for value in selected_player_ids for part in str(value).split(",") if part.strip()]
    if not all(part.isdigit() for part in parts):
        abort(400,description="Mã cầu thủ không hợp lệ.")
    selected_player_ids = [int(part) for part in parts]

    # If Quick Training is clicked
    if quick_mode:
        if quick_mode == "easy":
            selected_drill_ids = ["att_1", "def_1", "pos_1"]  # 3 bài Dễ
        elif quick_mode == "hard":
            selected_drill_ids = ["att_5", "def_5", "pos_4", "fit_3", "team_1", "att_4"]  # 6 bài Nặng
        else:  # medium / normal
            selected_drill_ids = ["att_3", "def_2", "pos_2", "fit_2"]  # 4 bài Vừa phải

    # Fallback to defaults if empty
    if not selected_drill_ids:
        focus = data.get("focus", "attack")
        intensity = data.get("intensity", "normal")
        if focus == "attack":
            selected_drill_ids = ["att_1", "att_2", "att_4"]
        elif focus == "defense":
            selected_drill_ids = ["def_1", "def_2", "def_3"]
        elif focus == "fitness":
            selected_drill_ids = ["fit_1", "fit_2", "fit_4"]
        else:
            selected_drill_ids = ["pos_1", "pos_2", "pos_3"]

    if not isinstance(selected_drill_ids,list) or not all(isinstance(d,str) for d in selected_drill_ids):
        abort(400,description="Danh sách bài tập không hợp lệ.")
    # Gather selected drill objects (up to 6)
    selected_drills = []
    total_fatigue = 0.0
    for did in list(dict.fromkeys(selected_drill_ids))[:6]:
        if did in DRILLS_CATALOG:
            dobj = DRILLS_CATALOG[did]
            selected_drills.append(dobj)
            total_fatigue += dobj["fatigue"]

    if not selected_drills:
        conn.close()
        return {"ok": False, "message": "Hãy chọn ít nhất một bài tập hợp lệ."}, 400
    total_fatigue = max(3, round(total_fatigue * 2, 2))

    # All owned players
    all_players = db_all(conn, "SELECT * FROM squad_player")
    owned_dict = {p["id"]: p for p in all_players}

    # If player_ids empty, default to all healthy players
    if not selected_player_ids:
        selected_player_ids = [p["id"] for p in all_players if (p["injured_until_day"] or 0) <= season_day]

    trained_count = 0
    injuries_count = 0
    progress_details = []

    for pid in dict.fromkeys(selected_player_ids):
        p = owned_dict.get(pid)
        if not p or (p["injured_until_day"] or 0) > season_day:
            continue

        mods = trait_effect_mods(parse_traits(p["traits"]))
        actual_fatigue = max(1, round(total_fatigue * mods["fatigue"]))
        skills = {skill for drill in selected_drills for skill in drill["skills"]}
        result = rules.train_player(conn, p, skills, actual_fatigue, gain_mult * mods["gain"])
        if result is None:
            continue
        new_cond = result["new_condition"]
        got_injured = new_cond < 70 and actual_fatigue > 12 and random.random() < .003 * injury_mult
        if got_injured:
            injuries_count += 1
            conn.execute("UPDATE squad_player SET injured_until_day=? WHERE id=?", (season_day+2,pid))
        log_player_state(conn, pid, season_day, note="training")
        trained_count += 1
        progress_details.append({
            "name": p["name"],
            "fatigue_loss": actual_fatigue,
            "new_condition": new_cond,
            "got_injured": got_injured,
        })

    # Increment daily training progress gauge
    current_pts = club["training_points_today"] or 0
    new_pts = min(40, current_pts + (max(2, len(selected_drills)) if trained_count else 0))
    db_exec(conn, "UPDATE club SET training_points_today=? WHERE id=1", (new_pts,))

    # Save session report for the "BÁO CÁO" button
    report_data = {
        "trained_count": trained_count,
        "drills_count": len(selected_drills),
        "total_fatigue": total_fatigue,
        "injuries_count": injuries_count,
        "drills_names": [d["name"] for d in selected_drills],
        "new_pts": new_pts,
    }
    fl_session["last_training_report"] = report_data
    rules.event(conn, "training", f"{trained_count} cầu thủ; {len(selected_drills)} bài; tối đa {rules.MAX_DAILY_SKILL_GAIN} điểm kỹ năng/ngày")
    conn.close()
    msg = f"Đã hoàn thành buổi tập ({len(selected_drills)} bài) cho {trained_count} cầu thủ. Tiêu hao ~{total_fatigue}% thể lực."
    if injuries_count > 0:
        msg += f" (Chấn thương nhẹ: {injuries_count} cầu thủ)."
    if not trained_count:
        msg="Chưa thể tập: cần ít nhất 20% thể lực và đủ thể lực cho bài đã chọn; cầu thủ có thể đã đạt giới hạn phát triển trong ngày hoặc kỹ năng đã đạt 200%."
    flash(msg)

    if is_json:
        return {
            "ok": True,
            "message": msg,
            "report": report_data,
        }

    return redirect(url_for("training"))


@app.post("/training/heal")
def training_heal():
    return use_rest_pack("squad_player", "training")


@app.post("/youth/recover/<int:yid>")
def youth_recover(yid):
    return use_rest_pack("youth_player", "youth", yid)


def use_rest_pack(table, target, pid=None):
    conn=get_db()
    if pid is None:
        raw=request.form.get("player_id", "")
        pid=int(raw) if raw.isdigit() else None
    if request.form.get("heal_all")=="1" and table=="squad_player":
        players=db_all(conn,"SELECT id FROM squad_player WHERE condition<100")
    else:
        players=db_all(conn,f"SELECT id FROM {table} WHERE id=? AND condition<100",(pid,))
    if not players:
        flash("Không có cầu thủ cần hồi thể lực; thẻ của bạn được giữ nguyên.")
        return redirect(url_for(target))
    cost=len(players)  # One card per player, including the whole-team action.
    available=db_one(conn,"SELECT rest_packs FROM club")[0]
    if available<cost:
        flash(f"Cần {cost} thẻ thể lực: mỗi cầu thủ 1 thẻ để hồi 10%.")
        return redirect(url_for(target))
    clock=rules.game_clock(conn,now_ts())
    for player in players:
        conn.execute(f"UPDATE {table} SET condition=MIN(100,condition+?),condition_clock=CASE WHEN condition+?>=100 THEN ? ELSE condition_clock END WHERE id=?",
                     (rules.REST_PACK_RECOVERY,rules.REST_PACK_RECOVERY,clock,player["id"]))
    conn.execute("UPDATE club SET rest_packs=rest_packs-?",(cost,))
    rules.event(conn,"rest_pack",f"{cost} thẻ; hồi 10% cho {len(players)} cầu thủ")
    conn.close()
    flash(f"Đã dùng {cost} thẻ; mỗi cầu thủ hồi 10% thể lực (tối đa 100%).")
    return redirect(url_for(target))


@app.post("/training/morale/<int:pid>")
def training_morale(pid):
    conn=get_db()
    p=db_one(conn,"SELECT * FROM squad_player WHERE id=?",(pid,))
    if p is None:
        abort(404)
    if p["morale"]>=100:
        flash("Tinh thần đã đầy; không dùng thẻ.")
    elif db_one(conn,"SELECT morale_packs FROM club")[0]<1:
        flash("Không đủ thẻ tinh thần.")
    else:
        conn.execute("UPDATE club SET morale_packs=morale_packs-1")
        conn.execute("UPDATE squad_player SET morale=MIN(100,morale+10) WHERE id=?",(pid,))
        rules.event(conn,"morale_pack",f"{p['name']}: +10 tinh thần")
        flash("Đã dùng 1 thẻ hồi 10% tinh thần.")
    conn.close()
    return redirect(url_for("player_detail",pid=pid))


@app.post("/training/cure_injury/<int:pid>")
def training_cure_injury(pid):
    conn=get_db()
    p=db_one(conn,"SELECT * FROM squad_player WHERE id=?",(pid,))
    if p is None:
        abort(404)
    club=db_one(conn,"SELECT * FROM club")
    remaining=max(0,p["injured_until_day"]-club["season_day"])
    cost=math.ceil(remaining/3)
    if not cost:
        flash("Cầu thủ không bị chấn thương; thẻ của bạn được giữ nguyên.")
    elif club["injury_packs"]<cost:
        flash(f"Cần {cost} thẻ y tế (1 thẻ cho mỗi tối đa 3 ngày chấn thương).")
    else:
        conn.execute("UPDATE squad_player SET injured_until_day=0 WHERE id=?",(pid,))
        conn.execute("UPDATE club SET injury_packs=injury_packs-?",(cost,))
        rules.event(conn,"medical",f"Chữa trị {p['name']}; {cost} thẻ")
        flash(f"Đã dùng {cost} thẻ y tế chữa chấn thương; thể lực không tăng miễn phí.")
    conn.close()
    return redirect(url_for("training"))

FACILITY_BENEFITS = {
    "stadium": {
        "title": "Sân Vận Động Chính",
        "icon": "🏟️",
        "desc": "Mở rộng sức chứa khán đài chỗ ngồi, đón nhiều CĐV hơn và tăng mạnh tiền bán vé mỗi trận.",
        "pos_class": "fac-pin-stadium",
        "pct_display": 50,
    },
    "parking": {
        "title": "Bãi Đỗ Xe",
        "icon": "🚗",
        "desc": "Chứa được nhiều ô tô & xe máy của CĐV hơn, tăng mạnh doanh thu đỗ xe ngày thi đấu.",
        "pos_class": "fac-pin-parking",
        "pct_display": 90,
    },
    "food_court": {
        "title": "Khu Ẩm Thực",
        "icon": "🍔",
        "desc": "Bán thức ăn, nước uống và dịch vụ ăn uống cho CĐV đến sân, tăng doanh thu thương mại.",
        "pos_class": "fac-pin-food",
        "pct_display": 90,
    },
    "youth_academy": {
        "title": "Cơ Sở Đào Tạo Trẻ",
        "icon": "🎓",
        "desc": "Nâng cao chất lượng khởi điểm của lứa 11 cầu thủ trẻ U18 mỗi mùa giải; mọi cầu thủ đều có thể tập lên 200%.",
        "pos_class": "fac-pin-youth",
        "pct_display": 90,
    },
    "training_ground": {
        "title": "T.Tâm Huấn Luyện",
        "icon": "⚡",
        "desc": "Gia tăng tốc độ tập luyện và định kỳ tự động cấp Thẻ Hồi Thể Lực (Rest Packs ⚡).",
        "pos_class": "fac-pin-training",
        "pct_display": 90,
    },
    "medical_center": {
        "title": "Khu Trị Thương & Bệnh Viện",
        "icon": "🏥",
        "desc": "Giúp cầu thủ hồi phục chấn thương nhanh và định kỳ nhận Thẻ Y Tế chữa chấn thương (➕).",
        "pos_class": "fac-pin-medical",
        "pct_display": 90,
    },
    "fan_club": {
        "title": "Fan CLB & Store",
        "icon": "🚩",
        "desc": "Thu hút hội CĐV và bán vật phẩm lưu niệm, tăng tiền tài trợ và bản quyền hình ảnh.",
        "pos_class": "fac-pin-fan",
        "pct_display": 90,
    },
}

def upgrade_cost(name: str, level: int) -> int:
    base = {
        "stadium": 350_000,
        "parking": 150_000,
        "food_court": 180_000,
        "youth_academy": 250_000,
        "training_ground": 220_000,
        "medical_center": 200_000,
        "fan_club": 120_000,
    }.get(name, 200_000)
    return int(base * (1.55 ** max(0,level - 2)))

def upgrade_time_seconds(name: str, level: int) -> int:
    return int(600 * max(1,level-1) ** 1.5)

@app.route("/facilities")
def facilities():
    conn = get_db()
    finalize_builds(conn)
    ensure_all_facilities(conn)
    facs = db_all(conn, "SELECT * FROM facility")
    facs_dict = {f["name"]: dict(f) for f in facs}
    facilities_view = []
    ts = int(rules.game_clock(conn,now_ts()))

    for item in ALL_FACILITIES:
        name = item[0]
        meta = FACILITY_BENEFITS.get(name, {})
        f = facs_dict.get(name, {"level": 1, "build_complete_at": None})
        lvl = f["level"]
        next_lvl = lvl + 1
        cost = upgrade_cost(name, next_lvl)
        tsec = upgrade_time_seconds(name, next_lvl)
        remaining = 0
        if f.get("build_complete_at") is not None and ts < f["build_complete_at"]:
            remaining = f["build_complete_at"] - ts

        facilities_view.append({
            "name": name,
            "level": lvl,
            "title": meta.get("title", name),
            "icon": meta.get("icon", "🏟️"),
            "desc": meta.get("desc", ""),
            "pos_class": meta.get("pos_class", "fac-pin-stadium"),
            "pct_display": meta.get("pct_display", 90),
            "benefit_current": rules.facility_description(name,lvl),
            "benefit_next": rules.facility_description(name,min(10,lvl+1)),
            "next_level": next_lvl,
            "next_cost": cost,
            "next_time": tsec,
            "remaining": remaining,
            "token_instant_cost": max(1, math.ceil(remaining/300)) if remaining else 0,
        })

    ctx = load_context(conn)
    eff = facility_effects(conn)
    conn.close()
    return render_template("facilities.html", **ctx, facilities=facilities_view, effects=eff, chapter=ctx["club"]["club_level"], hourly_rate=round(eff["fan_revenue_daily"]/24))

@app.post("/upgrade/<name>")
def upgrade(name: str):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    fac = db_one(conn, "SELECT * FROM facility WHERE name=?", (name,))
    if fac is None:
        conn.close()
        flash("Không tìm thấy công trình.")
        return redirect(url_for("facilities"))
    if fac["build_complete_at"] is not None and rules.game_clock(conn,now_ts()) < fac["build_complete_at"]:
        conn.close()
        flash("Công trình đang nâng cấp, chưa xong.")
        return redirect(url_for("facilities"))

    if fac["level"] >= min(rules.MAX_FACILITY_LEVEL, club["club_level"]+2):
        flash("Công trình đạt giới hạn theo cấp CLB (tối đa cấp CLB + 2, trần cấp 10).")
        return redirect(url_for("facilities"))
    next_level = fac["level"] + 1
    cost = upgrade_cost(name, next_level)
    if club["cash"] < cost:
        conn.close()
        flash("Không đủ tiền mặt để nâng cấp.")
        return redirect(url_for("facilities"))

    done_at = int(rules.game_clock(conn,now_ts())) + upgrade_time_seconds(name, next_level)
    db_exec(conn, "UPDATE club SET cash=cash-? WHERE id=1", (cost,))
    db_exec(conn, "UPDATE facility SET pending_level=?, build_complete_at=? WHERE name=?", (next_level, done_at, name))
    conn.close()
    flash(f"🏗️ Đã bắt đầu nâng cấp {name} lên Cấp {next_level}! Thời gian thi công: {upgrade_time_seconds(name, next_level)}s.")
    return redirect(url_for("facilities"))

@app.post("/upgrade_instant/<name>")
def upgrade_instant(name: str):
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    fac = db_one(conn, "SELECT * FROM facility WHERE name=?", (name,))
    if fac is None:
        conn.close()
        flash("Không tìm thấy công trình.")
        return redirect(url_for("facilities"))

    if fac["build_complete_at"] is None or fac["pending_level"] is None:
        flash("Cần bắt đầu và thanh toán nâng cấp bằng tiền mặt trước.")
        return redirect(url_for("facilities"))
    remaining = max(0,fac["build_complete_at"]-rules.game_clock(conn,now_ts()))
    token_cost = math.ceil(remaining/300)
    if club["tokens"] < token_cost:
        flash(f"Cần {token_cost} Token để hoàn thành sớm.")
        return redirect(url_for("facilities"))
    conn.execute("UPDATE club SET tokens=tokens-?",(token_cost,))
    conn.execute("UPDATE facility SET level=pending_level,pending_level=NULL,build_complete_at=NULL WHERE name=?",(name,))
    rules.event(conn,"construction_complete",name,tokens=-token_cost)
    conn.close()
    flash("Đã hoàn tất nâng cấp và áp dụng quyền lợi mới.")
    return redirect(url_for("facilities"))


@app.route("/youth")
def youth():
    conn = get_db()
    finalize_builds(conn)
    ensure_youth_table(conn)
    ensure_all_facilities(conn)
    youth_players = get_or_regen_youth_squad(conn)
    ctx = load_context(conn)
    eff = facility_effects(conn)
    youth_fac = db_one(conn, "SELECT level FROM facility WHERE name='youth_academy'")
    youth_lvl = youth_fac["level"] if youth_fac else 1
    conn.close()
    return render_template("youth.html", **ctx, youth_players=youth_players, youth_level=youth_lvl, effects=eff)

@app.post("/youth/sign/<int:yid>")
def youth_sign(yid: int):
    conn = get_db()
    if not can_recruit(conn):
        return redirect(url_for("market"))

    ensure_youth_table(conn)
    yp = db_one(conn, "SELECT * FROM youth_player WHERE id=? AND is_signed=0", (yid,))
    if not yp:
        conn.close()
        flash("Không tìm thấy cầu thủ trẻ này hoặc đã ký hợp đồng rồi.")
        return redirect(url_for("youth"))

    signing_fee=int(rules.player_value(yp)*.65)
    if db_one(conn,"SELECT cash FROM club")[0]<signing_fee:
        flash(f"Cần {fmt_money(signing_fee)} tiền phí ký hợp đồng học viện.")
        return redirect(url_for("youth"))
    conn.execute("UPDATE club SET cash=cash-?",(signing_fee,))
    rules.event(conn,"youth_sign",yp["name"],cash=-signing_fee)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    s_day = int(club["season_day"])

    traits = gen_traits(yp["position"], yp["finishing"], yp["passing"], yp["pace"], yp["tackling"], yp["marking"], yp["stamina_skill"])
    salary = int(1_000 + player_overall(yp) * 90)
    pid = db_insert(conn, """
        INSERT INTO squad_player
        (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill,
         traits, potential, condition, morale, form, injured_until_day, salary,
         height, weight, preferred_foot, special_skill, nationality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 95, 90, 80, 0, ?, ?, ?, ?, ?, ?)
    """, (yp["name"], yp["position"], yp["age"], yp["finishing"], yp["passing"], yp["pace"],
          yp["tackling"], yp["marking"], yp["stamina_skill"], traits_to_str(traits),
          yp["potential"], salary, yp["height"], yp["weight"], yp["preferred_foot"],
          yp["special_skill"], yp["nationality"]))

    db_exec(conn, "UPDATE youth_player SET is_signed=1 WHERE id=?", (yid,))
    conn.execute("UPDATE squad_player SET condition=?, retirement_age=?, condition_clock=?, experience_matches=?,seasons_played=? WHERE id=?",
        (yp["condition"],yp["retirement_age"],yp["condition_clock"],yp["experience_matches"],yp["seasons_played"],pid))
    log_player_state(conn, pid, s_day, note="youth_graduate")
    conn.close()
    flash(f"🎉 Ký hợp đồng thành công! Cầu thủ trẻ {yp['name']} ({yp['position']}) đã chính thức gia nhập Đội 1!")
    return redirect(url_for("youth"))

@app.post("/youth/sign_all")
def youth_sign_all():
    conn = get_db()
    ensure_youth_table(conn)
    unsigned = db_all(conn, "SELECT * FROM youth_player WHERE is_signed=0")
    if not unsigned:
        conn.close()
        flash("Không có cầu thủ trẻ nào sẵn sàng để ký hợp đồng.")
        return redirect(url_for("youth"))

    if not can_recruit(conn,len(unsigned)):
        return redirect(url_for("youth"))
    signing_fee=sum(int(rules.player_value(p)*.65) for p in unsigned)
    if db_one(conn,"SELECT cash FROM club")[0]<signing_fee:
        flash(f"Cần {fmt_money(signing_fee)} tiền phí ký hợp đồng toàn bộ lứa trẻ.")
        return redirect(url_for("youth"))
    conn.execute("UPDATE club SET cash=cash-?",(signing_fee,))
    rules.event(conn,"youth_sign_all",f"{len(unsigned)} cầu thủ",cash=-signing_fee)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    s_day = int(club["season_day"])
    count = 0
    for yp in unsigned:
        traits = gen_traits(yp["position"], yp["finishing"], yp["passing"], yp["pace"], yp["tackling"], yp["marking"], yp["stamina_skill"])
        salary = int(1_000 + player_overall(yp) * 90)
        pid = db_insert(conn, """
            INSERT INTO squad_player
            (name, position, age, finishing, passing, pace, tackling, marking, stamina_skill,
             traits, potential, condition, morale, form, injured_until_day, salary,
             height, weight, preferred_foot, special_skill, nationality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 95, 90, 80, 0, ?, ?, ?, ?, ?, ?)
        """, (yp["name"], yp["position"], yp["age"], yp["finishing"], yp["passing"], yp["pace"],
              yp["tackling"], yp["marking"], yp["stamina_skill"], traits_to_str(traits),
              yp["potential"], salary, yp["height"], yp["weight"], yp["preferred_foot"],
              yp["special_skill"], yp["nationality"]))
        db_exec(conn, "UPDATE youth_player SET is_signed=1 WHERE id=?", (yp["id"],))
        conn.execute("UPDATE squad_player SET condition=?, retirement_age=?, condition_clock=? WHERE id=?",(yp["condition"],yp["retirement_age"],yp["condition_clock"],pid))
        log_player_state(conn, pid, s_day, note="youth_graduate")
        count += 1

    conn.close()
    flash(f"🎉 Đã ký hợp đồng và đôn toàn bộ {count} cầu thủ trẻ U18 lên Đội 1 thành công!")
    return redirect(url_for("youth"))

@app.post("/youth/train/<int:yid>")
def youth_train(yid: int):
    return individual_training("youth", yid)


@app.post("/player/individual_train/<int:pid>")
def player_individual_train(pid: int):
    return individual_training("senior", pid)


def individual_training(source, pid):
    conn = get_db()
    table = "youth_player" if source == "youth" else "squad_player"
    target = "youth" if source == "youth" else "training"
    p = db_one(conn, f"SELECT * FROM {table} WHERE id=?" + (" AND is_signed=0" if source=="youth" else ""), (pid,))
    if p is None:
        abort(404)
    club = db_one(conn,"SELECT * FROM club")
    try:
        desired = float(request.form.get("target_cltt", player_overall(p)+.2))
    except (ValueError, TypeError):
        abort(400, description="Mục tiêu huấn luyện không hợp lệ.")
    if not math.isfinite(desired):
        abort(400, description="Mục tiêu huấn luyện không hợp lệ.")
    if desired <= player_overall(p) or (source=="senior" and p["injured_until_day"]>club["season_day"]):
        flash("Cầu thủ chưa thể tập luyện hoặc mục tiêu không cao hơn chỉ số hiện tại.")
        return redirect(url_for(target))
    cost = 2
    if club["tokens"] < cost:
        flash("Cần 2 Token cho một buổi tập cá nhân.")
        return redirect(url_for(target))
    before = player_overall(p)
    result = rules.train_player(conn,p,rules.SKILLS,12,facility_effects(conn)["train_gain_mult"],source=source)
    if result is None:
        flash("Đã đạt giới hạn phát triển trong ngày, kỹ năng đã đạt 200% hoặc thể lực không đủ để tập (tối thiểu 20%).")
    else:
        conn.execute("UPDATE club SET tokens=tokens-?", (cost,))
        updated = db_one(conn,f"SELECT * FROM {table} WHERE id=?",(pid,))
        current = player_overall(updated)
        if source=="youth":
            conn.execute("UPDATE youth_player SET overall=?,trained_ovr=trained_ovr+? WHERE id=?",(current,current-before,pid))
        else:
            log_player_state(conn,pid,club["season_day"],note="individual_training")
        rules.event(conn,"individual_training",f"{p['name']}: {before:.2f} → {current:.2f}",tokens=-cost)
        flash(f"Đã tập cá nhân cho {p['name']}: CLTT {before:.2f} → {current:.2f}; 2 Token. Mọi cầu thủ đều có thể tập lên 200%; tốc độ phụ thuộc tuổi và chỉ số hiện tại.")
    conn.close()
    return redirect(url_for(target))


def finalize_builds(conn):
    conn.execute("""UPDATE facility SET level=COALESCE(pending_level,level),pending_level=NULL,build_complete_at=NULL
        WHERE build_complete_at IS NOT NULL AND build_complete_at<=?""", (rules.game_clock(conn,now_ts()),))
    conn.commit()

def winner_of_fixture(conn, fix):
    if fix["winner"]:
        return fix["winner"]
    if fix["home_goals"] != fix["away_goals"]:
        return fix["home"] if fix["home_goals"] > fix["away_goals"] else fix["away"]
    home = db_one(conn,"SELECT base_strength FROM team WHERE name=?",(fix["home"],))[0]
    away = db_one(conn,"SELECT base_strength FROM team WHERE name=?",(fix["away"],))[0]
    return random.choices([fix["home"],fix["away"]],weights=[home,away])[0]


def record_fixture_player_stats(conn, fid, comp, home, away, hg, ag, user_team, user_xi=None):
    if db_one(conn, "SELECT 1 FROM player_match_stat WHERE fixture_id=?", (fid,)):
        return
    for team, goals, conceded in ((home, hg, ag), (away, ag, hg)):
        is_user = team == user_team
        if is_user:
            roster = [dict(p) for p in (user_xi or [])]
        else:
            roster = [dict(p) for p in db_all(conn, "SELECT * FROM ai_player WHERE team_name=? ORDER BY shirt_number", (team,))]
        if not roster:
            continue
        scores = [0]*len(roster)
        assists = [0]*len(roster)
        for _ in range(goals):
            weights = [6 if p["position"] == "ST" else 3 if "M" in p["position"] else .2 if p["position"] == "GK" else 1 for p in roster]
            scorer = random.choices(range(len(roster)), weights=weights)[0]
            scores[scorer] += 1
            if len(roster)>1 and random.random()<.7:
                assister = random.choice([i for i in range(len(roster)) if i != scorer])
                assists[assister] += 1
        ratings = [round(clamp(6.2 + (goals-conceded)*.18 + scores[i]*.7 + assists[i]*.35 + random.uniform(-.5,.5), 3, 10), 2) for i in range(len(roster))]
        best = max(range(len(roster)), key=lambda i:ratings[i])
        for i,p in enumerate(roster):
            conn.execute("""INSERT INTO player_match_stat
                (fixture_id,competition,team_name,player_name,is_user_player,goals,assists,rating,player_id,appearances,yellow_cards,motm)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""".replace('+',''),
                (fid,comp,team,p["name"],int(is_user),scores[i],assists[i],ratings[i],p["id"] if is_user else None,
                 int(random.random()<.08),int(i==best and goals>=conceded)))
    conn.execute("UPDATE player_match_stat SET motm=0 WHERE fixture_id=?",(fid,))
    best=db_one(conn,"SELECT id FROM player_match_stat WHERE fixture_id=? ORDER BY rating DESC,goals DESC,id LIMIT 1",(fid,))
    if best:
        conn.execute("UPDATE player_match_stat SET motm=1 WHERE id=?",(best["id"],))
    if user_xi:
        for player in user_xi:
            conn.execute("UPDATE squad_player SET experience_matches=experience_matches+1 WHERE id=?",(player["id"],))
    conn.commit()

def schedule_fixture(conn, comp, round_no, home, away, day, leg, tie_id, label):
    while db_one(conn, "SELECT 1 FROM fixture WHERE ABS(day-?)<3 AND (home IN (?,?) OR away IN (?,?)) LIMIT 1",
                 (day, home, away, home, away)):
        day += 1
    db_exec(conn, """INSERT INTO fixture(competition,day,round,home,away,leg,tie_id,group_name)
        VALUES (?,?,?,?,?,?,?,?)""",(comp,day,round_no,home,away,leg,tie_id,label))
    db_exec(conn,"UPDATE club SET season_length=MAX(season_length,?)",(day+7,))


def resolve_tie(conn, fixtures):
    if not fixtures or any(not f["played"] for f in fixtures):
        return None
    if fixtures[-1]["winner"]:
        return fixtures[-1]["winner"]
    first = fixtures[0]
    totals = {first["home"]: 0, first["away"]: 0}
    for f in fixtures:
        totals[f["home"]] += f["home_goals"]
        totals[f["away"]] += f["away_goals"]
    if len(set(totals.values()))>1:
        winner=max(totals,key=totals.get)
    else:
        teams=list(totals)
        strengths=[db_one(conn,"SELECT base_strength FROM team WHERE name=?",(team,))[0] for team in teams]
        winner=random.choices(teams,weights=strengths)[0]
    if first["tie_id"]:
        db_exec(conn,"UPDATE fixture SET winner=?, aggregate_score=? WHERE competition=? AND tie_id=?",
                (winner,f"{totals[first['home']]} - {totals[first['away']]} (agg)",first["competition"],first["tie_id"]))
    else:
        db_exec(conn,"UPDATE fixture SET winner=? WHERE id=?",(winner,first["id"]))
    return winner


def progress_elimination(conn, comp):
    minimum = 1 if comp=="ApexCup" else 7
    rounds = [r[0] for r in db_all(conn,"SELECT DISTINCT round FROM fixture WHERE competition=? AND round>=? ORDER BY round",(comp,minimum))]
    for round_no in rounds:
        fixtures = db_all(conn,"SELECT * FROM fixture WHERE competition=? AND round=? ORDER BY tie_id,leg,id",(comp,round_no))
        ties = {}
        for f in fixtures:
            ties.setdefault(f["tie_id"] or str(f["id"]),[]).append(f)
        winners = [resolve_tie(conn,fs) for fs in ties.values()]
        if not winners or any(w is None for w in winners) or len(winners)==1:
            continue
        next_round = round_no+1
        if next_round in rounds:
            continue
        if comp=="ApexCup":
            days = rules.CUP_DAYS[next_round]
        else:
            days = rules.INTERNATIONAL_DAYS[next_round]
            if len(winners)==2:
                days=days[:1]
        last_day = max(f["day"] for f in fixtures)
        for index,(home,away) in enumerate(zip(winners[::2],winners[1::2])):
            tie_id = f"{comp}_R{next_round}_{index}"
            label = "Chung kết" if len(winners)==2 else "Bán kết" if len(winners)==4 else "Tứ kết" if len(winners)==8 else "Vòng 16" if len(winners)==16 else "Vòng 32"
            schedule_fixture(conn,comp,next_round,home,away,max(days[0],last_day+3),1,tie_id,label)
            if len(days)>1:
                schedule_fixture(conn,comp,next_round,away,home,max(days[1],last_day+6),2,tie_id,label)


def progress_cup_knockout_2legged(conn):
    progress_elimination(conn,"ApexCup")


def progress_champions_knockout(conn, comp="Champions"):
    groups = db_all(conn,"SELECT * FROM fixture WHERE competition=? AND round<=6",(comp,))
    if not groups or any(not f["played"] for f in groups):
        return
    if not db_one(conn,"SELECT 1 FROM fixture WHERE competition=? AND round=7",(comp,)):
        tables = compute_all_groups(conn,comp)
        firsts = [rows[0]["team"] for rows in tables.values()]
        seconds = [rows[1]["team"] for rows in tables.values()]
        seconds=seconds[1:]+seconds[:1]
        for index,(home,away) in enumerate(zip(firsts,seconds)):
            tid=f"{comp}_R7_{index}"
            label="Vòng 16" if len(firsts)==8 else "Tứ kết"
            for leg,day in enumerate(rules.INTERNATIONAL_DAYS[7],1):
                h,a = (away,home) if leg==1 else (home,away)
                schedule_fixture(conn,comp,7,h,a,max(day,max(f["day"] for f in groups)+leg*3),leg,tid,label)
    progress_elimination(conn,comp)


def progress_knockout(conn, competition):
    if competition=="ApexCup":
        progress_cup_knockout_2legged(conn)
    elif competition in ("Champions","Europa"):
        progress_champions_knockout(conn,competition)


def auto_play_ai_fixtures(conn: sqlite3.Connection, up_to_day: int, user_team: str) -> int:
    """Simulate fixtures for AI vs AI teams up to a given day (keeps the world moving)."""
    total = 0
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

            record_fixture_player_stats(conn, fix["id"], fix["competition"], fix["home"], fix["away"], hg, ag, user_team, None)

            f_tie = fix["tie_id"] if ("tie_id" in fix.keys() if hasattr(fix, "keys") else "tie_id" in fix) else None
            f_leg = fix["leg"] if ("leg" in fix.keys() if hasattr(fix, "keys") else "leg" in fix) else 1
            if f_tie and f_leg == 2:
                leg1 = db_one(conn, "SELECT home_goals, away_goals FROM fixture WHERE tie_id=? AND leg=1", (f_tie,))
                if leg1:
                    t1_tot = (leg1["home_goals"] or 0) + ag
                    t2_tot = (leg1["away_goals"] or 0) + hg
                    agg_str = f"{t1_tot} - {t2_tot} (agg)"
                    db_exec(conn, "UPDATE fixture SET aggregate_score=? WHERE tie_id=?", (agg_str, f_tie))

            progress_knockout(conn, fix["competition"])

    return total

def compute_league_table(conn: sqlite3.Connection) -> List[Dict]:
    """Compute the 20-team National League table (Screenshot 1)."""
    teams = [r["name"] for r in db_all(conn, "SELECT DISTINCT name FROM team WHERE league='League'")]
    if not teams:
        teams = [r["name"] for r in db_all(conn, "SELECT DISTINCT home FROM fixture WHERE competition='League'")]

    table = {t: {"team": t, "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "PTS": 0, "form": []} for t in teams}
    matches = db_all(conn, "SELECT * FROM fixture WHERE competition='League' AND played=1 ORDER BY day ASC, id ASC")
    for m in matches:
        h, a = m["home"], m["away"]
        hg, ag = m["home_goals"] or 0, m["away_goals"] or 0
        if h in table:
            table[h]["P"] += 1
            table[h]["GF"] += hg
            table[h]["GA"] += ag
            if hg > ag:
                table[h]["W"] += 1
                table[h]["PTS"] += 3
                table[h]["form"].append("T")
            elif hg == ag:
                table[h]["D"] += 1
                table[h]["PTS"] += 1
                table[h]["form"].append("H")
            else:
                table[h]["L"] += 1
                table[h]["form"].append("B")
        if a in table:
            table[a]["P"] += 1
            table[a]["GF"] += ag
            table[a]["GA"] += hg
            if ag > hg:
                table[a]["W"] += 1
                table[a]["PTS"] += 3
                table[a]["form"].append("T")
            elif ag == hg:
                table[a]["D"] += 1
                table[a]["PTS"] += 1
                table[a]["form"].append("H")
            else:
                table[a]["L"] += 1
                table[a]["form"].append("B")

    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    my_name = club["name"] if club else ""

    rows = list(table.values())
    rows.sort(key=lambda r: (r["PTS"], (r["GF"] - r["GA"]), r["GF"]), reverse=True)

    team_meta = {r["name"]: dict(r) for r in db_all(conn, "SELECT * FROM team")}
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        gd = r["GF"] - r["GA"]
        r["GD"] = f"+{gd}" if gd > 0 else str(gd)
        f5 = r["form"][-5:]
        while len(f5) < 5:
            f5.insert(0, "—")
        r["form5"] = f5
        r["trend"] = "same"

        if i <= 4: r["zone"] = "c1"        # Top 4 đá C1 (Screenshot 1: viền xanh ngọc)
        elif 5 <= i <= 8: r["zone"] = "c2" # Top 5-8 đá C2 (Screenshot 1: viền cam)
        elif i >= 18: r["zone"] = "relegation" # Xuống hạng (viền đỏ)
        else: r["zone"] = "mid"

        t_meta = team_meta.get(r["team"], {})
        r["logo"] = t_meta.get("logo_icon", "🛡️")
        r["flag"] = t_meta.get("country_flag", "🏴󠁧󠁢󠁥󠁮󠁧󠁿")
        r["is_user"] = (r["team"] == my_name)

    return rows

def compute_all_groups(conn: sqlite3.Connection, comp: str = "Champions") -> Dict[str, List[Dict]]:
    """Compute Group Stage tables (Bảng A-H) for C1/C2."""
    groups_data = {}
    group_rows = db_all(conn, "SELECT DISTINCT group_name FROM fixture WHERE competition=? AND group_name IS NOT NULL ORDER BY group_name", (comp,))
    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    my_name = club["name"] if club else ""

    for gr in group_rows:
        g_name = gr["group_name"]
        fixes = db_all(conn, "SELECT * FROM fixture WHERE competition=? AND group_name=? ORDER BY round, id", (comp, g_name))
        teams = set()
        for f in fixes:
            teams.add(f["home"])
            teams.add(f["away"])

        table = {t: {"team": t, "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "PTS": 0} for t in teams}
        for f in fixes:
            if f["played"]:
                h, a = f["home"], f["away"]
                hg, ag = f["home_goals"] or 0, f["away_goals"] or 0
                table[h]["P"] += 1
                table[a]["P"] += 1
                table[h]["GF"] += hg
                table[h]["GA"] += ag
                table[a]["GF"] += ag
                table[a]["GA"] += hg
                if hg > ag:
                    table[h]["W"] += 1
                    table[h]["PTS"] += 3
                elif hg < ag:
                    table[a]["W"] += 1
                    table[a]["PTS"] += 3
                else:
                    table[h]["D"] += 1
                    table[a]["D"] += 1
                    table[h]["PTS"] += 1
                    table[a]["PTS"] += 1

        rows = list(table.values())
        rows.sort(key=lambda r: (r["PTS"], (r["GF"] - r["GA"]), r["GF"]), reverse=True)
        team_meta = {r["name"]: dict(r) for r in db_all(conn, "SELECT * FROM team")}
        for i, r in enumerate(rows, start=1):
            r["rank"] = i
            gd = r["GF"] - r["GA"]
            r["GD"] = f"+{gd}" if gd > 0 else str(gd)
            r["advancing"] = (i <= 2)
            r["is_user"] = (r["team"] == my_name)
            t_m = team_meta.get(r["team"], {})
            r["logo"] = t_m.get("logo_icon", "🛡️")
        groups_data[g_name] = rows

    return groups_data

def compute_cup_bracket_data(conn: sqlite3.Connection) -> List[Dict]:
    """Compute knockout bracket pairs with 2 legs and aggregate scores for Cúp Quốc Gia Apex."""
    rounds_data = []
    round_labels = {
        1: "Vòng 64 (32 Cặp Đấu - Lượt Đi & Lượt Về)",
        2: "Vòng 32 (16 Cặp Đấu)",
        3: "Vòng 16 (8 Cặp Đấu)",
        4: "Tứ Kết (4 Cặp Đấu)",
        5: "Bán Kết (2 Cặp Đấu)",
        6: "Chung Kết",
    }
    club = db_one(conn, "SELECT name FROM club WHERE id=1")
    my_name = club["name"] if club else ""

    for r_num in range(1, 7):
        fixes = db_all(conn, "SELECT * FROM fixture WHERE competition='ApexCup' AND round=? ORDER BY tie_id, leg", (r_num,))
        if not fixes:
            continue
        ties: Dict[str, Dict] = {}
        for f in fixes:
            tid = f["tie_id"] or f"TIE_{f['id']}"
            if tid not in ties:
                ties[tid] = {
                    "tie_id": tid,
                    "round": r_num,
                    "leg1": None,
                    "leg2": None,
                    "team1": f["home"] if f["leg"] == 1 else f["away"],
                    "team2": f["away"] if f["leg"] == 1 else f["home"],
                    "agg_str": f["aggregate_score"] if (("aggregate_score" in f.keys() if hasattr(f, "keys") else "aggregate_score" in f) and f["aggregate_score"]) else "",
                    "has_user": False,
                }
            if f["leg"] == 1:
                ties[tid]["leg1"] = dict(f)
            else:
                ties[tid]["leg2"] = dict(f)
            if f["home"] == my_name or f["away"] == my_name:
                ties[tid]["has_user"] = True

        rounds_data.append({
            "round": r_num,
            "label": round_labels.get(r_num, f"Vòng {r_num}"),
            "ties": list(ties.values()),
        })

    return rounds_data

def compute_player_leaderboards(conn: sqlite3.Connection, comp_filter: Optional[str] = None) -> Dict[str, List[Dict]]:
    """Compute top player leaderboards: Goals (Bàn thắng), MVP Rating (Xếp hạng), Assists (Kiến tạo) (Screenshot 2)."""
    where_sql = "WHERE fixture_id>0"
    params = []
    if comp_filter and comp_filter != "all":
        where_sql += " AND competition=?"
        params.append(comp_filter)

    cur = conn.execute(f"""
        SELECT player_name, team_name, is_user_player, SUM(goals) as total_stat, ROUND(AVG(rating), 2) as avg_rating
        FROM player_match_stat
        {where_sql}
        GROUP BY player_name, team_name
        HAVING total_stat > 0
        ORDER BY total_stat DESC, avg_rating DESC, player_name ASC
        LIMIT 5
    """, tuple(params))
    scorers_raw = [dict(r) for r in cur.fetchall()]

    cur = conn.execute(f"""
        SELECT player_name, team_name, is_user_player, ROUND(AVG(rating), 2) as total_stat, SUM(goals) as goals
        FROM player_match_stat
        {where_sql}
        GROUP BY player_name, team_name
        HAVING total_stat > 0
        ORDER BY total_stat DESC, goals DESC, player_name ASC
        LIMIT 5
    """, tuple(params))
    ratings_raw = [dict(r) for r in cur.fetchall()]

    cur = conn.execute(f"""
        SELECT player_name, team_name, is_user_player, SUM(assists) as total_stat, ROUND(AVG(rating), 2) as avg_rating
        FROM player_match_stat
        {where_sql}
        GROUP BY player_name, team_name
        HAVING total_stat > 0
        ORDER BY total_stat DESC, avg_rating DESC, player_name ASC
        LIMIT 5
    """, tuple(params))
    assists_raw = [dict(r) for r in cur.fetchall()]

    team_rows = {r["name"]: dict(r) for r in db_all(conn, "SELECT * FROM team")}

    def format_list(items, stat_type):
        result = []
        for i, it in enumerate(items, start=1):
            t_name = it["team_name"]
            t_meta = team_rows.get(t_name, {})
            val = it["total_stat"]
            if stat_type == "rating":
                val_str = f"{val:.2f}".replace(".", ",")
            else:
                val_str = str(int(val))

            result.append({
                "rank": i,
                "player_name": it["player_name"],
                "team_name": t_name,
                "club_logo": t_meta.get("logo_icon", "🛡️"),
                "country_flag": t_meta.get("country_flag", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
                "value_str": val_str,
                "is_user": bool(it["is_user_player"]),
                "jersey_color": "#c8102e" if "Liverpool" in t_name else ("#6cabdd" if "City" in t_name else "#22c55e"),
            })
        return result

    return {
        "scorers": format_list(scorers_raw, "goals"),
        "ratings": format_list(ratings_raw, "rating"),
        "assists": format_list(assists_raw, "assists"),
    }

@app.route("/competitions")
def competitions():
    conn = get_db()
    finalize_builds(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    day = club["season_day"]
    my = club["name"]

    # AI teams also play their fixtures
    auto_play_ai_fixtures(conn, up_to_day=int(day), user_team=my)

    # Tabs & competition filter
    active_tab = request.args.get("tab", "standings")  # standings | matches | stats
    selected_comp = request.args.get("comp", "League")  # League | Champions | Europa | ApexCup | all

    # Pending matches for user (can play missed matches too)
    pending = db_all(conn, """
        SELECT * FROM fixture
        WHERE played=0 AND day<=? AND (home=? OR away=?)
        ORDER BY day ASC, competition ASC, round ASC
    """, (day, my, my))

    # Matches today for user
    todays = db_all(conn, """
        SELECT * FROM fixture
        WHERE day=? AND (home=? OR away=?)
        ORDER BY competition
    """, (day, my, my))

    # Standings
    standings = compute_league_table(conn)
    champions_groups = compute_all_groups(conn, "Champions")
    europa_groups = compute_all_groups(conn, "Europa")
    cup_rounds = compute_cup_bracket_data(conn)

    # Leaderboards (Screenshot 2: Bàn Thắng, Xếp Hạng, Kiến Tạo)
    leaderboards = compute_player_leaderboards(conn, comp_filter=selected_comp)

    # Fixtures & results for matches tab
    if selected_comp == "all":
        comp_matches = db_all(conn, "SELECT * FROM fixture ORDER BY day DESC, id DESC LIMIT 60")
    else:
        comp_matches = db_all(conn, "SELECT * FROM fixture WHERE competition=? ORDER BY round ASC, day ASC, id ASC LIMIT 80", (selected_comp,))

    recent = db_all(conn, "SELECT * FROM fixture WHERE played=1 ORDER BY day DESC, id DESC LIMIT 12")

    # Match previews (win/draw/loss odds & xG)
    previews: Dict[int, Dict] = {}
    if pending:
        user_strength, _ = compute_user_strength(conn, club)
        for f in pending:
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
    return render_template(
        "competitions.html",
        **ctx,
        pending=pending,
        todays=todays,
        standings=standings,
        champions_groups=champions_groups,
        europa_groups=europa_groups,
        cup_rounds=cup_rounds,
        leaderboards=leaderboards,
        comp_matches=comp_matches,
        recent=recent,
        previews=previews,
        active_tab=active_tab,
        selected_comp=selected_comp,
    )


def friendly_offer(conn):
    club=db_one(conn,"SELECT * FROM club")
    if club["friendly_offer_day"]!=club["season_day"]:
        conn.execute("UPDATE club SET friendly_opponent=?,friendly_strength=?,friendly_offer_day=season_day",
            (gen_team_name({club["name"]}),round(52+min(30,2*(club["club_level"]-1))+random.uniform(-4,4),1)))
        conn.commit()
        club=db_one(conn,"SELECT * FROM club")
    busy=db_one(conn,"SELECT 1 FROM fixture WHERE ABS(day-?)<3 AND (home=? OR away=?)",
                (club["season_day"],club["name"],club["name"]))
    rest_ok=club["last_friendly_day"]<0 or club["season_day"]-club["last_friendly_day"]>=3
    return club, not busy and rest_ok


@app.route("/friendly")
def friendly():
    conn=get_db()
    club,can_play=friendly_offer(conn)
    strength,_=compute_user_strength(conn,club)
    odds=match_odds(strength,club["friendly_strength"],1.02)
    ctx=load_context(conn)
    conn.close()
    return render_template("friendly.html",**ctx,can_play=can_play,est_strength=round(strength,1),
        opp_strength=club["friendly_strength"],win=round(odds["win"]*100),draw=round(odds["draw"]*100),loss=round(odds["loss"]*100))


@app.post("/friendly/play")
def friendly_play():
    conn=get_db()
    club,can_play=friendly_offer(conn)
    if not can_play:
        flash("Cần cách các trận chính thức và giao hữu ít nhất 3 ngày.")
        return redirect(url_for("friendly"))
    strength,xi_pairs=compute_user_strength(conn,club)
    if len(xi_pairs)<11:
        flash("Cần 11 cầu thủ khỏe mạnh để đá giao hữu.")
        return redirect(url_for("friendly"))
    hg,ag=simulate_match(strength,club["friendly_strength"],1.02)
    reward=25_000 if hg>ag else 20_000 if hg==ag else 15_000
    conn.execute("INSERT INTO friendly_match(season_number,day,opponent,opponent_strength,goals_for,goals_against) VALUES (?,?,?,?,?,?)",
                 (club["season_number"],club["season_day"],club["friendly_opponent"],club["friendly_strength"],hg,ag))
    conn.execute("UPDATE club SET last_friendly_day=season_day,cash=cash+?",(reward,))
    for p,_ in xi_pairs:
        conn.execute("UPDATE squad_player SET condition=MAX(0,condition-25),experience_matches=experience_matches+1 WHERE id=?",(p["id"],))
    rules.event(conn,"friendly",f"{club['friendly_opponent']}: {hg}-{ag}",cash=reward)
    conn.close()
    flash(f"Giao hữu: {hg}-{ag}; +{fmt_money(reward)} tiền; cầu thủ ra sân mất 25% thể lực.")
    return redirect(url_for("friendly"))


def compute_club_season_stats(conn, club):
    stats = rules.summary(rules.club_matches(conn))
    stats.update(rules.player_records(conn))
    table = compute_league_table(conn)
    rank = next((r["rank"] for r in table if r["is_user"]), None)
    stats["league_status"] = f"Vị trí thứ {rank}" if rank else "Chưa có dữ liệu"
    for comp,key in (("Champions","champions_status"),("ApexCup","cup_status"),("SuperCup","supercup_status")):
        fixtures = db_all(conn, "SELECT * FROM fixture WHERE competition=? AND (home=? OR away=?) ORDER BY round DESC,leg DESC", (comp,club["name"],club["name"]))
        stats[key] = "Không tham dự"
        if fixtures:
            latest = fixtures[0]
            stats[key] = latest["group_name"] or f"Vòng {latest['round']}"
            if latest["winner"] and (latest["round"] == (6 if comp=="ApexCup" else 10)):
                stats[key] = "Vô địch" if latest["winner"]==club["name"] else "Á quân" if club["name"] in (latest["home"],latest["away"]) else "Đã kết thúc"
    return stats

def compute_club_history_stats(conn, club, season):
    archives = db_all(conn, "SELECT * FROM season_record ORDER BY season_number")
    matches = [m for record in archives for m in json.loads(record["matches"])] + rules.club_matches(conn)
    summary = rules.summary(matches)
    titles = {key: 0 for key in ("league_titles","champions_titles","supercup_titles","cup_titles","ultimate_titles")}
    for record in archives:
        for key,value in json.loads(record["titles"]).items():
            titles[key] = titles.get(key,0) + value
    return {**summary, **titles, **rules.player_records(conn, archived=True),
            "longest_win": summary["max_win_streak"], "longest_unbeaten": summary["max_unbeaten"]}

@app.route("/club")
def club_profile():
    conn = get_db()
    finalize_builds(conn)
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    tab = request.args.get("tab", "tongquan")  # tongquan | muagiai | lichsu

    eff = facility_effects(conn)
    capacity = eff.get("capacity", 10100) if isinstance(eff, dict) else 10100

    season_stats = compute_club_season_stats(conn, club)
    history_stats = compute_club_history_stats(conn, club, season_stats)

    ctx = load_context(conn)
    conn.close()

    return render_template(
        "club.html",
        **ctx,
        active_tab=tab,
        capacity=capacity,
        season_stats=season_stats,
        history_stats=history_stats,
    )

@app.post("/club/update_profile")
def club_update_profile():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    old_name = club["name"]

    new_club_name = clean_name(request.form.get("club_name", ""), max_len=28)
    if new_club_name and len(new_club_name) >= 3 and new_club_name != old_name:
        exists = db_one(conn, "SELECT 1 FROM team WHERE name=?", (new_club_name,))
        if not exists:
            db_exec(conn, "UPDATE club SET name=? WHERE id=1", (new_club_name,))
            db_exec(conn, "UPDATE team SET name=? WHERE name=?", (new_club_name, old_name))
            db_exec(conn,"UPDATE player_match_stat SET team_name=? WHERE is_user_player=1",(new_club_name,))
            db_exec(conn, "UPDATE fixture SET home=? WHERE home=?", (new_club_name, old_name))
            db_exec(conn, "UPDATE fixture SET away=? WHERE away=?", (new_club_name, old_name))

    stadium_name = clean_name(request.form.get("stadium_name", ""), max_len=35) or club["stadium_name"]
    manager_name = clean_name(request.form.get("manager_name", ""), max_len=30) or club["manager_name"]
    city = clean_name(request.form.get("city", ""), max_len=30) or club["city"]
    country = clean_name(request.form.get("country", ""), max_len=30) or "Việt Nam"
    country_flag = request.form.get("country_flag", "").strip() or "🇻🇳"
    fanclub_name = clean_name(request.form.get("fanclub_name", ""), max_len=35) or club["fanclub_name"]
    home_kit = request.form.get("home_kit_color", "").strip() or "#dc2626"
    away_kit = request.form.get("away_kit_color", "").strip() or "#ffffff"

    db_exec(conn, """
        UPDATE club SET
            stadium_name=?, manager_name=?, city=?, country=?, country_flag=?,
            fanclub_name=?, home_kit_color=?, away_kit_color=?
        WHERE id=1
    """, (stadium_name, manager_name, city, country, country_flag, fanclub_name, home_kit, away_kit))
    conn.close()
    flash("Đã cập nhật chi tiết câu lạc bộ thành công!")
    return redirect(url_for("club_profile", tab="tongquan"))


def get_player_short_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    if not parts:
        return "Player"
    if len(parts) == 1:
        return parts[0]
    if len(parts[0]) <= 2 and parts[0].endswith("."):
        return " ".join(parts[1:])
    if len(parts) >= 3:
        return " ".join(parts[-2:])
    return parts[-1]


def get_team_context(conn: sqlite3.Connection, active_tab: str = "sodo") -> Dict:
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    formation = club["formation"]
    day = int(club["season_day"])
    my = club["name"]

    coords = HORIZONTAL_FORMATION_LAYOUT.get(formation, HORIZONTAL_FORMATION_LAYOUT.get("4-4-2"))
    slot_rows = get_lineup_slots(conn, formation)
    slot_positions = [row["position"] for row in slot_rows]

    players_raw = db_all(conn, "SELECT * FROM squad_player ORDER BY position, id")
    all_players: List[Dict] = []
    by_id = {}
    for p in players_raw:
        ovr = round(player_overall(p), 1)
        eff = round(player_effective_rating(p, day), 1)
        inj = (p["injured_until_day"] or 0) > day
        inj_days = max(0, (p["injured_until_day"] or 0) - day)
        shirt_num = p["shirt_number"] if "shirt_number" in p.keys() and p["shirt_number"] else p["id"]
        nat = p["nationality"] if "nationality" in p.keys() and p["nationality"] else "VN"
        nat_flag = NAT_FLAGS.get(nat, "🇻🇳")
        short_name = get_player_short_name(p["name"])
        transfer_status = p["transfer_status"] if "transfer_status" in p.keys() else 0
        contract_years = p["contract_years"] if "contract_years" in p.keys() and p["contract_years"] else 3
        market_val = rules.player_value(p)

        mor = int(p["morale"])
        if mor >= 85:
            mor_arrow = "up"
        elif mor >= 70:
            mor_arrow = "diag_up"
        elif mor >= 45:
            mor_arrow = "level"
        else:
            mor_arrow = "diag_down"

        if ovr >= 85:
            sc = 5
        elif ovr >= 75:
            sc = 4
        elif ovr >= 65:
            sc = 3
        elif ovr >= 55:
            sc = 2
        else:
            sc = 1

        pdict = {
            **dict(p),
            "overall": ovr,
            "effective": eff,
            "injured": inj,
            "injured_days": inj_days,
            "shirt_number": shirt_num,
            "short_name": short_name,
            "nationality": nat,
            "nat_flag": nat_flag,
            "transfer_status": transfer_status,
            "contract_years": contract_years,
            "market_value": market_val,
            "morale_arrow": mor_arrow,
            "star_count": sc,
            "cltt_stars": cltt_to_stars(ovr),
            "assigned_slot": "-",
        }
        all_players.append(pdict)
        by_id[p["id"]] = pdict

    # Build Starting XI slots
    slots_view = []
    captain_slot = None
    starting_pids = set()
    for i, pos in enumerate(slot_positions):
        sr = slot_rows[i]
        pid = sr["player_id"]
        assigned = by_id.get(pid) if pid else None
        is_cap = (sr["is_captain"] == 1) or (club["captain_pid"] and pid == club["captain_pid"])
        if is_cap:
            captain_slot = i
        arr = sr["arrow_trend"] if "arrow_trend" in sr.keys() else 0
        x, y = coords[i] if coords and i < len(coords) else (50, 50)
        if sr["pitch_x"] is not None and sr["pitch_y"] is not None:
            x, y = sr["pitch_x"], sr["pitch_y"]

        if assigned:
            starting_pids.add(assigned["id"])
            assigned["assigned_slot"] = pos

        slots_view.append({
            "slot_index": i,
            "slot_pos": pos,
            "x": x,
            "y": y,
            "player": assigned,
            "is_captain": is_cap,
            "arrow_trend": arr,
        })

    # Group Bench and Reserves
    remaining_players = [p for p in all_players if p["id"] not in starting_pids]
    bench_explicit = [p for p in remaining_players if p.get("bench_slot", 0) > 0]
    bench_explicit.sort(key=lambda p: p.get("bench_slot", 0))
    if not bench_explicit and not club["bench_initialized"]:
        sorted_rem = sorted(remaining_players, key=lambda p: p["overall"], reverse=True)
        bench_players = sorted_rem[:7]
    else:
        bench_players = bench_explicit[:7]

    bench_pids = {p["id"] for p in bench_players}
    for bp in bench_players:
        bp["assigned_slot"] = "SUB"

    reserve_players = [p for p in remaining_players if p["id"] not in bench_pids]

    # Calculate starting XI strength
    strength, xi_pairs = compute_user_strength(conn, club)
    xg_u, xg_o = expected_goals(strength, 60.0, home_adv=1.0)
    starting_ovrs = [s["player"]["overall"] for s in slots_view if s["player"]]
    cltt_avg = round(sum(starting_ovrs) / len(starting_ovrs), 1) if starting_ovrs else round(strength, 1)

    # Set Pieces takers (for Button 4)
    active_xi = [s["player"] for s in slots_view if s["player"]]
    def best_taker(key, fallback_p):
        pid = club[key] if key in club.keys() else None
        if pid and pid in by_id:
            return by_id[pid]
        return fallback_p

    def_pen = max(active_xi, key=lambda p: p["finishing"]) if active_xi else None
    def_fk = max(active_xi, key=lambda p: p["passing"] + p["finishing"]) if active_xi else None
    def_cor = max(active_xi, key=lambda p: p["passing"] + p["pace"]) if active_xi else None
    def_cap = (
        slots_view[captain_slot]["player"]
        if (captain_slot is not None and captain_slot < len(slots_view) and slots_view[captain_slot]["player"])
        else (max(active_xi, key=lambda p: p["overall"]) if active_xi else None)
    )

    set_pieces = {
        "captain": best_taker("captain_pid", def_cap),
        "penalty": best_taker("penalty_pid", def_pen),
        "freekick_r": best_taker("freekick_r_pid", def_fk),
        "freekick_l": best_taker("freekick_l_pid", def_fk),
        "corner_r": best_taker("corner_r_pid", def_cor),
        "corner_l": best_taker("corner_l_pid", def_cor),
    }

    # Upcoming opponent preview (for Button 3)
    upcoming_fix = db_one(conn, """
        SELECT * FROM fixture
        WHERE played=0 AND (home=? OR away=?)
        ORDER BY day ASC, id ASC LIMIT 1
    """, (my, my))

    opp_data = None
    if upcoming_fix:
        opp_name = upcoming_fix["away"] if upcoming_fix["home"] == my else upcoming_fix["home"]
        opp_row = db_one(conn, "SELECT base_strength FROM team WHERE name=?", (opp_name,))
        opp_base = float(opp_row["base_strength"] if opp_row else 60.0)
        opp_formation = "4-3-3" if "4-3-3" in opp_name else "4-4-2"
        opp_slots = formation_slots(opp_formation)
        opp_coords = HORIZONTAL_FORMATION_LAYOUT.get(opp_formation, HORIZONTAL_FORMATION_LAYOUT["4-4-2"])

        opp_players = []
        roster=db_all(conn,"SELECT * FROM ai_player WHERE team_name=? ORDER BY shirt_number",(opp_name,))
        for idx,p in enumerate(roster[:11]):
            c_x,c_y=opp_coords[idx]
            opp_players.append({"pos":p["position"],"name":p["name"],"number":p["shirt_number"],
                                "overall":p["overall"],"stars":cltt_to_stars(p["overall"]),"x":c_x,"y":c_y})

        opp_data = {
            "name": opp_name,
            "base_strength": round(opp_base, 1),
            "cltt_stars": cltt_to_stars(opp_base),
            "formation": opp_formation,
            "is_home": (upcoming_fix["home"] == opp_name),
            "players": opp_players,
        }

    # Team tactics parameters (for Tab 3)
    marking_type = club["marking_type"] if "marking_type" in club.keys() and club["marking_type"] else "man"
    pressing_style = club["pressing_style"] if "pressing_style" in club.keys() and club["pressing_style"] else "low"
    defensive_line = club["defensive_line"] if "defensive_line" in club.keys() and club["defensive_line"] else "tight"
    tackling_style = club["tackling_style"] if "tackling_style" in club.keys() and club["tackling_style"] else "normal"
    attacking_focus = club["attacking_focus"] if "attacking_focus" in club.keys() and club["attacking_focus"] else "mixed"
    counter_attack = club["counter_attack"] if "counter_attack" in club.keys() and club["counter_attack"] else "off"

    if pressing_style == "high" or club["pressing"] == "high":
        stamina_drain_level = "CAO"
        stamina_drain_pct = 85
    elif pressing_style == "low" and club["mentality"] == "defensive":
        stamina_drain_level = "THẤP"
        stamina_drain_pct = 35
    else:
        stamina_drain_level = "VỪA PHẢI"
        stamina_drain_pct = 60

    tactics_data = {
        "formation": formation,
        "mentality": club["mentality"],
        "pressing": club["pressing"],
        "passing": club["passing"],
        "marking_type": marking_type,
        "pressing_style": pressing_style,
        "defensive_line": defensive_line,
        "tackling_style": tackling_style,
        "attacking_focus": attacking_focus,
        "counter_attack": counter_attack,
        "stamina_drain_level": stamina_drain_level,
        "stamina_drain_pct": stamina_drain_pct,
    }

    return {
        "club": club,
        "formation": formation,
        "active_tab": active_tab,
        "position_zones": POSITION_ZONES,
        "pitch_bounds": {"minX": MIN_X, "maxX": MAX_X, "minY": MIN_Y, "maxY": MAX_Y},
        "custom_layout": any(s["pitch_x"] is not None and
                             (abs(s["pitch_x"]-coords[i][0])>.01 or abs(s["pitch_y"]-coords[i][1])>.01)
                             for i,s in enumerate(slot_rows)),
        "slots": slots_view,
        "all_players": all_players,
        "player_profiles": [{**player_to_modal_dict(p, source="squad", conn=conn),
                             "shirt_number": p["shirt_number"], "role": compute_role(p),
                             "traits": parse_traits(p["traits"]), "morale": p["morale"], "form": p["form"]}
                            for p in all_players],
        "players": all_players,
        "starting_xi": [s["player"] for s in slots_view if s["player"]],
        "bench_players": bench_players,
        "reserve_players": reserve_players,
        "captain_slot": captain_slot,
        "set_pieces": set_pieces,
        "upcoming_opponent": opp_data,
        "tactics_data": tactics_data,
        "est_strength": round(strength, 1),
        "est_xg": round(xg_u, 2),
        "est_xg_opp": round(xg_o, 2),
        "cltt_avg": cltt_avg,
        "cltt_stars": cltt_to_stars(cltt_avg),
    }


@app.route("/team")
@app.route("/lineup")
def lineup():
    conn = get_db()
    tab = request.args.get("tab", "sodo")
    payload = get_team_context(conn, active_tab=tab)
    ctx = load_context(conn)
    conn.close()
    merged = {**ctx, **payload}
    return render_template("lineup.html", **merged)


@app.route("/squad")
def squad():
    conn = get_db()
    tab = request.args.get("tab", "doihinh")
    payload = get_team_context(conn, active_tab=tab)
    ctx = load_context(conn)
    conn.close()
    merged = {**ctx, **payload}
    return render_template("lineup.html", **merged)


@app.route("/tactics")
def tactics():
    conn = get_db()
    tab = request.args.get("tab", "chienthuat")
    payload = get_team_context(conn, active_tab=tab)
    ctx = load_context(conn)
    conn.close()
    merged = {**ctx, **payload}
    return render_template("lineup.html", **merged)


@app.post("/tactics")
def save_tactics():
    formation = request.form.get("formation", "4-4-2")
    mentality = request.form.get("mentality", "balanced")
    pressing = request.form.get("pressing", "normal")
    passing = request.form.get("passing", "short")
    conn = get_db()
    db_exec(conn, "UPDATE club SET formation=?, mentality=?, pressing=?, passing=? WHERE id=1",
            (formation, mentality, pressing, passing))
    ensure_lineup_slots(conn, formation)
    conn.close()
    flash("Đã lưu đội hình & chiến thuật.")
    return redirect(url_for("lineup", tab="chienthuat"))


@app.post("/tactics/autosave")
def tactics_autosave():
    data = request.get_json(silent=True) or request.form
    formation = data.get("formation", "4-4-2")
    mentality = data.get("mentality", "balanced")
    pressing = data.get("pressing", "normal")
    passing = data.get("passing", "short")
    marking_type = data.get("marking_type", "man")
    pressing_style = data.get("pressing_style", "low")
    defensive_line = data.get("defensive_line", "tight")
    tackling_style = data.get("tackling_style", "normal")
    attacking_focus = data.get("attacking_focus", "mixed")
    counter_attack = data.get("counter_attack", "off")

    conn = get_db()
    db_exec(conn, """
        UPDATE club SET
            formation=?, mentality=?, pressing=?, passing=?,
            marking_type=?, pressing_style=?, defensive_line=?,
            tackling_style=?, attacking_focus=?, counter_attack=?
        WHERE id=1
    """, (formation, mentality, pressing, passing, marking_type, pressing_style, defensive_line, tackling_style, attacking_focus, counter_attack))
    ensure_lineup_slots(conn, formation)
    conn.close()

    if pressing_style == "high" or pressing == "high":
        drain_level = "CAO"
        drain_pct = 85
    elif pressing_style == "low" and mentality == "defensive":
        drain_level = "THẤP"
        drain_pct = 35
    else:
        drain_level = "VỪA PHẢI"
        drain_pct = 60

    return {
        "ok": True,
        "message": "Đã tự động lưu chiến thuật.",
        "drain_level": drain_level,
        "drain_pct": drain_pct,
    }


@app.post("/lineup/save")
def save_lineup():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    formation = club["formation"]
    slots = formation_slots(formation)

    is_json = request.is_json
    data = request.get_json(silent=True) or request.form
    if not hasattr(data, "get"):
        abort(400, description="Đội hình không hợp lệ.")

    slot_to_player: Dict[int, Optional[int]] = {}
    arrow_trends: Dict[int, int] = {}
    coordinates = {}
    for i in range(len(slots)):
        x_raw, y_raw = data.get(f"x_{i}", ""), data.get(f"y_{i}", "")
        if x_raw != "" or y_raw != "":
            try:
                x, y = float(x_raw), float(y_raw)
                position_at(x, y)
            except (ValueError, TypeError, OverflowError):
                abort(400, description="Tọa độ cầu thủ không hợp lệ hoặc nằm ngoài sân.")
            coordinates[i] = (x, y)
        raw = str(data.get(f"slot_{i}", "")).strip()
        if raw.isdigit():
            slot_to_player[i] = int(raw)
        else:
            slot_to_player[i] = None

        raw_arr = str(data.get(f"arrow_{i}", "0")).strip()
        try:
            arrow_trends[i] = int(raw_arr)
        except ValueError:
            arrow_trends[i] = 0

    cap_raw = str(data.get("captain_slot", "")).strip()
    captain_slot = int(cap_raw) if cap_raw.isdigit() else None

    owned_ids = {p["id"] for p in db_all(conn, "SELECT id FROM squad_player")}
    selected = [pid for pid in slot_to_player.values() if pid is not None]
    bench = []
    for b_idx in range(7):
        raw = str(data.get(f"bench_{b_idx}", "")).strip()
        if raw and not raw.isdigit():
            abort(400, description="Danh sách dự bị không hợp lệ.")
        if raw:
            bench.append((b_idx + 1, int(raw)))
    bench_ids = [pid for _, pid in bench]
    if (any(pid not in owned_ids for pid in selected) or len(selected) != len(set(selected))
            or any(pid not in owned_ids for pid in bench_ids) or len(bench_ids) != len(set(bench_ids))
            or set(selected).intersection(bench_ids)
            or (captain_slot is not None and captain_slot not in range(len(slots)))):
        conn.close()
        abort(400, description="Đội hình không hợp lệ. Vui lòng chọn cầu thủ trong đội của bạn.")

    set_lineup(conn, formation, slot_to_player, captain_slot, arrow_trends, coordinates)

    # Save set pieces if provided
    for key in ["captain_pid", "penalty_pid", "freekick_r_pid", "freekick_l_pid", "corner_r_pid", "corner_l_pid"]:
        val = str(data.get(key, "")).strip()
        if val.isdigit() and int(val) in owned_ids:
            db_exec(conn, f"UPDATE club SET {key}=? WHERE id=1", (int(val),))

    # Save bench players if provided
    db_exec(conn, "UPDATE squad_player SET bench_slot=0")
    for bench_slot, pid in bench:
        db_exec(conn, "UPDATE squad_player SET bench_slot=? WHERE id=?", (bench_slot, pid))
    db_exec(conn, "UPDATE club SET bench_initialized=1 WHERE id=1")

    conn.close()

    if is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return {"ok": True, "message": "Đã lưu đội hình thành công."}

    flash("Đã lưu đội hình ra sân.")
    return redirect(url_for("lineup", tab="sodo"))


@app.post("/lineup/autopick")
def lineup_autopick():
    conn = get_db()
    club = db_one(conn, "SELECT * FROM club WHERE id=1")
    auto_pick_and_save_lineup(conn, club["season_day"], club["formation"])
    conn.close()
    flash("Đã auto-pick đội hình mạnh nhất theo sơ đồ.")
    return redirect(url_for("lineup", tab="sodo"))


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
    return redirect(url_for("lineup", tab="sodo"))


@app.post("/player/<int:pid>/toggle_transfer")
def toggle_player_transfer(pid: int):
    conn = get_db()
    p = db_one(conn, "SELECT id, transfer_status FROM squad_player WHERE id=?", (pid,))
    if not p:
        conn.close()
        return {"ok": False, "error": "Không tìm thấy cầu thủ"}, 404
    new_st = 0 if (p["transfer_status"] or 0) == 1 else 1
    db_exec(conn, "UPDATE squad_player SET transfer_status=? WHERE id=?", (new_st, pid))
    conn.close()
    return {"ok": True, "status": new_st}

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

    recent = db_one(conn,"SELECT 1 FROM fixture WHERE played=1 AND day>? AND (home=? OR away=?)",
                    (club["season_day"]-3,my,my))
    if recent:
        flash("Cần ít nhất 3 ngày giữa hai trận chính thức.")
        return redirect(url_for("competitions"))
    if len(db_all(conn,"SELECT id FROM squad_player WHERE injured_until_day<=?",(club["season_day"],)))<11:
        flash("Cần tối thiểu 11 cầu thủ khỏe mạnh để thi đấu.")
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

    # Ghi nhận thông số cầu thủ (Vua phá lưới, vua kiến tạo, rating MVP)
    comp = fix["competition"]
    record_fixture_player_stats(conn, fid, comp, fix["home"], fix["away"], home_goals, away_goals, my, xi)

    # Cập nhật tổng tỉ số aggregate nếu là lượt 2 của cặp đấu
    f_tie = fix["tie_id"] if ("tie_id" in fix.keys() if hasattr(fix, "keys") else "tie_id" in fix) else None
    f_leg = fix["leg"] if ("leg" in fix.keys() if hasattr(fix, "keys") else "leg" in fix) else 1
    if f_tie and f_leg == 2:
        leg1 = db_one(conn, "SELECT home_goals, away_goals FROM fixture WHERE tie_id=? AND leg=1", (f_tie,))
        if leg1:
            t1_tot = (leg1["home_goals"] or 0) + away_goals
            t2_tot = (leg1["away_goals"] or 0) + home_goals
            agg_str = f"{t1_tot} - {t2_tot} (agg)"
            db_exec(conn, "UPDATE fixture SET aggregate_score=? WHERE tie_id=?", (agg_str, f_tie))

    # Tiến trình vòng knockout cho Cúp Quốc Gia Apex và Cúp C1
    if comp == "ApexCup":
        progress_cup_knockout_2legged(conn)
    elif comp in ("Champions", "Europa"):
        progress_champions_knockout(conn)
    progress_knockout(conn, comp)

    # Rewards
    attendance = 0
    # matchday revenue (only if user home)
    ticket_revenue = 0
    parking_revenue = 0
    food_revenue = 0
    if fix["home"] == my:
        cap = eff["capacity"]
        avg_morale = sum(p["morale"] for p in players) / max(1, len(players))
        occ = clamp(0.55 + 0.004 * (avg_morale - 70), 0.40, 0.95)
        price = 12
        attendance = int(cap * occ)
        ticket_revenue = int(attendance * price)
        # Bãi đỗ xe: chứa được nhiều xe CĐV hơn, tăng thu nhập đỗ xe
        p_cap = eff.get("parking_capacity", 700)
        p_fee = eff.get("parking_fee", 25)
        parking_revenue = int(min(p_cap, int(attendance * 0.35)) * p_fee)
        # Khu ẩm thực: thu được nhiều tiền hơn của CĐV đến sân
        food_per_fan = eff.get("food_income_per_fan", 15)
        food_revenue = int(attendance * food_per_fan)

    conn.execute("UPDATE fixture SET attendance=? WHERE id=?",(attendance,fid))
    base_cash = {"League": 65_000, "ApexCup": 75_000, "Cup": 75_000, "Champions": 100_000, "Europa": 85_000, "SuperCup": 85_000}.get(comp, 65_000)
    token_reward = {"League": 0, "ApexCup": 1, "Cup": 1, "Champions": 2, "Europa": 1, "SuperCup": 1}.get(comp, 0)

    # match result bonus
    my_goals = home_goals if fix["home"] == my else away_goals
    opp_goals = away_goals if fix["home"] == my else home_goals
    if my_goals > opp_goals:
        base_cash = int(base_cash * 1.25)
    elif my_goals == opp_goals:
        base_cash = int(base_cash * 1.05)
    else:
        base_cash = int(base_cash * 0.95)

    cash_gain = base_cash + ticket_revenue + parking_revenue + food_revenue
    db_exec(conn, "UPDATE club SET cash=cash+?, tokens=tokens+? WHERE id=1", (cash_gain, token_reward))

    # Apply fatigue & chance of injuries (traits may affect both)
    pressing = club["pressing"]
    fatigue = 35 if pressing == "low" else (45 if pressing == "high" else 40)
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

    rules.event(conn,"match_reward",f"Trận {fid}; khán giả {attendance}",cash_gain,token_reward)
    conn.close()

    msg = f"Kết quả: {fix['home']} {home_goals} - {away_goals} {fix['away']}. +{fmt_money(cash_gain)} tiền"
    if token_reward:
        msg += f", +{token_reward} token"
    if injured_name:
        msg += f". Chấn thương: {injured_name}"
    flash(msg)
    return redirect(url_for("competitions"))

@app.post("/reset_season")
@app.post("/season/next")
def reset_season():
    conn=get_db()
    club=db_one(conn,"SELECT * FROM club")
    for comp in ("ApexCup","Champions","Europa"):
        progress_knockout(conn,comp)
    if club["season_day"]<club["season_length"] or db_one(conn,"SELECT 1 FROM fixture WHERE played=0 LIMIT 1"):
        flash("Chỉ bắt đầu mùa mới sau khi hoàn thành tất cả trận đấu và hết kỳ nghỉ cuối mùa.")
        return redirect(url_for("home"))
    table=compute_league_table(conn)
    rank=next(r["rank"] for r in table if r["is_user"])
    before=club["club_level"]
    after=before+int(rank<=8)
    titles={"league_titles":int(rank==1),"champions_titles":0,"cup_titles":0,"supercup_titles":0,"ultimate_titles":0}
    for comp,key in (("Champions","champions_titles"),("ApexCup","cup_titles"),("SuperCup","supercup_titles")):
        final=db_one(conn,"SELECT * FROM fixture WHERE competition=? ORDER BY round DESC,leg DESC LIMIT 1",(comp,))
        if final and final["group_name"]=="Chung kết":
            titles[key]=int(final["winner"]==club["name"])
    summary=compute_club_season_stats(conn,club)
    conn.execute("INSERT INTO season_record VALUES (?,?,?,?,?,?,?)",(club["season_number"],rank,before,after,json.dumps(summary,ensure_ascii=False),json.dumps(rules.club_matches(conn),ensure_ascii=False),json.dumps(titles)))
    conn.execute("INSERT INTO player_stat_archive SELECT *,? FROM player_match_stat",(club["season_number"],))
    conn.execute("""UPDATE club SET season_number=season_number+1, season_day=0, season_length=?, club_level=?,
        last_season_rank=?,last_friendly_day=-1,daily_gift_claimed_day=-1,daily_offer_bought_day=-1,
        training_points_today=0,last_wage_day=0""",(rules.SEASON_DAYS,after,rank))
    retired=rules.age_squad_for_new_season(conn)
    for name in retired:
        rules.event(conn,"retirement",f"{name} đã giải nghệ; lịch sử thi đấu được giữ lại")
    conn.execute("DELETE FROM ai_player")
    generate_season(conn)
    rules.ensure_ai_rosters(conn,gen_name)
    get_or_regen_youth_squad(conn,force=True)
    shortage=max(0,11-db_one(conn,"SELECT COUNT(*) FROM squad_player")[0])
    if shortage:
        # Emergency promotion uses the actual new academy cohort, with its stored attributes.
        for youth in db_all(conn,"SELECT * FROM youth_player WHERE is_signed=0 ORDER BY overall ASC LIMIT ?",(shortage,)):
            pid=db_insert(conn,"""INSERT INTO squad_player(name,position,age,finishing,passing,pace,tackling,marking,stamina_skill,
                traits,potential,condition,morale,form,injured_until_day,salary,height,weight,preferred_foot,special_skill,nationality)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                (youth["name"],youth["position"],youth["age"],*[youth[k] for k in rules.SKILLS],"",youth["potential"],youth["condition"],75,65,
                 int(1000+player_overall(youth)*90),youth["height"],youth["weight"],youth["preferred_foot"],youth["special_skill"],youth["nationality"]))
            conn.execute("UPDATE youth_player SET is_signed=1 WHERE id=?",(youth["id"],))
            rules.event(conn,"emergency_promotion",f"Đôn {youth['name']} để đủ 11 cầu thủ sau giải nghệ")
    rules.initialize_lifecycle(conn,now_ts())
    # Store absolute injury recovery relative to the new season's day counter.
    conn.execute("UPDATE squad_player SET injured_until_day=MAX(0,injured_until_day-?)",(club["season_day"],))
    conn.execute("DELETE FROM scout_listing")
    conn.execute("DELETE FROM assistant_recommendation")
    db_exec(conn,"UPDATE club SET market_reset_at=?",(now_ts()+MARKET_BATCH_SECONDS,))
    regen_market_batch(conn,now_ts()+MARKET_BATCH_SECONDS)
    rules.event(conn,"season_start",f"Kết thúc mùa trước hạng {rank}; cấp {before} → {after}")
    conn.close()
    flash(f"Mùa trước kết thúc hạng {rank}/20. " + (f"Thăng lên cấp {after}!" if rank<=8 else f"Giữ cấp {before}.") + f" Đã lưu lịch sử và bắt đầu mùa mới; toàn đội tăng 1 tuổi, giảm 20% kỹ năng. {len(retired)} cầu thủ giải nghệ.")
    return redirect(url_for("home"))


# Keep every game mutation atomic; authentication has its own account transaction.
for _rule in app.url_map.iter_rules():
    if "POST" in _rule.methods and not _rule.endpoint.startswith("auth."):
        _view=app.view_functions[_rule.endpoint]
        if not getattr(_view,"_game_atomic",False):
            _wrapped=rules.atomic_action(_view,get_db)
            _wrapped._game_atomic=True
            app.view_functions[_rule.endpoint]=_wrapped


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
