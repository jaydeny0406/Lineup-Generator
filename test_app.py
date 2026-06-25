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

    def test_named_relay_split_is_not_an_individual_seed(self):
        payload = {
            "eventRecords": [
                {
                    "Gender": "M",
                    "Event": "400 Meters",
                    "PersonalEvent": True,
                    "Result": "51.00",
                    "FirstName": "Avery",
                    "LastName": "Runner",
                    "IDResult": 1,
                },
                {
                    "Gender": "M",
                    "Event": "400 Meters - Relay Split",
                    "PersonalEvent": True,
                    "Result": "49.5h",
                    "FirstName": "Avery",
                    "LastName": "Runner",
                    "IDResult": 2,
                },
            ],
            "relayMembers": [],
            "preferences": {},
        }
        result = app.parse_athletic_api_data(payload, "Test", "school", "mens")
        self.assertEqual(len(result.performances), 1)
        self.assertEqual(result.performances[0].event, "400m")
        self.assertEqual(result.performances[0].value, 51.0)
        self.assertEqual(len(result.relay_splits), 1)
        self.assertEqual(result.relay_splits[0].event, "400m")
        self.assertEqual(result.relay_splits[0].value, 49.5)

    def test_relay_split_description_is_also_classified_as_split_only(self):
        payload = {
            "eventRecords": [
                {
                    "Gender": "F",
                    "Event": "800 Meters",
                    "Description": "Relay Split",
                    "PersonalEvent": True,
                    "Result": "2:14.5h",
                    "FirstName": "Jordan",
                    "LastName": "Runner",
                    "IDResult": 3,
                }
            ],
            "relayMembers": [],
            "preferences": {},
        }
        result = app.parse_athletic_api_data(payload, "Test", "school", "womens")
        self.assertEqual(result.performances, [])
        self.assertEqual(len(result.relay_splits), 1)
        self.assertEqual(result.relay_splits[0].event, "800m")
        self.assertEqual(result.relay_splits[0].value, 134.5)

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
    def test_injured_athlete_is_removed_from_all_team_data(self):
        data = app.ScrapeResult(
            performances=[
                app.Performance("Alex Carter", "100m", "10.90", 10.9, True, "Team", "school"),
                app.Performance("Healthy Runner", "100m", "11.10", 11.1, True, "Team", "school"),
            ],
            relay_history=[
                app.RelayPerformance(
                    "4x100 relay",
                    ("Healthy Runner", "Alex Carter", "Third Runner", "Fourth Runner"),
                    "43.00",
                    43.0,
                    "Team",
                    "school",
                )
            ],
            relay_splits=[
                app.Performance("Alex Carter", "100m", "10.5h", 10.5, True, "Team", "school")
            ],
        )
        filtered = app.filter_injured_athletes(data, ["  ALEX-CARTER  "])
        self.assertEqual([perf.athlete for perf in filtered.performances], ["Healthy Runner"])
        self.assertEqual(filtered.relay_history, [])
        self.assertEqual(filtered.relay_splits, [])

    def test_run_optimizer_does_not_select_injured_athlete(self):
        school = [
            app.Performance("Injured Star", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Healthy One", "100m", "10.90", 10.9, True, "Team", "school"),
            app.Performance("Healthy Two", "100m", "11.00", 11.0, True, "Team", "school"),
            app.Performance("Healthy Three", "100m", "11.10", 11.1, True, "Team", "school"),
        ]
        scrape_result = app.ScrapeResult(school, [], [])
        with patch("app.scrape_team_data", return_value=scrape_result):
            result = app.run_optimizer(
                "https://example.test",
                [],
                "mens",
                ["injured star"],
            )
        selected_names = {
            entry["athlete"]
            for entries in result.lineup.values()
            for entry in entries
        }
        selected_names.update(
            athlete
            for relay in result.relays.values()
            for athlete in relay["athletes"]
        )
        self.assertNotIn("Injured Star", selected_names)

    def test_ui_includes_injured_athletes_input(self):
        self.assertIn('id="injured-athletes"', app.HTML_PAGE)
        self.assertIn("injuredAthletes:", app.HTML_PAGE)

    def test_ui_includes_clickable_athlete_panel(self):
        self.assertIn('id="athlete-panel"', app.HTML_PAGE)
        self.assertIn("athlete-chip", app.HTML_PAGE)
        self.assertIn("openAthletePanel", app.HTML_PAGE)
        self.assertIn("buildAthleteIndex", app.HTML_PAGE)
        self.assertNotIn("body.athlete-panel-open section", app.HTML_PAGE)

    def test_ui_includes_event_sort_options(self):
        self.assertIn('id="event-sort"', app.HTML_PAGE)
        self.assertIn('data-sort="schedule"', app.HTML_PAGE)
        self.assertIn('data-sort="distance"', app.HTML_PAGE)
        self.assertIn("sortedEventNames", app.HTML_PAGE)
        self.assertIn('"4x800 relay", "4x100 relay", "3200m", "110h"', app.HTML_PAGE)
        self.assertIn('"shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"', app.HTML_PAGE)
        self.assertIn('"100m", "200m", "400m", "800m", "1600m", "3200m", "110h", "300h"', app.HTML_PAGE)
        self.assertIn('"4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"', app.HTML_PAGE)

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
            result = app.run_optimizer_both(
                "https://example.test",
                ["https://opponent.test"],
                ["Injured Runner"],
            )
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
        self.assertEqual(optimize.call_args_list[0].args[3], ["Injured Runner"])
        self.assertEqual(optimize.call_args_list[1].args[3], ["Injured Runner"])

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

    def test_opponent_team_is_limited_to_top_three_entries(self):
        school = [
            app.Performance("School A One", "100m", "10.88", 10.88, True, "School A", "school"),
            app.Performance("School A Two", "100m", "10.98", 10.98, True, "School A", "school"),
            app.Performance("School A Three", "100m", "11.34", 11.34, True, "School A", "school"),
        ]
        opponent_times = [11.00, 11.01, 11.03, 11.07, 11.17, 11.24]
        opponents = [
            app.Performance(
                f"School B Runner {index}",
                "100m",
                f"{time:.2f}",
                time,
                True,
                "School B",
                "opponent",
            )
            for index, time in enumerate(opponent_times, start=1)
        ]
        selected = app.select_opponent_entries(opponents, "100m")
        self.assertEqual([entry.value for entry in selected], [11.00, 11.01, 11.03])
        total, details = app.score_event_details("100m", school, opponents)
        self.assertEqual(details["School A Three"]["place_label"], "6th")
        self.assertEqual(details["School A Three"]["points"], 3.0)
        self.assertEqual(total, 21.0)

    def test_each_opponent_team_gets_three_entries(self):
        opponents = []
        for source, starting_time in (("School B", 11.00), ("School C", 10.90)):
            for index in range(5):
                time = starting_time + index * 0.01
                opponents.append(
                    app.Performance(
                        f"{source} Runner {index}",
                        "100m",
                        f"{time:.2f}",
                        time,
                        True,
                        source,
                        "opponent",
                    )
                )
        selected = app.select_opponent_entries(opponents, "100m")
        self.assertEqual(len(selected), 6)
        self.assertEqual(
            {source: sum(entry.source == source for entry in selected) for source in ("School B", "School C")},
            {"School B": 3, "School C": 3},
        )

    def test_display_mark_removes_athletic_net_suffix(self):
        self.assertEqual(app.format_display_mark("9:43.55a"), "9:43.55")
        self.assertEqual(app.format_display_mark("49.5h"), "49.5")
        self.assertEqual(app.format_display_mark("10.86"), "10.86")

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
        self.assertFalse(app.can_event_set_stand(["400m", "4x400 relay"]))

    def test_elite_pass_replaces_worse_individual_entry_without_losing_points(self):
        school = [
            app.Performance("Star Sprinter", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Star Sprinter", "200m", "21.50", 21.5, True, "Team", "school"),
            app.Performance("Star Sprinter", "long jump", "21' 8", 260.0, False, "Team", "school"),
            app.Performance("Star Sprinter", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Relay One", "400m", "51.00", 51.0, True, "Team", "school"),
            app.Performance("Relay Two", "400m", "52.00", 52.0, True, "Team", "school"),
            app.Performance("Relay Three", "400m", "53.00", 53.0, True, "Team", "school"),
        ]
        lineup = {
            "100m": ["Star Sprinter"],
            "200m": ["Star Sprinter"],
            "long jump": ["Star Sprinter"],
            "400m": ["Relay One", "Relay Two", "Relay Three"],
        }
        improved_lineup, _relays = app.optimize_elite_sprint_utilization(
            lineup,
            {},
            school,
            [],
            [],
            [],
            [],
            [],
        )
        self.assertIn("Star Sprinter", improved_lineup["400m"])
        self.assertNotIn("Relay Three", improved_lineup["400m"])

    def test_elite_pass_replaces_slowest_synthetic_relay_leg(self):
        school = [
            app.Performance("Star Sprinter", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Star Sprinter", "200m", "21.50", 21.5, True, "Team", "school"),
            app.Performance("Star Sprinter", "long jump", "21' 8", 260.0, False, "Team", "school"),
            app.Performance("Relay One", "100m", "11.00", 11.0, True, "Team", "school"),
            app.Performance("Relay Two", "100m", "11.10", 11.1, True, "Team", "school"),
            app.Performance("Relay Three", "100m", "11.20", 11.2, True, "Team", "school"),
            app.Performance("Relay Four", "100m", "11.30", 11.3, True, "Team", "school"),
        ]
        lineup = {
            "100m": ["Star Sprinter"],
            "200m": ["Star Sprinter"],
            "long jump": ["Star Sprinter"],
        }
        relays = {
            "4x100 relay": app.RelaySelection(
                "4x100 relay",
                ("Relay One", "Relay Two", "Relay Three", "Relay Four"),
                41.9,
                "synthetic",
                "best individual PR/relay split",
                (11.0, 11.1, 11.2, 11.3),
            )
        }
        _lineup, improved_relays = app.optimize_elite_sprint_utilization(
            lineup,
            relays,
            school,
            [],
            [],
            [],
            [],
            [],
        )
        self.assertIn("Star Sprinter", improved_relays["4x100 relay"].athletes)
        self.assertNotIn("Relay Four", improved_relays["4x100 relay"].athletes)

    def test_elite_pass_does_not_replace_higher_ranked_elite_later(self):
        school = [
            app.Performance("Alpha Star", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Alpha Star", "200m", "21.50", 21.5, True, "Team", "school"),
            app.Performance("Alpha Star", "long jump", "22' 0", 264.0, False, "Team", "school"),
            app.Performance("Alpha Star", "400m", "49.00", 49.0, True, "Team", "school"),
            app.Performance("Beta Star", "100m", "10.60", 10.6, True, "Team", "school"),
            app.Performance("Beta Star", "200m", "21.60", 21.6, True, "Team", "school"),
            app.Performance("Beta Star", "long jump", "21' 8", 260.0, False, "Team", "school"),
            app.Performance("Beta Star", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Relay One", "400m", "51.00", 51.0, True, "Team", "school"),
            app.Performance("Relay Two", "400m", "52.00", 52.0, True, "Team", "school"),
        ]
        lineup = {
            "100m": ["Alpha Star", "Beta Star"],
            "200m": ["Alpha Star", "Beta Star"],
            "long jump": ["Alpha Star", "Beta Star"],
            "400m": ["Relay One", "Relay Two", "Alpha Star"],
        }
        improved_lineup, _relays = app.optimize_elite_sprint_utilization(
            lineup,
            {},
            school,
            [],
            [],
            [],
            [],
            [],
        )
        self.assertIn("Alpha Star", improved_lineup["400m"])
        self.assertIn("Beta Star", improved_lineup["400m"])
        self.assertNotIn("Relay Two", improved_lineup["400m"])

    def test_elite_rank_uses_only_flat_sprint_and_relay_value(self):
        school = [
            app.Performance("Flat Star", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Flat Star", "200m", "21.50", 21.5, True, "Team", "school"),
            app.Performance("Flat Star", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Hurdle Star", "110h", "14.40", 14.4, True, "Team", "school"),
            app.Performance("Hurdle Star", "300h", "39.00", 39.0, True, "Team", "school"),
            app.Performance("Hurdle Star", "200m", "21.70", 21.7, True, "Team", "school"),
            app.Performance("Hurdle Star", "high jump", "6-2", 74.0, False, "Team", "school"),
            app.Performance("Jump Star", "long jump", "23' 0", 276.0, False, "Team", "school"),
            app.Performance("Jump Star", "triple jump", "46' 0", 552.0, False, "Team", "school"),
            app.Performance("Jump Star", "100m", "10.90", 10.9, True, "Team", "school"),
        ]
        potentials = app.compute_scores(school, [])
        ranked = app.rank_elite_sprint_jump_athletes(school, potentials, {}, [], [], [])
        self.assertIn("Flat Star", ranked)
        self.assertNotIn("Hurdle Star", ranked)
        self.assertNotIn("Jump Star", ranked)

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

    def test_named_split_can_improve_synthetic_relay_only(self):
        school = [
            app.Performance("Alpha Runner", "400m", "51.00", 51.0, True, "Team", "school"),
            app.Performance("Bravo Runner", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Charlie Runner", "400m", "51.50", 51.5, True, "Team", "school"),
            app.Performance("Delta Runner", "400m", "52.50", 52.5, True, "Team", "school"),
        ]
        split_records = [
            app.Performance("Alpha Runner", "400m", "49.5h", 49.5, True, "Team", "school")
        ]
        selection = app.synthesize_relay(
            "4x400 relay",
            school,
            defaultdict(list),
            [],
            split_records,
        )
        self.assertEqual(
            [perf.value for perf in school if perf.athlete == "Alpha Runner"],
            [51.0],
        )
        self.assertIn(49.5, selection.leg_times)
        self.assertAlmostEqual(selection.projected_time, 200.5)

    def test_low_scoring_relay_still_gets_depth_lineup(self):
        school = [
            app.Performance(
                f"School Runner {index}",
                "200m",
                f"{23.0 + index / 10:.2f}",
                23.0 + index / 10,
                True,
                "School",
                "school",
            )
            for index in range(8)
        ]
        opponent_relays = [
            app.RelayPerformance(
                "4x200 relay",
                (f"A{index}", f"B{index}", f"C{index}", f"D{index}"),
                f"1:{20 + index:02d}.00",
                80.0 + index,
                f"Opponent {index}",
                "opponent",
            )
            for index in range(5)
        ]
        selection = app.choose_relay_team(
            "4x200 relay",
            school,
            [],
            defaultdict(list),
            [],
            opponent_relays,
        )
        self.assertIsNotNone(selection)
        self.assertEqual(len(selection.athletes), 4)
        self.assertIn("depth runners", selection.source_mark)

    def test_opponent_school_enters_only_its_fastest_relay(self):
        opponent_relays = [
            app.RelayPerformance(
                "4x200 relay",
                (f"A{index}", f"B{index}", f"C{index}", f"D{index}"),
                f"1:{30 + index:02d}.00",
                90.0 + index,
                "School B",
                "opponent",
            )
            for index in range(5)
        ]
        estimates = app.estimate_opponent_relays(
            "4x200 relay",
            [],
            opponent_relays,
        )
        self.assertEqual(estimates, [89.8])
        self.assertEqual(
            app.projected_relay_points("4x200 relay", 91.0, [], opponent_relays),
            8.0,
        )

    def test_each_opponent_school_gets_one_relay_entry(self):
        opponent_relays = [
            app.RelayPerformance(
                "4x400 relay",
                ("B One", "B Two", "B Three", "B Four"),
                "3:20.00",
                200.0,
                "School B",
                "opponent",
            ),
            app.RelayPerformance(
                "4x400 relay",
                ("B Five", "B Six", "B Seven", "B Eight"),
                "3:22.00",
                202.0,
                "School B",
                "opponent",
            ),
            app.RelayPerformance(
                "4x400 relay",
                ("C One", "C Two", "C Three", "C Four"),
                "3:21.00",
                201.0,
                "School C",
                "opponent",
            ),
        ]
        estimates = sorted(
            app.estimate_opponent_relays("4x400 relay", [], opponent_relays)
        )
        self.assertEqual(estimates, [199.8, 200.8])

    def test_complete_team_generates_all_eighteen_events(self):
        school = []
        for event in [event for event in app.EVENTS if event not in app.RELAY_EVENTS]:
            count = 8 if event == "400m" else 4 if event in {"100m", "200m", "800m"} else 1
            for index in range(count):
                is_time = event in app.TRACK_EVENTS
                value = (10.0 + index) if is_time else (200.0 + index)
                school.append(
                    app.Performance(
                        f"{event} Athlete {index}",
                        event,
                        str(value),
                        value,
                        is_time,
                        "School",
                        "school",
                    )
                )
        result = app.build_lineup(school, [])
        self.assertEqual(result["missing_events"], [])
        self.assertTrue(
            all(result["lineup"][event] for event in app.EVENTS if event not in app.RELAY_EVENTS)
        )
        self.assertEqual(set(result["relays"]), app.RELAY_EVENTS)
        self.assertEqual(
            len([event for event in app.EVENTS if result["lineup"].get(event) or event in result["relays"]]),
            18,
        )


if __name__ == "__main__":
    unittest.main()
