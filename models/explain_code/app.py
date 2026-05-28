from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from infer_ascend_multiview_classifier import FIXED_OUTPUT_ROOT, infer_file


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InferClassifierRequest(BaseModel):
    input: str = Field(..., description="Path to one .kicad_pcb file")
    checkpoint: str = Field(..., description="Path to one saved checkpoint file")


@dataclass
class JobRecord:
    job_id: str
    job_type: Literal["infer_classifier"]
    status: str
    request: Dict[str, object]
    created_at: str
    updated_at: str
    input_path: str
    checkpoint_path: str
    output_dir: str
    report_path: Optional[str] = None
    prediction_json_path: Optional[str] = None
    report_text: Optional[str] = None
    prediction: Optional[Dict[str, object]] = None
    error: Optional[str] = None
    traceback_text: Optional[str] = None


app = FastAPI(title="PCB Inference Service", version="1.0.0")
_jobs: Dict[str, JobRecord] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **updates: object) -> Dict[str, object]:
    with _jobs_lock:
        job = _jobs[job_id]
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = _utc_now_iso()
        return asdict(job)


def _prediction_paths(input_path: Path) -> Dict[str, Path]:
    board_out = FIXED_OUTPUT_ROOT.resolve() / input_path.stem
    return {
        "output_dir": board_out,
        "report_path": board_out / "report.txt",
        "prediction_json_path": board_out / "prediction.json",
    }


def _run_infer_job(job_id: str, input_path: Path, checkpoint_path: Path) -> None:
    _set_job(job_id, status="running")
    paths = _prediction_paths(input_path)
    try:
        report_text = infer_file(input_path, checkpoint_path)
        prediction = None
        prediction_json_path = paths["prediction_json_path"]
        if prediction_json_path.is_file():
            prediction = json.loads(prediction_json_path.read_text(encoding="utf-8"))
        _set_job(
            job_id,
            status="succeeded",
            output_dir=str(paths["output_dir"]),
            report_path=str(paths["report_path"]),
            prediction_json_path=str(prediction_json_path),
            report_text=report_text,
            prediction=prediction,
            error=None,
            traceback_text=None,
        )
    except Exception as exc:
        _set_job(
            job_id,
            status="failed",
            output_dir=str(paths["output_dir"]),
            report_path=str(paths["report_path"]),
            prediction_json_path=str(paths["prediction_json_path"]),
            error=str(exc),
            traceback_text=traceback.format_exc(),
        )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs")
def list_jobs() -> Dict[str, object]:
    with _jobs_lock:
        jobs = [asdict(job) for job in _jobs.values()]
    jobs.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, object]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return asdict(job)


@app.post("/jobs/infer-classifier")
def create_infer_classifier_job(request: InferClassifierRequest) -> Dict[str, object]:
    input_path = Path(request.input).resolve()
    checkpoint_path = Path(request.checkpoint).resolve()

    if not input_path.is_file():
        raise HTTPException(status_code=400, detail=f"input file does not exist: {input_path}")
    if input_path.suffix != ".kicad_pcb":
        raise HTTPException(status_code=400, detail=f"input must be a .kicad_pcb file: {input_path}")
    if not checkpoint_path.is_file():
        raise HTTPException(status_code=400, detail=f"checkpoint file does not exist: {checkpoint_path}")

    job_id = uuid.uuid4().hex
    paths = _prediction_paths(input_path)
    record = JobRecord(
        job_id=job_id,
        job_type="infer_classifier",
        status="queued",
        request={"input": str(input_path), "checkpoint": str(checkpoint_path)},
        created_at=_utc_now_iso(),
        updated_at=_utc_now_iso(),
        input_path=str(input_path),
        checkpoint_path=str(checkpoint_path),
        output_dir=str(paths["output_dir"]),
    )
    with _jobs_lock:
        _jobs[job_id] = record

    worker = threading.Thread(target=_run_infer_job, args=(job_id, input_path, checkpoint_path), daemon=True)
    worker.start()

    return {
        "job_id": job_id,
        "status": "queued",
        "input_path": str(input_path),
        "checkpoint_path": str(checkpoint_path),
        "output_dir": str(paths["output_dir"]),
        "status_url": f"/jobs/{job_id}",
    }
