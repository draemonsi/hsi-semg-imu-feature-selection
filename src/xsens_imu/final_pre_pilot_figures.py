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


FIGURE_SIZE = (10, 5.5)
TALL_FIGURE_SIZE = (10, 7.0)
OUTPUT_DPI = 300
REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


def observed_time_seconds(df: pd.DataFrame) -> np.ndarray:
    return (df["SampleTimeFine"].to_numpy(dtype=float) - float(df["SampleTimeFine"].iloc[0])) / 16667.0 / 60.0


def style_axis(axis, ylabel: str, xlabel: str | None = None) -> None:
    axis.set_ylabel(ylabel)
    if xlabel:
        axis.set_xlabel(xlabel)
    axis.grid(True, linewidth=0.35, alpha=0.35)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def save_figure(fig: plt.Figure, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=OUTPUT_DPI, bbox_inches="tight")
    plt.close(fig)


def annotate_method_note(fig: plt.Figure) -> None:
    fig.text(
        0.01,
        0.01,
        "IMU-derived relative knee flexion estimate; flexion axis is data-derived and not anatomically calibrated.",
        fontsize=8,
    )


def plot_raw_acc(thigh: pd.DataFrame, shank: pd.DataFrame, output_dir: Path) -> Path:
    fig, axes = plt.subplots(2, 1, figsize=TALL_FIGURE_SIZE, sharex=True)
    for axis, data, title in [
        (axes[0], thigh, "Representative thigh sensor: DOT_Thigh_L"),
        (axes[1], shank, "Representative shank sensor: DOT_Shank_L"),
    ]:
        t = observed_time_seconds(data)
        for column in ["Acc_X", "Acc_Y", "Acc_Z"]:
            axis.plot(t, data[column], linewidth=1.0, label=column)
        axis.set_title(title)
        style_axis(axis, "Raw acceleration")
        axis.legend(loc="upper right", ncols=3, frameon=False)
    axes[1].set_xlabel("Time (s)")
    fig.suptitle("SQUAT raw accelerometer signals", fontsize=14)
    output_path = output_dir / "01_squat_raw_acc_representative_thigh_shank.png"
    save_figure(fig, output_path)
    return output_path


def plot_raw_vs_filtered_axis(
    thigh: pd.DataFrame,
    shank: pd.DataFrame,
    output_dir: Path,
    signal: str,
    ylabel: str,
    title: str,
    filename: str,
) -> Path:
    fig, axes = plt.subplots(2, 1, figsize=TALL_FIGURE_SIZE, sharex=True)
    for axis, data, sensor in [
        (axes[0], thigh, "DOT_Thigh_L"),
        (axes[1], shank, "DOT_Shank_L"),
    ]:
        t = observed_time_seconds(data)
        axis.plot(t, data[signal], color="0.65", linewidth=0.9, label=f"raw {signal}")
        axis.plot(t, data[f"{signal}_filtered"], color="tab:blue", linewidth=1.5, label=f"filtered {signal}")
        axis.set_title(sensor)
        style_axis(axis, ylabel)
        axis.legend(loc="upper right", frameon=False)
    axes[1].set_xlabel("Time (s)")
    fig.suptitle(title, fontsize=14)
    output_path = output_dir / filename
    save_figure(fig, output_path)
    return output_path


def plot_left_orientation(left: pd.DataFrame, output_dir: Path) -> Path:
    fig, axis = plt.subplots(figsize=FIGURE_SIZE)
    axis.plot(
        left["time_observed_60hz_s"],
        left["thigh_orientation_change_deg"],
        linewidth=1.8,
        label="Left thigh orientation change",
    )
    axis.plot(
        left["time_observed_60hz_s"],
        left["shank_orientation_change_deg"],
        linewidth=1.8,
        label="Left shank orientation change",
    )
    style_axis(axis, "Orientation change (deg)", "Time (s)")
    axis.set_title("SQUAT left segment-orientation curves")
    axis.legend(loc="upper right", frameon=False)
    output_path = output_dir / "04_squat_left_segment_orientation_curves.png"
    save_figure(fig, output_path)
    return output_path


def peak_times_from_summary(summary: pd.DataFrame, side: str) -> list[float]:
    value = summary.loc[(summary["activity"] == "SQUAT") & (summary["side"] == side), "peak_times_s"].iloc[0]
    if not isinstance(value, str) or not value:
        return []
    return [float(item) for item in value.split(";") if item]


def plot_knee_with_peaks(data: pd.DataFrame, summary: pd.DataFrame, side: str, output_dir: Path) -> Path:
    fig, axis = plt.subplots(figsize=FIGURE_SIZE)
    t = data["time_observed_60hz_s"].to_numpy(dtype=float)
    angle = data["knee_flexion_estimate_deg"].to_numpy(dtype=float)
    axis.plot(t, angle, color="tab:red", linewidth=2.0, label=f"{side} estimate")
    peak_times = peak_times_from_summary(summary, side)
    if peak_times:
        peak_values = np.interp(peak_times, t, angle)
        axis.scatter(peak_times, peak_values, color="black", s=28, zorder=3, label="Detected squat peaks")
    style_axis(axis, "IMU-derived relative knee flexion estimate (deg)", "Time (s)")
    axis.set_title(f"SQUAT {side} knee flexion estimate")
    axis.legend(loc="upper right", frameon=False)
    annotate_method_note(fig)
    output_path = output_dir / f"0{'5' if side == 'L' else '6'}_squat_{'left' if side == 'L' else 'right'}_knee_flexion_estimate.png"
    save_figure(fig, output_path)
    return output_path


def plot_left_right_overlay(left: pd.DataFrame, right: pd.DataFrame, output_dir: Path) -> Path:
    fig, axis = plt.subplots(figsize=FIGURE_SIZE)
    axis.plot(
        left["time_observed_60hz_s"],
        left["knee_flexion_estimate_deg"],
        linewidth=2.0,
        label="Left",
    )
    axis.plot(
        right["time_observed_60hz_s"],
        right["knee_flexion_estimate_deg"],
        linewidth=2.0,
        label="Right",
    )
    style_axis(axis, "IMU-derived relative knee flexion estimate (deg)", "Time (s)")
    axis.set_title("SQUAT left vs right knee flexion estimates")
    axis.legend(loc="upper right", frameon=False)
    annotate_method_note(fig)
    output_path = output_dir / "07_squat_left_vs_right_knee_flexion_overlay.png"
    save_figure(fig, output_path)
    return output_path


def make_summary_table(summary: pd.DataFrame) -> Path:
    squat = summary[summary["activity"] == "SQUAT"].copy()
    table = squat[
        [
            "side",
            "knee_angle_min_deg",
            "knee_angle_max_deg",
            "knee_angle_range_deg",
            "squat_cycle_count",
            "peak_times_s",
            "left_right_corr",
            "left_right_peak_lag_samples",
        ]
    ].copy()
    table.insert(0, "angle_label", "IMU-derived relative knee flexion estimate")
    table["axis_note"] = "Flexion axis is data-derived and not anatomically calibrated."
    output_path = RESULTS_ROOT / "manifests" / "squat_knee_summary_table.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(output_path, index=False)
    return output_path


def run(output_dir: Path) -> list[Path]:
    processed_root = RESULTS_ROOT / "processed" / "processed_csv" / "SQUAT"
    orientation_root = RESULTS_ROOT / "orientation" / "orientation_and_joint_angles"

    thigh_left = pd.read_csv(processed_root / "DOT_Thigh_L" / "SQUAT__DOT_Thigh_L__THIGH_L_SQUAT_processed.csv")
    shank_left = pd.read_csv(processed_root / "DOT_Shank_L" / "SQUAT__DOT_Shank_L__SHANK_L_SQUAT_processed.csv")
    left = pd.read_csv(orientation_root / "data" / "SQUAT__L_orientation_knee_estimate.csv")
    right = pd.read_csv(orientation_root / "data" / "SQUAT__R_orientation_knee_estimate.csv")
    summary = pd.read_csv(orientation_root / "orientation_joint_angle_summary.csv")

    outputs = [
        plot_raw_acc(thigh_left, shank_left, output_dir),
        plot_raw_vs_filtered_axis(
            thigh_left,
            shank_left,
            output_dir,
            "Acc_Y",
            "Acceleration",
            "SQUAT raw vs filtered accelerometer signal",
            "02_squat_raw_vs_filtered_acc_y.png",
        ),
        plot_raw_vs_filtered_axis(
            thigh_left,
            shank_left,
            output_dir,
            "Gyr_Z",
            "Gyroscope",
            "SQUAT raw vs filtered gyroscope signal",
            "03_squat_raw_vs_filtered_gyr_z.png",
        ),
        plot_left_orientation(left, output_dir),
        plot_knee_with_peaks(left, summary, "L", output_dir),
        plot_knee_with_peaks(right, summary, "R", output_dir),
        plot_left_right_overlay(left, right, output_dir),
        make_summary_table(summary),
    ]
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Export final pre-pilot presentation figures.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RESULTS_ROOT / "figures" / "final_pre_pilot_figures",
    )
    args = parser.parse_args()
    outputs = run(args.output_dir)
    print(f"Final pre-pilot outputs written for figure dir: {args.output_dir}")
    for output in outputs:
        print(f"- {output}")


if __name__ == "__main__":
    main()
