import json
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import g

import game_rules as rules
from app import (app, auto_play_ai_fixtures, compute_club_history_stats,
                 compute_club_season_stats, compute_player_leaderboards, facility_effects,
                 player_effective_rating, player_overall, progress_knockout,
                 record_fixture_player_stats, regen_market_batch, regen_scout_batch,
                 get_or_regen_assistant_recommendations, get_or_regen_youth_squad,
                 format_youth_player_dict, starting_xi_from_saved)


class GameRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = tempfile.TemporaryDirectory()
        original = app.config.copy()
        app.config.update(TESTING=True, DATA_DIR=cls.seed.name, SECRET_KEY="rules-test-key")
        client=app.test_client()
        page=client.get("/register").get_data(as_text=True)
        token=re.search(r'name="csrf_token" value="([^"]+)"',page)[1]
        response=client.post("/register", data=dict(csrf_token=token,username="manager",club_name="Real FC",password="secret-password",confirm_password="secret-password"))
        if response.status_code!=302:
            raise AssertionError(response.get_data(as_text=True))
        client.get("/")
        with client.session_transaction() as session:
            cls.seed_session=dict(session)
        app.config.clear()
        app.config.update(original)

    @classmethod
    def tearDownClass(cls):
        cls.seed.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        shutil.copytree(Path(self.seed.name),self.root,dirs_exist_ok=True)
        self.original=app.config.copy()
        app.config.update(TESTING=True,DATA_DIR=str(self.root),SECRET_KEY="rules-test-key")
        self.client=app.test_client()
        with self.client.session_transaction() as session:
            session.update(self.seed_session)
        self.conn=sqlite3.connect(next((self.root/"games").glob("*.sqlite3")),factory=rules.GameConnection)
        self.conn.row_factory=sqlite3.Row

    def tearDown(self):
        self.conn.close()
        app.config.clear()
        app.config.update(self.original)
        self.temp.cleanup()

    def post(self,path,data=None):
        return self.client.post(path,data={"csrf_token":self.seed_session["csrf_token"],**(data or {})})

    def player(self,pid=1):
        return self.conn.execute("SELECT * FROM squad_player WHERE id=?",(pid,)).fetchone()

    def club(self):
        return self.conn.execute("SELECT * FROM club").fetchone()

    def set_player(self,pid=1,**values):
        self.conn.execute("UPDATE squad_player SET "+",".join(k+"=?" for k in values)+" WHERE id=?",(*values.values(),pid))
        self.conn.commit()

    def play_first_match(self):
        fixture=self.conn.execute("SELECT * FROM fixture WHERE competition='League' AND (home='Real FC' OR away='Real FC') ORDER BY day LIMIT 1").fetchone()
        self.conn.execute("UPDATE club SET season_day=?",(fixture["day"],))
        self.conn.commit()
        with patch("app.simulate_match",return_value=(2,1)):
            self.assertEqual(self.post(f"/play/{fixture['id']}").status_code,302)
        return self.conn.execute("SELECT * FROM fixture WHERE id=?",(fixture["id"],)).fetchone()

    def complete_season(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.deferred=True
        try:
            squad=list(self.conn.execute("SELECT * FROM squad_player LIMIT 11"))
            for _ in range(200):
                auto_play_ai_fixtures(self.conn,2000,"Real FC")
                f=self.conn.execute("SELECT * FROM fixture WHERE played=0 AND (home='Real FC' OR away='Real FC') ORDER BY day,id LIMIT 1").fetchone()
                if f is None:
                    break
                hg,ag=(2,0) if f["home"]=="Real FC" else (0,2)
                self.conn.execute("UPDATE fixture SET played=1,home_goals=?,away_goals=?,attendance=? WHERE id=?",(hg,ag,900 if f["home"]=="Real FC" else 0,f["id"]))
                record_fixture_player_stats(self.conn,f["id"],f["competition"],f["home"],f["away"],hg,ag,"Real FC",squad)
                progress_knockout(self.conn,f["competition"])
            self.conn.execute("UPDATE club SET season_day=season_length")
            sqlite3.Connection.commit(self.conn)
        finally:
            self.conn.deferred=False
        self.assertIsNone(self.conn.execute("SELECT 1 FROM fixture WHERE played=0").fetchone())

    def test_initial_state_contains_no_achievements_or_fake_identity(self):
        club=self.club()
        self.assertEqual(club["club_level"],1)
        self.assertEqual((club["cash"],club["tokens"]),(2_000_000,20))
        self.assertEqual(club["manager_name"],"manager")
        self.assertEqual(club["training_points_today"],0)
        self.assertEqual(set(r[0] for r in self.conn.execute("SELECT level FROM facility")),{1})
        season=compute_club_season_stats(self.conn,club)
        history=compute_club_history_stats(self.conn,club,season)
        for key in ("wins","draws","losses","goals_for","goals_against","avg_attendance"):
            self.assertEqual(season[key],0,key)
            self.assertEqual(history[key],0,key)
        self.assertEqual(history["league_titles"],0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM player_match_stat").fetchone()[0],0)
        html=self.client.get("/").get_data(as_text=True)
        self.assertIn("Cấp 1 🛡️",html)
        self.assertNotIn("Cấp 8 (Huyền Thoại)",html)
        self.assertNotIn("GIẢI ROSHN SAUDI",html)
        self.assertIn("0/273",html)

    def test_calendar_has_complete_league_and_three_days_between_games(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixture WHERE competition='League'").fetchone()[0],380)
        by_team={}
        for f in self.conn.execute("SELECT * FROM fixture ORDER BY day"):
            for team in (f["home"],f["away"]):
                days=by_team.setdefault(team,[])
                if days:
                    self.assertGreaterEqual(f["day"]-days[-1],3,(team,days[-1],f["day"]))
                days.append(f["day"])
        self.assertEqual(len(self.conn.execute("SELECT DISTINCT round FROM fixture WHERE competition='League'").fetchall()),38)

    def test_real_match_statistics_and_attendance_do_not_change_on_refresh(self):
        self.post("/lineup/autopick")
        before=[dict(p) for p in self.conn.execute("SELECT * FROM squad_player")]
        f=self.play_first_match()
        stats=list(self.conn.execute("SELECT * FROM player_match_stat WHERE fixture_id=? AND is_user_player=1",(f["id"],)))
        self.assertEqual(sum(p["appearances"] for p in stats),11)
        self.assertEqual(sum(p["goals"] for p in stats),2)
        self.assertTrue(all(p["player_id"] is not None for p in stats))
        experienced=sum(p[0] for p in self.conn.execute("SELECT experience_matches FROM squad_player"))
        self.assertEqual(experienced,11)
        self.assertTrue(any(self.player(p["id"])["condition"] <= p["condition"]-35 for p in before))
        club=self.club()
        snapshot=compute_club_season_stats(self.conn,club)
        for _ in range(2):
            self.assertEqual(snapshot,compute_club_season_stats(self.conn,club))
        self.post(f"/play/{f['id']}")
        self.assertEqual(sum(p[0] for p in self.conn.execute("SELECT experience_matches FROM squad_player")),11)

    def test_training_persists_and_repeated_posts_cannot_inflate_skills(self):
        self.set_player(condition=100,potential=100,**{k:55 for k in rules.SKILLS})
        before=dict(self.player())
        self.post("/training",dict(quick_mode="hard",player_ids="1"))
        after=dict(self.player())
        self.assertGreater(player_overall(after),player_overall(before))
        self.assertLess(after["condition"],before["condition"])
        for _ in range(12):
            self.post("/training",dict(quick_mode="hard",player_ids="1"))
        final=dict(self.player())
        self.assertLessEqual(sum(final[k]-before[k] for k in rules.SKILLS),rules.MAX_DAILY_SKILL_GAIN+.001)
        # A new browser session reads the same trained attributes.
        other=app.test_client()
        with other.session_transaction() as session:
            session.update(self.seed_session)
        self.assertEqual(other.get("/player/1").status_code,200)
        self.assertEqual(final["finishing"],self.player()["finishing"])

    def test_training_difficulty_and_age_control_growth_but_old_potential_does_not(self):
        for pid in (1,2,3,4):
            self.set_player(pid,condition=100,age=18,potential=100,**{k:55 for k in rules.SKILLS})
        self.set_player(3,age=34)
        self.set_player(4,potential=55)
        self.post("/training",dict(quick_mode="easy",player_ids="1"))
        self.post("/training",dict(quick_mode="hard",player_ids=["2","3","4"]))
        gain=lambda pid:sum(self.player(pid)[k]-55 for k in rules.SKILLS)
        self.assertGreater(gain(2),gain(1))
        self.assertGreater(gain(2),gain(3))
        self.assertGreater(gain(4),0)
        self.assertAlmostEqual(gain(4),gain(2),places=4)
        self.assertLess(self.player(2)["condition"],self.player(1)["condition"])

    def test_individual_and_youth_training_can_exceed_legacy_potential(self):
        self.set_player(condition=100,potential=61,**{k:70 for k in rules.SKILLS})
        self.post("/player/individual_train/1",dict(target_cltt="200"))
        self.assertTrue(all(self.player()[k]>70 for k in rules.SKILLS))
        self.assertEqual(self.player()["potential"],61)
        self.client.get("/youth")
        yid=self.conn.execute("SELECT id FROM youth_player WHERE is_signed=0 LIMIT 1").fetchone()[0]
        self.conn.execute("UPDATE youth_player SET potential=61,condition=100,"+
                          ",".join(k+"=70" for k in rules.SKILLS)+" WHERE id=?",(yid,))
        self.conn.commit()
        self.post(f"/youth/train/{yid}",dict(target_cltt="200"))
        youth=self.conn.execute("SELECT * FROM youth_player WHERE id=?",(yid,)).fetchone()
        self.assertTrue(all(youth[k]>70 for k in rules.SKILLS))
        self.assertEqual(youth["potential"],61)

    def test_training_can_reach_exactly_two_hundred_then_stops_without_cost(self):
        self.set_player(condition=100,potential=61,**{k:199.999 for k in rules.SKILLS})
        self.post("/player/individual_train/1",dict(target_cltt="200"))
        self.assertTrue(all(self.player()[k]==200 for k in rules.SKILLS))
        stamina=self.player()["condition"]
        tokens=self.club()["tokens"]
        self.post("/player/individual_train/1",dict(target_cltt="201"))
        self.post("/training",dict(quick_mode="hard",player_ids="1"))
        self.assertTrue(all(self.player()[k]==200 for k in rules.SKILLS))
        self.assertEqual(self.player()["condition"],stamina)
        self.assertEqual(self.club()["tokens"],tokens)

    def test_under_twenty_stamina_cannot_train_and_one_pack_restores_ten(self):
        self.set_player(condition=19,potential=100,**{k:55 for k in rules.SKILLS})
        before=dict(self.player())
        self.post("/training",dict(quick_mode="easy",player_ids="1"))
        self.assertEqual(self.player()["finishing"],before["finishing"])
        self.assertEqual(self.player()["condition"],19)
        packs=self.club()["rest_packs"]
        self.post("/training/heal",dict(player_id="1"))
        self.assertEqual(self.player()["condition"],29)
        self.assertEqual(self.club()["rest_packs"],packs-1)
        self.post("/training",dict(quick_mode="easy",player_ids="1"))
        self.assertGreater(player_overall(self.player()),player_overall(before))
        morale_packs=self.club()["morale_packs"]
        self.set_player(morale=50)
        self.post("/training/morale/1")
        self.assertEqual(self.player()["morale"],60)
        self.assertEqual(self.club()["morale_packs"],morale_packs-1)

    def test_lineup_swaps_between_pitch_bench_and_reserves_persist(self):
        assignments={"slot_0":"1","slot_1":"2","bench_0":"3","bench_1":"4"}
        self.assertEqual(self.post("/lineup/save",assignments).status_code,302)
        assignments.update(slot_0="3",bench_0="1")
        self.post("/lineup/save",assignments)
        self.assertEqual(self.conn.execute("SELECT player_id FROM lineup_slot WHERE slot_index=0 AND formation='4-4-2'").fetchone()[0],3)
        self.assertEqual(self.player(1)["bench_slot"],1)
        self.assertEqual(self.player(3)["bench_slot"],0)
        assignments.update(bench_1="5")
        self.post("/lineup/save",assignments)
        self.assertEqual(self.player(4)["bench_slot"],0)
        self.assertEqual(self.player(5)["bench_slot"],2)
        assignments.update(bench_0="",bench_1="")
        self.post("/lineup/save",assignments)
        self.assertEqual(self.club()["bench_initialized"],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM squad_player WHERE bench_slot>0").fetchone()[0],0)
        html=self.client.get("/lineup").get_data(as_text=True)
        self.assertIn("ĐỘI HÌNH DỰ BỊ (0)",html)
        self.assertIn("ĐỘI HÌNH KHÔNG ĐĂNG KÝ (16)",html)
        self.assertNotIn("switchBenchTab",html)
        # A captain may occupy a high-index slot while other positions are empty.
        self.post("/lineup/save",{"slot_7":"9","captain_slot":"7"})
        self.assertEqual(self.client.get("/lineup").status_code,200)

    def test_lineup_rejects_duplicate_overlapping_or_unowned_bench_before_changes(self):
        self.post("/lineup/save",{"slot_0":"1","bench_0":"2"})
        for invalid in ({"slot_0":"1","bench_0":"1"},
                        {"slot_0":"3","bench_0":"2","bench_1":"2"},
                        {"slot_0":"3","bench_0":"99999"},
                        {"slot_0":"3","bench_0":"wrong"}):
            self.assertEqual(self.post("/lineup/save",invalid).status_code,400)
            self.assertEqual(self.player(2)["bench_slot"],1)
            self.assertEqual(self.conn.execute("SELECT player_id FROM lineup_slot WHERE slot_index=0 AND formation='4-4-2'").fetchone()[0],1)

    def test_team_popup_uses_saved_player_data_without_transfer_offer_tab(self):
        self.set_player(name="Lorenzo Moretti",shirt_number=27,finishing=123.5,experience_matches=12)
        html=self.client.get("/lineup").get_data(as_text=True)
        profiles=json.loads(re.search(r'id="squadPlayerProfiles">(.*?)</script>',html,re.S)[1])
        player=next(p for p in profiles if p["id"]==1)
        self.assertEqual(player["name"],"Lorenzo Moretti")
        self.assertEqual(player["shirt_number"],27)
        self.assertEqual(player["experience_matches"],12)
        self.assertEqual(player["career_stats"]["career"]["goals_total"],0)
        self.assertIn('id="squadPlayerModal"',html)
        self.assertNotIn('data-tab="bids"',html)
        self.assertNotIn('window.location.assign',html)
        self.assertIn('data-tab="bids"',self.client.get("/market").get_data(as_text=True))

    def test_free_pitch_position_persists_and_changes_match_role_not_natural_position(self):
        self.set_player(position="ST",injured_until_day=0)
        self.post("/lineup/save",{"slot_9":"1","x_9":"82.125","y_9":"56.5"})
        self.post("/lineup/save",{"slot_9":"1","x_9":"64.125","y_9":"50.5"})
        row=self.conn.execute("SELECT * FROM lineup_slot WHERE formation='4-4-2' AND slot_index=9").fetchone()
        self.assertEqual((row["pitch_x"],row["pitch_y"],row["position"]),(64.125,50.5,"AMC"))
        self.assertEqual(self.player()["position"],"ST")
        self.assertEqual(next(role for p,role in starting_xi_from_saved(self.conn,0,"4-4-2") if p["id"]==1),"AMC")
        self.assertIn('left: 64.125%; top: 50.5%;',self.client.get("/lineup").get_data(as_text=True))
        # Old clients that only submit assignments must preserve the custom coordinates.
        self.post("/lineup/save",{"slot_9":"1"})
        self.assertEqual(self.conn.execute("SELECT position FROM lineup_slot WHERE formation='4-4-2' AND slot_index=9").fetchone()[0],"AMC")
        self.post("/lineup/autopick")
        row=self.conn.execute("SELECT * FROM lineup_slot WHERE formation='4-4-2' AND slot_index=9").fetchone()
        self.assertEqual(row["position"],"ST")
        self.assertIsNone(row["pitch_x"])

    def test_invalid_pitch_coordinates_cannot_change_saved_lineup(self):
        self.post("/lineup/save",{"slot_9":"1","x_9":"80","y_9":"50"})
        for invalid in ({"x_9":"nan","y_9":"50"},{"x_9":"inf","y_9":"50"},
                        {"x_9":"120","y_9":"50"},{"x_9":"50","y_9":"0"},
                        {"x_9":"60"},{"x_9":"bad","y_9":"50"}):
            self.assertEqual(self.post("/lineup/save",{"slot_9":"2",**invalid}).status_code,400)
            row=self.conn.execute("SELECT * FROM lineup_slot WHERE formation='4-4-2' AND slot_index=9").fetchone()
            self.assertEqual((row["player_id"],row["pitch_x"],row["pitch_y"],row["position"]),(1,80,50,"ST"))

    def test_recovery_every_eight_hours_is_offline_and_idempotent(self):
        clock=int(time.time())
        self.set_player(condition=40,condition_clock=clock)
        with patch("app.now_ts",return_value=clock+rules.RECOVERY_INTERVAL-1):
            self.client.get("/training")
        self.assertEqual(self.player()["condition"],40)
        with patch("app.now_ts",return_value=clock+rules.RECOVERY_INTERVAL):
            self.client.get("/training")
            self.client.get("/training")
        self.assertEqual(self.player()["condition"],50)
        with patch("app.now_ts",return_value=clock+3*rules.RECOVERY_INTERVAL):
            self.client.get("/training")
        self.assertEqual(self.player()["condition"],70)

    def test_advance_simulated_day_recovers_thirty_not_arbitrary_bonus(self):
        clock=int(time.time())
        self.set_player(condition=40,condition_clock=clock)
        with patch("app.now_ts",return_value=clock):
            self.post("/advance_day")
        self.assertEqual(self.player()["condition"],70)
        self.assertEqual(self.club()["clock_offset_seconds"],86400)

    def test_stamina_at_full_cannot_bank_extra_recovery(self):
        clock=int(time.time())
        self.set_player(condition=100,condition_clock=clock-86400)
        with patch("app.now_ts",return_value=clock):
            self.post("/training",dict(quick_mode="easy",player_ids="1"))
            before=self.player()["condition"]
            self.client.get("/training")
        self.assertEqual(self.player()["condition"],before)

    def test_veteran_experience_affects_performance_not_raw_skill(self):
        self.set_player(age=19,experience_matches=0,seasons_played=0,condition=100,morale=100,form=100)
        young=dict(self.player())
        old={**young,"age":33,"experience_matches":200,"seasons_played":8}
        self.assertEqual(player_overall(young),player_overall(old))
        self.assertGreater(player_effective_rating(old,0),player_effective_rating(young,0))
        self.assertLessEqual(rules.experience_multiplier(old),1.2)

    def test_individual_training_cannot_buy_arbitrary_target_or_potential(self):
        p=dict(self.player())
        self.post("/player/individual_train/1",dict(target_cltt="200"))
        after=self.player()
        self.assertEqual(after["potential"],p["potential"])
        self.assertLessEqual(sum(after[k]-p[k] for k in rules.SKILLS),rules.MAX_DAILY_SKILL_GAIN+.001)
        self.assertEqual(self.post("/player/individual_train/1",dict(target_cltt="nan")).status_code,400)

    def test_new_season_top_eight_ageing_and_twenty_percent_decline_once(self):
        self.complete_season()
        self.set_player(retirement_age=45)
        before=dict(self.player())
        cash=self.club()["cash"]
        self.post("/season/next")
        after=self.player()
        self.assertEqual(self.club()["club_level"],2)
        self.assertEqual(self.club()["season_number"],2)
        self.assertEqual(after["age"],before["age"]+1)
        for key in rules.SKILLS:
            self.assertAlmostEqual(after[key],before[key]*.8,places=5)
        self.assertEqual(self.club()["cash"],cash)
        self.post("/season/next")
        self.assertEqual(self.player()["age"],after["age"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM season_record").fetchone()[0],1)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM player_stat_archive").fetchone()[0],0)

    def test_rank_nine_does_not_gain_a_level(self):
        self.complete_season()
        with patch("app.compute_league_table",return_value=[dict(rank=9,is_user=True)]):
            self.post("/season/next")
        self.assertEqual(self.club()["club_level"],1)
        self.assertEqual(self.club()["last_season_rank"],9)

    def test_personal_retirement_archives_player_and_clears_lineup(self):
        self.complete_season()
        p=self.player()
        self.set_player(retirement_age=p["age"]+1)
        self.post("/lineup/autopick")
        self.post("/season/next")
        self.assertIsNone(self.player())
        retired=self.conn.execute("SELECT * FROM retired_player WHERE player_id=1").fetchone()
        self.assertIsNotNone(retired)
        self.assertEqual(retired["age"],p["age"]+1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM lineup_slot WHERE player_id=1").fetchone())
        self.assertGreater(len(set(row[0] for row in self.conn.execute("SELECT retirement_age FROM squad_player"))),1)

    def test_all_cups_progress_through_finals_with_recovery_days(self):
        self.complete_season()
        for comp,round_no in (("ApexCup",6),("Champions",10),("Europa",9)):
            final=self.conn.execute("SELECT * FROM fixture WHERE competition=? AND round=? ORDER BY leg DESC LIMIT 1",(comp,round_no)).fetchone()
            self.assertIsNotNone(final,comp)
            self.assertIsNotNone(final["winner"],comp)
        by_team={}
        for f in self.conn.execute("SELECT * FROM fixture ORDER BY day,id"):
            for team in (f["home"],f["away"]):
                previous=by_team.get(team)
                if previous is not None:
                    self.assertGreaterEqual(f["day"]-previous,3,(team,previous,f["day"]))
                by_team[team]=f["day"]

    def test_upgrade_benefits_apply_only_after_build_and_no_cash_bypass(self):
        before=facility_effects(self.conn)["capacity"]
        cash=self.club()["cash"]
        self.post("/upgrade_instant/stadium")
        self.assertEqual(self.club()["cash"],cash)
        self.assertEqual(facility_effects(self.conn)["capacity"],before)
        self.post("/upgrade/stadium")
        self.assertLess(self.club()["cash"],cash)
        self.assertEqual(facility_effects(self.conn)["capacity"],before)
        self.post("/advance_day")
        self.client.get("/facilities")
        self.assertGreater(facility_effects(self.conn)["capacity"],before)

    def test_free_token_and_early_reset_exploits_are_closed(self):
        before=dict(self.club())
        for _ in range(3):
            self.post("/resource/exchange",dict(type="token"))
            self.post("/reset_season")
        self.assertEqual(self.club()["tokens"],before["tokens"])
        self.assertEqual(self.club()["cash"],before["cash"])
        self.assertEqual(self.club()["season_number"],1)

    def test_daily_gift_has_no_skill_boost_and_cannot_repeat(self):
        players=[tuple(p) for p in self.conn.execute("SELECT "+",".join(rules.SKILLS)+" FROM squad_player ORDER BY id")]
        self.post("/claim_daily_gift")
        packs=self.club()["rest_packs"]
        self.post("/claim_daily_gift")
        self.assertEqual(self.club()["rest_packs"],packs)
        self.assertEqual(players,[tuple(p) for p in self.conn.execute("SELECT "+",".join(rules.SKILLS)+" FROM squad_player ORDER BY id")])

    def test_offer_is_persisted_and_purchase_keeps_previewed_attributes(self):
        record=self.conn.execute("SELECT * FROM daily_player_offer").fetchone()
        p=json.loads(record["player_json"])
        self.client.get("/")
        self.assertEqual(record["player_json"],self.conn.execute("SELECT player_json FROM daily_player_offer").fetchone()[0])
        self.post("/buy_daily_offer")
        bought=self.conn.execute("SELECT * FROM squad_player ORDER BY id DESC LIMIT 1").fetchone()
        for key in ("name","nationality","age","position",*rules.SKILLS):
            self.assertEqual(bought[key],p[key],key)

    def test_international_names_and_nationalities_are_persisted_across_sources(self):
        with patch("app.generate_identity",return_value=("Lorenzo Moretti","IT")):
            regen_market_batch(self.conn,int(time.time())+180)
            regen_scout_batch(self.conn,int(time.time())+172800)
            get_or_regen_assistant_recommendations(self.conn)
            get_or_regen_youth_squad(self.conn,force=True)
        for table in ("market_listing","scout_listing","assistant_recommendation","youth_player"):
            players=list(self.conn.execute(f"SELECT * FROM {table}"))
            self.assertTrue(players,table)
            self.assertTrue(all(p["name"]=="Lorenzo Moretti" and p["nationality"]=="IT" for p in players),table)
        youth=self.conn.execute("SELECT * FROM youth_player LIMIT 1").fetchone()
        preview=format_youth_player_dict(self.conn,youth)
        self.assertEqual(preview["flag"],"🇮🇹")
        self.assertEqual(json.loads(preview["json_data"])["flag"],"🇮🇹")
        for path in ("/market","/youth"):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.conn.execute("SELECT name FROM youth_player WHERE id=?",(youth["id"],)).fetchone()[0],youth["name"])

    def test_youth_signing_does_not_regenerate_or_restore_condition(self):
        y=self.conn.execute("SELECT * FROM youth_player LIMIT 1").fetchone()
        self.conn.execute("UPDATE youth_player SET condition=30 WHERE id=?",(y["id"],))
        self.conn.commit()
        self.post(f"/youth/sign/{y['id']}")
        self.client.get("/youth")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM youth_player").fetchone()[0],11)
        promoted=self.conn.execute("SELECT * FROM squad_player ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(promoted["condition"],30)
        self.assertEqual(promoted["retirement_age"],y["retirement_age"])
        self.assertEqual((promoted["name"],promoted["nationality"]),(y["name"],y["nationality"]))

    def test_invalid_recovery_and_cure_do_not_consume_packs(self):
        before=dict(self.club())
        self.post("/training/heal",dict(player_id="99999"))
        self.assertEqual(self.post("/training/cure_injury/99999").status_code,404)
        self.post("/training/cure_injury/1")
        self.assertEqual(self.club()["rest_packs"],before["rest_packs"])
        self.assertEqual(self.club()["injury_packs"],before["injury_packs"])

    def test_mass_retirement_cannot_leave_club_unable_to_field_eleven(self):
        self.complete_season()
        self.conn.execute("UPDATE squad_player SET retirement_age=age+1")
        self.conn.commit()
        self.post("/season/next")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM retired_player").fetchone()[0],18)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM squad_player").fetchone()[0],11)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM youth_player WHERE is_signed=1").fetchone()[0],11)

    def test_legacy_migration_removes_only_sample_history_and_backs_up_once(self):
        self.conn.execute("UPDATE club SET rules_version=0,club_level=8,hist_wins=278")
        self.conn.commit()
        before=self.player()["finishing"]
        self.client.get("/club")
        self.assertEqual(self.club()["club_level"],1)
        self.assertEqual(self.club()["hist_wins"],0)
        self.assertEqual(self.player()["finishing"],before)
        self.assertEqual(len(list((self.root/"games").glob("*.before-rules.sqlite3"))),1)
        self.conn.execute("UPDATE club SET club_level=2")
        self.conn.commit()
        self.client.get("/club")
        self.assertEqual(self.club()["club_level"],2)

    def test_failed_action_rolls_back_money_and_player_changes(self):
        self.complete_season()
        cash=self.club()["cash"]
        age=self.player()["age"]
        with patch("app.generate_season",side_effect=RuntimeError("simulated failure")):
            with self.assertRaisesRegex(RuntimeError,"simulated"):
                self.post("/season/next")
        self.assertEqual(self.club()["cash"],cash)
        self.assertEqual(self.club()["season_number"],1)
        self.assertEqual(self.player()["age"],age)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM season_record").fetchone()[0],0)

    def test_json_csrf_accepts_valid_tokens_and_rejects_wrong_shapes(self):
        token=self.seed_session["csrf_token"]
        response=self.client.post("/training",json={"csrf_token":token,"quick_mode":"easy","player_ids":[1]})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(self.client.post("/training",json=[1]).status_code,400)
        self.assertEqual(self.client.post("/training",json={"csrf_token":1}).status_code,400)

    def test_actual_browser_form_csv_trains_only_selected_players(self):
        self.set_player(condition=100,potential=100,**{k:55 for k in rules.SKILLS})
        untouched=dict(self.player(3))
        result=self.post("/training",dict(drill_ids="att_1,att_2,def_1,pos_1",player_ids="1,2,"))
        self.assertEqual(result.status_code,302)
        self.assertGreater(sum(self.player()[k] for k in rules.SKILLS),55*6)
        self.assertEqual(self.player(3)["condition"],untouched["condition"])
        self.assertEqual(self.post("/training",dict(drill_ids="att_1",player_ids="1,wrong")).status_code,400)


if __name__=="__main__":
    unittest.main()
