"""Config loading, and the live-conversation pause budget in particular."""
from __future__ import annotations

from backend.config import LiveConfig, load_config


def test_live_section_is_optional_and_defaults_are_the_measured_ones():
    """A config.toml with no [live] section must still load."""
    live = LiveConfig()
    assert live.silence_end_ms == 1200
    assert live.max_utterance_seconds == 30.0


def test_repo_config_allows_a_full_second_of_hesitation():
    """Regression guard on the turn-end threshold.

    Measured against silero-vad with a two-clause sentence: a threshold of T ms
    splits the sentence whenever the learner's mid-sentence pause reaches T. The
    original 600 ms therefore cut every pause of 600 ms or longer, which is
    ordinary hesitation for the B2 learner this app targets — the app even
    reports `longest_pause_seconds` as a fluency metric, so it expects pauses it
    was refusing to sit through.

    Below 1000 ms the learner gets interrupted mid-sentence. Do not lower it.
    """
    config = load_config()
    assert config.live.silence_end_ms >= 1000


def test_live_values_come_from_the_file():
    config = load_config()
    assert isinstance(config.live.silence_end_ms, int)
    assert config.live.max_utterance_seconds > 0
