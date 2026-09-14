from __future__ import annotations

import argparse
import math
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from build_manifest import (
    EXPECTED_DEVICE_TAGS,
    activity_from_filename,
    build_manifest,
    infer_expected_steps,
    normalize_sensor_name,
    packet_issues,
    parse_output_rate_hz,
    read_xsens_csv,
    sample_diffs,
    summarize_recording,
)


ACC_COLUMNS = ["Acc_X", "Acc_Y", "Acc_Z"]
GYR_COLUMNS = ["Gyr_X", "Gyr_Y", "Gyr_Z"]
QUAT_COLUMNS = ["Quat_W", "Quat_X", "Quat_Y", "Quat_Z"]
SIGNAL_COLUMNS = ACC_COLUMNS + GYR_COLUMNS + QUAT_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "UNKNOWN"


def relative_time_seconds(
    sample_time: pd.Series, expected_step: int | None, output_rate_hz: float | None
) -> np.ndarray:
    values = sample_time.to_numpy(dtype=float)
    if values.size == 0:
        return values
    if expected_step and output_rate_hz:
        return ((values - values[0]) / expected_step) / output_rate_hz
    return np.arange(values.size, dtype=float)


def vector_magnitude(table: pd.DataFrame, columns: list[str]) -> pd.Series:
    return np.sqrt((table[columns] ** 2).sum(axis=1))


def quaternion_norm(table: pd.DataFrame) -> pd.Series:
    return vector_magnitude(table, QUAT_COLUMNS)


def robust_jump_threshold(deltas: pd.Series) -> float:
    finite = deltas.replace([np.inf, -np.inf], np.nan).dropna()
    if finite.empty:
        return math.inf
    median = float(finite.median())
    mad = float((finite - median).abs().median())
    if mad > 0:
        return median + 10.0 * 1.4826 * mad
    std = float(finite.std(ddof=0))
    if std > 0:
        return median + 8.0 * std
    return math.inf


def jump_metrics(series: pd.Series) -> tuple[float, int, float]:
    deltas = series.diff().abs()
    threshold = robust_jump_threshold(deltas)
    finite = deltas.replace([np.inf, -np.inf], np.nan).dropna()
    max_jump = float(finite.max()) if not finite.empty else 0.0
    count = int((finite > threshold).sum()) if math.isfinite(threshold) else 0
    return max_jump, count, threshold


def linear_drift(series: pd.Series, time_seconds: np.ndarray) -> float:
    values = series.to_numpy(dtype=float)
    mask = np.isfinite(values) & np.isfinite(time_seconds)
    if mask.sum() < 2:
        return np.nan
    time = time_seconds[mask]
    y = values[mask]
    if float(time[-1] - time[0]) == 0.0:
        return np.nan
    slope, intercept = np.polyfit(time, y, 1)
    fitted_start = slope * time[0] + intercept
    fitted_end = slope * time[-1] + intercept
    return float(fitted_end - fitted_start)


def dead_channels(table: pd.DataFrame) -> list[str]:
    dead = []
    for column in ACC_COLUMNS + GYR_COLUMNS:
        finite = table[column].replace([np.inf, -np.inf], np.nan).dropna()
        if finite.empty:
            dead.append(column)
            continue
        if finite.nunique(dropna=True) <= 1 or float(finite.std(ddof=0)) <= 1e-12:
            dead.append(column)
    return dead


def abnormal_gap_indices(table: pd.DataFrame, expected_step: int | None) -> list[int]:
    if expected_step is None:
        return []
    diffs = sample_diffs(table["SampleTimeFine"])
    return [index + 1 for index, diff in enumerate(diffs) if int(diff) != expected_step]


def plot_recording(
    recording_path: Path,
    table: pd.DataFrame,
    output_path: Path,
    time_seconds: np.ndarray,
    gap_indices: list[int],
    title: str,
) -> None:
    acc_mag = vector_magnitude(table, ACC_COLUMNS)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS)

    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(title, fontsize=13)

    for column in ACC_COLUMNS:
        axes[0].plot(time_seconds, table[column], linewidth=0.8, label=column)
    axes[0].set_ylabel("Acceleration")
    axes[0].legend(loc="upper right", ncols=3, fontsize=8)

    for column in GYR_COLUMNS:
        axes[1].plot(time_seconds, table[column], linewidth=0.8, label=column)
    axes[1].set_ylabel("Gyro")
    axes[1].legend(loc="upper right", ncols=3, fontsize=8)

    for column in QUAT_COLUMNS:
        axes[2].plot(time_seconds, table[column], linewidth=0.8, label=column)
    axes[2].set_ylabel("Quaternion")
    axes[2].legend(loc="upper right", ncols=4, fontsize=8)

    axes[3].plot(time_seconds, acc_mag, linewidth=0.8, label="Acc magnitude")
    axes[3].plot(time_seconds, gyr_mag, linewidth=0.8, label="Gyro magnitude")
    axes[3].set_ylabel("Magnitude")
    axes[3].set_xlabel("Relative time at observed 60 Hz spacing (s)")
    axes[3].legend(loc="upper right", fontsize=8)

    gap_label_used = False
    for index in gap_indices:
        if 0 <= index < len(time_seconds):
            for axis in axes:
                axis.axvline(
                    time_seconds[index],
                    color="crimson",
                    linestyle="--",
                    linewidth=0.9,
                    alpha=0.75,
                    label="packet/time gap" if not gap_label_used else None,
                )
            gap_label_used = True

    if gap_label_used:
        for axis in axes:
            handles, labels = axis.get_legend_handles_labels()
            unique = dict(zip(labels, handles))
            axis.legend(unique.values(), unique.keys(), loc="upper right", fontsize=8)

    for axis in axes:
        axis.grid(True, linewidth=0.3, alpha=0.4)

    fig.text(0.01, 0.01, f"Source: {recording_path.as_posix()}", fontsize=8)
    fig.tight_layout(rect=[0, 0.02, 1, 0.97])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def qc_metrics(recording, expected_step: int | None) -> dict[str, object]:
    table = recording.table
    device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))
    activity = activity_from_filename(recording.path)
    output_rate = recording.metadata.get("OutputRate", "")
    output_rate_hz = parse_output_rate_hz(output_rate)
    time_seconds = relative_time_seconds(table["SampleTimeFine"], expected_step, output_rate_hz)

    acc_mag = vector_magnitude(table, ACC_COLUMNS)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS)
    quat_norm = quaternion_norm(table)
    gap_indices = abnormal_gap_indices(table, expected_step)
    missing_count, duplicate_count, missing_preview, duplicate_preview = packet_issues(
        table["PacketCounter"]
    )

    signal_values = table[SIGNAL_COLUMNS]
    non_finite_fraction = float((~np.isfinite(signal_values.to_numpy())).sum() / signal_values.size)
    dead = dead_channels(table)

    row: dict[str, object] = {
        "filename": recording.path.name,
        "relative_path": recording.path.as_posix(),
        "device_tag": device_tag,
        "activity": activity,
        "start_time": recording.metadata.get("StartTime", ""),
        "output_rate": output_rate,
        "output_rate_hz": output_rate_hz,
        "row_count": len(table),
        "expected_sample_time_step_for_rate": expected_step or "",
        "missing_packet_count": missing_count,
        "missing_packet_preview": ";".join(map(str, missing_preview)),
        "duplicate_packet_count": duplicate_count,
        "duplicate_packet_preview": ";".join(map(str, duplicate_preview)),
        "abnormal_time_gap_count": len(gap_indices),
        "abnormal_time_gap_indices": ";".join(map(str, gap_indices[:20])),
        "non_finite_fraction": non_finite_fraction,
        "dead_channels": ";".join(dead),
        "acc_mag_mean": float(acc_mag.mean()),
        "acc_mag_std": float(acc_mag.std(ddof=0)),
        "gyr_mag_mean": float(gyr_mag.mean()),
        "gyr_mag_std": float(gyr_mag.std(ddof=0)),
        "quat_norm_mean": float(quat_norm.mean()),
        "quat_norm_std": float(quat_norm.std(ddof=0)),
        "quat_norm_max_deviation_from_1": float((quat_norm - 1.0).abs().max()),
    }

    for column in ACC_COLUMNS + GYR_COLUMNS:
        series = table[column]
        max_jump, jump_count, jump_threshold = jump_metrics(series)
        row[f"{column}_mean"] = float(series.mean())
        row[f"{column}_std"] = float(series.std(ddof=0))
        row[f"{column}_min"] = float(series.min())
        row[f"{column}_max"] = float(series.max())
        row[f"{column}_max_abs_jump"] = max_jump
        row[f"{column}_extreme_jump_count"] = jump_count
        row[f"{column}_extreme_jump_threshold"] = jump_threshold

    total_extreme_jumps = sum(int(row[f"{column}_extreme_jump_count"]) for column in ACC_COLUMNS + GYR_COLUMNS)
    row["total_extreme_jump_count"] = total_extreme_jumps

    if activity == "STANDING":
        for column in GYR_COLUMNS:
            row[f"{column}_bias_standing"] = float(table[column].mean())
            row[f"{column}_rms_standing"] = float(np.sqrt(np.mean(table[column] ** 2)))
            row[f"{column}_linear_drift_standing"] = linear_drift(table[column], time_seconds)
        for column in ACC_COLUMNS + QUAT_COLUMNS:
            row[f"{column}_linear_drift_standing"] = linear_drift(table[column], time_seconds)
        row["gyro_rms_magnitude_standing"] = float(np.sqrt(np.mean(gyr_mag ** 2)))
        row["acc_stability_mean_axis_std_standing"] = float(
            table[ACC_COLUMNS].std(ddof=0).mean()
        )
        row["acc_stability_max_axis_std_standing"] = float(
            table[ACC_COLUMNS].std(ddof=0).max()
        )
        row["quat_stability_mean_component_std_standing"] = float(
            table[QUAT_COLUMNS].std(ddof=0).mean()
        )
        row["quat_stability_max_component_std_standing"] = float(
            table[QUAT_COLUMNS].std(ddof=0).max()
        )
    return row


def build_qc(raw_dir: Path, manifest_path: Path, summary_path: Path, plot_root: Path) -> pd.DataFrame:
    manifest = build_manifest(raw_dir, manifest_path)
    recordings = [read_xsens_csv(path) for path in sorted(raw_dir.rglob("*.csv"))]
    expected_steps = infer_expected_steps(recordings)
    rows = []

    for recording in recordings:
        output_rate = recording.metadata.get("OutputRate", "").strip()
        expected_step = expected_steps.get(output_rate)
        row = qc_metrics(recording, expected_step)
        table = recording.table
        output_rate_hz = parse_output_rate_hz(recording.metadata.get("OutputRate", ""))
        time_seconds = relative_time_seconds(
            table["SampleTimeFine"], expected_step, output_rate_hz
        )
        gap_indices = abnormal_gap_indices(table, expected_step)
        sensor = safe_name(row["device_tag"])
        activity = safe_name(row["activity"])
        plot_path = plot_root / activity / sensor / f"{recording.path.stem}_raw_qc.png"
        title = (
            f"{recording.path.name} | {row['device_tag']} | {row['activity']} | "
            f"rows={row['row_count']}"
        )
        if gap_indices:
            title += f" | gaps={len(gap_indices)}"
        plot_recording(recording.path, table, plot_path, time_seconds, gap_indices, title)
        row["plot_path"] = plot_path.as_posix()
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False)
    return summary


def print_summary(summary: pd.DataFrame, manifest_path: Path, summary_path: Path, plot_root: Path) -> None:
    print(f"QC recordings processed: {len(summary)}")
    print(f"Manifest: {manifest_path}")
    print(f"QC summary: {summary_path}")
    print(f"Plot root: {plot_root}")
    print()

    missing_sensors = sorted(EXPECTED_DEVICE_TAGS - set(summary["device_tag"]))
    if missing_sensors:
        print("Missing expected sensors:", ", ".join(missing_sensors))
    print()

    print("Warnings:")
    warnings = 0
    for row in summary.itertuples(index=False):
        issues = []
        if row.missing_packet_count:
            issues.append(f"missing packets={row.missing_packet_count} [{row.missing_packet_preview}]")
        if row.duplicate_packet_count:
            issues.append(f"duplicate packets={row.duplicate_packet_count}")
        if row.abnormal_time_gap_count:
            issues.append(f"time gaps={row.abnormal_time_gap_count} at indices [{row.abnormal_time_gap_indices}]")
        if row.non_finite_fraction > 0:
            issues.append(f"non-finite fraction={row.non_finite_fraction:.6f}")
        if row.dead_channels:
            issues.append(f"dead channels={row.dead_channels}")
        if row.total_extreme_jump_count:
            issues.append(f"extreme jumps={row.total_extreme_jump_count}")
        if row.quat_norm_max_deviation_from_1 > 0.01:
            issues.append(f"quat norm max deviation={row.quat_norm_max_deviation_from_1:.6f}")
        if issues:
            warnings += 1
            print(f"- {row.filename}: " + "; ".join(issues))
    if warnings == 0:
        print("- none")
    print()

    print("Standing baseline metrics:")
    standing = summary[summary["activity"] == "STANDING"]
    for row in standing.itertuples(index=False):
        print(
            f"- {row.filename}: gyro_rms_mag={row.gyro_rms_magnitude_standing:.4f}, "
            f"acc_axis_std_max={row.acc_stability_max_axis_std_standing:.4f}, "
            f"quat_component_std_max={row.quat_stability_max_component_std_standing:.4f}, "
            f"quat_norm_max_dev={row.quat_norm_max_deviation_from_1:.6f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only raw signal QC for Xsens DOT CSVs.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=RESULTS_ROOT / "manifests" / "xsens_manifest.csv",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=RESULTS_ROOT / "qc" / "raw_qc_summary.csv",
    )
    parser.add_argument(
        "--plot-root",
        type=Path,
        default=RESULTS_ROOT / "qc" / "raw_qc",
    )
    args = parser.parse_args()

    summary = build_qc(args.raw_dir, args.manifest, args.summary, args.plot_root)
    print_summary(summary, args.manifest, args.summary, args.plot_root)


if __name__ == "__main__":
    main()
