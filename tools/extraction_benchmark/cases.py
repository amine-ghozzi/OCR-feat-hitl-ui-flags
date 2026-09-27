"""Stage 2 — cases: run FinAlzePipeline on each sampled doc -> cases/<case>.json.

Heavy (loads the ML stack on first use). Each candidate is the serialized
ExtractionResult; the sample row is updated in place with the observed tier + QCS band
so later stages and the report can stratify on what actually happened.

Also captures RUNTIME + RESOURCE consumption per case (wall time, CPU time, peak
RSS) so per-engine benchmark runs (FINALZE_OCR_ENGINE) can be compared on cost,
not just accuracy. The first case per pipeline carries the one-time model-load
cost; it is flagged so analysis can separate load from steady-state.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import config as C


def _serialize(result) -> Dict[str, Any]:
    """ExtractionResult -> JSON-safe dict (it has no to_dict; canonical tables do)."""
    payload = {
        "text": result.text,
        "tables": result.tables,
        "qcs_score": result.qcs_score,
        "tier_used": result.tier_used,
        "confidence_details": result.confidence_details,
        "metadata": result.metadata,
        "canonical_tables": [
            t.to_dict() if hasattr(t, "to_dict") else t
            for t in (result.canonical_tables or [])
        ],
    }
    # default=str makes Decimal / Path / datetime safe, matching the worker serializer.
    return json.loads(json.dumps(payload, default=str))


class _RSSSampler:
    """Background thread sampling this process's RSS to capture the peak during
    one extraction. Portable (psutil) across Windows containers and Linux; if
    psutil is missing, peak is reported as None and the run still proceeds."""

    def __init__(self, interval_s: float = 0.2):
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.peak_mb: Optional[float] = None
        try:
            import psutil  # noqa: PLC0415
            self._proc = psutil.Process()
        except Exception:
            self._proc = None

    def _run(self) -> None:
        peak = 0
        while not self._stop.is_set():
            try:
                rss = self._proc.memory_info().rss
                if rss > peak:
                    peak = rss
            except Exception:
                break
            self._stop.wait(self._interval)
        self.peak_mb = round(peak / (1024 * 1024), 1) if peak else None

    def __enter__(self) -> "_RSSSampler":
        if self._proc is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def _cpu_times():
    try:
        import psutil  # noqa: PLC0415
        t = psutil.Process().cpu_times()
        return float(t.user), float(t.system)
    except Exception:
        return None, None


def _env_fingerprint() -> Dict[str, Any]:
    """Record what actually ran, so a metrics file is self-describing."""
    fp: Dict[str, Any] = {
        "engine": os.environ.get("FINALZE_OCR_ENGINE", "auto"),
        "host": "container" if os.path.exists("/.dockerenv") else "local",
    }
    for mod in ("docling", "rapidocr", "easyocr", "PIL", "torch", "numpy"):
        try:
            m = __import__(mod)
            fp[f"{mod}_version"] = getattr(m, "__version__", "?")
        except Exception:
            pass
    return fp


def run(run_id: str) -> Dict[str, Any]:
    C.ensure_src_on_path()
    from pipeline import FinAlzePipeline, PipelineConfig  # heavy import — do it lazily

    sample_path = C.run_dir(run_id) / "_sample.json"
    if not sample_path.exists():
        raise FileNotFoundError(f"run {run_id}: missing _sample.json — run `sample` first")
    sample = C.read_json(sample_path)
    cases_dir = C.stage_dir(run_id, "cases")

    pipelines: Dict[str, Any] = {}   # one pipeline per accounting standard, reused across cases
    per_case: List[Dict[str, Any]] = []
    done, failed = 0, []
    for row in sample:
        std = row.get("standard") or "NCT"
        pipe = pipelines.get(std)
        first_for_pipeline = pipe is None
        if pipe is None:
            pipe = pipelines[std] = FinAlzePipeline(PipelineConfig(accounting_standard=std))
        src = C.REPO_ROOT / row["source_path"]

        cu0, cs0 = _cpu_times()
        t0 = time.perf_counter()
        try:
            with _RSSSampler() as sampler:
                result = pipe.process_document(src, accounting_standard=std)
        except Exception as exc:  # noqa: BLE001 — record + continue so one bad doc doesn't abort the run
            failed.append({"case_id": row["case_id"], "error": str(exc)})
            continue
        wall_s = round(time.perf_counter() - t0, 3)
        cu1, cs1 = _cpu_times()
        cpu_user_s = round(cu1 - cu0, 3) if (cu0 is not None and cu1 is not None) else None
        cpu_sys_s = round(cs1 - cs0, 3) if (cs0 is not None and cs1 is not None) else None

        C.write_json(cases_dir / f"{row['case_id']}.json", _serialize(result))
        row["tier"] = result.tier_used
        row["qcs_band"] = C.qcs_band(result.qcs_score)
        # runtime/resource is recorded on the row too (stratifiable) and aggregated below
        metrics = {
            "case_id": row["case_id"],
            "wall_s": wall_s,
            "cpu_user_s": cpu_user_s,
            "cpu_sys_s": cpu_sys_s,
            "rss_peak_mb": sampler.peak_mb,
            "first_for_pipeline": first_for_pipeline,  # carries one-time model-load cost
            "tier": result.tier_used,
            "qcs_score": result.qcs_score,
        }
        row["runtime"] = metrics
        per_case.append(metrics)
        done += 1

    C.write_json(sample_path, sample)  # persist observed tier/QCS/runtime back into the sample

    # Aggregate engine metrics — steady-state excludes the model-load (first) case.
    steady = [m["wall_s"] for m in per_case if not m["first_for_pipeline"] and m["wall_s"] is not None]
    all_wall = [m["wall_s"] for m in per_case if m["wall_s"] is not None]
    rss = [m["rss_peak_mb"] for m in per_case if m["rss_peak_mb"] is not None]
    load_cases = [m["wall_s"] for m in per_case if m["first_for_pipeline"] and m["wall_s"] is not None]

    def _median(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return round((s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2), 3)

    engine_metrics = {
        "run": run_id,
        **_env_fingerprint(),
        "n_cases": done,
        "totals": {
            "wall_s": round(sum(all_wall), 3) if all_wall else None,
            "median_steady_wall_s": _median(steady),
            "median_all_wall_s": _median(all_wall),
            "max_rss_peak_mb": max(rss) if rss else None,
            "model_load_wall_s": round(sum(load_cases), 3) if load_cases else None,
        },
        "per_case": per_case,
    }
    C.write_json(C.run_dir(run_id) / "_engine_metrics.json", engine_metrics)

    return {"run": run_id, "cases": done, "failed": failed, "cases_dir": str(cases_dir),
            "engine": engine_metrics["engine"], "median_steady_wall_s": engine_metrics["totals"]["median_steady_wall_s"]}
