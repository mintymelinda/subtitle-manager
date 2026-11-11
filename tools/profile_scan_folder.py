"""Profile the real scan pipeline for one mounted media folder.

This is an opt-in diagnostic, not a unit test. It runs the same scan worker as
POST /api/scan and therefore uses the configured database, provider credentials,
and subtitle staging directory. It never patches or writes to media files.
"""
import argparse
import json
import logging
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from app import main as scan_module
from app.config import settings
from app.database import create_job, get_job, initialize
from app.observability import configure_logging, logger


CAPTURED_EVENTS = {
    "scan_inventory_completed",
    "media_inspection_cache_hit",
    "media_inspection_cache_miss",
    "candidate_discovery_skipped",
    "opensubtitles_search_started",
    "opensubtitles_search_completed",
    "subtitle_candidate_download_selection",
    "subtitle_candidate_staged",
    "subtitle_candidate_staging_summary",
    "media_scan_completed",
    "media_scan_failed",
    "profile_file_timing",
    "scan_db_batch_completed",
}


class ProfileCapture(logging.Handler):
    """Collect structured scan telemetry without printing one line per event."""

    def __init__(self, media_root: Path):
        super().__init__(level=logging.INFO)
        self.media_root = media_root.resolve()
        self.stages = []
        self.events = []

    def _relative_media_path(self, value):
        if not value:
            return value
        try:
            path = Path(value)
            if path.is_absolute():
                return path.resolve(strict=False).relative_to(self.media_root).as_posix()
        except (OSError, ValueError):
            pass
        return value

    def emit(self, record):
        event = getattr(record, "event", None)
        if event == "stage_timing":
            self.stages.append({
                "stage": getattr(record, "stage", "unknown"),
                "elapsed_ms": getattr(record, "elapsed_ms", None),
                "media_path": self._relative_media_path(getattr(record, "media_path", None)),
                "bytes_processed": getattr(record, "bytes_processed", None),
                "file_size_bytes": getattr(record, "file_size_bytes", None),
                "tool": getattr(record, "tool", None),
                "database_operation": getattr(record, "database_operation", None),
            })
        elif event in CAPTURED_EVENTS:
            self.events.append({
                "event": event,
                "media_path": getattr(record, "media_path", None),
                "phase": getattr(record, "phase", None),
                "status": getattr(record, "status", None),
                "elapsed_ms": getattr(record, "elapsed_ms", None),
                "file_count": getattr(record, "file_count", None),
                "provider_count": getattr(record, "provider_count", None),
                "selected_count": getattr(record, "selected_count", None),
                "accepted_count": getattr(record, "accepted_count", None),
                "filter_reason": getattr(record, "filter_reason", None),
                "batch_size": getattr(record, "batch_size", None),
                "operation_count": getattr(record, "operation_count", None),
                "queue_depth": getattr(record, "queue_depth", None),
            })


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize_stages(stages):
    grouped = defaultdict(list)
    for item in stages:
        if isinstance(item["elapsed_ms"], (int, float)):
            grouped[item["stage"]].append(item["elapsed_ms"])
    summary = {}
    for name, values in sorted(grouped.items()):
        summary[name] = {
            "count": len(values),
            "total_ms": round(sum(values), 2),
            "mean_ms": round(statistics.mean(values), 2),
            "median_ms": round(statistics.median(values), 2),
            "p95_ms": round(percentile(values, 0.95), 2),
            "max_ms": round(max(values), 2),
        }
    return summary


def slowest_files(stages, events, limit=20):
    by_file = defaultdict(lambda: defaultdict(list))
    for item in stages:
        path = item["media_path"]
        elapsed = item["elapsed_ms"]
        if path and isinstance(elapsed, (int, float)):
            by_file[path][item["stage"]].append(elapsed)

    wall_times = {
        item["media_path"]: item["elapsed_ms"]
        for item in events
        if item["event"] == "profile_file_timing"
        and item["media_path"]
        and isinstance(item["elapsed_ms"], (int, float))
    }
    ranked = []
    for path in set(by_file) | set(wall_times):
        stage_values = by_file[path]
        ranked.append({
            "media_path": path,
            "file_wall_clock_ms": round(wall_times.get(path, 0), 2),
            "stages_ms": {
                stage: round(sum(values), 2)
                for stage, values in sorted(stage_values.items())
            },
        })
    return sorted(ranked, key=lambda item: item["file_wall_clock_ms"], reverse=True)[:limit]


def main():
    parser = argparse.ArgumentParser(
        description="Run and profile a real Subtitle Manager scan for one folder."
    )
    parser.add_argument(
        "folder", nargs="?", default="/media/tv/Futurama",
        help="folder inside MEDIA_ROOT (default: /media/tv/Futurama)",
    )
    parser.add_argument(
        "--languages", nargs="+", default=["en"],
        help="target language codes (default: en)",
    )
    args = parser.parse_args()
    api_key_override = os.environ.get("PROFILE_OPENSUBTITLES_API_KEY", "").strip() or None

    media_root = Path(settings.media_root).resolve(strict=True)
    folder = Path(args.folder).resolve(strict=True)
    if not folder.is_dir() or not folder.is_relative_to(media_root):
        parser.error(f"folder must be a directory inside {media_root}: {folder}")

    relative_root = folder.relative_to(media_root).as_posix()
    languages = args.languages

    configure_logging("WARNING")
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    capture = ProfileCapture(media_root)
    logger.addHandler(capture)
    original_scan_one_file = scan_module._scan_one_file

    def timed_scan_one_file(job_id, root, scan_root, path, *worker_args, **worker_kwargs):
        started = time.perf_counter()
        try:
            return original_scan_one_file(
                job_id, root, scan_root, path, *worker_args, **worker_kwargs
            )
        finally:
            logger.info(
                "profile_file_timing",
                extra={
                    "event": "profile_file_timing",
                    "job_id": job_id,
                    "media_path": path.relative_to(media_root).as_posix(),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    started = time.perf_counter()
    job_id = None
    job_result = None
    try:
        initialize()
        job = create_job("scan", total=0, metadata={
            "scan_roots": [relative_root],
            "target_languages": languages,
            "profile": True,
        })
        job_id = job["id"]
        scan_module._scan_one_file = timed_scan_one_file
        scan_module._scan_worker(
            job_id, media_root, [folder], languages, api_key_override=api_key_override
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        job_result = get_job(job_id)
    finally:
        scan_module._scan_one_file = original_scan_one_file
        logger.removeHandler(capture)

    events_by_name = Counter(item["event"] for item in capture.events)
    cache_hits = events_by_name["media_inspection_cache_hit"]
    cache_misses = events_by_name["media_inspection_cache_miss"]
    report = {
        "profile": "real_scan_folder",
        "folder": str(folder),
        "media_root": str(media_root),
        "target_languages": languages,
        "opensubtitles_api_key_override": bool(api_key_override),
        "job_id": job_id,
        "job_status": job_result["status"] if job_result else "missing",
        "job_progress": {
            "total": job_result.get("total") if job_result else None,
            "completed": job_result.get("completed") if job_result else None,
            "succeeded": job_result.get("succeeded") if job_result else None,
            "failed": job_result.get("failed") if job_result else None,
        },
        "wall_clock_ms": elapsed_ms,
        "event_counts": dict(sorted(events_by_name.items())),
        "inspection_cache": {
            "hits": cache_hits,
            "misses": cache_misses,
            "hit_rate_percent": round(cache_hits * 100 / (cache_hits + cache_misses), 1)
            if cache_hits + cache_misses else None,
        },
        "stage_summary": summarize_stages(capture.stages),
        "database_writer": {
            "batch_count": sum(1 for event in capture.events if event["event"] == "scan_db_batch_completed"),
            "operation_count": sum(event.get("operation_count") or 0 for event in capture.events
                                    if event["event"] == "scan_db_batch_completed"),
            "max_batch_size": max((event.get("batch_size") or 0 for event in capture.events
                                    if event["event"] == "scan_db_batch_completed"), default=0),
        },
        "slowest_files": slowest_files(capture.stages, capture.events),
        "notes": [
            "This invokes the production scan worker and uses its normal database and staging behavior.",
            "Media files are inspected only; this profiler does not patch or rewrite them.",
            "File wall-clock timings include cache lookup and all per-file work.",
            "Stage timings are nested. Do not add a parent stage to its child stages.",
            "A configured OpenSubtitles provider may perform real searches and candidate downloads.",
            "PROFILE_OPENSUBTITLES_API_KEY overrides only the API key for this profiler run; saved settings are unchanged.",
        ],
    }
    print("SCAN_FOLDER_PROFILE " + json.dumps(report, sort_keys=True))
    return 0 if report["job_status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
