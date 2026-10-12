"""Run mock scenarios with `python -m patchpilot demo`."""

import argparse
import json

from .scenarios import SCENARIOS, run_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="PatchPilot Phase 1 — MOCK ONLY")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Print scripted outcomes; no repository tests are executed")
    demo.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args()
    names = SCENARIOS if args.scenario == "all" else (args.scenario,)
    for name in names:
        state = run_scenario(name)
        print(json.dumps({"scenario": name, "simulated": True, **state.model_dump(mode="json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
