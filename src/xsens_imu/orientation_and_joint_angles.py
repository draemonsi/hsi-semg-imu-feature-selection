from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="matplotlib-"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from build_manifest import (
    activity_from_filename,
    infer_expected_steps,
    normalize_sensor_name,
    parse_output_rate_hz,
    read_xsens_csv,
)
from raw_signal_qc import ACC_COLUMNS, GYR_COLUMNS, relative_time_seconds, safe_name
from raw_vs_processed_pipeline import run as run_raw_vs_processed
from standing_baseline_analysis import (
    normalize_quaternions,
    quat_conjugate,
    quat_multiply,
)


QUAT_COLUMNS = ["Quat_W", "Quat_X", "Quat_Y", "Quat_Z"]
FILTERED_COLUMNS = [f"{column}_filtered" for column in ACC_COLUMNS + GYR_COLUMNS]
REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"
OUTPUT_ROOT = RESULTS_ROOT / "orientation" / "orientation_and_joint_angles"


@dataclass(frozen=True)
class RecordingRef:
    filename: str
    path: Path
    processed_path: Path
    activity: str
    device_tag: str
    side: str
    segment: str


def side_from_device_tag(device_tag: str) -> str:
    if device_tag.endswith("_L"):
        return "L"
    if device_tag.endswith("_R"):
        return "R"
    return ""


def segment_from_device_tag(device_tag: str) -> str:
    if "Thigh" in device_tag:
        return "THIGH"
    if "Shank" in device_tag:
        return "SHANK"
    return ""


def load_processed_summary(path: Path) -> dict[str, Path]:
    if not path.exists():
        return {}
    summary = pd.read_csv(path)
    return {
        str(row.filename): Path(str(row.processed_csv))
        for row in summary.itertuples(index=False)
    }


def ensure_processed_outputs(raw_dir: Path) -> dict[str, Path]:
    summary_path = RESULTS_ROOT / "processed" / "raw_vs_processed_summary.csv"
    processed_by_file = load_processed_summary(summary_path)
    expected_count = len(list(raw_dir.rglob("*.csv")))
    if len(processed_by_file) < expected_count:
        run_raw_vs_processed(
            raw_dir,
            RESULTS_ROOT / "processed" / "raw_vs_processed",
            RESULTS_ROOT / "processed" / "processed_csv",
            10.0,
            4,
            RESULTS_ROOT / "processed" / "raw_vs_processed_spectral_summary.csv",
            summary_path,
        )
        processed_by_file = load_processed_summary(summary_path)
    return processed_by_file


def recording_refs(raw_dir: Path, processed_by_file: dict[str, Path]) -> list[RecordingRef]:
    refs = []
    for path in sorted(raw_dir.rglob("*.csv")):
        recording = read_xsens_csv(path)
        device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))
        refs.append(
            RecordingRef(
                filename=path.name,
                path=path,
                processed_path=processed_by_file.get(path.name, Path()),
                activity=activity_from_filename(path),
                device_tag=device_tag,
                side=side_from_device_tag(device_tag),
                segment=segment_from_device_tag(device_tag),
            )
        )
    return refs


def synchronized_pairs(refs: list[RecordingRef], raw_dir: Path) -> list[tuple[RecordingRef, RecordingRef]]:
    recordings = {ref.filename: read_xsens_csv(ref.path) for ref in refs}
    expected_steps = infer_expected_steps(list(recordings.values()))
    pairs = []
    for activity in sorted({ref.activity for ref in refs}):
        for side in ["L", "R"]:
            thigh = next(
                (ref for ref in refs if ref.activity == activity and ref.side == side and ref.segment == "THIGH"),
                None,
            )
            shank = next(
                (ref for ref in refs if ref.activity == activity and ref.side == side and ref.segment == "SHANK"),
                None,
            )
            if thigh is None or shank is None:
                continue
            thigh_rec = recordings[thigh.filename]
            shank_rec = recordings[shank.filename]
            step = expected_steps.get(thigh_rec.metadata.get("OutputRate", "").strip())
            thigh_first = int(thigh_rec.table["SampleTimeFine"].iloc[0])
            shank_first = int(shank_rec.table["SampleTimeFine"].iloc[0])
            if step is not None and abs(thigh_first - shank_first) <= step:
                pairs.append((thigh, shank))
    return pairs


def quat_relative_orientation(thigh_quat: np.ndarray, shank_quat: np.ndarray) -> np.ndarray:
    thigh = normalize_quaternions(thigh_quat)
    shank = normalize_quaternions(shank_quat)
    return normalize_quaternions(quat_multiply(np.apply_along_axis(quat_conjugate, 1, thigh), shank))


def quat_to_rotvec(quat: np.ndarray) -> np.ndarray:
    q = normalize_quaternions(quat)
    rotvec = np.zeros((len(q), 3), dtype=float)
    for index, value in enumerate(q):
        if not np.isfinite(value).all():
            rotvec[index] = np.nan
            continue
        if value[0] < 0:
            value = -value
        w = float(np.clip(value[0], -1.0, 1.0))
        angle = 2.0 * np.arccos(w)
        sin_half = np.sqrt(max(0.0, 1.0 - w * w))
        if sin_half < 1e-12:
            axis = np.zeros(3)
        else:
            axis = value[1:4] / sin_half
        rotvec[index] = axis * np.degrees(angle)
    return rotvec


def orientation_change_angle(quat: np.ndarray) -> np.ndarray:
    q = normalize_quaternions(quat)
    if len(q) == 0:
        return np.array([])
    rel = quat_multiply(q, np.tile(quat_conjugate(q[0]), (len(q), 1)))
    scalar = np.clip(np.abs(normalize_quaternions(rel)[:, 0]), -1.0, 1.0)
    return np.degrees(2.0 * np.arccos(scalar))


def prepare_pair(thigh_ref: RecordingRef, shank_ref: RecordingRef) -> pd.DataFrame:
    thigh = pd.read_csv(thigh_ref.processed_path)
    shank = pd.read_csv(shank_ref.processed_path)
    suffix_thigh = "_thigh"
    suffix_shank = "_shank"
    paired = pd.merge(
        thigh,
        shank,
        on=["SampleTimeFine"],
        how="inner",
        suffixes=(suffix_thigh, suffix_shank),
    )
    return paired


def dominant_axis(rotvec: np.ndarray) -> np.ndarray:
    valid = rotvec[np.isfinite(rotvec).all(axis=1)]
    if len(valid) < 3:
        return np.array([np.nan, np.nan, np.nan], dtype=float)
    _, _, vh = np.linalg.svd(valid, full_matrices=False)
    axis = vh[0]
    axis = axis / np.linalg.norm(axis)
    projection = rotvec @ axis
    if np.nanmax(projection) < abs(np.nanmin(projection)):
        axis = -axis
    return axis


def knee_estimate_from_pair(paired: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    thigh_quat = paired[[f"{column}_thigh" for column in QUAT_COLUMNS]].to_numpy(dtype=float)
    shank_quat = paired[[f"{column}_shank" for column in QUAT_COLUMNS]].to_numpy(dtype=float)
    q_rel = quat_relative_orientation(thigh_quat, shank_quat)
    q_zeroed = quat_multiply(q_rel, np.tile(quat_conjugate(q_rel[0]), (len(q_rel), 1)))
    rotvec = quat_to_rotvec(q_zeroed)
    axis = dominant_axis(rotvec)
    angle = rotvec @ axis
    if np.nanmax(angle) < abs(np.nanmin(angle)):
        angle = -angle
        axis = -axis
    angle = angle - float(angle[0])
    thigh_orientation = orientation_change_angle(thigh_quat)
    shank_orientation = orientation_change_angle(shank_quat)
    return angle, axis, rotvec, thigh_orientation, shank_orientation


def detect_cycles(activity: str, angle: np.ndarray, sample_rate_hz: float) -> tuple[int, np.ndarray]:
    if activity != "SQUAT":
        return 0, np.array([], dtype=int)
    finite = np.nan_to_num(angle, nan=np.nanmedian(angle))
    angle_range = float(np.nanmax(finite) - np.nanmin(finite))
    if angle_range < 5.0:
        return 0, np.array([], dtype=int)
    peaks, _ = find_peaks(
        finite,
        prominence=max(5.0, 0.20 * angle_range),
        distance=max(1, int(sample_rate_hz * 1.0)),
    )
    return int(len(peaks)), peaks


def timing_similarity(left: pd.DataFrame, right: pd.DataFrame) -> dict[str, object]:
    merged = pd.merge(
        left[["SampleTimeFine", "knee_flexion_estimate_deg"]],
        right[["SampleTimeFine", "knee_flexion_estimate_deg"]],
        on="SampleTimeFine",
        suffixes=("_left", "_right"),
    )
    if len(merged) < 3:
        return {"left_right_corr": np.nan, "left_right_peak_lag_samples": np.nan}
    left_signal = merged["knee_flexion_estimate_deg_left"].to_numpy(dtype=float)
    right_signal = merged["knee_flexion_estimate_deg_right"].to_numpy(dtype=float)
    corr = float(np.corrcoef(left_signal, right_signal)[0, 1])
    left_centered = left_signal - np.nanmean(left_signal)
    right_centered = right_signal - np.nanmean(right_signal)
    xcorr = np.correlate(left_centered, right_centered, mode="full")
    lag = int(np.argmax(xcorr) - (len(left_centered) - 1))
    return {"left_right_corr": corr, "left_right_peak_lag_samples": lag}


def plot_pair(
    paired: pd.DataFrame,
    thigh_ref: RecordingRef,
    shank_ref: RecordingRef,
    time_seconds: np.ndarray,
    thigh_orientation: np.ndarray,
    shank_orientation: np.ndarray,
    knee_angle: np.ndarray,
    peaks: np.ndarray,
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(6, 1, figsize=(14, 15), sharex=True)
    fig.suptitle(
        f"{thigh_ref.activity} {thigh_ref.side} | {thigh_ref.device_tag} vs {shank_ref.device_tag} | IMU relative knee flexion estimate",
        fontsize=13,
    )

    for segment, color_prefix in [("thigh", ""), ("shank", "--")]:
        for column in ACC_COLUMNS:
            axes[0].plot(
                time_seconds,
                paired[f"{column}_{segment}"],
                linewidth=0.7,
                linestyle="-" if segment == "thigh" else "--",
                label=f"{segment} {column}",
            )
        for column in GYR_COLUMNS:
            axes[1].plot(
                time_seconds,
                paired[f"{column}_{segment}"],
                linewidth=0.7,
                linestyle="-" if segment == "thigh" else "--",
                label=f"{segment} {column}",
            )
        for column in ACC_COLUMNS:
            axes[2].plot(
                time_seconds,
                paired[f"{column}_filtered_{segment}"],
                linewidth=0.7,
                linestyle="-" if segment == "thigh" else "--",
                label=f"{segment} {column}",
            )
        for column in GYR_COLUMNS:
            axes[3].plot(
                time_seconds,
                paired[f"{column}_filtered_{segment}"],
                linewidth=0.7,
                linestyle="-" if segment == "thigh" else "--",
                label=f"{segment} {column}",
            )

    axes[0].set_ylabel("Raw Acc")
    axes[1].set_ylabel("Raw Gyr")
    axes[2].set_ylabel("Filtered Acc")
    axes[3].set_ylabel("Filtered Gyr")

    axes[4].plot(time_seconds, thigh_orientation, label="thigh orientation change", linewidth=1.0)
    axes[4].plot(time_seconds, shank_orientation, label="shank orientation change", linewidth=1.0)
    axes[4].set_ylabel("Segment orient. change (deg)")

    axes[5].plot(time_seconds, knee_angle, color="tab:red", linewidth=1.1, label="relative knee flexion estimate")
    if len(peaks):
        axes[5].plot(time_seconds[peaks], knee_angle[peaks], "ko", markersize=4, label="detected squat peak")
    axes[5].set_ylabel("Estimate (deg)")
    axes[5].set_xlabel("Relative time at observed 60 Hz spacing (s)")

    for axis in axes:
        axis.grid(True, linewidth=0.3, alpha=0.4)
        axis.legend(loc="upper right", fontsize=7, ncols=3)

    note = (
        "Convention: thigh-shank relative quaternion zeroed to first paired sample; "
        "signed angle is projection onto pair-specific dominant squat/relative-rotation axis. "
        "Not a clinically validated anatomical knee angle."
    )
    fig.text(0.01, 0.01, note, fontsize=8)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def analyze_pair(thigh_ref: RecordingRef, shank_ref: RecordingRef, output_root: Path) -> tuple[dict[str, object], pd.DataFrame]:
    paired = prepare_pair(thigh_ref, shank_ref)
    output_rate_hz = 60.0
    time_seconds = (paired["SampleTimeFine"].to_numpy(dtype=float) - float(paired["SampleTimeFine"].iloc[0])) / 16667.0 / output_rate_hz
    knee_angle, axis, rotvec, thigh_orientation, shank_orientation = knee_estimate_from_pair(paired)
    cycle_count, peaks = detect_cycles(thigh_ref.activity, knee_angle, output_rate_hz)
    pair_output = paired[["SampleTimeFine", "PacketCounter_thigh", "PacketCounter_shank"]].copy()
    pair_output["time_observed_60hz_s"] = time_seconds
    pair_output["thigh_orientation_change_deg"] = thigh_orientation
    pair_output["shank_orientation_change_deg"] = shank_orientation
    pair_output["knee_flexion_estimate_deg"] = knee_angle
    pair_output["relative_rotvec_x_deg"] = rotvec[:, 0]
    pair_output["relative_rotvec_y_deg"] = rotvec[:, 1]
    pair_output["relative_rotvec_z_deg"] = rotvec[:, 2]

    stem = f"{safe_name(thigh_ref.activity)}__{thigh_ref.side}"
    data_path = output_root / "data" / f"{stem}_orientation_knee_estimate.csv"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    pair_output.to_csv(data_path, index=False)

    plot_path = output_root / "plots" / f"{stem}_orientation_knee_estimate.png"
    plot_pair(
        paired,
        thigh_ref,
        shank_ref,
        time_seconds,
        thigh_orientation,
        shank_orientation,
        knee_angle,
        peaks,
        plot_path,
    )

    summary = {
        "activity": thigh_ref.activity,
        "side": thigh_ref.side,
        "thigh_file": thigh_ref.filename,
        "shank_file": shank_ref.filename,
        "paired_samples": len(paired),
        "axis_convention": "relative thigh-to-shank quaternion zeroed to first paired sample; angle is signed projection of relative rotation vector onto pair-specific dominant relative-rotation axis; positive direction chosen toward larger flexion-like excursion",
        "dominant_axis_x": float(axis[0]),
        "dominant_axis_y": float(axis[1]),
        "dominant_axis_z": float(axis[2]),
        "knee_angle_min_deg": float(np.nanmin(knee_angle)),
        "knee_angle_max_deg": float(np.nanmax(knee_angle)),
        "knee_angle_range_deg": float(np.nanmax(knee_angle) - np.nanmin(knee_angle)),
        "squat_cycle_count": cycle_count,
        "peak_times_s": ";".join(f"{time_seconds[index]:.3f}" for index in peaks),
        "thigh_orientation_range_deg": float(np.nanmax(thigh_orientation) - np.nanmin(thigh_orientation)),
        "shank_orientation_range_deg": float(np.nanmax(shank_orientation) - np.nanmin(shank_orientation)),
        "data_path": data_path.as_posix(),
        "plot_path": plot_path.as_posix(),
        "clinical_validity_note": "IMU-derived relative knee flexion estimate only; no sensor-to-segment anatomical calibration applied.",
    }
    return summary, pair_output


def run(raw_dir: Path, output_root: Path) -> pd.DataFrame:
    processed_by_file = ensure_processed_outputs(raw_dir)
    refs = recording_refs(raw_dir, processed_by_file)
    pairs = synchronized_pairs(refs, raw_dir)
    summaries = []
    pair_outputs: dict[tuple[str, str], pd.DataFrame] = {}
    for thigh_ref, shank_ref in pairs:
        summary, pair_output = analyze_pair(thigh_ref, shank_ref, output_root)
        summaries.append(summary)
        pair_outputs[(summary["activity"], summary["side"])] = pair_output

    summary_df = pd.DataFrame(summaries)
    for activity in sorted(summary_df["activity"].unique()) if not summary_df.empty else []:
        left = pair_outputs.get((activity, "L"))
        right = pair_outputs.get((activity, "R"))
        if left is not None and right is not None:
            comparison = timing_similarity(left, right)
            mask = summary_df["activity"] == activity
            for key, value in comparison.items():
                summary_df.loc[mask, key] = value

    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "orientation_joint_angle_summary.csv"
    summary_df.to_csv(summary_path, index=False)
    return summary_df


def print_summary(summary: pd.DataFrame, output_root: Path) -> None:
    print(f"Synchronized thigh-shank pairs analyzed: {len(summary)}")
    print(f"Output root: {output_root}")
    print()
    if summary.empty:
        return
    for row in summary.itertuples(index=False):
        print(
            f"{row.activity} {row.side}: range={row.knee_angle_range_deg:.2f} deg "
            f"(min={row.knee_angle_min_deg:.2f}, max={row.knee_angle_max_deg:.2f}), "
            f"squat_cycles={row.squat_cycle_count}, samples={row.paired_samples}"
        )
    squat = summary[summary["activity"] == "SQUAT"]
    if len(squat) == 2:
        corr = squat["left_right_corr"].dropna()
        lag = squat["left_right_peak_lag_samples"].dropna()
        if not corr.empty:
            print()
            print(f"SQUAT left/right correlation: {corr.iloc[0]:.3f}")
            print(f"SQUAT left/right cross-correlation lag: {int(lag.iloc[0])} samples")
    print()
    print("Convention: relative thigh-to-shank quaternion, zeroed to first paired sample; flexion estimate is projection onto the pair-specific dominant relative-rotation axis.")
    print("Clinical note: these are IMU-derived relative knee flexion estimates, not validated anatomical knee angles.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive segment orientation and IMU knee-angle estimates.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()

    summary = run(args.raw_dir, args.output_root)
    print_summary(summary, args.output_root)


if __name__ == "__main__":
    main()
