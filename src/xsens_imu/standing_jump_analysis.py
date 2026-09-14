from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from build_manifest import (
    activity_from_filename,
    infer_expected_steps,
    normalize_sensor_name,
    parse_output_rate_hz,
    read_xsens_csv,
)
from raw_signal_qc import (
    ACC_COLUMNS,
    GYR_COLUMNS,
    QUAT_COLUMNS,
    abnormal_gap_indices,
    relative_time_seconds,
    safe_name,
    vector_magnitude,
)
from standing_baseline_analysis import (
    normalize_quaternions,
    quat_conjugate,
    quat_multiply,
    relative_orientation_angle_deg,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


EVENT_EXCLUSION_SAMPLES = 2
GAP_EXCLUSION_SAMPLES = 2


def quat_pair_delta(q_before: np.ndarray, q_after: np.ndarray) -> np.ndarray:
    delta = quat_multiply(
        q_after.reshape(1, 4),
        quat_conjugate(q_before).reshape(1, 4),
    )[0]
    if delta[0] < 0:
        delta = -delta
    return normalize_quaternions(delta.reshape(1, 4))[0]


def axis_angle_from_delta(delta: np.ndarray) -> tuple[float, np.ndarray]:
    w = float(np.clip(delta[0], -1.0, 1.0))
    angle = float(np.degrees(2.0 * np.arccos(w)))
    sin_half = float(np.sqrt(max(0.0, 1.0 - w * w)))
    if sin_half < 1e-12:
        axis = np.array([np.nan, np.nan, np.nan], dtype=float)
    else:
        axis = delta[1:4] / sin_half
    return angle, axis


def consecutive_orientation_changes(table: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    quats = normalize_quaternions(table[QUAT_COLUMNS].to_numpy(dtype=float))
    angles = np.full(len(table), np.nan)
    axes = np.full((len(table), 3), np.nan)
    for index in range(1, len(table)):
        if not np.isfinite(quats[index - 1]).all() or not np.isfinite(quats[index]).all():
            continue
        delta = quat_pair_delta(quats[index - 1], quats[index])
        angle, axis = axis_angle_from_delta(delta)
        angles[index] = angle
        axes[index] = axis
    return angles, axes


def excluded_mask(length: int, gap_indices: list[int], event_index: int | None) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    for index in gap_indices:
        lo = max(0, index - GAP_EXCLUSION_SAMPLES)
        hi = min(length, index + GAP_EXCLUSION_SAMPLES + 1)
        mask[lo:hi] = True
    if event_index is not None:
        lo = max(0, event_index - EVENT_EXCLUSION_SAMPLES)
        hi = min(length, event_index + EVENT_EXCLUSION_SAMPLES + 1)
        mask[lo:hi] = True
    return mask


def segment_indices(length: int, event_index: int, gap_indices: list[int]) -> dict[str, np.ndarray]:
    excluded = excluded_mask(length, gap_indices, event_index)
    pre = np.arange(0, max(0, event_index - EVENT_EXCLUSION_SAMPLES))
    post = np.arange(min(length, event_index + EVENT_EXCLUSION_SAMPLES + 1), length)
    return {
        "pre": pre[~excluded[pre]],
        "post": post[~excluded[post]],
    }


def fit_drift(time: np.ndarray, values: np.ndarray, indices: np.ndarray) -> tuple[float, float]:
    if indices.size < 2:
        return np.nan, np.nan
    x = time[indices]
    y = values[indices]
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2 or float(x[mask][-1] - x[mask][0]) == 0.0:
        return np.nan, np.nan
    slope, intercept = np.polyfit(x[mask], y[mask], 1)
    drift = float((slope * x[mask][-1] + intercept) - (slope * x[mask][0] + intercept))
    return drift, float(slope)


def segment_orientation_angle(table: pd.DataFrame, indices: np.ndarray) -> np.ndarray:
    if indices.size == 0:
        return np.array([], dtype=float)
    quats = normalize_quaternions(table[QUAT_COLUMNS].to_numpy(dtype=float)[indices])
    valid = np.flatnonzero(np.isfinite(quats).all(axis=1))
    if valid.size == 0:
        return np.full(indices.size, np.nan)
    reference = quats[valid[0]]
    relative = quat_multiply(quats, np.tile(quat_conjugate(reference), (len(quats), 1)))
    relative = normalize_quaternions(relative)
    scalar = np.clip(np.abs(relative[:, 0]), -1.0, 1.0)
    return np.degrees(2.0 * np.arccos(scalar))


def segment_stats(
    table: pd.DataFrame,
    time_seconds: np.ndarray,
    indices: np.ndarray,
    prefix: str,
) -> dict[str, object]:
    row: dict[str, object] = {
        f"{prefix}_sample_count": int(indices.size),
        f"{prefix}_start_s": float(time_seconds[indices[0]]) if indices.size else np.nan,
        f"{prefix}_end_s": float(time_seconds[indices[-1]]) if indices.size else np.nan,
    }
    acc_mag = vector_magnitude(table, ACC_COLUMNS).to_numpy(dtype=float)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS).to_numpy(dtype=float)
    segment_angle = segment_orientation_angle(table, indices)

    for name, values in [("acc_mag", acc_mag), ("gyr_mag", gyr_mag)]:
        data = values[indices] if indices.size else np.array([], dtype=float)
        row[f"{prefix}_{name}_mean"] = float(np.nanmean(data)) if data.size else np.nan
        row[f"{prefix}_{name}_std"] = float(np.nanstd(data)) if data.size else np.nan
        row[f"{prefix}_{name}_min"] = float(np.nanmin(data)) if data.size else np.nan
        row[f"{prefix}_{name}_max"] = float(np.nanmax(data)) if data.size else np.nan
        drift, slope = fit_drift(time_seconds, values, indices)
        row[f"{prefix}_{name}_drift"] = drift
        row[f"{prefix}_{name}_drift_rate_per_s_observed"] = slope

    row[f"{prefix}_orientation_std_deg"] = (
        float(np.nanstd(segment_angle)) if segment_angle.size else np.nan
    )
    row[f"{prefix}_orientation_max_change_deg"] = (
        float(np.nanmax(segment_angle)) if segment_angle.size else np.nan
    )
    drift, slope = fit_drift(
        time_seconds[indices] if indices.size else np.array([]),
        segment_angle,
        np.arange(segment_angle.size),
    )
    row[f"{prefix}_orientation_drift_deg"] = drift
    row[f"{prefix}_orientation_drift_rate_deg_per_s_observed"] = slope

    for column in ACC_COLUMNS + GYR_COLUMNS:
        values = table[column].to_numpy(dtype=float)
        data = values[indices] if indices.size else np.array([], dtype=float)
        row[f"{prefix}_{column}_mean"] = float(np.nanmean(data)) if data.size else np.nan
        row[f"{prefix}_{column}_std"] = float(np.nanstd(data)) if data.size else np.nan
        drift, slope = fit_drift(time_seconds, values, indices)
        row[f"{prefix}_{column}_drift"] = drift
        row[f"{prefix}_{column}_drift_rate_per_s_observed"] = slope
    return row


def event_context(table: pd.DataFrame, event_index: int, output_rate_hz: float | None) -> dict[str, object]:
    radius = int(round((output_rate_hz or 60.0) * 0.5))
    lo = max(0, event_index - radius)
    hi = min(len(table), event_index + radius + 1)
    acc_mag = vector_magnitude(table, ACC_COLUMNS).to_numpy(dtype=float)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS).to_numpy(dtype=float)
    return {
        "event_window_acc_mag_mean": float(np.nanmean(acc_mag[lo:hi])),
        "event_window_acc_mag_std": float(np.nanstd(acc_mag[lo:hi])),
        "event_window_acc_mag_min": float(np.nanmin(acc_mag[lo:hi])),
        "event_window_acc_mag_max": float(np.nanmax(acc_mag[lo:hi])),
        "event_window_gyr_mag_mean": float(np.nanmean(gyr_mag[lo:hi])),
        "event_window_gyr_mag_std": float(np.nanstd(gyr_mag[lo:hi])),
        "event_window_gyr_mag_min": float(np.nanmin(gyr_mag[lo:hi])),
        "event_window_gyr_mag_max": float(np.nanmax(gyr_mag[lo:hi])),
    }


def plot_jump(
    filename: str,
    device_tag: str,
    time_seconds: np.ndarray,
    acc_mag: np.ndarray,
    gyr_mag: np.ndarray,
    global_angle: np.ndarray,
    consecutive_angle: np.ndarray,
    event_index: int,
    gap_indices: list[int],
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(4, 1, figsize=(13, 9), sharex=True)
    fig.suptitle(f"{filename} | {device_tag} | standing orientation jump", fontsize=13)

    axes[0].plot(time_seconds, acc_mag, linewidth=0.9)
    axes[0].set_ylabel("Acc magnitude")
    axes[1].plot(time_seconds, gyr_mag, linewidth=0.9, color="tab:orange")
    axes[1].set_ylabel("Gyr magnitude")
    axes[2].plot(time_seconds, global_angle, linewidth=0.9, color="tab:green")
    axes[2].set_ylabel("Rel. orientation (deg)")
    axes[3].plot(time_seconds, consecutive_angle, linewidth=0.9, color="tab:purple")
    axes[3].set_ylabel("Step angle (deg)")
    axes[3].set_xlabel("Relative time at observed 60 Hz spacing (s)")

    event_time = time_seconds[event_index]
    for axis in axes:
        axis.axvline(event_time, color="purple", linestyle=":", linewidth=1.2, label="largest jump")
        axis.axvspan(time_seconds[0], event_time, color="tab:blue", alpha=0.06, label="pre")
        axis.axvspan(event_time, time_seconds[-1], color="tab:green", alpha=0.06, label="post")

    gap_label = False
    for index in gap_indices:
        if 0 <= index < len(time_seconds):
            for axis in axes:
                axis.axvline(
                    time_seconds[index],
                    color="crimson",
                    linestyle="--",
                    linewidth=0.9,
                    alpha=0.75,
                    label="packet/time gap" if not gap_label else None,
                )
            gap_label = True

    for axis in axes:
        axis.grid(True, linewidth=0.3, alpha=0.4)
        handles, labels = axis.get_legend_handles_labels()
        unique = dict(zip(labels, handles))
        axis.legend(unique.values(), unique.keys(), loc="upper right", fontsize=8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def analyze_recording(recording, expected_step: int | None, plot_root: Path) -> dict[str, object]:
    table = recording.table
    device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))
    output_rate_hz = parse_output_rate_hz(recording.metadata.get("OutputRate", ""))
    time_seconds = relative_time_seconds(table["SampleTimeFine"], expected_step, output_rate_hz)
    gap_indices = abnormal_gap_indices(table, expected_step)
    global_angle = relative_orientation_angle_deg(table)
    consecutive_angle, consecutive_axes = consecutive_orientation_changes(table)
    usable_angles = consecutive_angle.copy()
    for gap_index in gap_indices:
        lo = max(0, gap_index - GAP_EXCLUSION_SAMPLES)
        hi = min(len(usable_angles), gap_index + GAP_EXCLUSION_SAMPLES + 1)
        usable_angles[lo:hi] = np.nan
    event_index = int(np.nanargmax(usable_angles))
    event_angle = float(consecutive_angle[event_index])
    event_axis = consecutive_axes[event_index]

    quats = normalize_quaternions(table[QUAT_COLUMNS].to_numpy(dtype=float))
    before_quat = quats[event_index - 1] if event_index > 0 else np.full(4, np.nan)
    after_quat = quats[event_index]

    segments = segment_indices(len(table), event_index, gap_indices)
    row: dict[str, object] = {
        "filename": recording.path.name,
        "device_tag": device_tag,
        "start_time": recording.metadata.get("StartTime", ""),
        "output_rate": recording.metadata.get("OutputRate", ""),
        "sync_status": recording.metadata.get("SyncStatus", ""),
        "filter_profile": recording.metadata.get("FilterProfile", ""),
        "measurement_mode": recording.metadata.get("Measurement Mode", ""),
        "event_index": event_index,
        "event_time_s_observed": float(time_seconds[event_index]),
        "event_sample_time_fine": int(table["SampleTimeFine"].iloc[event_index]),
        "event_packet_counter": int(table["PacketCounter"].iloc[event_index]),
        "event_step_rotation_deg": event_angle,
        "event_axis_x": float(event_axis[0]),
        "event_axis_y": float(event_axis[1]),
        "event_axis_z": float(event_axis[2]),
        "quat_before_w": float(before_quat[0]),
        "quat_before_x": float(before_quat[1]),
        "quat_before_y": float(before_quat[2]),
        "quat_before_z": float(before_quat[3]),
        "quat_after_w": float(after_quat[0]),
        "quat_after_x": float(after_quat[1]),
        "quat_after_y": float(after_quat[2]),
        "quat_after_z": float(after_quat[3]),
        "gap_indices_excluded": ";".join(map(str, gap_indices)),
        **event_context(table, event_index, output_rate_hz),
    }
    row.update(segment_stats(table, time_seconds, segments["pre"], "pre"))
    row.update(segment_stats(table, time_seconds, segments["post"], "post"))

    pre_score = row["pre_orientation_max_change_deg"] + row["pre_gyr_mag_std"] + row["pre_acc_mag_std"]
    post_score = row["post_orientation_max_change_deg"] + row["post_gyr_mag_std"] + row["post_acc_mag_std"]
    row["more_stable_segment"] = "pre" if pre_score <= post_score else "post"
    if row["more_stable_segment"] == "pre":
        row["best_interval_start_s"] = row["pre_start_s"]
        row["best_interval_end_s"] = row["pre_end_s"]
    else:
        row["best_interval_start_s"] = row["post_start_s"]
        row["best_interval_end_s"] = row["post_end_s"]

    plot_path = plot_root / safe_name(device_tag) / f"{recording.path.stem}_jump_analysis.png"
    plot_jump(
        recording.path.name,
        device_tag,
        time_seconds,
        vector_magnitude(table, ACC_COLUMNS).to_numpy(dtype=float),
        vector_magnitude(table, GYR_COLUMNS).to_numpy(dtype=float),
        global_angle,
        consecutive_angle,
        event_index,
        gap_indices,
        plot_path,
    )
    row["plot_path"] = plot_path.as_posix()
    return row


def axis_similarity(rows: pd.DataFrame) -> tuple[float, float]:
    axes = rows[["event_axis_x", "event_axis_y", "event_axis_z"]].to_numpy(dtype=float)
    dots = []
    abs_dots = []
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            if not np.isfinite(axes[i]).all() or not np.isfinite(axes[j]).all():
                continue
            dot = float(np.dot(axes[i], axes[j]))
            dots.append(dot)
            abs_dots.append(abs(dot))
    return (
        float(np.mean(dots)) if dots else np.nan,
        float(np.mean(abs_dots)) if abs_dots else np.nan,
    )


def run(raw_dir: Path, summary_path: Path, plot_root: Path) -> pd.DataFrame:
    recordings = [read_xsens_csv(path) for path in sorted(raw_dir.rglob("*.csv"))]
    expected_steps = infer_expected_steps(recordings)
    standing = [
        recording
        for recording in recordings
        if activity_from_filename(recording.path) == "STANDING"
    ]
    rows = [
        analyze_recording(
            recording,
            expected_steps.get(recording.metadata.get("OutputRate", "").strip()),
            plot_root,
        )
        for recording in standing
    ]
    summary = pd.DataFrame(rows)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False)
    return summary


def print_summary(summary: pd.DataFrame, summary_path: Path, plot_root: Path) -> None:
    print(f"Standing jump recordings analyzed: {len(summary)}")
    print()
    for row in summary.itertuples(index=False):
        print(
            f"{row.device_tag}: event={row.event_time_s_observed:.3f}s "
            f"packet={row.event_packet_counter}, step={row.event_step_rotation_deg:.2f} deg, "
            f"axis=({row.event_axis_x:.3f},{row.event_axis_y:.3f},{row.event_axis_z:.3f}), "
            f"stable={row.more_stable_segment} [{row.best_interval_start_s:.3f}-{row.best_interval_end_s:.3f}s]"
        )
        print(
            f"  pre orient max/std/drift={row.pre_orientation_max_change_deg:.3f}/"
            f"{row.pre_orientation_std_deg:.3f}/{row.pre_orientation_drift_deg:.3f}; "
            f"post={row.post_orientation_max_change_deg:.3f}/"
            f"{row.post_orientation_std_deg:.3f}/{row.post_orientation_drift_deg:.3f}"
        )

    sample_spread = int(summary["event_index"].max() - summary["event_index"].min())
    packet_spread = int(summary["event_packet_counter"].max() - summary["event_packet_counter"].min())
    sample_time_spread = int(summary["event_sample_time_fine"].max() - summary["event_sample_time_fine"].min())
    time_spread = float(summary["event_time_s_observed"].max() - summary["event_time_s_observed"].min())
    mean_axis_dot, mean_abs_axis_dot = axis_similarity(summary)
    print()
    print(f"Event index spread: {sample_spread} samples")
    print(f"Event packet-counter spread: {packet_spread} packets")
    print(f"Event SampleTimeFine spread: {sample_time_spread}")
    print(f"Event time spread: {time_spread:.6f} s on observed 60 Hz axis")
    print(f"Mean signed axis dot product: {mean_axis_dot:.3f}")
    print(f"Mean absolute axis dot product: {mean_abs_axis_dot:.3f}")
    print(f"Summary written to: {summary_path}")
    print(f"Plots written under: {plot_root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigate common STANDING orientation jump.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument(
        "--summary",
        type=Path,
        default=RESULTS_ROOT / "qc" / "standing_jump_analysis.csv",
    )
    parser.add_argument(
        "--plot-root",
        type=Path,
        default=RESULTS_ROOT / "qc" / "standing_jump",
    )
    args = parser.parse_args()

    summary = run(args.raw_dir, args.summary, args.plot_root)
    print_summary(summary, args.summary, args.plot_root)


if __name__ == "__main__":
    main()
