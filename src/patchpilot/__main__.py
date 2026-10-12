"""Run mock scenarios with `python -m patchpilot demo`."""

import argparse
import json
import sys
import time

from .scenarios import SCENARIOS, run_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="PatchPilot — approval-gated CI repair")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Print scripted outcomes; no repository tests are executed")
    demo.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    repair = commands.add_parser("repair", help="Repair a failed workflow and wait for approval")
    repair.add_argument("--repo", required=True)
    repair.add_argument("--run-id", required=True)
    for command in ("status", "approve", "reject"):
        action = commands.add_parser(command)
        action.add_argument("--job-id", required=True)
    worker = commands.add_parser("worker", help="Execute durable queued jobs in a separate process")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "demo":
        names = SCENARIOS if args.scenario == "all" else (args.scenario,)
        for name in names:
            state = run_scenario(name)
            from .reporting import result_payload

            metrics = result_payload(state)["metrics"]
            print(json.dumps({"scenario": name, "simulated": True, "metrics": metrics, **state.model_dump(mode="json")}, ensure_ascii=False))
        return
    from .config import Settings
    from .service import RepairService
    from .storage import JobBusyError

    try:
        service = RepairService(Settings.from_env())
        if args.command == "repair":
            result = service.repair(args.repo, args.run_id)
        elif args.command == "status":
            result = service.status(args.job_id)
        elif args.command in {"approve", "reject"}:
            result = service.execute(args.job_id, resume=args.command == "approve")
        else:
            while True:
                for job_id in service.store.pending():
                    try:
                        print(json.dumps(service.execute(job_id), ensure_ascii=False), flush=True)
                    except JobBusyError:
                        continue
                if args.once:
                    return
                time.sleep(2)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("operation_error") or result["status"] in {"REPAIR_FAILED", "MANUAL_ACTION_REQUIRED", "CI_FAILED", "PUBLISH_FAILED"}:
            raise SystemExit(1)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "MANUAL_ACTION_REQUIRED", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
