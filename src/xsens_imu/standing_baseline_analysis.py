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
    packet_issues,
    parse_output_rate_hz,
    read_xsens_csv,
)
from raw_signal_qc import (
    ACC_COLUMNS,
    GYR_COLUMNS,
    QUAT_COLUMNS,
    abnormal_gap_indices,
    linear_drift,
    relative_time_seconds,
    safe_name,
    vector_magnitude,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


def normalize_quaternions(quaternions: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(quaternions, axis=1)
    normalized = quaternions.copy()
    valid = np.isfinite(norms) & (norms > 0)
    normalized[valid] = normalized[valid] / norms[valid, None]
    normalized[~valid] = np.nan
    return normalized


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]], dtype=float)


def quat_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a.T
    bw, bx, by, bz = b.T
    return np.column_stack(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ]
    )


def relative_orientation_angle_deg(table: pd.DataFrame) -> np.ndarray:
    quats = normalize_quaternions(table[QUAT_COLUMNS].to_numpy(dtype=float))
    valid_indices = np.flatnonzero(np.isfinite(quats).all(axis=1))
    if valid_indices.size == 0:
        return np.full(len(table), np.nan)

    reference = quats[valid_indices[0]]
    relative = quat_multiply(quats, np.tile(quat_conjugate(reference), (len(quats), 1)))
    relative = normalize_quaternions(relative)
    scalar = np.clip(np.abs(relative[:, 0]), -1.0, 1.0)
    return np.degrees(2.0 * np.arccos(scalar))


def robust_threshold(values: np.ndarray, multiplier: float = 8.0) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.inf
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    if mad > 0:
        return median + multiplier * 1.4826 * mad
    std = float(np.std(finite))
    if std > 0:
        return median + multiplier * std
    return np.inf


def event_indices(angle_deg: np.ndarray, gap_indices: list[int]) -> list[int]:
    angle_step = np.abs(np.diff(angle_deg, prepend=angle_deg[0]))
    threshold = max(1.0, robust_threshold(angle_step, multiplier=10.0))
    candidates = np.flatnonzero(angle_step > threshold).tolist()

    # Keep packet/time gaps separate from orientation events when they are adjacent.
    gap_neighbors = set()
    for gap_index in gap_indices:
        gap_neighbors.update(range(max(0, gap_index - 1), gap_index + 2))
    candidates = [index for index in candidates if index not in gap_neighbors]

    if not candidates:
        return []

    merged = [candidates[0]]
    for index in candidates[1:]:
        if index - merged[-1] > 5:
            merged.append(index)
    return merged


def window_slice(index: int, sample_count: int, output_rate_hz: float | None) -> slice:
    radius = int(round((output_rate_hz or 60.0) * 0.5))
    return slice(max(0, index - radius), min(sample_count, index + radius + 1))


def movement_context(
    table: pd.DataFrame,
    time_seconds: np.ndarray,
    angle_deg: np.ndarray,
    events: list[int],
    output_rate_hz: float | None,
) -> tuple[str, str]:
    if not events:
        return "no major orientation events detected", ""

    acc_mag = vector_magnitude(table, ACC_COLUMNS).to_numpy(dtype=float)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS).to_numpy(dtype=float)
    gyr_threshold = robust_threshold(gyr_mag, multiplier=6.0)
    acc_dev = np.abs(acc_mag - np.nanmedian(acc_mag))
    acc_threshold = robust_threshold(acc_dev, multiplier=6.0)

    details = []
    moving_flags = []
    for index in events:
        region = window_slice(index, len(table), output_rate_hz)
        local_gyr_max = float(np.nanmax(gyr_mag[region]))
        local_acc_dev_max = float(np.nanmax(acc_dev[region]))
        moving = local_gyr_max > gyr_threshold or local_acc_dev_max > acc_threshold
        moving_flags.append(moving)
        details.append(
            f"{time_seconds[index]:.3f}s:angle={angle_deg[index]:.2f},"
            f"gyr_max={local_gyr_max:.3f},acc_dev_max={local_acc_dev_max:.3f},"
            f"{'movement' if moving else 'static'}"
        )

    if any(moving_flags):
        verdict = "some orientation events coincide with Acc/Gyr activity"
    else:
        verdict = "orientation events occur while Acc/Gyr remain approximately static"
    return verdict, ";".join(details)


def plot_standing_baseline(
    recording_name: str,
    device_tag: str,
    time_seconds: np.ndarray,
    acc_mag: pd.Series,
    gyr_mag: pd.Series,
    angle_deg: np.ndarray,
    gap_indices: list[int],
    events: list[int],
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(13, 8), sharex=True)
    fig.suptitle(f"{recording_name} | {device_tag} | standing baseline", fontsize=13)

    axes[0].plot(time_seconds, acc_mag, linewidth=0.9, color="tab:blue")
    axes[0].set_ylabel("Acc magnitude")

    axes[1].plot(time_seconds, gyr_mag, linewidth=0.9, color="tab:orange")
    axes[1].set_ylabel("Gyr magnitude")

    axes[2].plot(time_seconds, angle_deg, linewidth=0.9, color="tab:green")
    axes[2].set_ylabel("Relative orientation (deg)")
    axes[2].set_xlabel("Relative time at observed 60 Hz spacing (s)")

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

    event_label = False
    for index in events:
        if 0 <= index < len(time_seconds):
            for axis in axes:
                axis.axvline(
                    time_seconds[index],
                    color="purple",
                    linestyle=":",
                    linewidth=1.1,
                    alpha=0.85,
                    label="orientation event" if not event_label else None,
                )
            event_label = True

    for axis in axes:
        axis.grid(True, linewidth=0.3, alpha=0.4)
        handles, labels = axis.get_legend_handles_labels()
        if labels:
            unique = dict(zip(labels, handles))
            axis.legend(unique.values(), unique.keys(), loc="upper right", fontsize=8)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def baseline_verdict(
    missing_packet_count: int,
    max_angle_deg: float,
    orientation_event_context: str,
    acc_mag_std: float,
    gyro_mag_std: float,
) -> str:
    if missing_packet_count > 0 or max_angle_deg > 45.0:
        return "POOR"
    if max_angle_deg > 15.0:
        return "USABLE_WITH_CAUTION"
    if "movement" in orientation_event_context and max_angle_deg > 5.0:
        return "USABLE_WITH_CAUTION"
    if acc_mag_std > 0.05 or gyro_mag_std > 1.0:
        return "USABLE_WITH_CAUTION"
    return "GOOD"


def analyze_recording(recording, expected_step: int | None, output_root: Path) -> dict[str, object]:
    table = recording.table
    device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))
    output_rate_hz = parse_output_rate_hz(recording.metadata.get("OutputRate", ""))
    time_seconds = relative_time_seconds(table["SampleTimeFine"], expected_step, output_rate_hz)
    acc_mag = vector_magnitude(table, ACC_COLUMNS)
    gyr_mag = vector_magnitude(table, GYR_COLUMNS)
    angle_deg = relative_orientation_angle_deg(table)
    gap_indices = abnormal_gap_indices(table, expected_step)
    events = event_indices(angle_deg, gap_indices)
    movement_verdict, event_detail = movement_context(
        table, time_seconds, angle_deg, events, output_rate_hz
    )
    missing_count, duplicate_count, missing_preview, duplicate_preview = packet_issues(
        table["PacketCounter"]
    )

    duration = float(time_seconds[-1] - time_seconds[0]) if len(time_seconds) > 1 else np.nan
    max_angle = float(np.nanmax(angle_deg))
    drift_rate = max_angle / duration if duration and np.isfinite(duration) else np.nan

    row: dict[str, object] = {
        "filename": recording.path.name,
        "device_tag": device_tag,
        "row_count": len(table),
        "duration_observed_60hz_seconds": duration,
        "missing_packet_count": missing_count,
        "missing_packet_preview": ";".join(map(str, missing_preview)),
        "duplicate_packet_count": duplicate_count,
        "duplicate_packet_preview": ";".join(map(str, duplicate_preview)),
        "abnormal_gap_indices": ";".join(map(str, gap_indices)),
        "acc_mag_mean": float(acc_mag.mean()),
        "acc_mag_std": float(acc_mag.std(ddof=0)),
        "acc_mag_min": float(acc_mag.min()),
        "acc_mag_max": float(acc_mag.max()),
        "gyr_mag_mean": float(gyr_mag.mean()),
        "gyr_mag_std": float(gyr_mag.std(ddof=0)),
        "gyr_mag_min": float(gyr_mag.min()),
        "gyr_mag_max": float(gyr_mag.max()),
        "relative_orientation_max_deg": max_angle,
        "relative_orientation_final_deg": float(angle_deg[-1]),
        "relative_orientation_std_deg": float(np.nanstd(angle_deg)),
        "relative_orientation_drift_rate_deg_per_s_observed": drift_rate,
        "major_orientation_event_count": len(events),
        "major_orientation_event_times_s": ";".join(f"{time_seconds[i]:.3f}" for i in events),
        "major_orientation_event_details": event_detail,
        "orientation_event_context": movement_verdict,
    }

    for column in ACC_COLUMNS:
        row[f"{column}_mean"] = float(table[column].mean())
        row[f"{column}_std"] = float(table[column].std(ddof=0))
        row[f"{column}_min"] = float(table[column].min())
        row[f"{column}_max"] = float(table[column].max())
        row[f"{column}_drift"] = linear_drift(table[column], time_seconds)

    for column in GYR_COLUMNS:
        row[f"{column}_bias"] = float(table[column].mean())
        row[f"{column}_rms"] = float(np.sqrt(np.mean(table[column] ** 2)))
        row[f"{column}_std"] = float(table[column].std(ddof=0))
        row[f"{column}_min"] = float(table[column].min())
        row[f"{column}_max"] = float(table[column].max())
        row[f"{column}_drift"] = linear_drift(table[column], time_seconds)

    for column in QUAT_COLUMNS:
        row[f"{column}_mean"] = float(table[column].mean())
        row[f"{column}_std"] = float(table[column].std(ddof=0))
        row[f"{column}_drift"] = linear_drift(table[column], time_seconds)

    row["baseline_verdict"] = baseline_verdict(
        missing_count,
        max_angle,
        movement_verdict,
        float(acc_mag.std(ddof=0)),
        float(gyr_mag.std(ddof=0)),
    )

    plot_path = (
        output_root
        / safe_name(device_tag)
        / f"{recording.path.stem}_standing_baseline.png"
    )
    plot_standing_baseline(
        recording.path.name,
        device_tag,
        time_seconds,
        acc_mag,
        gyr_mag,
        angle_deg,
        gap_indices,
        events,
        plot_path,
    )
    row["plot_path"] = plot_path.as_posix()
    return row


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


def print_terminal_summary(summary: pd.DataFrame) -> None:
    print(f"Standing recordings analyzed: {len(summary)}")
    print()
    for row in summary.itertuples(index=False):
        print(
            f"{row.device_tag}: verdict={row.baseline_verdict}, "
            f"acc_mag_std={row.acc_mag_std:.5f}, gyr_mag_mean={row.gyr_mag_mean:.4f}, "
            f"gyr_mag_std={row.gyr_mag_std:.4f}, "
            f"max_orientation_change={row.relative_orientation_max_deg:.2f} deg, "
            f"drift_rate={row.relative_orientation_drift_rate_deg_per_s_observed:.3f} deg/s"
        )
        print(f"  events: {row.orientation_event_context}")
        if row.missing_packet_count:
            print(
                f"  packet gaps: missing={row.missing_packet_count} "
                f"[{row.missing_packet_preview}], gap_indices=[{row.abnormal_gap_indices}]"
            )
    print()
    counts = summary["baseline_verdict"].value_counts().to_dict()
    print("Verdicts:", ", ".join(f"{key}={value}" for key, value in counts.items()))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze STANDING recordings as possible static baselines."
    )
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument(
        "--summary",
        type=Path,
        default=RESULTS_ROOT / "qc" / "standing_baseline_summary.csv",
    )
    parser.add_argument(
        "--plot-root",
        type=Path,
        default=RESULTS_ROOT / "qc" / "standing_baseline",
    )
    args = parser.parse_args()

    summary = run(args.raw_dir, args.summary, args.plot_root)
    print_terminal_summary(summary)
    print()
    print(f"Summary written to: {args.summary}")
    print(f"Plots written under: {args.plot_root}")


if __name__ == "__main__":
    main()
