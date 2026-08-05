"""Beat grid and EDL — the contract the whole pipeline hangs on.

Two rules are load-bearing and were both learned the hard way (vault:
`learnings/active/AUTO-REELS.md`, playbook step 9):

1. Cut points are quantised on the CUMULATIVE beat position, never per-shot
   duration. A beat is almost never a whole number of frames (126 BPM at 24 fps
   = 11.43), so rounding durations accumulates drift and the picture walks away
   from the music by the end. Rounding cumulative positions keeps every cut
   within +-0.5 frame and the total length exact.

2. Anything else that syncs to the edit (light hits, text events) must derive
   its beat index from THIS module, not recompute it from time. `beat_of_frame`
   uses the same rounding as `frame_of_beat`; a naive floor(f / beat_frames)
   lands a frame late on roughly half the bars, because the beat boundary falls
   inside a frame rather than on its edge.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Grid:
    bpm: float
    fps: int = 24
    phase: float = 0.0          # seconds from t=0 to the first downbeat
    beats_per_bar: int = 4

    @property
    def beat(self) -> float:
        return 60.0 / self.bpm

    @property
    def bar(self) -> float:
        return self.beat * self.beats_per_bar

    @property
    def beat_frames(self) -> float:
        return self.beat * self.fps

    @property
    def phase_frames(self) -> float:
        """Offset from the first video frame to the song's first downbeat.

        Without this the whole edit sits a fraction of a bar off the music: a
        track whose downbeat lands 1.24 s in would have every cut early by that
        much, which is more than a frame and plainly audible against the beat."""
        return self.phase * self.fps

    def frame_of_beat(self, beat: float) -> int:
        return round(self.phase_frames + beat * self.beat_frames)

    def beat_of_frame(self, frame: int) -> int:
        """Beat index whose rounded frame is <= frame. The +0.5 mirrors
        frame_of_beat's round() — see rule 2 in the module docstring."""
        return int(math.floor((frame - self.phase_frames + 0.5) / self.beat_frames))

    def frac_into_beat(self, frame: int) -> float:
        """0.0 exactly on a beat's frame, rising to ~1.0 just before the next."""
        bi = self.beat_of_frame(frame)
        return (frame - self.frame_of_beat(bi)) / self.beat_frames

    def is_downbeat(self, frame: int) -> bool:
        return self.beat_of_frame(frame) % self.beats_per_bar == 0


@dataclass
class Shot:
    """One cut. `end_beat` is the CUMULATIVE beat where the shot ends."""
    source: str
    in_sec: float
    end_beat: float
    note: str = ""
    crop_x: int | None = None       # portrait-crop offset; None = centre
    hflip: bool = False             # mirror so the subject keeps its side
    # `stage` is the shot's position in the STORY, not on the timeline: the
    # state of the world it shows. Ground intact = 1, ground cracking = 2, hand
    # out = 3, figure standing = 4. Once the hand is out, cutting back to intact
    # ground is a causality error, and it is invisible to every other check —
    # the cut is on the beat, the axis holds, the colour matches, and the film
    # still tells a broken story. `heat` is the same idea for intimacy.
    stage: int | None = None
    heat: int | None = None         # 0..n intimacy level; a stage by another name
    # --- per-shot treatment ---------------------------------------------
    grade: str = ""                 # ffmpeg filter chain, e.g. the colour match
    zoom: list[float] | None = None  # [from, to] push-in across the shot
    focus: list[float] | None = None  # [x, y] in 0..1 the zoom holds on
    speed: float = 1.0              # <1 slow motion, >1 sped up
    reprise: bool = False           # deliberate recall of earlier footage
    # Cross-dissolve INTO the next shot, in frames. 0 = hard cut. The rule that
    # makes a batch of takes read as one story: dissolve WITHIN a story stage
    # (consecutive shots are variations of the same moment, so a dissolve reads
    # as continuity), cut HARD between stages (there the story actually moves,
    # and a cut is the punctuation that says so).
    dissolve: int = 0
    # Accent punch: the shot lands already zoomed and settles back over
    # `punch_frames`. Short is the whole point — reference practice puts these
    # at 0.2-0.4 s (5-10 frames at 24 fps) and on the beat; a long punch stops
    # being an accent and becomes a camera move.
    punch: float = 0.0              # starting zoom, e.g. 1.18; 0 = no punch
    punch_frames: int = 7
    ca: int = 0                     # frames of chromatic aberration at the cut

    @property
    def source_frames_per_output(self) -> float:
        """Source frames consumed per output frame — speed, by another name."""
        return max(0.05, self.speed)


@dataclass
class EDL:
    grid: Grid
    shots: list[Shot]
    sources: dict[str, str] = field(default_factory=dict)   # id -> path

    # ---- derived -----------------------------------------------------------
    @property
    def edges(self) -> list[int]:
        return [0] + [self.grid.frame_of_beat(s.end_beat) for s in self.shots]

    @property
    def durations(self) -> list[int]:
        e = self.edges
        return [e[i + 1] - e[i] for i in range(len(self.shots))]

    @property
    def total_frames(self) -> int:
        return self.edges[-1]

    @property
    def duration(self) -> float:
        return self.total_frames / self.grid.fps

    def in_frame(self, shot: Shot) -> int:
        return round(shot.in_sec * self.grid.fps)

    # ---- validation --------------------------------------------------------
    def validate(self, source_frames: dict[str, int]) -> list[str]:
        """Return every problem found. Empty list = the EDL is renderable.

        Catches, before a single frame is encoded:
          * a shot running past the last frame of its clip;
          * two shots overlapping in the SAME source — not harmless, the same
            moment plays twice and reads as a stutter;
          * a non-monotonic beat grid;
          * an emotional ladder that steps backwards (see vault:
            `reference/Эмоциональная непрерывность сцены`), when `heat` is set.
        """
        problems: list[str] = []
        durs = self.durations
        prev_end = 0.0
        for i, s in enumerate(self.shots, 1):
            if s.end_beat <= prev_end:
                problems.append(f"S{i}: end_beat {s.end_beat} does not advance past {prev_end}")
            prev_end = s.end_beat
            if durs[i - 1] <= 0:
                problems.append(f"S{i}: quantises to {durs[i - 1]} frames")
            if s.in_sec < 0:
                problems.append(f"S{i}: negative in-point")

        used: dict[str, list[tuple[int, int, int]]] = {}
        for i, (s, n) in enumerate(zip(self.shots, durs, strict=False), 1):
            a = self.in_frame(s)
            # a slowed shot eats FEWER source frames than it occupies on the
            # timeline; a sped-up one eats more. Bounds must use the real read.
            b = a + max(1, round(n * s.source_frames_per_output))
            last = source_frames.get(s.source)
            if last is None:
                problems.append(f"S{i}: unknown source '{s.source}'")
            elif b > last:
                problems.append(
                    f"S{i}: runs to frame {b} but source '{s.source}' ends at {last}"
                    f" (max in-point {(last - n) / self.grid.fps:.2f}s)")
            for (j, a2, b2) in used.get(s.source, []):
                if a < b2 and b > a2 and not s.reprise:
                    # A deliberate recall minutes later is recapitulation, not a
                    # stutter — mark such a shot `reprise` and the reuse is
                    # allowed. Un-flagged reuse stays an error: that is the case
                    # where the same moment plays twice close together.
                    problems.append(
                        f"S{i} overlaps S{j} in source '{s.source}': "
                        f"{a}..{b} vs {a2}..{b2} — the same moment would play twice"
                        " (set reprise=true if the recall is deliberate)")
            used.setdefault(s.source, []).append((i, a, b))

        # --- story causality --------------------------------------------
        # The world may not un-happen. A regression is an error unless the shot
        # is explicitly a reprise/flashback; the running maximum is what matters,
        # not the previous shot, or a single dip would hide everything after it.
        seen_max: int | None = None
        for i, s in enumerate(self.shots, 1):
            st = s.stage
            if st is None:
                continue
            if seen_max is not None and st < seen_max and not s.reprise:
                culprit = next((j for j, p in enumerate(self.shots[:i - 1], 1)
                                if p.stage == seen_max), None)
                problems.append(
                    f"S{i} (stage {st}: {s.note or s.source}) goes BACK in the story "
                    f"after S{culprit} already reached stage {seen_max} — the world "
                    "cannot un-happen; move it earlier, or set reprise=true if the "
                    "recall is deliberate")
            seen_max = st if seen_max is None else max(seen_max, st)

        heats = [(i, s.heat) for i, s in enumerate(self.shots, 1) if s.heat is not None]
        for (i, h), (j, h2) in zip(heats, heats[1:], strict=False):
            if h2 < h - 1 and not self.shots[j - 1].reprise:
                problems.append(
                    f"S{j} drops the emotional ladder from {h} to {h2} after S{i} — "
                    "order shots by rising closeness before laying them on beats")
        return problems

    # ---- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "grid": {"bpm": self.grid.bpm, "fps": self.grid.fps,
                     "phase": self.grid.phase, "beats_per_bar": self.grid.beats_per_bar},
            "sources": self.sources,
            "shots": [vars(s) for s in self.shots],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> EDL:
        g = d["grid"]
        return EDL(grid=Grid(bpm=float(g["bpm"]), fps=int(g.get("fps", 24)),
                             phase=float(g.get("phase", 0.0)),
                             beats_per_bar=int(g.get("beats_per_bar", 4))),
                   shots=[Shot(**s) for s in d["shots"]],
                   sources=dict(d.get("sources", {})))

    @staticmethod
    def load(path: str | Path) -> EDL:
        return EDL.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def table(self) -> str:
        rows = ["shot  src   in_f  n_f   t_start  t_end    src_in src_out flip  note"]
        e, durs = self.edges, self.durations
        for i, (s, n) in enumerate(zip(self.shots, durs, strict=False), 1):
            a = self.in_frame(s)
            rows.append("S%-4d %-5s %4d  %3d  %7.3f %7.3f  %6.2f %6.2f  %-4s  %s"
                        % (i, s.source, a, n, e[i - 1] / self.grid.fps, e[i] / self.grid.fps,
                           a / self.grid.fps, (a + n) / self.grid.fps,
                           "yes" if s.hflip else "", s.note))
        rows.append("TOTAL %d frames = %.3f s (%.4g beats / %.4g bars)"
                    % (self.total_frames, self.duration,
                       self.shots[-1].end_beat,
                       self.shots[-1].end_beat / self.grid.beats_per_bar))
        return "\n".join(rows)
