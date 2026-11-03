from clippulse.config import Settings, load_settings


def test_defaults_are_conservative():
    s = Settings()
    assert s.max_temp_streamers == 10 and s.temp_ttl_hours == 48 and s.pinned_streamers == ()
    assert s.lookups_per_client == 5 and s.lookups_global == 30


def test_pinned_streamers_are_parsed_from_a_comma_list():
    s = load_settings({"PINNED_STREAMERS": " RDCworld1, @kaicenat ,,xqc "})
    assert s.pinned_streamers == ("rdcworld1", "kaicenat", "xqc")
    assert load_settings({"PINNED_STREAMERS": ""}).pinned_streamers == ()
    assert load_settings({}).pinned_streamers == ()


def test_existing_stacks_keep_working_with_the_old_max_streamers_name():
    assert load_settings({"MAX_STREAMERS": "7"}).max_temp_streamers == 7


def test_every_limit_can_be_tuned_from_the_environment():
    s = load_settings({"TEMP_TTL_HOURS": "12", "TEMP_MIN_KEEP_HOURS": "2", "LOOKUPS_PER_CLIENT": "3",
                       "CLIENT_WINDOW_SECONDS": "60", "LOOKUPS_GLOBAL": "9", "GLOBAL_WINDOW_SECONDS": "120",
                       "TOUCH_INTERVAL_MINUTES": "15"})
    assert (s.temp_ttl_hours, s.temp_min_keep_hours, s.lookups_per_client, s.client_window_seconds,
            s.lookups_global, s.global_window_seconds, s.touch_interval_minutes) == (12, 2, 3, 60, 9, 120, 15)
