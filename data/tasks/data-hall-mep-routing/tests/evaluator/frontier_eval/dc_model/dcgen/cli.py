"""Generate a set of ground-truth hall instances.

    python -m dc_model.dcgen.cli --out halls --seeds 0-11

Each instance directory holds the ground-truth IFC and its manifest. The IFC is
the ANSWER KEY — it is never shipped to an agent. ``dcsheet`` renders the agent's
drawing from it, and ``dcgrade`` scores against it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import build
from .sampler import manifest, sample_program


def parse_seeds(spec: str) -> list[int]:
    """Accept ``0-11``, ``0,3,7`` or a mix.

    Raises on malformed input. Silently returning an empty list would mean a typo'd
    ``--seeds 5-3`` generates nothing while still exiting 0, which reads as
    success.
    """
    seeds: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part.lstrip("-"):
                lo_s, hi_s = part.split("-", 1)
                lo, hi = int(lo_s), int(hi_s)
                if hi < lo:
                    raise ValueError(f"reversed seed range {part!r}")
                seeds.extend(range(lo, hi + 1))
            else:
                seeds.append(int(part))
        except ValueError as exc:
            raise ValueError(f"bad --seeds component {part!r}: {exc}") from None
    if not seeds:
        raise ValueError(f"--seeds {spec!r} selected no seeds")
    # duplicates would regenerate the same directory and inflate the counts
    return sorted(dict.fromkeys(seeds))


def generate(out_dir: Path, seeds: list[int], difficulty: float | None,
             strict: bool = True) -> dict:
    """Generate every seed. Returns a summary; never partially writes an instance."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written, rejected = [], []

    for seed in seeds:
        # spread difficulty across the set when not pinned
        d = difficulty if difficulty is not None else (seed % 6) / 5.0
        # round, not truncate: int(0.29 * 100) is 28, so the directory name
        # would misreport the difficulty it was generated at
        inst_dir = out_dir / f"hall_seed{seed:03d}_d{round(d * 100):03d}"

        program = sample_program(seed, d)
        try:
            # create the directory only once the build is known to succeed, so a
            # rejected seed cannot leave an empty instance dir that downstream
            # globs treat as an instance with a missing ground_truth.ifc
            inst_dir.mkdir(parents=True, exist_ok=True)
            result = build(program, str(inst_dir / "ground_truth.ifc"), strict=strict)
        except ValueError as exc:
            if inst_dir.exists() and not any(inst_dir.iterdir()):
                inst_dir.rmdir()
            rejected.append({"seed": seed, "difficulty": d, "reason": str(exc)})
            print(f"  seed {seed:3d} d={d:.1f}  REJECTED", file=sys.stderr)
            continue

        meta = manifest(program)
        meta.update({
            "difficulty": d,
            "element_count": result.element_count,
            "n_racks": len(result.racks),
            "n_crahs": len(result.crahs),
            "n_pdus": len(result.pdus),
            "n_containment": len(result.containment),
            "n_columns": len(result.columns),
            "n_sprinklers": len(result.sprinklers),
            "n_route_segments": len(result.routes),
            # Task B's objective denominator. Recorded here rather than
            # recomputed at grade time so the ratio an agent is measured
            # against is the one the generator actually achieved.
            "l_opt_mm": result.l_opt_mm,
            "ifc": "ground_truth.ifc",
        })
        (inst_dir / "manifest.json").write_text(json.dumps(meta, indent=2))
        written.append(meta)
        print(f"  seed {seed:3d} d={d:.1f}  {meta['racks_placed']:3d} racks  "
              f"{result.element_count:4d} elements  -> {inst_dir.name}")

    summary = {
        "n_requested": len(seeds),
        "n_written": len(written),
        "n_rejected": len(rejected),
        "rejected": rejected,
        "instances": [
            {k: m[k] for k in ("seed", "difficulty", "difficulty_level",
                               "racks_placed", "racks_nominal", "element_count")}
            for m in written
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    ap.add_argument("--seeds", default="0-11", help="e.g. 0-11 or 0,3,7")
    ap.add_argument("--difficulty", type=float, default=None,
                    help="pin difficulty 0.0-1.0; default spreads across the set")
    ap.add_argument("--no-strict", action="store_true",
                    help="emit instances that fail self-check (diagnostics only)")
    args = ap.parse_args(argv)

    if args.difficulty is not None and not 0.0 <= args.difficulty <= 1.0:
        ap.error("--difficulty must be in [0.0, 1.0]")
    seeds = parse_seeds(args.seeds)
    print(f"generating {len(seeds)} instances into {args.out}")
    summary = generate(args.out, seeds, args.difficulty, strict=not args.no_strict)

    print(f"\n{summary['n_written']} written, {summary['n_rejected']} rejected")
    if summary["n_rejected"]:
        print("REJECTED seeds produced ground truth that fails our own grader:")
        for r in summary["rejected"]:
            print(f"  seed {r['seed']}: {r['reason'].splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
