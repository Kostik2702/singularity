"""Reel engine: the beat contract, the EDL guards and the safety checks.

These lock in the three things that cost a re-render when they were wrong:
the cumulative-beat quantisation, the EDL validator, and a flash detector that
is not accidentally lenient.
"""
from __future__ import annotations

import json
import math

import pytest

from singularity.reel import qa
from singularity.reel.edl import EDL, Grid, Shot
from singularity.reel.effects import LightAct, LightRig, act_arc, camera_rig
from singularity.reel.music import suggest_section
from singularity.reel.render import plan_fit


# --- beat grid ----------------------------------------------------------------

def test_cumulative_quantisation_has_no_drift():
    """126 BPM at 24 fps is 11.43 frames/beat. Rounding DURATIONS accumulates
    error; rounding cumulative positions does not."""
    g = Grid(bpm=126.0, fps=24)
    edl = EDL(grid=g, shots=[Shot("A", 0.0, b) for b in range(4, 85, 4)],
              sources={"A": "x"})
    assert edl.total_frames == g.frame_of_beat(84) == 960
    assert sum(edl.durations) == edl.total_frames
    naive = sum(round(4 * g.beat_frames) for _ in range(21))
    assert naive != edl.total_frames          # the drift this contract prevents


def test_beat_of_frame_is_the_inverse_of_frame_of_beat():
    """A naive floor(f / beat_frames) lands a beat early on roughly half the
    bars, because the beat boundary falls inside a frame."""
    g = Grid(bpm=126.0, fps=24)
    mismatches = 0
    for beat in range(0, 84):
        f = g.frame_of_beat(beat)
        assert g.beat_of_frame(f) == beat
        assert g.frac_into_beat(f) == pytest.approx(0.0, abs=1e-9)
        if int(math.floor(f / g.beat_frames)) != beat:
            mismatches += 1
    assert mismatches > 0                      # the bug this method exists to avoid


def test_downbeats_land_on_bars():
    g = Grid(bpm=105.5, fps=24)
    assert g.is_downbeat(g.frame_of_beat(0))
    assert g.is_downbeat(g.frame_of_beat(4))
    assert not g.is_downbeat(g.frame_of_beat(5))


# --- EDL validation -----------------------------------------------------------

def _edl(shots):
    return EDL(grid=Grid(bpm=105.5, fps=24), shots=shots,
               sources={"A": "a.mp4", "B": "b.mp4"})


def test_validator_accepts_a_sound_edl():
    e = _edl([Shot("A", 0.0, 5), Shot("B", 2.9, 16), Shot("A", 4.7, 23)])
    assert e.validate({"A": 361, "B": 361}) == []


def test_validator_catches_running_past_the_clip():
    e = _edl([Shot("A", 11.08, 7)])
    problems = e.validate({"A": 361})
    assert problems and "ends at 361" in problems[0]


def test_validator_catches_overlap_in_the_same_source():
    """Two shots reading the same moment is not harmless — it plays twice."""
    e = _edl([Shot("A", 8.20, 5), Shot("A", 8.60, 10)])
    problems = e.validate({"A": 361})
    assert any("overlaps" in p for p in problems)


def test_validator_allows_the_same_moment_in_different_sources():
    e = _edl([Shot("A", 8.2, 5), Shot("B", 8.2, 10)])
    assert e.validate({"A": 361, "B": 361}) == []


def test_validator_catches_a_backwards_emotional_ladder():
    e = _edl([Shot("A", 0.0, 4, heat=1), Shot("B", 0.0, 8, heat=5),
              Shot("A", 6.0, 12, heat=2)])
    problems = e.validate({"A": 361, "B": 361})
    assert any("emotional ladder" in p for p in problems)


def test_validator_catches_the_world_un_happening():
    """The exact defect found in the Dead Revolutionary cut: ground cracks (2),
    a hand bursts out of it (3), and then the intact crack is on screen again.
    Every other check passes — the cut is on the beat, the axis holds, the
    colour matches — because none of them know what the shots MEAN."""
    e = _edl([Shot("A", 0.0, 4, stage=2, note="земля трескается"),
              Shot("B", 0.0, 8, stage=3, note="рука пробивает землю"),
              Shot("A", 4.0, 12, stage=2, note="снова трещина")])
    problems = e.validate({"A": 361, "B": 361})
    assert any("goes BACK in the story" in p for p in problems)
    assert any("stage 2" in p for p in problems)


def test_a_marked_reprise_may_return_to_an_earlier_stage():
    e = _edl([Shot("A", 0.0, 4, stage=2), Shot("B", 0.0, 8, stage=5),
              Shot("A", 4.0, 12, stage=2, reprise=True, note="финальная реприза")])
    assert e.validate({"A": 361, "B": 361}) == []


def test_causality_uses_the_running_maximum_not_the_previous_shot():
    """A single dip must not hide everything after it."""
    e = _edl([Shot("A", 0.0, 4, stage=5), Shot("B", 0.0, 8, stage=1),
              Shot("A", 4.0, 12, stage=2)])
    problems = e.validate({"A": 361, "B": 361})
    assert len([p for p in problems if "goes BACK" in p]) == 2


def test_casting_sheet_refuses_rejected_and_unjudged_clips(tmp_path):
    from singularity.reel.casting import apply_to, save, scaffold
    sheet = scaffold([{"name": "good.mp4", "path": "a.mp4", "duration": 7.0},
                      {"name": "broken.mp4", "path": "b.mp4", "duration": 7.0}])
    sheet["clips"]["good.mp4"].update(verdict="use", stage=3)
    sheet["clips"]["broken.mp4"].update(verdict="reject", note="сломанная анатомия")
    save(sheet, tmp_path / "cast.json")

    e = EDL(grid=Grid(bpm=105.5, fps=24),
            shots=[Shot("A", 0.0, 4), Shot("B", 0.0, 8)],
            sources={"A": "a.mp4", "B": "b.mp4"})
    problems = apply_to(sheet, e)
    assert any("rejects" in p and "сломанная анатомия" in p for p in problems)
    assert e.shots[0].stage == 3          # stage flowed from the sheet into the EDL


def test_dissolve_extends_the_read_so_the_grid_does_not_drift():
    """xfade consumes the overlap out of the running time. If the shot does not
    supply those frames as EXTRA, every dissolve shortens the film and the cuts
    walk off the beat one transition at a time."""
    g = Grid(bpm=129.25, fps=24)
    e = EDL(grid=g, shots=[Shot("A", 0.0, 8, dissolve=8), Shot("A", 4.0, 16)],
            sources={"A": "a.mp4"})
    n0 = e.durations[0]
    # what render.py reads for shot 0: its own frames plus the overlap
    assert n0 + e.shots[0].dissolve > n0
    # and the timeline itself is untouched by the transition
    assert e.total_frames == g.frame_of_beat(16)


def test_punch_is_short_enough_to_read_as_an_accent():
    """Reference practice puts accent zooms at 0.2-0.4 s. Longer stops being an
    accent and becomes a camera move."""
    s = Shot("A", 0.0, 4, punch=1.18)
    assert 0.15 <= s.punch_frames / 24 <= 0.45


def test_casting_scaffold_starts_undecided():
    from singularity.reel.casting import scaffold, undecided
    sheet = scaffold([{"name": "x.mp4", "path": "x.mp4", "duration": 4.0}])
    assert undecided(sheet) == ["x.mp4"]


def test_validator_catches_a_non_advancing_grid():
    e = _edl([Shot("A", 0.0, 8), Shot("B", 0.0, 8)])
    assert any("does not advance" in p for p in e.validate({"A": 361, "B": 361}))


def test_edl_roundtrips_through_json(tmp_path):
    e = _edl([Shot("A", 0.0, 5, note="вход", heat=0, hflip=True)])
    p = tmp_path / "e.json"
    e.save(p)
    back = EDL.load(p)
    assert back.shots[0].hflip is True
    assert back.shots[0].note == "вход"
    assert back.total_frames == e.total_frames
    assert json.loads(p.read_text(encoding="utf-8"))["grid"]["bpm"] == 105.5


# --- format planning ----------------------------------------------------------

def test_letterbox_downscales_where_crop_upscales():
    """The counter-intuitive fact the owner gate is built on."""
    crop = plan_fit(1280, 720, (1080, 1920), "crop")
    box = plan_fit(1280, 720, (1080, 1920), "letterbox")
    assert crop["upscales"] and crop["scale"] > 2.5
    assert not box["upscales"] and box["scale"] < 1.0
    assert crop["kept_width_pct"] < 35 and box["kept_width_pct"] == 100.0
    assert box["free_band_px"] > 1000        # room the copy lives in


# --- flash safety -------------------------------------------------------------

def _series_to_flashes(series, fps=24, window=6, threshold=0.10):
    """Run the detector's core over a synthetic luminance series."""
    import numpy as np
    lum = np.asarray(series, dtype=np.float64)
    flashes = []
    for i in range(lum.size):
        lo_i, hi_i = max(0, i - window), min(lum.size, i + window + 1)
        if lum[i] < lum[lo_i:hi_i].max():
            continue
        rise = lum[i] - lum[lo_i:i + 1].min()
        fall = lum[i] - lum[i:hi_i].min()
        if rise >= threshold and fall >= threshold and min(lum[lo_i:i + 1].min(),
                                                           lum[i:hi_i].min()) < 0.80:
            flashes.append(i)
    return flashes


def test_windowed_detector_sees_a_flash_that_a_monotonic_walk_would_miss():
    """Real footage is noisy: one frame ticking up truncates a monotonic walk
    and hides the flash. This is why the detector uses windowed extrema."""
    base = [0.10] * 40
    base[20] = 0.40
    base[21] = 0.13          # noisy descent — a monotonic walk stops here
    base[22] = 0.14          # ...on this uptick, reporting fall = 0.26 -> then 0
    base[23] = 0.10
    assert 20 in _series_to_flashes(base)


def test_steady_footage_reports_no_flashes():
    assert _series_to_flashes([0.18 + 0.01 * (i % 3) for i in range(80)]) == []


def test_contrast_ratio_matches_wcag_reference():
    assert qa.contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0, abs=0.01)
    assert qa.contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)


# --- light rig ----------------------------------------------------------------

def test_light_rig_refuses_a_cadence_above_the_wcag_ceiling():
    fast = Grid(bpm=200.0, fps=24)            # 3.33 beats/s
    rig = LightRig(grid=fast, acts=[LightAct(0, 16, on_beat=0.2)])
    assert rig.verify_cadence()               # must complain

    ok = LightRig(grid=Grid(bpm=126.0, fps=24), acts=[LightAct(0, 16, on_beat=0.1)])
    assert ok.verify_cadence() == []          # 2.1/s is under the ceiling


def test_light_hits_land_exactly_on_the_cut_frame():
    """The grid rounding bug: the hit must fire on the same frame the EDL cuts."""
    g = Grid(bpm=126.0, fps=24)
    rig = LightRig(grid=g, acts=act_arc(84))
    for beat in range(0, 84, 4):
        amp, _ = rig.amount(g.frame_of_beat(beat))
        assert amp > 0.05, f"bar at beat {beat} did not fire on its own frame"


def test_light_decays_within_a_few_frames():
    g = Grid(bpm=126.0, fps=24)
    rig = LightRig(grid=g, acts=[LightAct(0, 84, on_bar=0.36)])
    f = g.frame_of_beat(4)
    peak, _ = rig.amount(f)
    later, _ = rig.amount(f + 4)
    assert later < peak * 0.25


# --- camera flash rig ---------------------------------------------------------

def test_camera_rig_refuses_a_cadence_over_the_wcag_ceiling():
    """The gate must REFUSE, not warn. 200 BPM on the beat is 3.33 flashes a
    second and the ceiling is medical, not a matter of taste."""
    g = Grid(bpm=200.0, fps=24)
    rig = camera_rig(g, 40)
    assert rig.verify_cadence(), "a 3.33/s on-beat cadence was allowed through"


def test_camera_rig_passes_at_a_reel_tempo():
    g = Grid(bpm=105.5, fps=24)
    assert camera_rig(g, 16).verify_cadence() == []


def test_camera_rig_leaves_the_closing_shot_dark():
    """Act IV drops the beat pulse entirely: a flash across the last beats would
    interrupt the one image the reel exists to deliver."""
    g = Grid(bpm=105.5, fps=24)
    rig = camera_rig(g, 16)
    end = g.frame_of_beat(16)
    hits = rig.hits(end)
    assert hits, "the rig fired nothing at all"
    assert rig.acts[-1].on_beat == 0.0, "act IV must DROP the pulse, not taper it"
    quiet = (end - max(h["frame"] for h in hits)) / g.fps
    assert quiet >= 1.0, f"only {quiet:.2f}s of darkness over the closing shot"


def test_camera_rig_hits_are_frame_exact_and_ordered():
    """The sound pass delays clicks by these times. A hit off by a frame reads as
    two events instead of one."""
    g = Grid(bpm=105.5, fps=24)
    hits = camera_rig(g, 16).hits(g.frame_of_beat(16))
    for h in hits:
        assert h["frame"] == g.frame_of_beat(h["beat"])
        assert h["t"] == pytest.approx(h["frame"] / g.fps, abs=1e-4)
    assert [h["frame"] for h in hits] == sorted(h["frame"] for h in hits)


def test_camera_rig_amplitude_climbs_across_the_acts():
    g = Grid(bpm=105.5, fps=24)
    hits = camera_rig(g, 16).hits(g.frame_of_beat(16))
    bars = [h["amp"] for h in hits if h["bar"]]
    assert bars == sorted(bars), f"bar hits do not build: {bars}"


# --- section choice -----------------------------------------------------------

def test_suggest_section_climbs_into_the_peak_and_lands_after_it():
    bars = ([{"bar": i + 1, "t": i * 2.0, "rms": 0.08, "bright": 0.1} for i in range(10)]
            + [{"bar": 11 + i, "t": (10 + i) * 2.0, "rms": 0.19, "bright": 0.35}
               for i in range(8)]
            + [{"bar": 19 + i, "t": (18 + i) * 2.0, "rms": 0.09, "bright": 0.2}
               for i in range(6)])
    pick = suggest_section(bars, 12)
    assert 4 <= pick["start_bar"] <= 10       # starts in the climb, not at the peak
