"""Profile the real scan pipeline for one mounted media folder.

This is an opt-in diagnostic, not a unit test. It runs the same scan worker as
POST /api/scan and therefore uses the configured database, provider credentials,
and subtitle staging directory. It never patches or writes to media files.
"""
import argparse
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from app.config import settings
from app.database import create_job, get_job, initialize
from app.main import _scan_worker
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
}
OUTER_FILE_STAGES = {"media_inspection", "candidate_staging"}


class ProfileCapture(logging.Handler):
    """Collect structured scan telemetry without printing one line per event."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.stages = []
        self.events = []

    def emit(self, record):
        event = getattr(record, "event", None)
        if event == "stage_timing":
            self.stages.append({
                "stage": getattr(record, "stage", "unknown"),
                "elapsed_ms": getattr(record, "elapsed_ms", None),
                "media_path": getattr(record, "media_path", None),
                "bytes_processed": getattr(record, "bytes_processed", None),
                "file_size_bytes": getattr(record, "file_size_bytes", None),
                "tool": getattr(record, "tool", None),
            })
        elif event in CAPTURED_EVENTS:
            self.events.append({
                "event": event,
                "media_path": getattr(record, "media_path", None),
                "phase": getattr(record, "phase", None),
                "status": getattr(record, "status", None),
                "file_count": getattr(record, "file_count", None),
                "provider_count": getattr(record, "provider_count", None),
                "selected_count": getattr(record, "selected_count", None),
                "accepted_count": getattr(record, "accepted_count", None),
                "filter_reason": getattr(record, "filter_reason", None),
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


def slowest_files(stages, limit=20):
    by_file = defaultdict(lambda: defaultdict(list))
    for item in stages:
        path = item["media_path"]
        elapsed = item["elapsed_ms"]
        if path and isinstance(elapsed, (int, float)):
            by_file[path][item["stage"]].append(elapsed)

    ranked = []
    for path, stage_values in by_file.items():
        # Use only the two outer per-file stages here. Their nested FFprobe,
        # FFmpeg, and provider timings are shown separately in stage_summary.
        outer_total = sum(
            sum(values) for stage, values in stage_values.items()
            if stage in OUTER_FILE_STAGES
        )
        ranked.append({
            "media_path": path,
            "outer_pipeline_ms": round(outer_total, 2),
            "stages_ms": {
                stage: round(sum(values), 2)
                for stage, values in sorted(stage_values.items())
            },
        })
    return sorted(ranked, key=lambda item: item["outer_pipeline_ms"], reverse=True)[:limit]


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

    media_root = Path(settings.media_root).resolve(strict=True)
    folder = Path(args.folder).resolve(strict=True)
    if not folder.is_dir() or not folder.is_relative_to(media_root):
        parser.error(f"folder must be a directory inside {media_root}: {folder}")

    relative_root = folder.relative_to(media_root).as_posix()
    languages = args.languages

    configure_logging("WARNING")
    logger.setLevel(logging.INFO)
    capture = ProfileCapture()
    logger.addHandler(capture)
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
        _scan_worker(job_id, media_root, [folder], languages)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        job_result = get_job(job_id)
    finally:
        logger.removeHandler(capture)

    events_by_name = Counter(item["event"] for item in capture.events)
    cache_hits = events_by_name["media_inspection_cache_hit"]
    cache_misses = events_by_name["media_inspection_cache_miss"]
    report = {
        "profile": "real_scan_folder",
        "folder": str(folder),
        "media_root": str(media_root),
        "target_languages": languages,
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
        "slowest_files": slowest_files(capture.stages),
        "notes": [
            "This invokes the production scan worker and uses its normal database and staging behavior.",
            "Media files are inspected only; this profiler does not patch or rewrite them.",
            "Stage timings are nested. Do not add a parent stage to its child stages.",
            "Slowest-file ranking uses only media_inspection and candidate_staging outer stages.",
            "A configured OpenSubtitles provider may perform real searches and candidate downloads.",
        ],
    }
    print("SCAN_FOLDER_PROFILE " + json.dumps(report, sort_keys=True))
    return 0 if report["job_status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
