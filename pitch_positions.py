"""Shared pitch regions, in percentages from the left goal towards the right."""
import math

MIN_X, MAX_X = 3, 97
MIN_Y, MAX_Y = 5, 95
# Adjacent regions use half-open intervals, so boundaries have one owner.
POSITION_ZONES = [
    {"position": "GK", "x1": 0, "x2": 18, "y1": 25, "y2": 75},
    {"position": "DL", "x1": 0, "x2": 36, "y1": 0, "y2": 25},
    {"position": "DR", "x1": 0, "x2": 36, "y1": 75, "y2": 100},
    {"position": "DC", "x1": 18, "x2": 36, "y1": 25, "y2": 75},
    {"position": "DMC", "x1": 36, "x2": 44, "y1": 25, "y2": 75},
    {"position": "MC", "x1": 44, "x2": 58, "y1": 25, "y2": 75},
    {"position": "ML", "x1": 36, "x2": 58, "y1": 0, "y2": 25},
    {"position": "MR", "x1": 36, "x2": 58, "y1": 75, "y2": 100},
    {"position": "AMC", "x1": 58, "x2": 70, "y1": 25, "y2": 75},
    {"position": "AML", "x1": 58, "x2": 100, "y1": 0, "y2": 25},
    {"position": "AMR", "x1": 58, "x2": 100, "y1": 75, "y2": 100},
    {"position": "ST", "x1": 70, "x2": 100, "y1": 25, "y2": 75},
]


def position_at(x, y):
    if not (math.isfinite(x) and math.isfinite(y) and MIN_X <= x <= MAX_X and MIN_Y <= y <= MAX_Y):
        raise ValueError("Vị trí nằm ngoài sân.")
    return next(zone["position"] for zone in POSITION_ZONES
                if zone["x1"] <= x < zone["x2"] and zone["y1"] <= y < zone["y2"])
