"""Fake drivers for hermetic tests/CI — no network, no models.

`FakeTranscriber` returns a fixed transcript for any input.
`FakeLLM` echoes a canned clip-analysis JSON so the whole pipeline runs
without a real LLM endpoint.
"""

from __future__ import annotations

import json

from app.media.transcriber import Segment, Transcript, Word

_FIXTURE_SEGMENTS = [
    Segment(
        start=0.0, end=3.0, text="Ini kalimat pembuka yang menarik.",
        words=[Word(0.0, 1.0, "Ini"), Word(1.0, 2.0, "kalimat"),
               Word(2.0, 3.0, "pembuka")],
    ),
    Segment(
        start=3.5, end=8.0, text="Lalu di sini ada poin penting yang standalone.",
        words=[Word(3.5, 4.5, "Lalu"), Word(4.5, 5.5, "di"),
               Word(5.5, 6.5, "sini"), Word(6.5, 8.0, "penting.")],
    ),
    Segment(
        start=9.0, end=14.0, text="Dan akhirnya kesimpulan dari bahasan ini.",
        words=[Word(9.0, 10.0, "Dan"), Word(10.0, 11.0, "akhirnya"),
               Word(11.0, 14.0, "kesimpulan.")],
    ),
]


class FakeTranscriber:
    """TranscriberPort that ignores input and returns a fixed transcript."""

    def transcribe(self, video_path: str, work_dir: str, *, token=None) -> Transcript:
        if token is not None:
            token.check()
        return Transcript(language="id", duration=14.0, segments=list(_FIXTURE_SEGMENTS))


_CANNED_CLIPS = {
    "clips": [
        {
            "start": 0.0, "end": 8.0, "viral_score": 88,
            "hook": "INI HOOK PALSU", "topic": "pembukaan",
            "caption": "caption #fyp #shorts", "reason": "pembuka kuat",
            "risk_flags": [],
        },
        {
            "start": 9.0, "end": 14.0, "viral_score": 71,
            "hook": "KESIMPULAN", "topic": "penutup",
            "caption": "caption penutup #shorts", "reason": "payoff jelas",
            "risk_flags": ["tag_ai"],
        },
    ]
}


class FakeLLM:
    """LLMPort that returns a fixed JSON analysis, ignoring the prompt."""

    def chat(self, messages, *, json_mode: bool = True) -> str:
        return json.dumps(_CANNED_CLIPS)
