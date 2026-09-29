"""Run the real M1 mock read tools and show that normal incident dispatch is blocked."""

import argparse
import asyncio
import json
from pathlib import Path

from agent_harness.errors import ApprovalRequiredError
from agent_harness.tools.factory import build_mock_tools


async def run(directory: Path) -> dict:
    tools = build_mock_tools(directory)
    status = await tools.registry.execute("get_service_status", {"service_name": "checkout-api"})
    knowledge = await tools.registry.execute("search_knowledge_base", {"query": "checkout timeout"})
    try:
        await tools.registry.execute(
            "create_incident",
            {
                "title": "Checkout degradation",
                "description": "Synthetic observation.",
                "severity": "high",
            },
        )
    except ApprovalRequiredError as error:
        blocked = error.info.model_dump(mode="json")
    else:
        raise RuntimeError("Incident unexpectedly bypassed the approval policy.")
    return {
        "scope": "M1 tool demo; no LLM, no approval workflow yet",
        "service_status": status.model_dump(mode="json"),
        "knowledge_matches": knowledge.model_dump(mode="json"),
        "incident_request": blocked,
        "incident_count": tools.incidents.incident_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("mock_data"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.data_dir)), indent=2))


if __name__ == "__main__":
    main()
