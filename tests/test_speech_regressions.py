from __future__ import annotations

import pytest


@pytest.mark.parametrize("numeric_date", ["14/10/2026", "2026/10/14"])
def test_speech_cleanup_reads_slash_dates_instead_of_calling_them_file_paths(numeric_date):
    from jarvis.voice.speech import for_speech

    spoken = for_speech(f"The report date is {numeric_date}.")

    assert "14 October 2026" in spoken
    assert "file path" not in spoken
