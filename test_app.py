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

    def test_detects_indoor_short_sprints_and_hurdles(self):
        self.assertEqual(app.detect_event("Boys 55 Meters"), "55m")
        self.assertEqual(app.detect_event("Girls 60 Meter Dash"), "60m")
        self.assertEqual(app.detect_event("Boys 55 Meter Hurdles"), "55h")
        self.assertEqual(app.detect_event("Girls 60mH"), "60h")
        self.assertEqual(app.parse_mark("7.23h", "55m"), (7.23, True))

    def test_meet_url_validation_rejects_mixed_seasons(self):
        errors = app.validate_meet_urls(
            "https://www.athletic.net/team/1/track-and-field-indoor/2026/event-records",
            ["https://www.athletic.net/team/2/track-and-field-outdoor/2026/event-records"],
            "indoor",
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("Opponent 1 URL is for the outdoor season", errors[0])

    def test_parse_athletic_team_url_accepts_season_page_without_event_records(self):
        team_id, season_id, canonical = app.parse_athletic_team_url(
            "https://www.athletic.net/team/16797/track-and-field-outdoor/2026"
        )
        self.assertEqual(team_id, "16797")
        self.assertEqual(season_id, 2026)
        self.assertEqual(
            canonical,
            "https://www.athletic.net/team/16797/track-and-field-outdoor/2026/event-records",
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

    def test_api_team_name_replaces_placeholder_source(self):
        payload = {
            "team": {"Name": "Naperville (North) Track & Field and Cross Country"},
            "eventRecords": [
                {
                    "Gender": "M",
                    "Event": "100 Meters",
                    "PersonalEvent": True,
                    "Result": "10.86",
                    "FirstName": "Andrew",
                    "LastName": "Hebron",
                    "IDResult": 1,
                },
                {
                    "Gender": "M",
                    "Event": "100 Meters - Relay Split",
                    "PersonalEvent": True,
                    "Result": "10.5h",
                    "FirstName": "Andrew",
                    "LastName": "Hebron",
                    "IDResult": 2,
                },
            ],
            "relayMembers": [],
        }
        result = app.parse_athletic_api_data(payload, "Opponent 1", "opponent", "mens")
        self.assertEqual(result.performances[0].source, "Naperville (North)")
        self.assertEqual(result.relay_splits[0].source, "Naperville (North)")

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

    def test_resolve_team_name_from_athletic_title_before_api_records(self):
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
        }
        page = "<title>Naperville (North) - High School Track and Field Outdoor 2026</title>"
        with patch("app.fetch_text_url", side_effect=[page, app.json.dumps(payload)]):
            result = app.scrape_team_data(
                "https://www.athletic.net/team/16546/track-and-field-outdoor/2026/event-records",
                "school",
                "Your Team",
                "mens",
            )
        self.assertEqual(result.performances[0].source, "Naperville (North)")

    def test_resolve_team_name_handles_opponent_and_plain_season_links(self):
        payload = {
            "eventRecords": [
                {
                    "Gender": "M",
                    "Event": "100 Meters",
                    "PersonalEvent": True,
                    "Result": "11.00",
                    "FirstName": "Test",
                    "LastName": "Runner",
                    "IDResult": 1,
                }
            ],
            "relayMembers": [],
        }
        page = "<title>Yorkville - High School Track and Field Outdoor 2026</title>"
        with patch("app.fetch_text_url", side_effect=[page, app.json.dumps(payload)]):
            result = app.scrape_team_data(
                "https://www.athletic.net/team/16797/track-and-field-outdoor/2026",
                "opponent",
                "Opponent 2",
                "mens",
            )
        self.assertEqual(result.performances[0].source, "Yorkville")

    def test_extract_team_name_from_athletic_titles(self):
        self.assertEqual(
            app.extract_team_name("<title>Hinsdale (Central) - High School Track and Field Outdoor 2026</title>"),
            "Hinsdale (Central)",
        )
        self.assertEqual(
            app.extract_team_name("Title: Yorkville - High School Track and Field Outdoor 2026"),
            "Yorkville",
        )

    def test_html_fallback_team_title_replaces_placeholder_source(self):
        page = """
        <html>
          <head><title>Actual High School Track & Field and Cross Country | Athletic.net</title></head>
          <body>
            <h3>100 Meters</h3>
            <table>
              <tr><th>Rank</th><th>Athlete</th><th>Mark</th></tr>
              <tr><td>1</td><td>Alex Carter</td><td>10.92</td></tr>
            </table>
          </body>
        </html>
        """
        with patch("app.fetch_text_url", side_effect=[page, RuntimeError("api blocked"), page]):
            result = app.scrape_team_data(
                "https://www.athletic.net/team/16546/track-and-field-outdoor/2026/event-records",
                "opponent",
                "Opponent 1",
                "mens",
            )
        self.assertEqual(result.performances[0].source, "Actual High School")


class OptimizerTests(unittest.TestCase):
    def test_indoor_meet_config_uses_selected_short_sprint_program(self):
        config = app.meet_config_for("indoor", "60")

        self.assertEqual(
            config.schedule_order[:10],
            (
                "4x800 relay",
                "3200m",
                "60h",
                "60m",
                "800m",
                "4x200 relay",
                "400m",
                "1600m",
                "200m",
                "4x400 relay",
            ),
        )
        self.assertEqual(
            config.relay_events,
            frozenset({"4x800 relay", "4x200 relay", "4x400 relay"}),
        )
        self.assertNotIn("4x100 relay", config.events)
        self.assertNotIn("100m", config.events)
        self.assertNotIn("110h", config.events)
        self.assertNotIn("discus", config.events)

    def test_indoor_conversion_uses_official_target_pr_when_available(self):
        data = app.ScrapeResult(
            [
                app.Performance("Official Runner", "55m", "6.80", 6.8, True, "Team", "school"),
                app.Performance("Official Runner", "60m", "7.20", 7.2, True, "Team", "school"),
                app.Performance("Converted Runner", "55m", "6.90", 6.9, True, "Team", "school"),
            ],
            [],
            [],
        )

        prepared = app.prepare_scrape_result_for_meet(data, app.meet_config_for("indoor", "60"))
        marks = {perf.athlete: perf for perf in prepared.performances if perf.event == "60m"}

        self.assertEqual(marks["Official Runner"].value, 7.2)
        self.assertEqual(marks["Official Runner"].mark, "7.20")
        self.assertAlmostEqual(marks["Converted Runner"].value, 6.9 * 1.071)
        self.assertTrue(marks["Converted Runner"].mark.endswith("c"))

    def test_indoor_short_event_entries_identify_historical_and_predicted_marks(self):
        historical = app.Performance(
            "Historical Runner", "60m", "7.20", 7.2, True, "Team", "school"
        )
        predicted = app.Performance(
            "Predicted Runner", "60m", "7.39c", 7.39, True, "Team", "school"
        )

        self.assertEqual(app.entry_to_dict(historical)["mark_origin"], "historical")
        self.assertEqual(app.entry_to_dict(predicted)["mark_origin"], "predicted")
        standings = app.projected_event_standings("60m", [historical, predicted], [])
        self.assertEqual(
            [row["mark_origin"] for row in standings],
            ["historical", "predicted"],
        )

    def test_indoor_conversion_divides_from_60_to_55_for_dash_and_hurdles(self):
        data = app.ScrapeResult(
            [
                app.Performance("Dash Runner", "60m", "7.50", 7.5, True, "Team", "school"),
                app.Performance("Hurdle Runner", "60h", "8.60", 8.6, True, "Team", "school"),
            ],
            [],
            [],
        )

        prepared = app.prepare_scrape_result_for_meet(data, app.meet_config_for("indoor", "55"))
        marks = {(perf.athlete, perf.event): perf.value for perf in prepared.performances}

        self.assertAlmostEqual(marks[("Dash Runner", "55m")], 7.5 / 1.071)
        self.assertAlmostEqual(marks[("Hurdle Runner", "55h")], 8.6 / 1.075)

    def test_run_optimizer_applies_indoor_profile_without_outdoor_events(self):
        scrape_result = app.ScrapeResult(
            [
                app.Performance("Converted Runner", "55m", "6.50", 6.5, True, "Indoor Team", "school"),
                app.Performance("Official Runner", "60m", "7.10", 7.1, True, "Indoor Team", "school"),
                app.Performance("Outdoor Only", "100m", "10.50", 10.5, True, "Indoor Team", "school"),
            ],
            [],
            [],
        )
        url = "https://www.athletic.net/team/99/track-and-field-indoor/2026/event-records"

        with patch("app.scrape_team_data", return_value=scrape_result):
            result = app.run_optimizer(url, [], "mens", [], [], "indoor", "60")

        self.assertTrue(
            any(
                error.startswith("No eligible recorded athletes were available")
                for error in result.errors
            )
        )
        self.assertEqual(result.scraped["season_type"], "indoor")
        self.assertEqual(result.scraped["indoor_sprint_distance"], "60")
        self.assertIn("60m", result.edit_context["events"])
        self.assertNotIn("100m", result.edit_context["events"])
        self.assertNotIn("4x100 relay", result.edit_context["events"])
        school_events = {item["event"] for item in result.edit_context["school_performances"]}
        self.assertEqual(school_events, {"60m"})

    def test_run_optimizer_stops_before_scraping_a_mismatched_season_link(self):
        outdoor_url = (
            "https://www.athletic.net/team/99/track-and-field-outdoor/2026/event-records"
        )

        with patch("app.scrape_team_data") as scrape:
            result = app.run_optimizer(outdoor_url, [], season_type="indoor")

        scrape.assert_not_called()
        self.assertEqual(result.total_points, 0.0)
        self.assertIn("School URL is for the outdoor season", result.errors[0])

    def test_outdoor_profile_remains_the_legacy_event_program(self):
        config = app.meet_config_for("outdoor", "60")

        self.assertEqual(config.events, tuple(app.EVENTS))
        self.assertEqual(config.running_order, app.RUNNING_ORDER)
        self.assertIn("4x100 relay", config.relay_events)
        self.assertIn("100m", config.events)
        self.assertNotIn("60m", config.events)

    def test_indoor_distance_event_limits_match_outdoor_rules(self):
        token = app.ACTIVE_MEET_CONFIG.set(app.meet_config_for("indoor", "55"))
        try:
            self.assertTrue(app.can_event_set_stand(["4x800 relay", "800m", "4x400 relay"]))
            self.assertFalse(app.can_event_set_stand(["4x800 relay", "800m", "1600m"]))
            self.assertFalse(app.can_event_set_stand(["800m", "1600m", "4x400 relay"]))
        finally:
            app.ACTIVE_MEET_CONFIG.reset(token)

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

    def test_run_optimizer_filters_unavailable_opponent_athlete(self):
        school_result = app.ScrapeResult(
            [
                app.Performance("School Runner", "100m", "11.00", 11.0, True, "School", "school"),
            ],
            [],
            [],
        )
        opponent_result = app.ScrapeResult(
            [
                app.Performance("Unavailable Opp", "100m", "10.50", 10.5, True, "Opponent", "opponent"),
                app.Performance("Healthy Opp", "100m", "11.20", 11.2, True, "Opponent", "opponent"),
            ],
            [
                app.RelayPerformance(
                    "4x100 relay",
                    ("Unavailable Opp", "Healthy Opp", "Third Opp", "Fourth Opp"),
                    "43.00",
                    43.0,
                    "Opponent",
                    "opponent",
                )
            ],
            [],
        )
        with patch("app.scrape_team_data", side_effect=[school_result, opponent_result]):
            result = app.run_optimizer("school-url", ["opponent-url"], "mens", ["unavailable opp"])

        projected_100m_names = [row["athlete"] for row in result.event_standings.get("100m", [])]
        self.assertNotIn("Unavailable Opp", projected_100m_names)
        self.assertIn("Healthy Opp", projected_100m_names)

    def test_athlete_event_limits_are_normalized_and_use_the_stricter_duplicate(self):
        limits = app.normalize_athlete_event_limits(
            [
                {"athlete": "  Alex-Carter ", "maxEvents": 3},
                {"athlete": "ALEX CARTER", "maxEvents": 2},
            ]
        )

        self.assertEqual(limits, {"alex carter": 2})
        self.assertEqual(app.athlete_max_events("Alex Carter", limits), 2)
        self.assertEqual(app.athlete_max_events("Unrestricted Runner", limits), 4)

    def test_team_event_limit_accepts_only_supported_caps(self):
        self.assertEqual(app.normalize_team_event_limit(None), 4)
        self.assertEqual(app.normalize_team_event_limit("2"), 2)
        self.assertEqual(app.normalize_team_event_limit(3), 3)
        self.assertEqual(app.normalize_team_event_limit(4), 4)
        with self.assertRaisesRegex(ValueError, "must be 2, 3, or 4"):
            app.normalize_team_event_limit(1)

    def test_team_event_limit_applies_only_to_school_athletes_and_keeps_stricter_limits(self):
        school_data = app.ScrapeResult(
            [
                app.Performance("School Star", "100m", "10.80", 10.8, True, "School", "school"),
            ],
            [
                app.RelayPerformance(
                    "4x100 relay",
                    ("Relay Only", "School Star", "Third Runner", "Fourth Runner"),
                    "42.00",
                    42.0,
                    "School",
                    "school",
                )
            ],
            [
                app.Performance(
                    "Split Only", "100m", "10.60", 10.6, True, "School", "relay_split"
                )
            ],
        )

        limits = app.apply_team_event_limit_to_school(
            school_data,
            {"school star": 1},
            2,
        )

        self.assertEqual(limits["school star"], 1)
        self.assertEqual(limits["relay only"], 2)
        self.assertEqual(limits["split only"], 2)
        self.assertNotIn("opponent star", limits)
        self.assertEqual(app.athlete_max_events("Opponent Star", limits), 4)

    def test_run_optimizer_respects_manual_athlete_event_limit(self):
        field_events = ["shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"]
        performances = []
        for index, event in enumerate(field_events):
            performances.append(
                app.Performance("Limited Star", event, str(100 + index), 100.0 + index, False, "School", "school")
            )
            performances.append(
                app.Performance(f"Depth Athlete {index}", event, str(90 + index), 90.0 + index, False, "School", "school")
            )
        scrape_result = app.ScrapeResult(performances, [], [])

        with patch("app.scrape_team_data", return_value=scrape_result):
            result = app.run_optimizer(
                "school-url",
                [],
                "mens",
                [],
                [{"athlete": "limited star", "maxEvents": 2}],
            )

        selected_events = [
            event
            for event, entries in result.lineup.items()
            if any(entry["athlete"] == "Limited Star" for entry in entries)
        ]
        selected_events.extend(
            event
            for event, relay in result.relays.items()
            if "Limited Star" in relay["athletes"]
        )
        self.assertLessEqual(len(selected_events), 2)
        self.assertEqual(result.edit_context["athlete_event_limits"], {"limited star": 2})

    def test_run_optimizer_respects_team_wide_event_limit(self):
        field_events = ["shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"]
        performances = []
        for index, event in enumerate(field_events):
            performances.append(
                app.Performance("All Event Star", event, str(120 + index), 120.0 + index, False, "School", "school")
            )
            performances.append(
                app.Performance(f"Depth Athlete {index}", event, str(90 + index), 90.0 + index, False, "School", "school")
            )
        scrape_result = app.ScrapeResult(performances, [], [])

        with patch("app.scrape_team_data", return_value=scrape_result):
            result = app.run_optimizer("school-url", [], team_event_limit=2)

        athlete_counts = defaultdict(int)
        for entries in result.lineup.values():
            for entry in entries:
                athlete_counts[entry["athlete"]] += 1
        for relay in result.relays.values():
            for athlete in relay["athletes"]:
                athlete_counts[athlete] += 1

        self.assertTrue(athlete_counts)
        self.assertTrue(all(count <= 2 for count in athlete_counts.values()))
        self.assertEqual(result.edit_context["team_event_limit"], 2)
        self.assertEqual(result.scraped["team_event_limit"], 2)

    def test_indoor_standard_limit_scans_past_an_illegal_candidate_to_fill_four_events(self):
        token = app.ACTIVE_MEET_CONFIG.set(app.meet_config_for("indoor", "55"))
        try:
            school = [
                app.Performance("Priority Sprinter", "55m", "6.60", 6.6, True, "Team", "school"),
                app.Performance("Priority Sprinter", "400m", "50.00", 50.0, True, "Team", "school"),
                app.Performance("Priority Sprinter", "200m", "22.50", 22.5, True, "Team", "school"),
                app.Performance("Priority Sprinter", "long jump", "21' 0", 252.0, False, "Team", "school"),
            ]
            history = [
                app.RelayPerformance(
                    "4x200 relay",
                    ("Priority Sprinter", "Relay Two", "Relay Three", "Relay Four"),
                    "1:30.00",
                    90.0,
                    "Team",
                    "school",
                )
            ]

            result = app.build_lineup(school, [], history, athlete_event_limits={})
            athlete_events = app.collect_athlete_events(result["lineup"], result["relays"])

            self.assertEqual(
                set(athlete_events["Priority Sprinter"]),
                {"55m", "200m", "long jump", "4x200 relay"},
            )
            self.assertTrue(app.lineup_is_valid(result["lineup"], result["relays"]))
        finally:
            app.ACTIVE_MEET_CONFIG.reset(token)

    def test_lineup_validation_counts_individual_and_relay_events_against_manual_limit(self):
        lineup = {"100m": ["Limited Runner"]}
        relays = {
            "4x200 relay": app.RelaySelection(
                "4x200 relay",
                ("Limited Runner", "Runner Two", "Runner Three", "Runner Four"),
                90.0,
                "synthetic",
                "test",
            )
        }
        limits = app.normalize_athlete_event_limits(
            [{"athlete": "Limited Runner", "maxEvents": 1}]
        )

        self.assertFalse(app.lineup_is_valid(lineup, relays, limits))
        self.assertEqual(
            app.event_limit_violations(lineup, relays, limits),
            ["Limited Runner has 2 events (maximum 1)"],
        )

    def test_synthetic_relay_excludes_athlete_already_at_manual_limit(self):
        school = [
            app.Performance(name, "100m", f"{time:.2f}", time, True, "School", "school")
            for name, time in [
                ("Limited Star", 10.50),
                ("Runner Two", 10.70),
                ("Runner Three", 10.80),
                ("Runner Four", 10.90),
                ("Runner Five", 11.00),
            ]
        ]
        athlete_events = defaultdict(list, {"Limited Star": ["long jump"]})
        limits = app.normalize_athlete_event_limits(
            [{"athlete": "Limited Star", "maxEvents": 1}]
        )

        relay = app.synthesize_relay(
            "4x100 relay",
            school,
            athlete_events,
            athlete_event_limits=limits,
        )

        self.assertIsNotNone(relay)
        self.assertNotIn("Limited Star", relay.athletes)

    def test_both_divisions_receive_the_same_manual_event_limits(self):
        empty_result = app.LineupResult({}, {}, {}, 0.0, {}, [])
        limits = [{"athlete": "Limited Runner", "maxEvents": 2}]

        with patch("app.run_optimizer", return_value=empty_result) as optimizer:
            app.run_optimizer_both("school-url", ["opponent-url"], ["Injured Runner"], limits)

        self.assertEqual(optimizer.call_count, 2)
        self.assertEqual(
            optimizer.call_args_list[0].args,
            (
                "school-url",
                ["opponent-url"],
                "mens",
                ["Injured Runner"],
                limits,
                "outdoor",
                "55",
                4,
            ),
        )
        self.assertEqual(
            optimizer.call_args_list[1].args,
            (
                "school-url",
                ["opponent-url"],
                "womens",
                ["Injured Runner"],
                limits,
                "outdoor",
                "55",
                4,
            ),
        )

    def test_both_divisions_receive_the_same_indoor_profile(self):
        empty_result = app.LineupResult({}, {}, {}, 0.0, {}, [])

        with patch("app.run_optimizer", return_value=empty_result) as optimizer:
            app.run_optimizer_both(
                "school-url",
                [],
                season_type="indoor",
                indoor_sprint_distance="60",
            )

        self.assertEqual(optimizer.call_count, 2)
        self.assertEqual(optimizer.call_args_list[0].args[-3:], ("indoor", "60", 4))
        self.assertEqual(optimizer.call_args_list[1].args[-3:], ("indoor", "60", 4))

    def test_both_divisions_receive_the_same_team_event_limit(self):
        empty_result = app.LineupResult({}, {}, {}, 0.0, {}, [])

        with patch("app.run_optimizer", return_value=empty_result) as optimizer:
            app.run_optimizer_both("school-url", [], team_event_limit=3)

        self.assertEqual(optimizer.call_count, 2)
        self.assertEqual(optimizer.call_args_list[0].args[-1], 3)
        self.assertEqual(optimizer.call_args_list[1].args[-1], 3)

    def test_demo_lineup_applies_manual_event_limits(self):
        result = app.demo_result([{"athlete": "Alex Carter", "maxEvents": 2}])
        event_count = sum(
            any(entry["athlete"] == "Alex Carter" for entry in entries)
            for entries in result.lineup.values()
        ) + sum("Alex Carter" in relay["athletes"] for relay in result.relays.values())

        self.assertLessEqual(event_count, 2)
        self.assertEqual(result.edit_context["athlete_event_limits"], {"alex carter": 2})

    def test_rescore_rejects_manual_edit_above_athlete_event_limit(self):
        school = [
            app.Performance("Limited Runner", "100m", "11.00", 11.0, True, "School", "school"),
            app.Performance("Limited Runner", "long jump", "20' 0", 240.0, False, "School", "school"),
        ]
        limits = {"limited runner": 1}
        context = app.build_edit_context(school, [], [], [], [], [], limits)
        payload = {
            "lineup": {
                "100m": [{"athlete": "Limited Runner"}],
                "long jump": [{"athlete": "Limited Runner"}],
            },
            "relays": {},
            "edit_context": context,
        }

        with self.assertRaisesRegex(ValueError, "Athlete event limit exceeded"):
            app.rescore_edited_result(payload)

    def test_opponent_projection_uses_independent_lineup_constraints(self):
        data = app.ScrapeResult(
            [
                app.Performance("Busy Opp", "100m", "10.90", 10.9, True, "Opponent", "opponent"),
                app.Performance("Busy Opp", "200m", "22.20", 22.2, True, "Opponent", "opponent"),
                app.Performance("Busy Opp", "400m", "50.20", 50.2, True, "Opponent", "opponent"),
                app.Performance("Busy Opp", "800m", "2:02.00", 122.0, True, "Opponent", "opponent"),
                app.Performance("Busy Opp", "1600m", "4:40.00", 280.0, True, "Opponent", "opponent"),
            ],
            [],
            [],
        )
        projection = app.build_independent_team_projection(data, "Opponent", app.PROJECTED_OPPONENT_ROLE)
        busy_events = [entry.event for entry in projection.entries if entry.athlete == "Busy Opp"]
        self.assertLessEqual(len(busy_events), app.MAX_EVENTS_PER_ATHLETE)
        self.assertTrue(app.can_event_set_stand(busy_events))

    def test_ui_includes_injured_athletes_input(self):
        self.assertIn('id="injured-athletes"', app.HTML_PAGE)
        self.assertIn("injuredAthletes:", app.HTML_PAGE)
        self.assertIn("Injured / unavailable athletes", app.HTML_PAGE)
        self.assertIn("including opponents", app.HTML_PAGE)
        self.assertIn('id="team-points"', app.HTML_PAGE)

    def test_ui_includes_persistent_athlete_event_limit_controls(self):
        self.assertIn('id="athlete-limit-list"', app.HTML_PAGE)
        self.assertIn('id="add-athlete-limit"', app.HTML_PAGE)
        self.assertIn("athleteEventLimits: collectAthleteEventLimits()", app.HTML_PAGE)
        self.assertIn("restoreAthleteEventLimits(state.athleteEventLimits || [])", app.HTML_PAGE)
        self.assertIn("athleteMaxEvents", app.HTML_PAGE)

    def test_ui_includes_school_wide_event_limit_control(self):
        self.assertIn('id="team-event-limit"', app.HTML_PAGE)
        self.assertIn('<option value="4" selected>4 events (standard)</option>', app.HTML_PAGE)
        self.assertIn('<option value="3">3 events</option>', app.HTML_PAGE)
        self.assertIn('<option value="2">2 events</option>', app.HTML_PAGE)
        self.assertIn("teamEventLimit: Number(teamEventLimitInput.value)", app.HTML_PAGE)
        self.assertGreaterEqual(
            app.HTML_PAGE.count("teamEventLimit: Number(teamEventLimitInput.value)"),
            2,
        )
        self.assertIn("state.teamEventLimit", app.HTML_PAGE)

    def test_ui_shows_persistent_score_delta_after_manual_edits(self):
        self.assertIn('id="score-change"', app.HTML_PAGE)
        self.assertIn('aria-live="polite"', app.HTML_PAGE)
        self.assertIn("manualScoreChanges", app.HTML_PAGE)
        self.assertIn("const previousPoints", app.HTML_PAGE)
        self.assertIn("after: Number(data.total_points || 0)", app.HTML_PAGE)
        self.assertIn("Projected team score changed from", app.HTML_PAGE)
        self.assertIn("No estimated net change", app.HTML_PAGE)
        self.assertIn("position: sticky", app.HTML_PAGE)

    def test_ui_includes_indoor_season_and_sprint_program_controls(self):
        self.assertIn('id="season-type"', app.HTML_PAGE)
        self.assertIn('<option value="outdoor" selected>Outdoor</option>', app.HTML_PAGE)
        self.assertIn('<option value="indoor">Indoor</option>', app.HTML_PAGE)
        self.assertIn('id="indoor-sprint-distance"', app.HTML_PAGE)
        self.assertIn("55m and 55m Hurdles", app.HTML_PAGE)
        self.assertIn("60m and 60m Hurdles", app.HTML_PAGE)
        self.assertIn("markOriginBadge", app.HTML_PAGE)
        self.assertIn("Historical", app.HTML_PAGE)
        self.assertIn("Predicted", app.HTML_PAGE)
        self.assertNotIn('id="demo-button"', app.HTML_PAGE)
        self.assertNotIn("Use Demo Data", app.HTML_PAGE)
        self.assertIn("seasonType: seasonTypeInput.value", app.HTML_PAGE)
        self.assertIn("indoorSprintDistance: indoorSprintDistanceInput.value", app.HTML_PAGE)
        self.assertIn("updateSeasonControls", app.HTML_PAGE)
        self.assertIn("currentRunningOrder", app.HTML_PAGE)

    def test_school_url_input_has_no_default_link(self):
        self.assertIn('id="school-url"', app.HTML_PAGE)
        self.assertIn('placeholder="Paste Athletic.net event records URL"', app.HTML_PAGE)
        self.assertNotIn('value="https://www.athletic.net/team/16546/track-and-field-outdoor/2025/event-records"', app.HTML_PAGE)

    def test_ui_includes_clickable_athlete_panel(self):
        self.assertIn('id="athlete-panel"', app.HTML_PAGE)
        self.assertIn("athlete-chip", app.HTML_PAGE)
        self.assertIn("openAthletePanel", app.HTML_PAGE)
        self.assertIn("buildAthleteIndex", app.HTML_PAGE)
        self.assertIn("left: 18px", app.HTML_PAGE)
        self.assertNotIn("body.athlete-panel-open section", app.HTML_PAGE)

    def test_ui_includes_event_sort_options(self):
        self.assertIn('id="event-sort"', app.HTML_PAGE)
        self.assertIn('data-sort="schedule"', app.HTML_PAGE)
        self.assertIn('data-sort="distance"', app.HTML_PAGE)
        self.assertIn('let activeEventSort = "distance"', app.HTML_PAGE)
        self.assertIn('<button class="sort-option active" data-sort="distance"', app.HTML_PAGE)
        self.assertIn("sortedEventNames", app.HTML_PAGE)
        self.assertIn('"4x800 relay", "4x100 relay", "3200m", "110h"', app.HTML_PAGE)
        self.assertIn('"shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"', app.HTML_PAGE)
        self.assertIn('"100m", "200m", "400m", "800m", "1600m", "3200m", "110h", "300h"', app.HTML_PAGE)
        self.assertIn('"4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"', app.HTML_PAGE)

    def test_ui_includes_save_load_project_controls(self):
        self.assertIn('id="save-button"', app.HTML_PAGE)
        self.assertIn('id="load-button"', app.HTML_PAGE)
        self.assertIn('id="load-file"', app.HTML_PAGE)
        self.assertIn("saveLineupProject", app.HTML_PAGE)
        self.assertIn("loadLineupProject", app.HTML_PAGE)
        self.assertIn("track-lineup-project", app.HTML_PAGE)
        self.assertIn("current lineup, relays, points, and parsed school/opponent data", app.HTML_PAGE)

    def test_ui_includes_individual_event_info_popover(self):
        self.assertIn("eventInfoIcon", app.HTML_PAGE)
        self.assertIn("event_standings", app.HTML_PAGE)
        self.assertIn("event-info-popover", app.HTML_PAGE)
        self.assertIn("standings-list", app.HTML_PAGE)
        self.assertIn("renderStandingRow", app.HTML_PAGE)
        self.assertIn("toggleEventInfo", app.HTML_PAGE)
        self.assertIn('data-event-info="${escapeHtml(event)}"', app.HTML_PAGE)
        self.assertIn(".event-info.open .event-info-popover", app.HTML_PAGE)
        self.assertNotIn(".event-info:hover .event-info-popover", app.HTML_PAGE)
        self.assertNotIn(".event-info:focus .event-info-popover", app.HTML_PAGE)

    def test_ui_includes_coach_edit_actions(self):
        self.assertIn('data-edit-action="move"', app.HTML_PAGE)
        self.assertIn('data-edit-action="remove"', app.HTML_PAGE)
        self.assertIn('data-edit-action="add"', app.HTML_PAGE)
        self.assertIn("move-action", app.HTML_PAGE)
        self.assertIn("remove-action", app.HTML_PAGE)
        self.assertIn("add-action", app.HTML_PAGE)
        self.assertIn("Who will replace", app.HTML_PAGE)
        self.assertIn("Who will ${editState.athlete} replace", app.HTML_PAGE)
        self.assertIn("replacementSuggestions", app.HTML_PAGE)
        self.assertIn("event-asterisk", app.HTML_PAGE)
        self.assertIn("choice-warning-text", app.HTML_PAGE)
        self.assertIn("would have back to back running events", app.HTML_PAGE)
        self.assertIn('${isCurrent ? "disabled" : ""}', app.HTML_PAGE)
        self.assertIn("/api/rescore", app.HTML_PAGE)

    def test_demo_result_includes_edit_context(self):
        result = app.demo_result()
        self.assertIn("school_performances", result.edit_context)
        self.assertIn("opponent_performances", result.edit_context)
        self.assertIn("relay_leg_values", result.edit_context)
        self.assertGreater(len(result.edit_context["school_performances"]), 0)

    def test_indoor_demo_and_rescore_preserve_meet_profile(self):
        result = app.demo_result(None, "indoor", "60")
        self.assertEqual(result.edit_context["meet_config"]["season_type"], "indoor")
        self.assertEqual(result.edit_context["meet_config"]["indoor_sprint_distance"], "60")
        self.assertIn("60m", result.edit_context["events"])
        self.assertNotIn("100m", result.edit_context["events"])

        rescored = app.rescore_edited_result(app.asdict(result))
        self.assertEqual(rescored.edit_context["meet_config"]["season_type"], "indoor")
        self.assertIn("60m", rescored.edit_context["events"])
        self.assertNotIn("100m", rescored.edit_context["events"])

    def test_rescore_edited_result_updates_points(self):
        result = app.demo_result()
        edited = app.asdict(result)
        edited["lineup"]["100m"] = [
            entry for entry in edited["lineup"].get("100m", [])
            if entry["athlete"] != "Alex Carter"
        ]
        rescored = app.rescore_edited_result(edited)
        self.assertIn("100m", rescored.event_points)
        self.assertLessEqual(rescored.event_points["100m"], result.event_points.get("100m", 0))
        self.assertIn("school_performances", rescored.edit_context)

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

    def test_event_standings_include_top_eight_projection_context(self):
        school = [
            app.Performance("School Fast", "100m", "10.60", 10.60, True, "Your Team", "school"),
            app.Performance("School Depth", "100m", "11.20", 11.20, True, "Your Team", "school"),
        ]
        opponents = [
            app.Performance(
                f"Opponent Runner {index}",
                "100m",
                f"{10.70 + index / 100:.2f}",
                10.70 + index / 100,
                True,
                f"Opponent School {index}",
                "opponent",
            )
            for index in range(8)
        ]
        result = app.evaluate_lineup(
            {"100m": ["School Fast", "School Depth"]},
            {},
            school,
            opponents,
        )
        standings = result.event_standings["100m"]
        self.assertEqual(len(standings), 8)
        self.assertEqual(standings[0]["athlete"], "School Fast")
        self.assertEqual(standings[0]["school"], "Your Team")
        self.assertEqual(standings[0]["team_role"], "school")
        self.assertEqual(standings[0]["projected_mark"], "10.60")
        self.assertEqual(standings[0]["place_label"], "1st")
        self.assertEqual(standings[0]["projected_points"], 10.0)
        self.assertEqual(standings[-1]["place_label"], "8th")
        self.assertEqual(standings[-1]["projected_points"], 1.0)

    def test_relay_standings_include_projected_opponent_relays(self):
        opponent_relay = app.Performance(
            "Opponent Relay",
            "4x100 relay",
            "42.00",
            42.0,
            True,
            "Opponent",
            app.PROJECTED_OPPONENT_ROLE,
        )
        result = app.evaluate_lineup(
            {},
            {
                "4x100 relay": app.RelaySelection(
                    "4x100 relay",
                    ("A", "B", "C", "D"),
                    43.0,
                    "synthetic",
                    "test",
                )
            },
            [],
            [opponent_relay],
            [],
            [],
            [opponent_relay],
        )
        standings = result.event_standings["4x100 relay"]
        self.assertEqual([row["school"] for row in standings], ["Opponent", "Your Team"])
        self.assertEqual([row["projected_points"] for row in standings], [10.0, 8.0])
        self.assertEqual(result.relays["4x100 relay"]["projected_points"], 8.0)

    def test_relay_fatigue_uses_meet_order(self):
        school = []
        for name in ("A", "B", "C", "D"):
            school.extend(
                [
                    app.Performance(name, "100m", "11.00", 11.0, True, "School", "school"),
                    app.Performance(name, "200m", "22.00", 22.0, True, "School", "school"),
                    app.Performance(name, "400m", "50.00", 50.0, True, "School", "school"),
                ]
            )
        opponent_relay = app.Performance(
            "Opponent Relay",
            "4x100 relay",
            "40.50",
            40.5,
            True,
            "Opponent",
            app.PROJECTED_OPPONENT_ROLE,
        )
        result = app.evaluate_lineup(
            {
                "100m": ["A", "B", "C"],
                "200m": ["A", "B", "C"],
                "400m": ["A", "B", "C"],
            },
            {
                "4x100 relay": app.RelaySelection(
                    "4x100 relay",
                    ("A", "B", "C", "D"),
                    40.0,
                    "historic",
                    "40.00",
                )
            },
            school,
            [opponent_relay],
            [],
            [],
            [opponent_relay],
        )
        self.assertEqual(result.relays["4x100 relay"]["projected_seconds"], 40.0)
        self.assertEqual(result.relays["4x100 relay"]["projected_points"], 10.0)

    def test_build_relay_fatigue_ignores_later_events(self):
        relay = app.RelaySelection(
            "4x100 relay",
            ("A", "B", "C", "D"),
            40.0,
            "historic",
            "40.00",
        )
        athlete_events = defaultdict(list)
        athlete_events["A"] = ["100m", "400m", "200m"]
        athlete_events["B"] = ["400m", "200m"]
        athlete_events["C"] = ["200m"]
        athlete_events["D"] = []
        self.assertEqual(app.prior_event_count(athlete_events["A"], "4x100 relay"), 0)
        self.assertEqual(app.relay_selection_time_for_build(relay, athlete_events), 40.0)

    def test_team_points_include_opponents(self):
        school = [app.Performance("School Runner", "100m", "10.80", 10.8, True, "School", "school")]
        opponents = [
            app.Performance(
                "Opponent Runner",
                "100m",
                "10.70",
                10.7,
                True,
                "Opponent",
                app.PROJECTED_OPPONENT_ROLE,
            )
        ]
        result = app.evaluate_lineup({"100m": ["School Runner"]}, {}, school, opponents)
        self.assertEqual(result.total_points, 8.0)
        self.assertEqual(result.team_points["Opponent"], 10.0)
        self.assertEqual(result.team_points["School"], 8.0)

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

    def test_top_opponent_individual_entries_uses_raw_top_three_per_event(self):
        data = app.ScrapeResult(
            [
                app.Performance(f"Opp Runner {index}", "400m", f"{time:.2f}", time, True, "Opponent", "opponent")
                for index, time in enumerate([47.94, 49.38, 50.31, 52.13], start=1)
            ],
            [],
            [],
        )
        entries = app.top_opponent_individual_entries(data, "Opponent")
        selected_400 = [entry for entry in entries if entry.event == "400m"]

        self.assertEqual([entry.value for entry in selected_400], [47.94, 49.38, 50.31])
        self.assertTrue(all(entry.team_role == app.PROJECTED_OPPONENT_ROLE for entry in selected_400))

    def test_run_optimizer_scores_against_opponent_raw_top_three(self):
        school_result = app.ScrapeResult(
            [
                app.Performance("School One", "400m", "49.60", 49.60, True, "School", "school"),
                app.Performance("School Two", "400m", "50.36", 50.36, True, "School", "school"),
                app.Performance("School Three", "400m", "50.75", 50.75, True, "School", "school"),
            ],
            [],
            [],
        )
        opponent_result = app.ScrapeResult(
            [
                app.Performance("Fast Opp", "400m", "47.94", 47.94, True, "Opponent", "opponent"),
                app.Performance("Second Opp", "400m", "49.38", 49.38, True, "Opponent", "opponent"),
                app.Performance("Third Opp", "400m", "50.31", 50.31, True, "Opponent", "opponent"),
                app.Performance("Fourth Opp", "400m", "52.13", 52.13, True, "Opponent", "opponent"),
            ],
            [],
            [],
        )
        with patch("app.scrape_team_data", side_effect=[school_result, opponent_result]):
            result = app.run_optimizer("school-url", ["opponent-url"], "mens")

        standings = result.event_standings["400m"]
        self.assertEqual([row["athlete"] for row in standings[:4]], ["Fast Opp", "Second Opp", "School One", "Third Opp"])
        self.assertEqual(result.lineup["400m"][0]["projected_place_label"], "3rd")

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
        self.assertFalse(app.can_event_set_stand(["4x800 relay", "800m", "1600m"]))
        self.assertTrue(app.can_event_set_stand(["3200m", "1600m"]))
        self.assertTrue(app.can_event_set_stand(["4x800 relay", "1600m"]))
        self.assertTrue(app.can_event_set_stand(["4x800 relay", "800m"]))
        self.assertTrue(app.can_event_set_stand(["800m", "4x400 relay"]))
        self.assertTrue(app.can_event_set_stand(["4x800 relay", "800m", "4x400 relay"]))
        self.assertTrue(app.can_event_set_stand(["4x800 relay", "400m", "4x400 relay"]))
        self.assertFalse(app.can_event_set_stand(["800m", "1600m", "3200m"]))
        self.assertFalse(app.can_event_set_stand(["800m", "1600m", "long jump"]))
        self.assertFalse(app.can_event_set_stand(["400m", "4x400 relay"]))

    def test_distance_pass_replaces_worse_individual_entry_when_points_increase(self):
        school = [
            app.Performance("Distance Star", "800m", "1:55.00", 115.0, True, "Team", "school"),
            app.Performance("Distance Star", "1600m", "4:15.00", 255.0, True, "Team", "school"),
            app.Performance("Steady One", "1600m", "4:30.00", 270.0, True, "Team", "school"),
            app.Performance("Steady Two", "1600m", "4:35.00", 275.0, True, "Team", "school"),
            app.Performance("Steady Three", "1600m", "5:00.00", 300.0, True, "Team", "school"),
        ]
        opponents = [
            app.Performance("Opp One", "1600m", "4:20.00", 260.0, True, "Opp", "opponent"),
            app.Performance("Opp Two", "1600m", "4:25.00", 265.0, True, "Opp", "opponent"),
            app.Performance("Opp Three", "1600m", "4:28.00", 268.0, True, "Opp", "opponent"),
        ]
        lineup = {event: [] for event in app.EVENTS if event not in app.RELAY_EVENTS}
        lineup["800m"] = ["Distance Star"]
        lineup["1600m"] = ["Steady One", "Steady Two", "Steady Three"]

        improved_lineup, _relays = app.optimize_distance_runner_utilization(
            lineup,
            {},
            school,
            opponents,
            [],
            [],
            [],
            [],
        )

        self.assertIn("Distance Star", improved_lineup["1600m"])
        self.assertNotIn("Steady Three", improved_lineup["1600m"])
        self.assertTrue(app.lineup_is_valid(improved_lineup, {}))

    def test_distance_pass_replaces_slowest_synthetic_relay_leg_when_points_increase(self):
        school = [
            app.Performance("Distance Star", "800m", "1:50.00", 110.0, True, "Team", "school"),
            app.Performance("Leg One", "800m", "2:00.00", 120.0, True, "Team", "school"),
            app.Performance("Leg Two", "800m", "2:00.00", 120.0, True, "Team", "school"),
            app.Performance("Leg Three", "800m", "2:00.00", 120.0, True, "Team", "school"),
            app.Performance("Leg Four", "800m", "2:03.00", 123.0, True, "Team", "school"),
        ]
        opponents = [
            app.Performance(
                "Opp Relay",
                "4x800 relay",
                "7:50.00",
                470.0,
                True,
                "Opp",
                app.PROJECTED_OPPONENT_ROLE,
            )
        ]
        lineup = {event: [] for event in app.EVENTS if event not in app.RELAY_EVENTS}
        lineup["800m"] = ["Distance Star"]
        relays = {
            "4x800 relay": app.RelaySelection(
                "4x800 relay",
                ("Leg One", "Leg Two", "Leg Three", "Leg Four"),
                481.0,
                "synthetic",
                "test relay",
                (120.0, 120.0, 120.0, 123.0),
                (
                    app.INDIVIDUAL_LEG_SOURCE,
                    app.INDIVIDUAL_LEG_SOURCE,
                    app.INDIVIDUAL_LEG_SOURCE,
                    app.INDIVIDUAL_LEG_SOURCE,
                ),
            )
        }

        _lineup, improved_relays = app.optimize_distance_runner_utilization(
            lineup,
            relays,
            school,
            opponents,
            [],
            [],
            [],
            [],
        )

        self.assertIn("Distance Star", improved_relays["4x800 relay"].athletes)
        self.assertNotIn("Leg Four", improved_relays["4x800 relay"].athletes)
        self.assertTrue(app.lineup_is_valid(lineup, improved_relays))

    def test_fatigue_factor_is_light_touch(self):
        self.assertEqual(app.fatigue_factor(0), 1.0)
        self.assertEqual(app.fatigue_factor(1), 1.0)
        self.assertEqual(app.fatigue_factor(2), 1.005)
        self.assertEqual(app.fatigue_factor(3), 1.01)
        self.assertAlmostEqual(app.apply_fatigue(50.0, True, 3), 50.5)
        self.assertEqual(app.apply_fatigue(240.0, False, 3), 240.0)

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

    def test_priority_runner_rank_appends_hurdlers_after_flat_sprinters(self):
        school = [
            app.Performance("Flat Star", "100m", "10.50", 10.5, True, "Team", "school"),
            app.Performance("Flat Star", "200m", "21.50", 21.5, True, "Team", "school"),
            app.Performance("Flat Star", "400m", "50.00", 50.0, True, "Team", "school"),
            app.Performance("Hurdle Star", "100m", "11.50", 11.5, True, "Team", "school"),
            app.Performance("Hurdle Star", "200m", "23.50", 23.5, True, "Team", "school"),
            app.Performance("Hurdle Star", "110h", "14.50", 14.5, True, "Team", "school"),
            app.Performance("Hurdle Star", "300h", "39.50", 39.5, True, "Team", "school"),
        ]
        potentials = {
            ("Flat Star", "100m"): 10.0,
            ("Flat Star", "200m"): 10.0,
            ("Flat Star", "400m"): 10.0,
            ("Hurdle Star", "110h"): 10.0,
            ("Hurdle Star", "300h"): 8.0,
            ("Hurdle Star", "100m"): 0.0,
            ("Hurdle Star", "200m"): 0.0,
        }
        pure_ranked = app.rank_elite_sprint_jump_athletes(school, potentials, {}, [], [], [])
        priority_ranked = app.rank_priority_running_athletes(school, potentials, {}, [], [], [])
        self.assertIn("Flat Star", pure_ranked)
        self.assertNotIn("Hurdle Star", pure_ranked)
        self.assertLess(priority_ranked.index("Flat Star"), priority_ranked.index("Hurdle Star"))

    def test_optimize_lineup_does_not_strip_protected_runner(self):
        school = [
            app.Performance("Protected Runner", "100m", "12.00", 12.0, True, "Team", "school"),
            app.Performance("Depth Runner", "100m", "10.50", 10.5, True, "Team", "school"),
        ]
        opponents = [
            app.Performance("Opponent Runner", "100m", "10.80", 10.8, True, "Opp", "opponent"),
        ]
        lineup = {"100m": ["Protected Runner"]}
        optimized, _relays = app.optimize_lineup(
            lineup,
            {},
            school,
            opponents,
            defaultdict(list),
            protected_athletes={"Protected Runner"},
        )
        self.assertEqual(optimized["100m"], ["Protected Runner"])

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

    def test_indoor_fastest_historic_4x200_is_reserved_before_individual_events(self):
        token = app.ACTIVE_MEET_CONFIG.set(app.meet_config_for("indoor", "60"))
        try:
            school = [
                app.Performance("Andrew Hebron", "60m", "7.25", 7.25, True, "Team", "school"),
                app.Performance("Andrew Hebron", "200m", "22.64", 22.64, True, "Team", "school"),
                app.Performance("Andrew Hebron", "400m", "50.78", 50.78, True, "Team", "school"),
                app.Performance("Jude Knechtel", "60m", "7.30", 7.30, True, "Team", "school"),
                app.Performance("Jude Knechtel", "200m", "23.12", 23.12, True, "Team", "school"),
                app.Performance("Jude Knechtel", "400m", "51.68", 51.68, True, "Team", "school"),
                app.Performance("Jayke Collins", "60m", "7.14", 7.14, True, "Team", "school"),
                app.Performance("Jayke Collins", "200m", "23.66", 23.66, True, "Team", "school"),
                app.Performance("Jayke Collins", "400m", "54.04", 54.04, True, "Team", "school"),
                app.Performance("Mason Hill", "60h", "8.45", 8.45, True, "Team", "school"),
                app.Performance("Mason Hill", "200m", "23.97", 23.97, True, "Team", "school"),
            ]
            history = [
                app.RelayPerformance(
                    "4x200 relay",
                    ("Andrew Hebron", "Jude Knechtel", "Jayke Collins", "Mason Hill"),
                    "1:29.18a",
                    89.18,
                    "Team",
                    "school",
                )
            ]

            result = app.build_lineup(school, [], history)
            relay = result["relays"]["4x200 relay"]
            athlete_events = app.collect_athlete_events(result["lineup"], result["relays"])

            self.assertEqual(relay.method, "historic")
            self.assertEqual(
                relay.athletes,
                ("Andrew Hebron", "Jude Knechtel", "Jayke Collins", "Mason Hill"),
            )
            self.assertIn("4x200 relay", athlete_events["Andrew Hebron"])
            self.assertNotIn("400m", athlete_events["Andrew Hebron"])
            self.assertTrue(app.lineup_is_valid(result["lineup"], result["relays"]))
        finally:
            app.ACTIVE_MEET_CONFIG.reset(token)

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
        self.assertEqual(
            selection.leg_sources,
            (
                app.INDIVIDUAL_LEG_SOURCE,
                app.RELAY_SPLIT_LEG_SOURCE,
                app.RELAY_SPLIT_LEG_SOURCE,
                app.RELAY_SPLIT_LEG_SOURCE,
            ),
        )
        self.assertAlmostEqual(selection.projected_time, 201.9)

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
        self.assertEqual(
            selection.leg_sources,
            (
                app.INDIVIDUAL_LEG_SOURCE,
                app.INDIVIDUAL_LEG_SOURCE,
                app.INDIVIDUAL_LEG_SOURCE,
                app.RELAY_SPLIT_LEG_SOURCE,
            ),
        )
        self.assertAlmostEqual(selection.projected_time, 201.7)

    def test_synthetic_relay_credit_applies_only_to_individual_legs(self):
        self.assertAlmostEqual(
            app.synthetic_relay_time(
                "4x100 relay",
                (11.0, 11.1, 11.2, 10.8),
                (
                    app.INDIVIDUAL_LEG_SOURCE,
                    app.RELAY_SPLIT_LEG_SOURCE,
                    app.INDIVIDUAL_LEG_SOURCE,
                    app.RELAY_SPLIT_LEG_SOURCE,
                ),
            ),
            42.7,
        )

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
        self.assertEqual(estimates, [90.0])
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
        self.assertEqual(estimates, [200.0, 201.0])

    def test_opponent_relay_estimates_do_not_use_synthetic_times(self):
        opponents = [
            app.Performance(f"Opp Runner {index}", "800m", "1:55.00", 115.0, True, "Opponent", "opponent")
            for index in range(4)
        ]
        self.assertEqual(app.estimate_opponent_relays("4x800 relay", opponents, []), [])

    def test_independent_opponent_projection_uses_historic_relay_not_synthetic(self):
        data = app.ScrapeResult(
            [
                app.Performance(f"Opp Runner {index}", "800m", "1:50.00", 110.0, True, "Opponent", "opponent")
                for index in range(4)
            ],
            [
                app.RelayPerformance(
                    "4x800 relay",
                    ("Opp Runner 0", "Opp Runner 1", "Opp Runner 2", "Opp Runner 3"),
                    "8:00.00",
                    480.0,
                    "Opponent",
                    "opponent",
                )
            ],
            [],
        )
        projection = app.build_independent_team_projection(data, "Opponent", app.PROJECTED_OPPONENT_ROLE)
        relay_entries = [entry for entry in projection.relay_entries if entry.event == "4x800 relay"]
        self.assertEqual(len(relay_entries), 1)
        self.assertEqual(relay_entries[0].value, 480.0)

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
