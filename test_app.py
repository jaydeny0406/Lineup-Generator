import unittest
from collections import defaultdict
from unittest.mock import patch

import app


class ParserTests(unittest.TestCase):
    def test_normalize_athletic_urls_without_losing_team_or_season(self):
        canonical = "https://www.athletic.net/team/16546/track-and-field-outdoor/2026/event-records"
        self.assertEqual(
            app.normalize_athletic_url(canonical + "?foo=bar#results"),
            canonical,
        )
        self.assertEqual(
            app.normalize_athletic_url("https://r.jina.ai/" + canonical),
            canonical,
        )
        self.assertEqual(
            app.normalize_athletic_url(
                "https://r.jina.ai/http://www.athletic.net/team/16546/track-and-field-outdoor/2026/event-records"
            ),
            canonical,
        )

    def test_parse_athletic_team_url_handles_indoor_season(self):
        team_id, season_id, canonical = app.parse_athletic_team_url(
            "www.athletic.net/team/99/track-and-field-indoor/2026/event-records/"
        )
        self.assertEqual(team_id, "99")
        self.assertEqual(season_id, 12026)
        self.assertEqual(
            canonical,
            "https://www.athletic.net/team/99/track-and-field-indoor/2026/event-records",
        )

    def test_parse_track_times(self):
        self.assertEqual(app.parse_mark("10.92", "100m"), (10.92, True))
        self.assertEqual(app.parse_mark("22.3h", "200m"), (22.3, True))
        self.assertEqual(app.parse_mark("1:59.20", "800m"), (119.2, True))
        self.assertEqual(app.parse_mark("2:22.3h", "800m"), (142.3, True))
        self.assertEqual(app.parse_mark("9:53.00", "3200m"), (593.0, True))

    def test_parse_field_marks(self):
        self.assertAlmostEqual(app.parse_mark("21' 4", "long jump")[0], 256.0)
        self.assertAlmostEqual(app.parse_mark("6-2", "high jump")[0], 74.0)
        self.assertAlmostEqual(app.parse_mark("45.00m", "discus")[0], 1771.65, places=1)

    def test_parse_simple_athletic_style_table(self):
        page = """
        <html><body>
          <h3>100 Meters</h3>
          <table>
            <tr><th>Rank</th><th>Athlete</th><th>Mark</th></tr>
            <tr><td>1</td><td>Alex Carter</td><td>10.92</td></tr>
          </table>
          <h3>Long Jump</h3>
          <table>
            <tr><td>1</td><td>Jalen Brooks</td><td>21' 4</td></tr>
          </table>
        </body></html>
        """
        rows, _relays = app.parse_athletic_records_html(page, "Test", "school")
        found = {(row.athlete, row.event, row.mark) for row in rows}
        self.assertIn(("Alex Carter", "100m", "10.92"), found)
        self.assertIn(("Jalen Brooks", "long jump", "21' 4"), found)

    def test_parse_reader_markdown_with_gender_filter(self):
        page = """
        2025 Outdoor Event Records
        Mens
        100 Meters
        1.        12        Ian MacConnachie        10.91        PB        May 15        DVC Boys
        Long Jump
        1.        11        Jalen Brooks        21' 4"        PB        Apr 10        Invite
        Womens
        100 Meters
        1.        12        Ava Runner        12.50        PB        May 15        DVC Girls
        """
        rows, _relays = app.parse_athletic_records_html(page, "Test", "school", "mens")
        found = {(row.athlete, row.event, row.mark) for row in rows}
        self.assertIn(("Ian MacConnachie", "100m", "10.91"), found)
        self.assertIn(("Jalen Brooks", "long jump", "21' 4"), found)
        self.assertNotIn(("Ava Runner", "100m", "12.50"), found)

    def test_parse_historic_relay_from_reader_markdown(self):
        page = """
        Mens
        4x100 Relay
        1.
          11        William Eloe
          12        Ian MacConnachie
          12        Justin Pegorsch
        16  10        Jude Knechtel
            42.61        PB        May 9        County
        """
        _rows, relays = app.parse_athletic_records_html(page, "Test", "school", "mens")
        self.assertEqual(len(relays), 1)
        self.assertEqual(relays[0].event, "4x100 relay")
        self.assertEqual(relays[0].athletes, ("William Eloe", "Ian MacConnachie", "Justin Pegorsch", "Jude Knechtel"))
        self.assertEqual(relays[0].value, 42.61)

    def test_relay_splits_are_separate_from_individual_prs(self):
        page = """
        Mens
        400 Meters
        1.        12        Avery Runner        51.00        PB        May 1        Meet
        4x400 Relay
        1.
          12        Avery Runner        49.5h
          12        Blake Runner        50.0h
          12        Casey Runner        50.5h
          12        Devon Runner        51.0h
            3:18.00        PB        May 2        Relay Meet
        """
        rows, relays = app.parse_athletic_records_html(page, "Test", "school", "mens")
        avery_400s = [row for row in rows if row.athlete == "Avery Runner" and row.event == "400m"]
        self.assertEqual(len(avery_400s), 1)
        self.assertEqual(avery_400s[0].value, 51.0)
        self.assertEqual(relays[0].splits, (49.5, 50.0, 50.5, 51.0))

    def test_parse_first_party_api_data(self):
        payload = {
            "eventRecords": [
                {
                    "Gender": "M",
                    "Event": "100 Meters",
                    "PersonalEvent": True,
                    "Result": "10.86a",
                    "FirstName": "Andrew",
                    "LastName": "Hebron",
                    "IDResult": 1,
                },
                {
                    "Gender": "M",
                    "Event": "4x100 Relay",
                    "PersonalEvent": False,
                    "Result": "41.75a",
                    "FirstName": "One<BR>Two<BR>Three<BR>Four",
                    "LastName": None,
                    "IDResult": 2,
                },
            ],
            "relayMembers": [
                {"IDResult": 2, "SortID": 1, "Name": "One Runner"},
                {"IDResult": 2, "SortID": 2, "Name": "Two Runner"},
                {"IDResult": 2, "SortID": 3, "Name": "Three Runner"},
                {"IDResult": 2, "SortID": 4, "Name": "Four Runner"},
            ],
            "preferences": {},
        }
        result = app.parse_athletic_api_data(payload, "Test", "school", "mens")
        self.assertEqual(len(result.performances), 1)
        self.assertEqual(result.performances[0].athlete, "Andrew Hebron")
        self.assertEqual(result.performances[0].value, 10.86)
        self.assertEqual(len(result.relay_history), 1)
        self.assertEqual(
            result.relay_history[0].athletes,
            ("One Runner", "Two Runner", "Three Runner", "Four Runner"),
        )

    def test_scrape_team_data_uses_api_for_reader_prefixed_input(self):
        payload = {
            "eventRecords": [
                {
                    "Gender": "M",
                    "Event": "100 Meters",
                    "PersonalEvent": True,
                    "Result": "10.86",
                    "FirstName": "Andrew",
                    "LastName": "Hebron",
                    "IDResult": 1,
                }
            ],
            "relayMembers": [],
            "preferences": {},
        }
        with patch("app.fetch_text_url", return_value=app.json.dumps(payload)) as fetch:
            result = app.scrape_team_data(
                "https://r.jina.ai/https://www.athletic.net/team/16546/"
                "track-and-field-outdoor/2026/event-records"
            )
        self.assertEqual(len(result.performances), 1)
        self.assertEqual(
            fetch.call_args.args[0],
            "https://www.athletic.net/api/v1/TeamHome/GetTeamEventRecords"
            "?teamId=16546&seasonId=2026",
        )


class OptimizerTests(unittest.TestCase):
    def test_both_mode_keeps_divisions_separate(self):
        mens_result = app.LineupResult(
            lineup={"100m": [{"athlete": "Mens Runner"}]},
            relays={},
            event_points={"100m": 10.0},
            total_points=10.0,
            scraped={"school_records": 1, "opponent_records": 0},
            errors=[],
        )
        womens_result = app.LineupResult(
            lineup={"100m": [{"athlete": "Womens Runner"}]},
            relays={},
            event_points={"100m": 8.0},
            total_points=8.0,
            scraped={"school_records": 1, "opponent_records": 0},
            errors=[],
        )
        with patch("app.run_optimizer", side_effect=[mens_result, womens_result]) as optimize:
            result = app.run_optimizer_both("https://example.test", ["https://opponent.test"])
        self.assertEqual(result["mode"], "both")
        self.assertEqual(
            result["division_results"]["mens"]["lineup"]["100m"][0]["athlete"],
            "Mens Runner",
        )
        self.assertEqual(
            result["division_results"]["womens"]["lineup"]["100m"][0]["athlete"],
            "Womens Runner",
        )
        self.assertEqual(optimize.call_args_list[0].args[2], "mens")
        self.assertEqual(optimize.call_args_list[1].args[2], "womens")

    def test_ui_calls_combined_division_both(self):
        self.assertIn('<option value="both">Both</option>', app.HTML_PAGE)
        self.assertNotIn('<option value="all">All</option>', app.HTML_PAGE)
        self.assertIn('data-division="mens"', app.HTML_PAGE)
        self.assertIn('data-division="womens"', app.HTML_PAGE)

    def test_demo_lineup_scores(self):
        result = app.demo_result()
        self.assertGreater(result.total_points, 0)
        self.assertGreater(result.scraped["school_records"], 0)
        for event, entries in result.lineup.items():
            self.assertLessEqual(len(entries), app.MAX_INDIVIDUAL_ENTRIES, event)
            for entry in entries:
                self.assertIn("projected_place_label", entry)
                self.assertIn("projected_points", entry)

    def test_event_entries_include_individual_projected_points(self):
        school = [
            app.Performance("John Doe", "100m", "10.86", 10.86, True, "Team", "school"),
            app.Performance("Alex Fast", "100m", "10.70", 10.70, True, "Team", "school"),
        ]
        opponents = [app.Performance("Opponent One", "100m", "10.80", 10.80, True, "Opp", "opponent")]
        _total, details = app.score_event_details("100m", school, opponents)
        self.assertEqual(details["Alex Fast"]["place_label"], "1st")
        self.assertEqual(details["Alex Fast"]["points"], 10.0)
        self.assertEqual(details["John Doe"]["place_label"], "3rd")
        self.assertEqual(details["John Doe"]["points"], 6.0)

    def test_output_lineup_entries_are_best_to_worst(self):
        school = [
            app.Performance("Slow Sprinter", "100m", "11.20", 11.20, True, "Team", "school"),
            app.Performance("Fast Sprinter", "100m", "10.80", 10.80, True, "Team", "school"),
            app.Performance("Middle Sprinter", "100m", "11.00", 11.00, True, "Team", "school"),
        ]
        result = app.evaluate_lineup(
            {"100m": ["Slow Sprinter", "Fast Sprinter", "Middle Sprinter"]},
            {},
            school,
            [],
        )
        self.assertEqual(
            [entry["athlete"] for entry in result.lineup["100m"]],
            ["Fast Sprinter", "Middle Sprinter", "Slow Sprinter"],
        )

    def test_distance_runner_event_cap(self):
        self.assertTrue(app.can_event_set_stand(["4x800 relay", "800m", "1600m"]))
        self.assertFalse(app.can_event_set_stand(["800m", "1600m", "3200m"]))
        self.assertFalse(app.can_event_set_stand(["800m", "1600m", "long jump"]))

    def test_historic_relay_can_beat_synthetic(self):
        school = [
            app.Performance("A Runner", "100m", "11.50", 11.50, True, "Team", "school"),
            app.Performance("B Runner", "100m", "11.60", 11.60, True, "Team", "school"),
            app.Performance("C Runner", "100m", "11.70", 11.70, True, "Team", "school"),
            app.Performance("D Runner", "100m", "11.80", 11.80, True, "Team", "school"),
        ]
        historic = [
            app.RelayPerformance(
                "4x100 relay",
                ("Hidden Speed", "A Runner", "B Runner", "C Runner"),
                "43.00",
                43.00,
                "Team",
                "school",
            )
        ]
        selection = app.choose_relay_team(
            "4x100 relay",
            school,
            [],
            defaultdict(list),
            historic,
            [],
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.method, "historic")
        self.assertEqual(selection.projected_time, 42.8)
        self.assertEqual(selection.athletes, ("Hidden Speed", "A Runner", "B Runner", "C Runner"))

    def test_synthetic_relay_uses_fastest_split_and_requested_leg_order(self):
        school = [
            app.Performance("Alpha Runner", "400m", "51.00", 51.0, True, "Team", "school"),
            app.Performance("Bravo Runner", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Charlie Runner", "400m", "51.50", 51.5, True, "Team", "school"),
            app.Performance("Delta Runner", "400m", "52.50", 52.5, True, "Team", "school"),
        ]
        history = [
            app.RelayPerformance(
                "4x400 relay",
                ("Alpha Runner", "Bravo Runner", "Charlie Runner", "Delta Runner"),
                "3:25.00",
                205.0,
                "Team",
                "school",
                (49.5, 50.5, 51.0, 52.0),
            )
        ]
        selection = app.synthesize_relay("4x400 relay", school, defaultdict(list), history)
        self.assertIsNotNone(selection)
        self.assertEqual(
            selection.athletes,
            ("Bravo Runner", "Charlie Runner", "Delta Runner", "Alpha Runner"),
        )
        self.assertEqual(selection.leg_times, (50.0, 51.0, 52.0, 49.5))
        self.assertAlmostEqual(selection.projected_time, 199.5)


if __name__ == "__main__":
    unittest.main()
