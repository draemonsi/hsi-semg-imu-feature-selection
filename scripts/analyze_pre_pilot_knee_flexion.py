from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("tmp/matplotlib").resolve()))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import correlate, correlation_lags, find_peaks, savgol_filter
from scipy.spatial.transform import Rotation


FS = 60.0
RAW_ROOT = Path("data/raw/xsens_imu/pre_pilot")
OUT_ROOT = Path("results/xsens_imu/pre_pilot/orientation")
FIG_ROOT = Path("results/xsens_imu/pre_pilot/figures/knee_angle")
SUMMARY_PATH = OUT_ROOT / "pre_pilot_knee_angle_summary.csv"

PARTICIPANTS = ["P01", "P02", "P03"]
ACTIVITIES = ["squat", "high_knee_hops", "jogging"]
LEGS = {
    "left": ("thigh_l", "shank_l"),
    "right": ("thigh_r", "shank_r"),
}
ACTIVITY_LABELS = {
    "squat": "Squat",
    "high_knee_hops": "High Knee Hops",
    "jogging": "Jogging",
}


@dataclass
class KneeResult:
    participant: str
    activity: str
    leg: str
    sensor_pair: str
    status: str
    data: pd.DataFrame | None
    min_angle: float | None = None
    max_angle: float | None = None
    angle_range: float | None = None
    cycle_count: int | None = None
    mean_cycle_duration: float | None = None
    peak_times: list[float] | None = None
    notes: str = ""


def read_xsens_csv(path: Path) -> pd.DataFrame:
    header_line = None
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader):
            if not row or all(not cell.strip() for cell in row):
                continue
            if "PacketCounter" in row:
                header_line = line_no
                break
    if header_line is None:
        raise ValueError(f"No PacketCounter header found in {path}")
    df = pd.read_csv(path, skiprows=header_line, index_col=False)
    df["SampleTimeFine"] = df["SampleTimeFine"].astype(np.int64)
    return df


def sensor_path(activity: str, participant: str, sensor: str) -> Path:
    return RAW_ROOT / activity / participant / f"{sensor}.csv"


def normalize_quat_wxyz(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, dtype=float)
    norms = np.linalg.norm(q, axis=1, keepdims=True)
    norms[norms == 0] = np.nan
    out = q / norms
    return out


def rotation_from_wxyz(df: pd.DataFrame, prefix: str) -> Rotation:
    q_wxyz = df[[f"Quat_W_{prefix}", f"Quat_X_{prefix}", f"Quat_Y_{prefix}", f"Quat_Z_{prefix}"]].to_numpy(float)
    q_wxyz = normalize_quat_wxyz(q_wxyz)
    valid = np.all(np.isfinite(q_wxyz), axis=1)
    if not np.all(valid):
        raise ValueError("Non-finite quaternion after normalization")
    q_xyzw = q_wxyz[:, [1, 2, 3, 0]]
    return Rotation.from_quat(q_xyzw)


def align_pair(thigh: pd.DataFrame, shank: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "SampleTimeFine",
        "Acc_X",
        "Acc_Y",
        "Acc_Z",
        "Gyr_X",
        "Gyr_Y",
        "Gyr_Z",
        "Quat_W",
        "Quat_X",
        "Quat_Y",
        "Quat_Z",
    ]
    paired = pd.merge(
        thigh[cols],
        shank[cols],
        on="SampleTimeFine",
        how="inner",
        suffixes=("_thigh", "_shank"),
    )
    paired = paired.sort_values("SampleTimeFine").reset_index(drop=True)
    paired["time_s"] = (paired["SampleTimeFine"] - paired["SampleTimeFine"].iloc[0]) / 1_000_000.0
    return paired


def baseline_indices(paired: pd.DataFrame) -> np.ndarray:
    n = len(paired)
    win = max(5, min(int(round(0.5 * FS)), n))
    search_n = min(max(win, int(round(2.0 * FS))), n)
    gyr_cols = [
        "Gyr_X_thigh",
        "Gyr_Y_thigh",
        "Gyr_Z_thigh",
        "Gyr_X_shank",
        "Gyr_Y_shank",
        "Gyr_Z_shank",
    ]
    gyr_mag = np.linalg.norm(paired[gyr_cols].to_numpy(float), axis=1)
    best_start = 0
    best_score = np.inf
    for start in range(0, search_n - win + 1):
        score = float(np.median(gyr_mag[start : start + win]))
        if score < best_score:
            best_score = score
            best_start = start
    return np.arange(best_start, best_start + win)


def dominant_axis(rotvec_deg: np.ndarray) -> np.ndarray:
    centered = rotvec_deg - np.median(rotvec_deg, axis=0)
    _, _, vh = np.linalg.svd(centered, full_matrices=False)
    axis = vh[0]
    axis = axis / np.linalg.norm(axis)
    projection = centered @ axis
    if abs(np.nanmin(projection)) > abs(np.nanmax(projection)):
        axis = -axis
    return axis


def smooth_for_peaks(angle: np.ndarray) -> np.ndarray:
    n = len(angle)
    if n < 9:
        return angle
    win = min(31, n if n % 2 == 1 else n - 1)
    if win < 9:
        win = 9 if n >= 9 else (n if n % 2 == 1 else n - 1)
    return savgol_filter(angle, window_length=win, polyorder=3)


def peak_params(activity: str, angle: np.ndarray) -> tuple[int, float, float]:
    dynamic_range = float(np.nanmax(angle) - np.nanmin(angle))
    prominence = max(dynamic_range * 0.18, np.nanstd(angle) * 0.75, 1e-6)
    if activity == "jogging":
        distance = int(round(0.25 * FS))
    elif activity == "high_knee_hops":
        distance = int(round(0.25 * FS))
    else:
        distance = int(round(0.45 * FS))
    return distance, prominence, dynamic_range


def compute_knee(participant: str, activity: str, leg: str) -> KneeResult:
    thigh_sensor, shank_sensor = LEGS[leg]
    thigh_path = sensor_path(activity, participant, thigh_sensor)
    shank_path = sensor_path(activity, participant, shank_sensor)
    pair_name = f"{thigh_sensor}+{shank_sensor}"
    if not thigh_path.exists() or not shank_path.exists():
        missing = []
        if not thigh_path.exists():
            missing.append(thigh_sensor)
        if not shank_path.exists():
            missing.append(shank_sensor)
        return KneeResult(
            participant,
            activity,
            leg,
            pair_name,
            "NOT COMPUTABLE — REQUIRED SENSOR PAIR MISSING",
            None,
            notes=f"Missing required sensor(s): {', '.join(missing)}",
        )

    thigh = read_xsens_csv(thigh_path)
    shank = read_xsens_csv(shank_path)
    paired = align_pair(thigh, shank)
    if len(paired) < 10:
        return KneeResult(
            participant,
            activity,
            leg,
            pair_name,
            "NOT COMPUTABLE — REQUIRED SENSOR PAIR MISSING",
            None,
            notes=f"Insufficient aligned samples after SampleTimeFine merge: {len(paired)}",
        )

    r_thigh = rotation_from_wxyz(paired, "thigh")
    r_shank = rotation_from_wxyz(paired, "shank")
    r_rel = r_thigh.inv() * r_shank

    base_idx = baseline_indices(paired)
    r_base = r_rel[base_idx].mean()
    r_delta = r_base.inv() * r_rel
    rotvec_deg = np.rad2deg(r_delta.as_rotvec())
    axis = dominant_axis(rotvec_deg)
    angle = rotvec_deg @ axis
    angle = angle - np.median(angle[base_idx])

    filtered_angle = smooth_for_peaks(angle)
    distance, prominence, dynamic_range = peak_params(activity, filtered_angle)
    peaks, props = find_peaks(filtered_angle, distance=distance, prominence=prominence)
    peak_times = paired["time_s"].to_numpy(float)[peaks].tolist()
    mean_cycle_duration = float(np.mean(np.diff(peak_times))) if len(peak_times) >= 2 else np.nan

    out = pd.DataFrame(
        {
            "time_s": paired["time_s"],
            "SampleTimeFine": paired["SampleTimeFine"],
            "knee_flexion_estimate_deg": angle,
            "knee_flexion_estimate_smooth_deg": filtered_angle,
            "relative_rotvec_x_deg": rotvec_deg[:, 0],
            "relative_rotvec_y_deg": rotvec_deg[:, 1],
            "relative_rotvec_z_deg": rotvec_deg[:, 2],
            "dominant_axis_x": axis[0],
            "dominant_axis_y": axis[1],
            "dominant_axis_z": axis[2],
            "is_peak": False,
        }
    )
    out.loc[peaks, "is_peak"] = True

    notes = (
        "IMU-derived relative knee flexion estimate; baseline is lowest-motion 0.5 s paired window "
        "within first 2 s using combined thigh+shank gyro magnitude; dominant axis from PCA of "
        "activity-local relative rotation vectors."
    )
    return KneeResult(
        participant=participant,
        activity=activity,
        leg=leg,
        sensor_pair=pair_name,
        status="COMPUTED",
        data=out,
        min_angle=float(np.nanmin(angle)),
        max_angle=float(np.nanmax(angle)),
        angle_range=float(dynamic_range),
        cycle_count=int(len(peaks)),
        mean_cycle_duration=mean_cycle_duration,
        peak_times=peak_times,
        notes=notes,
    )


def save_knee_plot(result: KneeResult) -> None:
    if result.data is None:
        return
    out_dir = OUT_ROOT / result.activity / result.participant / result.leg
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "knee_flexion_estimate.csv"
    result.data.to_csv(csv_path, index=False)

    t = result.data["time_s"].to_numpy(float)
    y = result.data["knee_flexion_estimate_deg"].to_numpy(float)
    ys = result.data["knee_flexion_estimate_smooth_deg"].to_numpy(float)
    peak_mask = result.data["is_peak"].to_numpy(bool)

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(t, y, color="0.72", lw=0.9, label="raw estimate")
    ax.plot(t, ys, color="#1f77b4", lw=1.5, label="smoothed for peak detection")
    ax.scatter(t[peak_mask], ys[peak_mask], color="#d62728", s=28, zorder=3, label="detected peaks")
    ax.axhline(0, color="0.2", lw=0.8, alpha=0.5)
    ax.set_title(
        f"{result.participant} {ACTIVITY_LABELS[result.activity]} {result.leg}: "
        "IMU-derived relative knee flexion estimate"
    )
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Relative flexion estimate (deg)")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "knee_flexion_estimate.png", dpi=180)
    plt.close(fig)


def bilateral_stats(results: dict[tuple[str, str, str], KneeResult]) -> dict[tuple[str, str], dict[str, float]]:
    stats: dict[tuple[str, str], dict[str, float]] = {}
    for participant in PARTICIPANTS:
        for activity in ACTIVITIES:
            left = results[(participant, activity, "left")]
            right = results[(participant, activity, "right")]
            if left.data is None or right.data is None:
                continue
            l = left.data[["SampleTimeFine", "knee_flexion_estimate_smooth_deg"]].rename(
                columns={"knee_flexion_estimate_smooth_deg": "left"}
            )
            r = right.data[["SampleTimeFine", "knee_flexion_estimate_smooth_deg"]].rename(
                columns={"knee_flexion_estimate_smooth_deg": "right"}
            )
            paired = pd.merge(l, r, on="SampleTimeFine", how="inner")
            if len(paired) < 10:
                continue
            x = paired["left"].to_numpy(float) - paired["left"].mean()
            y = paired["right"].to_numpy(float) - paired["right"].mean()
            corr = float(np.corrcoef(x, y)[0, 1]) if np.std(x) > 0 and np.std(y) > 0 else np.nan
            xc = correlate(x, y, mode="full")
            lags = correlation_lags(len(x), len(y), mode="full")
            lag = int(lags[int(np.argmax(xc))])
            stats[(participant, activity)] = {"corr": corr, "lag": lag}
    return stats


def save_bilateral_overlays(results: dict[tuple[str, str, str], KneeResult]) -> None:
    for participant in PARTICIPANTS:
        for activity in ACTIVITIES:
            left = results[(participant, activity, "left")]
            right = results[(participant, activity, "right")]
            if left.data is None and right.data is None:
                continue
            out_dir = OUT_ROOT / activity / participant
            out_dir.mkdir(parents=True, exist_ok=True)
            fig, ax = plt.subplots(figsize=(12, 5.5))
            for res, color in [(left, "#1f77b4"), (right, "#d62728")]:
                if res.data is None:
                    continue
                ax.plot(
                    res.data["time_s"],
                    res.data["knee_flexion_estimate_smooth_deg"],
                    lw=1.5,
                    color=color,
                    label=res.leg,
                )
            ax.axhline(0, color="0.2", lw=0.8, alpha=0.5)
            ax.set_title(f"{participant} {ACTIVITY_LABELS[activity]} bilateral knee flexion estimate")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel("Relative flexion estimate (deg)")
            ax.grid(True, alpha=0.25)
            ax.legend()
            fig.tight_layout()
            fig.savefig(out_dir / "bilateral_knee_flexion_overlay.png", dpi=180)
            plt.close(fig)


def write_summary(results: dict[tuple[str, str, str], KneeResult], bilateral: dict[tuple[str, str], dict[str, float]]) -> pd.DataFrame:
    rows = []
    for participant in PARTICIPANTS:
        for activity in ACTIVITIES:
            b = bilateral.get((participant, activity), {})
            for leg in ["left", "right"]:
                res = results[(participant, activity, leg)]
                rows.append(
                    {
                        "participant": participant,
                        "activity": activity,
                        "leg": leg,
                        "sensor_pair": res.sensor_pair,
                        "analysis_status": res.status,
                        "min_angle": res.min_angle if res.min_angle is not None else "",
                        "max_angle": res.max_angle if res.max_angle is not None else "",
                        "range": res.angle_range if res.angle_range is not None else "",
                        "cycle_count": res.cycle_count if res.cycle_count is not None else "",
                        "mean_cycle_duration": res.mean_cycle_duration if res.mean_cycle_duration is not None else "",
                        "peak_times": ";".join(f"{x:.3f}" for x in (res.peak_times or [])),
                        "bilateral_correlation": b.get("corr", ""),
                        "bilateral_lag_samples": b.get("lag", ""),
                        "notes": res.notes,
                    }
                )
    df = pd.DataFrame(rows)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    df.to_csv(SUMMARY_PATH, index=False)
    return df


def presentation_figures(results: dict[tuple[str, str, str], KneeResult], summary: pd.DataFrame) -> None:
    FIG_ROOT.mkdir(parents=True, exist_ok=True)

    def overlay(participant: str, activity: str, outfile: str) -> None:
        fig, ax = plt.subplots(figsize=(12, 5.5))
        plotted = False
        for leg, color in [("left", "#1f77b4"), ("right", "#d62728")]:
            res = results[(participant, activity, leg)]
            if res.data is None:
                continue
            ax.plot(
                res.data["time_s"],
                res.data["knee_flexion_estimate_smooth_deg"],
                lw=1.6,
                label=f"{leg} ({res.cycle_count} peaks)",
                color=color,
            )
            plotted = True
        if not plotted:
            plt.close(fig)
            return
        ax.axhline(0, color="0.2", lw=0.8, alpha=0.5)
        ax.set_title(f"{participant} {ACTIVITY_LABELS[activity]} left/right knee flexion estimate")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Relative flexion estimate (deg)")
        ax.grid(True, alpha=0.25)
        ax.legend()
        fig.tight_layout()
        fig.savefig(FIG_ROOT / outfile, dpi=200)
        plt.close(fig)

    overlay("P01", "squat", "01_P01_squat_left_right_overlay.png")
    overlay("P02", "squat", "02_P02_squat_left_right_overlay.png")

    for key, outfile, title in [
        (("P02", "high_knee_hops", "right"), "03_representative_high_knee_hops.png", "Representative high-knee hops"),
        (("P02", "jogging", "right"), "04_representative_P02_jogging.png", "Representative P02 jogging"),
    ]:
        res = results[key]
        if res.data is None:
            continue
        fig, ax = plt.subplots(figsize=(12, 5.5))
        peak_mask = res.data["is_peak"].to_numpy(bool)
        ax.plot(res.data["time_s"], res.data["knee_flexion_estimate_deg"], color="0.75", lw=0.8, label="raw estimate")
        ax.plot(res.data["time_s"], res.data["knee_flexion_estimate_smooth_deg"], lw=1.5, label="smoothed")
        ax.scatter(
            res.data.loc[peak_mask, "time_s"],
            res.data.loc[peak_mask, "knee_flexion_estimate_smooth_deg"],
            color="#d62728",
            s=25,
            label="peaks",
        )
        ax.set_title(f"{title}: {res.participant} {res.leg}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Relative flexion estimate (deg)")
        ax.grid(True, alpha=0.25)
        ax.legend()
        fig.tight_layout()
        fig.savefig(FIG_ROOT / outfile, dpi=200)
        plt.close(fig)

    squat = summary[(summary["activity"] == "squat") & (summary["analysis_status"] == "COMPUTED")].copy()
    if not squat.empty:
        squat["label"] = squat["participant"] + " " + squat["leg"]
        x = np.arange(len(squat))
        fig, ax1 = plt.subplots(figsize=(12, 5.8))
        ax1.bar(x - 0.18, squat["range"].astype(float), width=0.36, label="ROM estimate", color="#1f77b4")
        ax2 = ax1.twinx()
        ax2.bar(x + 0.18, squat["cycle_count"].astype(float), width=0.36, label="cycle count", color="#ff7f0e")
        ax1.set_xticks(x)
        ax1.set_xticklabels(squat["label"], rotation=30, ha="right")
        ax1.set_ylabel("Relative flexion range estimate (deg)")
        ax2.set_ylabel("Detected peak count")
        ax1.set_title("Squat participant comparison: ROM estimate and cycle count")
        ax1.grid(True, axis="y", alpha=0.25)
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")
        fig.tight_layout()
        fig.savefig(FIG_ROOT / "05_squat_rom_cycle_count_comparison.png", dpi=200)
        plt.close(fig)


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    FIG_ROOT.mkdir(parents=True, exist_ok=True)

    results: dict[tuple[str, str, str], KneeResult] = {}
    for participant in PARTICIPANTS:
        for activity in ACTIVITIES:
            for leg in ["left", "right"]:
                res = compute_knee(participant, activity, leg)
                results[(participant, activity, leg)] = res
                save_knee_plot(res)

    save_bilateral_overlays(results)
    bilateral = bilateral_stats(results)
    summary = write_summary(results, bilateral)
    presentation_figures(results, summary)

    print(f"computed={sum(1 for r in results.values() if r.status == 'COMPUTED')}")
    print(f"not_computable={sum(1 for r in results.values() if r.status != 'COMPUTED')}")
    print(f"processed_csvs={len(list(OUT_ROOT.rglob('knee_flexion_estimate.csv')))}")
    print(f"knee_plots={len(list(OUT_ROOT.rglob('knee_flexion_estimate.png')))}")
    print(f"bilateral_overlays={len(list(OUT_ROOT.rglob('bilateral_knee_flexion_overlay.png')))}")
    print(f"presentation_figures={len(list(FIG_ROOT.glob('*.png')))}")


if __name__ == "__main__":
    main()
