import argparse
import asyncio
import json

from marketing_dashboard import whiteboard as wb


async def _sync() -> dict:
    sync_id = wb._start_log("scheduled job")
    if not sync_id:
        return {"started": False, "reason": "A Neos sync is already running."}
    try:
        await wb.run_sync(sync_id)
    finally:
        from PeakTrac.neos_session import get_neos_client
        await get_neos_client().aclose()
    return {"started": True, "sync_id": sync_id}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["sync", "provider-reminder"])
    parser.add_argument("--force", action="store_true", help="provider-reminder: send now, ignoring the day of month")
    args = parser.parse_args()
    if args.job == "sync":
        result = asyncio.run(_sync())
    else:
        result = wb.send_provider_reminder(force=args.force)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
