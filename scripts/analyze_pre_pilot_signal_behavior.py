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
from scipy.signal import butter, find_peaks, periodogram, sosfiltfilt


FS = 60.0
CUTOFF = 10.0
RAW_ROOT = Path("data/raw/xsens_imu/pre_pilot")
OUT_ROOT = Path("results/xsens_imu/pre_pilot/signal_behavior")
FIG_ROOT = Path("results/xsens_imu/pre_pilot/figures/signal_behavior")

ACC_COLS = ["Acc_X", "Acc_Y", "Acc_Z"]
GYR_COLS = ["Gyr_X", "Gyr_Y", "Gyr_Z"]
SENSORS = ["pelvis", "thigh_l", "thigh_r", "shank_l", "shank_r"]
ACTIVITY_LABELS = {
    "standing": "Standing",
    "squat": "Squat",
    "high_knee_hops": "High Knee Hops",
    "jogging": "Jogging",
}


@dataclass
class Recording:
    activity: str
    participant: str
    sensor: str
    path: Path
    metadata: dict[str, str]
    data: pd.DataFrame

    @property
    def n(self) -> int:
        return len(self.data)

    @property
    def duration_s(self) -> float:
        return self.n / FS


def read_xsens_csv(path: Path) -> tuple[dict[str, str], pd.DataFrame]:
    metadata: dict[str, str] = {}
    header_line = None
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader):
            if not row or all(not cell.strip() for cell in row):
                continue
            if "PacketCounter" in row:
                header_line = line_no
                break
            if len(row) >= 2:
                metadata[row[0].strip().rstrip(":")] = ",".join(row[1:]).strip()
    if header_line is None:
        raise ValueError(f"No data header found in {path}")
    data = pd.read_csv(path, skiprows=header_line, index_col=False)
    return metadata, data


def load_recordings() -> list[Recording]:
    recordings: list[Recording] = []
    for path in sorted(RAW_ROOT.rglob("*.csv")):
        rel = path.relative_to(RAW_ROOT)
        if len(rel.parts) != 3:
            continue
        activity, participant, filename = rel.parts
        sensor = Path(filename).stem
        metadata, data = read_xsens_csv(path)
        if len(data) == 0:
            continue
        recordings.append(Recording(activity, participant, sensor, path, metadata, data))
    return recordings


def time_axis(n: int) -> np.ndarray:
    return np.arange(n) / FS


def magnitudes(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    acc_mag = np.linalg.norm(df[ACC_COLS].to_numpy(float), axis=1)
    gyr_mag = np.linalg.norm(df[GYR_COLS].to_numpy(float), axis=1)
    return acc_mag, gyr_mag


def filtered_frame(df: pd.DataFrame) -> pd.DataFrame:
    sos = butter(4, CUTOFF, btype="lowpass", fs=FS, output="sos")
    out = df.copy()
    for col in ACC_COLS + GYR_COLS:
        out[col] = sosfiltfilt(sos, df[col].to_numpy(float))
    return out


def safe_name(rec: Recording) -> str:
    return f"{rec.participant}_{rec.sensor}"


def plot_raw_recording(rec: Recording) -> None:
    df = rec.data
    t = time_axis(len(df))
    acc_mag, gyr_mag = magnitudes(df)
    out_dir = OUT_ROOT / "raw" / rec.activity / rec.participant / rec.sensor
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(4, 2, figsize=(14, 10), sharex=True)
    axes = axes.ravel()
    series = [
        ("Acc_X", df["Acc_X"]),
        ("Acc_Y", df["Acc_Y"]),
        ("Acc_Z", df["Acc_Z"]),
        ("Acc magnitude", acc_mag),
        ("Gyr_X", df["Gyr_X"]),
        ("Gyr_Y", df["Gyr_Y"]),
        ("Gyr_Z", df["Gyr_Z"]),
        ("Gyr magnitude", gyr_mag),
    ]
    for ax, (label, values) in zip(axes, series):
        ax.plot(t, values, lw=0.9)
        ax.set_title(label)
        ax.grid(True, alpha=0.25)
    axes[-1].set_xlabel("Time (s)")
    axes[-2].set_xlabel("Time (s)")
    fig.suptitle(f"Raw signals: {ACTIVITY_LABELS[rec.activity]} {rec.participant} {rec.sensor}")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_dir / "raw_time_domain.png", dpi=160)
    plt.close(fig)

    if rec.activity == "standing":
        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        for col in ACC_COLS:
            axes[0].plot(t, df[col], lw=0.9, label=col)
        for col in GYR_COLS:
            axes[1].plot(t, df[col], lw=0.9, label=col)
        axes[0].set_title("Standing accelerometer detail")
        axes[1].set_title("Standing gyroscope detail")
        for ax in axes:
            ax.grid(True, alpha=0.25)
            ax.legend(ncol=3, fontsize=8)
        axes[-1].set_xlabel("Time (s)")
        fig.suptitle(f"Standing stability view: {rec.participant} {rec.sensor}")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(out_dir / "standing_stability_detail.png", dpi=180)
        plt.close(fig)


def plot_raw_vs_filtered(rec: Recording) -> None:
    raw = rec.data
    filt = filtered_frame(raw)
    t = time_axis(len(raw))
    out_dir = OUT_ROOT / "raw_vs_filtered" / rec.activity / rec.participant / rec.sensor
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(3, 2, figsize=(14, 9), sharex=True)
    axes = axes.ravel()
    for ax, col in zip(axes, ACC_COLS + GYR_COLS):
        ax.plot(t, raw[col], color="0.72", lw=0.75, label="raw")
        ax.plot(t, filt[col], color="#1f77b4", lw=1.2, label="filtered")
        ax.set_title(col)
        ax.grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    axes[-2].set_xlabel("Time (s)")
    fig.suptitle(
        f"Raw vs filtered, 4th-order Butterworth 10 Hz: "
        f"{ACTIVITY_LABELS[rec.activity]} {rec.participant} {rec.sensor}"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out_dir / "raw_vs_filtered_axes.png", dpi=160)
    plt.close(fig)


def linear_drift(values: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    t = time_axis(len(values))
    slope, intercept = np.polyfit(t, values, 1)
    return float((slope * t[-1] + intercept) - intercept)


def rms(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(values))))


def robust_z_max(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    med = np.median(values)
    mad = np.median(np.abs(values - med))
    if mad == 0:
        return 0.0
    return float(np.max(np.abs(values - med) / (1.4826 * mad)))


def isolated_jump_score(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    if len(values) < 3:
        return 0.0
    diffs = np.abs(np.diff(values))
    signal_range = np.ptp(values)
    if signal_range == 0:
        return 0.0
    return float(np.max(diffs) / signal_range)


def standing_metrics(recordings: list[Recording]) -> pd.DataFrame:
    rows = []
    standing = [r for r in recordings if r.activity == "standing"]
    for rec in standing:
        df = rec.data
        acc_mag, gyr_mag = magnitudes(df)
        row = {
            "participant": rec.participant,
            "sensor": rec.sensor,
            "acc_x_std": df["Acc_X"].std(ddof=0),
            "acc_y_std": df["Acc_Y"].std(ddof=0),
            "acc_z_std": df["Acc_Z"].std(ddof=0),
            "acc_mag_std": np.std(acc_mag),
            "gyr_x_std": df["Gyr_X"].std(ddof=0),
            "gyr_y_std": df["Gyr_Y"].std(ddof=0),
            "gyr_z_std": df["Gyr_Z"].std(ddof=0),
            "gyr_mag_std": np.std(gyr_mag),
            "acc_peak_to_peak": np.ptp(acc_mag),
            "gyr_peak_to_peak": np.ptp(gyr_mag),
            "acc_drift": linear_drift(acc_mag),
            "gyr_drift": linear_drift(gyr_mag),
        }
        row["_spike_score"] = max(isolated_jump_score(df[c].to_numpy()) for c in ACC_COLS + GYR_COLS)
        rows.append(row)

    summary = pd.DataFrame(rows)
    if summary.empty:
        return summary

    cols = ["acc_mag_std", "gyr_mag_std", "acc_peak_to_peak", "gyr_peak_to_peak"]
    med = summary[cols].median()
    mad = (summary[cols] - med).abs().median().replace(0, np.nan)
    rel = ((summary[cols] - med) / (1.4826 * mad)).fillna(0.0)
    summary["_variation_score"] = rel.clip(lower=0).sum(axis=1)
    drift_ratio = (
        summary["acc_drift"].abs() / summary["acc_peak_to_peak"].replace(0, np.nan)
        + summary["gyr_drift"].abs() / summary["gyr_peak_to_peak"].replace(0, np.nan)
    ).fillna(0)
    summary["_drift_ratio"] = drift_ratio

    statuses = []
    for _, row in summary.iterrows():
        if row["_spike_score"] >= 0.75 and row["_variation_score"] >= 2.5:
            statuses.append("SUSPICIOUS_SPIKES")
        elif row["_variation_score"] >= 6 or row["_drift_ratio"] >= 0.70:
            statuses.append("POSSIBLE_MOVEMENT")
        elif row["_variation_score"] >= 2.5 or row["_drift_ratio"] >= 0.35:
            statuses.append("MINOR_VARIATION")
        else:
            statuses.append("STABLE")
    summary["status"] = statuses
    public_cols = [
        "participant",
        "sensor",
        "acc_x_std",
        "acc_y_std",
        "acc_z_std",
        "acc_mag_std",
        "gyr_x_std",
        "gyr_y_std",
        "gyr_z_std",
        "gyr_mag_std",
        "acc_peak_to_peak",
        "gyr_peak_to_peak",
        "acc_drift",
        "gyr_drift",
        "status",
    ]
    return summary[public_cols + ["_spike_score", "_variation_score", "_drift_ratio"]]


def dynamic_summary(recordings: list[Recording]) -> pd.DataFrame:
    rows = []
    for rec in recordings:
        df = rec.data
        filt = filtered_frame(df)
        acc_mag, gyr_mag = magnitudes(filt)
        freqs, power = periodogram(acc_mag - np.mean(acc_mag), fs=FS)
        valid = (freqs >= 0.2) & (freqs <= 8.0)
        dominant_freq = float(freqs[valid][np.argmax(power[valid])]) if np.any(valid) else np.nan
        prominence = max(np.std(acc_mag) * 0.75, 1e-12)
        peaks, _ = find_peaks(acc_mag, prominence=prominence)
        acc_p2p = {col: float(np.ptp(filt[col])) for col in ACC_COLS}
        gyr_p2p = {col: float(np.ptp(filt[col])) for col in GYR_COLS}
        raw_extreme_repeats = 0
        for col in ACC_COLS + GYR_COLS:
            values = df[col].to_numpy()
            raw_extreme_repeats = max(
                raw_extreme_repeats,
                int(np.sum(values == values.max())),
                int(np.sum(values == values.min())),
            )
        rows.append(
            {
                "participant": rec.participant,
                "activity": rec.activity,
                "sensor": rec.sensor,
                "row_count": rec.n,
                "duration_s": rec.duration_s,
                "acc_dominant_axis": max(acc_p2p, key=acc_p2p.get),
                "gyr_dominant_axis": max(gyr_p2p, key=gyr_p2p.get),
                "acc_max_peak_to_peak": max(acc_p2p.values()),
                "gyr_max_peak_to_peak": max(gyr_p2p.values()),
                "acc_mag_peak_count": int(len(peaks)),
                "acc_mag_dominant_frequency_hz": dominant_freq,
                "possible_clipping": raw_extreme_repeats >= 5,
            }
        )
    return pd.DataFrame(rows)


def plot_cross_sensor(recordings: list[Recording]) -> None:
    groups: dict[tuple[str, str], list[Recording]] = {}
    for rec in recordings:
        groups.setdefault((rec.activity, rec.participant), []).append(rec)

    for (activity, participant), recs in sorted(groups.items()):
        if len(recs) < 2:
            continue
        recs = sorted(recs, key=lambda r: SENSORS.index(r.sensor) if r.sensor in SENSORS else 999)
        min_time = min(float(r.data["SampleTimeFine"].iloc[0]) for r in recs)
        out_dir = OUT_ROOT / "cross_sensor" / activity / participant
        out_dir.mkdir(parents=True, exist_ok=True)
        for kind in ["acc", "gyr"]:
            fig, ax = plt.subplots(figsize=(13, 6))
            for rec in recs:
                df = rec.data
                t = (df["SampleTimeFine"].to_numpy(float) - min_time) / 1_000_000.0
                acc_mag, gyr_mag = magnitudes(df)
                values = acc_mag if kind == "acc" else gyr_mag
                ax.plot(t, values, lw=1.0, label=rec.sensor)
            ax.set_title(f"{ACTIVITY_LABELS[activity]} {participant}: {kind.upper()} magnitude across sensors")
            ax.set_xlabel("Aligned time from earliest SampleTimeFine (s)")
            ax.set_ylabel(f"{kind.upper()} magnitude")
            ax.grid(True, alpha=0.25)
            ax.legend(ncol=3)
            fig.tight_layout()
            fig.savefig(out_dir / f"{kind}_magnitude_cross_sensor.png", dpi=170)
            plt.close(fig)


def representative_figures(recordings: list[Recording]) -> None:
    FIG_ROOT.mkdir(parents=True, exist_ok=True)
    by_key = {(r.activity, r.participant, r.sensor): r for r in recordings}

    def plot_mag_signal(rec: Recording, outfile: str, title: str, filtered: bool = False) -> None:
        df = filtered_frame(rec.data) if filtered else rec.data
        t = time_axis(len(df))
        acc_mag, gyr_mag = magnitudes(df)
        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        axes[0].plot(t, acc_mag, lw=1.15)
        axes[1].plot(t, gyr_mag, lw=1.15, color="#d62728")
        axes[0].set_title("Acceleration magnitude")
        axes[1].set_title("Gyroscope magnitude")
        for ax in axes:
            ax.grid(True, alpha=0.25)
        axes[1].set_xlabel("Time (s)")
        fig.suptitle(title)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(FIG_ROOT / outfile, dpi=200)
        plt.close(fig)

    def plot_raw_filtered_axis(rec: Recording, outfile: str, title: str, cols: list[str]) -> None:
        raw = rec.data
        filt = filtered_frame(raw)
        t = time_axis(len(raw))
        fig, axes = plt.subplots(len(cols), 1, figsize=(12, 8), sharex=True)
        if len(cols) == 1:
            axes = [axes]
        for ax, col in zip(axes, cols):
            ax.plot(t, raw[col], color="0.72", lw=0.8, label="raw")
            ax.plot(t, filt[col], lw=1.3, label="filtered")
            ax.set_title(col)
            ax.grid(True, alpha=0.25)
        axes[0].legend()
        axes[-1].set_xlabel("Time (s)")
        fig.suptitle(title)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(FIG_ROOT / outfile, dpi=200)
        plt.close(fig)

    if ("standing", "P01", "pelvis") in by_key:
        plot_raw_filtered_axis(
            by_key[("standing", "P01", "pelvis")],
            "01_representative_standing_raw_acc_gyr.png",
            "Representative raw Standing Acc/Gyr: P01 pelvis",
            ACC_COLS + GYR_COLS,
        )
        plot_raw_filtered_axis(
            by_key[("standing", "P01", "pelvis")],
            "02_standing_raw_vs_filtered.png",
            "Standing raw vs filtered: P01 pelvis",
            ["Acc_X", "Acc_Y", "Acc_Z", "Gyr_X", "Gyr_Y", "Gyr_Z"],
        )
    if ("squat", "P01", "thigh_r") in by_key:
        plot_raw_filtered_axis(
            by_key[("squat", "P01", "thigh_r")],
            "03_squat_raw_vs_filtered.png",
            "Representative Squat raw vs filtered: P01 thigh_r",
            ACC_COLS + GYR_COLS,
        )
    if ("high_knee_hops", "P02", "shank_r") in by_key:
        plot_mag_signal(
            by_key[("high_knee_hops", "P02", "shank_r")],
            "04_high_knee_hops_signal.png",
            "Representative High Knee Hops signal: P02 shank_r",
        )
    if ("jogging", "P02", "shank_r") in by_key:
        plot_mag_signal(
            by_key[("jogging", "P02", "shank_r")],
            "05_jogging_signal.png",
            "Representative Jogging signal: P02 shank_r",
        )

    # Reuse generated cross-sensor style for presentation-ready overview figures.
    for activity, participant, outfile in [
        ("squat", "P01", "06_squat_multisensor_overview.png"),
        ("jogging", "P02", "07_jogging_multisensor_overview.png"),
    ]:
        recs = [r for r in recordings if r.activity == activity and r.participant == participant]
        if len(recs) < 2:
            continue
        recs = sorted(recs, key=lambda r: SENSORS.index(r.sensor))
        min_time = min(float(r.data["SampleTimeFine"].iloc[0]) for r in recs)
        fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
        for rec in recs:
            t = (rec.data["SampleTimeFine"].to_numpy(float) - min_time) / 1_000_000.0
            acc_mag, gyr_mag = magnitudes(rec.data)
            axes[0].plot(t, acc_mag, lw=1.0, label=rec.sensor)
            axes[1].plot(t, gyr_mag, lw=1.0, label=rec.sensor)
        axes[0].set_title("Acceleration magnitude")
        axes[1].set_title("Gyroscope magnitude")
        for ax in axes:
            ax.grid(True, alpha=0.25)
            ax.legend(ncol=3, fontsize=9)
        axes[-1].set_xlabel("Aligned time from earliest SampleTimeFine (s)")
        fig.suptitle(f"Synchronized multi-sensor {ACTIVITY_LABELS[activity]} overview: {participant}")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(FIG_ROOT / outfile, dpi=200)
        plt.close(fig)


def write_status_criteria() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    text = """# Standing Noise Status Criteria

Statuses are descriptive and dataset-relative because the CSV metadata does not document physical units.

- `STABLE`: standing magnitude variation is near the median of the standing recordings, with low drift and no robust outlier spikes.
- `MINOR_VARIATION`: variation or drift is above the central standing group but not extreme.
- `POSSIBLE_MOVEMENT`: variation or drift is a high outlier compared with other standing recordings, suggesting actual participant movement or placement disturbance may be present.
- `SUSPICIOUS_SPIKES`: a recording has both elevated standing variation and an isolated single-sample jump larger than 75% of that channel's full standing range, indicating spike-like behavior beyond gradual baseline change.

The variation score uses standing-only Acc/Gyr magnitude standard deviation and peak-to-peak values, normalized by robust median absolute deviation. Drift is the fitted linear change across the recording divided by signal peak-to-peak range. These criteria are intentionally descriptive and comparative within this dataset; they are not physical pass/fail limits.
"""
    (OUT_ROOT / "standing_status_criteria.md").write_text(text, encoding="utf-8")


def main() -> None:
    recordings = load_recordings()
    if not recordings:
        raise SystemExit("No curated recordings found")

    for root in [OUT_ROOT / "raw", OUT_ROOT / "raw_vs_filtered", OUT_ROOT / "cross_sensor", FIG_ROOT]:
        root.mkdir(parents=True, exist_ok=True)

    for rec in recordings:
        plot_raw_recording(rec)
        plot_raw_vs_filtered(rec)

    plot_cross_sensor(recordings)
    representative_figures(recordings)

    standing = standing_metrics(recordings)
    standing_public = standing.drop(columns=["_spike_score", "_variation_score", "_drift_ratio"], errors="ignore")
    standing_public.to_csv(OUT_ROOT / "standing_noise_summary.csv", index=False)
    standing.to_csv(OUT_ROOT / "standing_noise_summary_diagnostics.csv", index=False)
    dynamic_summary(recordings).to_csv(OUT_ROOT / "dynamic_behavior_summary.csv", index=False)
    write_status_criteria()

    print(f"recordings={len(recordings)}")
    print(f"standing_rows={len(standing_public)}")
    print(f"raw_plots={len(list((OUT_ROOT / 'raw').rglob('raw_time_domain.png')))}")
    print(f"raw_vs_filtered_plots={len(list((OUT_ROOT / 'raw_vs_filtered').rglob('raw_vs_filtered_axes.png')))}")
    print(f"cross_sensor_plots={len(list((OUT_ROOT / 'cross_sensor').rglob('*_magnitude_cross_sensor.png')))}")
    print(f"final_figures={len(list(FIG_ROOT.glob('*.png')))}")


if __name__ == "__main__":
    main()
