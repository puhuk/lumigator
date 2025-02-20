import asyncio
import csv
import json
from collections.abc import Callable
from io import BytesIO, StringIO
from pathlib import Path
from uuid import UUID

import loguru
from fastapi import BackgroundTasks, UploadFile
from lumigator_schemas.datasets import DatasetFormat
from lumigator_schemas.experiments import ExperimentCreate, ExperimentResponse
from lumigator_schemas.extras import ListingResponse
from lumigator_schemas.jobs import (
    JobEvalLiteCreate,
    JobInferenceCreate,
    JobStatus,
)
from s3fs import S3FileSystem

from backend.repositories.experiments import ExperimentRepository
from backend.services.datasets import DatasetService
from backend.services.jobs import JobService
from backend.settings import settings


class ExperimentService:
    def __init__(
        self,
        experiment_repo: ExperimentRepository,
        job_service: JobService,
        dataset_service: DatasetService,
    ):
        self._experiment_repo = experiment_repo
        self._job_service = job_service
        self._dataset_service = dataset_service

    def _results_to_binary_file(self, results: str, fields: list[str]) -> BytesIO:
        """Given a JSON string containing inference results and the fields
        we want to read from it, generate a binary file (as a BytesIO
        object) to be passed to the fastapi UploadFile method.
        """
        dataset = {k: v for k, v in results.items() if k in fields}

        # Create a CSV in memory
        csv_buffer = StringIO()
        csv_writer = csv.writer(csv_buffer)
        csv_writer.writerow(dataset.keys())
        csv_writer.writerows(zip(*dataset.values()))

        # Create a binary file from the CSV, since the upload function expects a binary file
        bin_data = BytesIO(csv_buffer.getvalue().encode("utf-8"))

        return bin_data

    def _add_dataset_to_db(self, job_id: UUID, request: JobInferenceCreate):
        loguru.logger.info("Adding a new dataset entry to the database...")
        s3 = S3FileSystem()

        # Get the dataset from the S3 bucket
        result_key = self._job_service._get_results_s3_key(job_id)
        with s3.open(f"{settings.S3_BUCKET}/{result_key}", "r") as f:
            results = json.loads(f.read())

        # define the fields we want to keep from the results JSON
        # and build a CSV file from it as a BytesIO object
        fields = ["examples", "ground_truth", request.output_field]
        bin_data = self._results_to_binary_file(results, fields)
        bin_data_size = len(bin_data.getvalue())

        # Figure out the dataset filename
        dataset_filename = self._dataset_service.get_dataset(dataset_id=request.dataset).filename
        dataset_filename = Path(dataset_filename).stem
        dataset_filename = f"{dataset_filename}-annotated.csv"

        upload_file = UploadFile(
            file=bin_data,
            size=bin_data_size,
            filename=dataset_filename,
            headers={"content-type": "text/csv"},
        )
        dataset_record = self._dataset_service.upload_dataset(
            upload_file,
            format=DatasetFormat.JOB,
            run_id=job_id,
            generated=True,
            generated_by=results["model"],
        )

        loguru.logger.info(
            f"Dataset '{dataset_filename}' with ID '{dataset_record.id}' added to the database."
        )

    async def on_job_complete(self, job_id: UUID, task: Callable = None, *args):
        """Watches a submitted job and, when it terminates successfully, runs a given task.

        Inputs:
        - job_id: the UUID of the job to watch
        - task: the function to be called after the job completes successfully
        - args: the arguments to be passed to the function `task()`
        """
        job_status = self._job_service.ray_client.get_job_status(job_id)

        valid_status = [
            JobStatus.CREATED.value.lower(),
            JobStatus.PENDING.value.lower(),
            JobStatus.RUNNING.value.lower(),
        ]
        stop_status = [JobStatus.FAILED.value.lower(), JobStatus.SUCCEEDED.value.lower()]

        loguru.logger.info(f"Watching {job_id}")
        while job_status.lower() not in stop_status and job_status.lower() in valid_status:
            await asyncio.sleep(5)
            job_status = self._job_service.ray_client.get_job_status(job_id)

        if job_status.lower() == JobStatus.FAILED.value.lower():
            loguru.logger.error(f"Job {job_id} failed: not running task {str(task)}")

        if job_status.lower() == JobStatus.SUCCEEDED.value.lower():
            loguru.logger.info(f"Job {job_id} finished successfully.")
            if task is not None:
                task(*args)

    def _run_eval(
        self, inference_job_id: UUID, request: ExperimentCreate, experiment_id: UUID = None
    ):
        # use the inference job id to recover the dataset record
        dataset_record = self._dataset_service._get_dataset_record_by_job_id(inference_job_id)

        # prepare the inputs for the evaluation job and pass the id of the new dataset
        job_eval_dict = {
            "name": f"{request.name}-evaluation",
            "model": request.model,
            "dataset": dataset_record.id,
            "max_samples": request.max_samples,
            "skip_inference": True,
        }

        # submit the job
        self._job_service.create_job(
            JobEvalLiteCreate.model_validate(job_eval_dict), experiment_id=experiment_id
        )

    def create_experiment(
        self, request: ExperimentCreate, background_tasks: BackgroundTasks
    ) -> ExperimentResponse:
        # The FastAPI BackgroundTasks object is used to run a function in the background.
        # It is a wrapper around Starlette's BackgroundTasks object.
        # A background task should be attached to a response,
        # and will run only once the response has been sent.
        # See here: https://www.starlette.io/background/

        experiment_record = self._experiment_repo.create(
            name=request.name, description=request.description
        )
        loguru.logger.info(f"Created experiment '{request.name}' with ID '{experiment_record.id}'.")

        # input is ExperimentCreate, we need to split the configs and generate one
        # JobInferenceCreate and one JobEvalCreate
        job_inference_dict = {
            "name": f"{request.name}-inference",
            "model": request.model,
            "dataset": request.dataset,
            "max_samples": request.max_samples,
            "model_url": request.model_url,
            "output_field": request.inference_output_field,
            "system_prompt": request.system_prompt,
        }

        # submit inference job first
        job_response = self._job_service.create_job(
            JobInferenceCreate.model_validate(job_inference_dict),
            experiment_id=experiment_record.id,
        )

        # Inference jobs produce a new dataset
        # Add the dataset to the (local) database
        background_tasks.add_task(
            self.on_job_complete,
            job_response.id,
            self._add_dataset_to_db,
            job_response.id,
            JobInferenceCreate.model_validate(job_inference_dict),
        )

        # run evaluation job afterwards
        # (NOTE: tasks in starlette are executed sequentially: https://www.starlette.io/background/)
        background_tasks.add_task(
            self.on_job_complete,
            job_response.id,
            self._run_eval,
            job_response.id,
            request,
            experiment_record.id,
        )

        return ExperimentResponse.model_validate(experiment_record)

    def list_experiments(
        self, skip: int = 0, limit: int = 100
    ) -> ListingResponse[ExperimentResponse]:
        records = self._experiment_repo.list(skip, limit)
        return ListingResponse(
            total=self._experiment_repo.count(),
            items=[ExperimentResponse.model_validate(x) for x in records],
        )
