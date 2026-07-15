import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path to allow execution from any directory
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.core_config import get_settings
from app.services.job_store import job_store
from app.services.pipeline_service import PipelineService
from app.utils.files import ensure_supported


async def main() -> None:
    parser = argparse.ArgumentParser(description="Process one local financial document into Excel.")
    parser.add_argument("file", type=Path, help="PDF or image path")
    args = parser.parse_args()

    source = args.file.resolve()
    ensure_supported(source.name)
    settings = get_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    job = job_store.create(source)
    completed = await PipelineService().process(job)
    print(json.dumps(completed.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
