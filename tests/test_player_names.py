import unittest

from app import NAT_FLAGS, gen_player
from player_names import NAME_POOLS, generate_identity


class PlayerNamesTests(unittest.TestCase):
    def test_all_nationalities_have_flags_and_varied_names(self):
        self.assertEqual(set(NAME_POOLS), set(NAT_FLAGS))
        self.assertTrue({"VN", "EN", "FR", "DE", "ES", "IT"}.issubset(NAME_POOLS))
        for nationality, (first, second) in NAME_POOLS.items():
            names = {f"{a} {b}" for a in first for b in second}
            self.assertGreaterEqual(len(names), 250, nationality)
            self.assertLessEqual(max(map(len, names)), 28, nationality)
            for _ in range(5):
                name, actual_nationality = generate_identity(nationality)
                self.assertEqual(actual_nationality, nationality)
                self.assertIn(name, names)

    def test_player_generator_keeps_requested_name_and_nationality_together(self):
        for nationality, (first, second) in NAME_POOLS.items():
            player = gen_player("MC", 55, 8, nationality=nationality)
            self.assertEqual(player["nationality"], nationality)
            self.assertIn(player["name"], {f"{a} {b}" for a in first for b in second})


if __name__ == "__main__":
    unittest.main()
