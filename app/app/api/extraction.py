import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.models.job import JobStatus
from app.services.job_store import job_store
from app.services.pipeline_service import PipelineService, get_download_path

router = APIRouter(tags=["extraction"])


class JobRequest(BaseModel):
    job_id: str


@router.post("/extract")
async def extract(request: JobRequest, background_tasks: BackgroundTasks) -> dict:
    job = job_store.get(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    background_tasks.add_task(_run_pipeline, request.job_id)
    return {"job_id": request.job_id, "status": JobStatus.processing}


@router.post("/generate-excel")
async def generate_excel(request: JobRequest, background_tasks: BackgroundTasks) -> dict:
    job = job_store.get(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    if job.status != JobStatus.completed:
        background_tasks.add_task(_run_pipeline, request.job_id)
        return {"job_id": request.job_id, "status": JobStatus.processing}
    return {"job_id": request.job_id, "status": job.status, "excel_path": str(job.excel_path)}


@router.get("/status/{job_id}")
async def status(job_id: str) -> dict:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job.model_dump(mode="json")


@router.get("/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    path = get_download_path(job_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Excel output not found")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


async def _run_pipeline(job_id: str) -> None:
    job = job_store.get(job_id)
    if not job:
        return
    await PipelineService().process(job)


@router.post("/extract-sync")
async def extract_sync(request: JobRequest) -> dict:
    job = job_store.get(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    completed = await asyncio.wait_for(PipelineService().process(job), timeout=None)
    return completed.model_dump(mode="json")


@router.get("/jobs/company/{company_slug}")
async def jobs_by_company(company_slug: str) -> dict:
    """
    List all extraction jobs that were uploaded under a specific company slug.
    Use this after calling POST /upload with a company_name to track and
    retrieve results for that company's documents.
    """
    jobs = job_store.list_by_company(company_slug)
    return {
        "company_slug": company_slug,
        "total": len(jobs),
        "jobs": [j.model_dump(mode="json") for j in jobs],
    }


@router.post("/extract/company/{company_slug}")
async def extract_company(company_slug: str, background_tasks: BackgroundTasks) -> dict:
    """
    Trigger background extraction for ALL jobs under a company slug that are
    still in 'uploaded' status. Returns the list of job_ids kicked off.
    """
    jobs = job_store.list_by_company(company_slug)
    if not jobs:
        raise HTTPException(status_code=404, detail=f"No jobs found for company '{company_slug}'")

    pending = [j for j in jobs if j.status == JobStatus.uploaded]
    if not pending:
        return {
            "company_slug": company_slug,
            "message": "No pending jobs — all already processed or processing.",
            "jobs": [j.model_dump(mode="json") for j in jobs],
        }

    for job in pending:
        background_tasks.add_task(_run_pipeline, job.job_id)

    return {
        "company_slug": company_slug,
        "triggered": len(pending),
        "job_ids": [j.job_id for j in pending],
        "status": JobStatus.processing,
    }
