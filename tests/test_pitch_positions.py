import unittest

from app import HORIZONTAL_FORMATION_LAYOUT, formation_slots
from pitch_positions import POSITION_ZONES, position_at


class PitchPositionsTests(unittest.TestCase):
    def test_presets_match_their_position_regions(self):
        for formation, coordinates in HORIZONTAL_FORMATION_LAYOUT.items():
            for position, (x, y) in zip(formation_slots(formation), coordinates):
                self.assertEqual(position_at(x, y), position, (formation, x, y))

    def test_regions_cover_playable_pitch_without_gaps_or_overlaps(self):
        for x in range(3, 98):
            for y in range(5, 96):
                matches = [z for z in POSITION_ZONES if z['x1'] <= x < z['x2'] and z['y1'] <= y < z['y2']]
                self.assertEqual(len(matches), 1, (x, y))

    def test_forward_changes_role_at_midfield_and_wing_boundaries(self):
        self.assertEqual(position_at(80, 50), 'ST')
        self.assertEqual(position_at(70, 50), 'ST')
        self.assertEqual(position_at(69.99, 50), 'AMC')
        self.assertEqual(position_at(80, 24.99), 'AML')
        self.assertEqual(position_at(80, 75), 'AMR')
        self.assertEqual(position_at(50, 50), 'MC')
        self.assertEqual(position_at(40, 50), 'DMC')
        self.assertEqual(position_at(25, 50), 'DC')
        self.assertEqual(position_at(10, 50), 'GK')
