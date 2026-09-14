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
from scipy.signal import butter, sosfiltfilt, welch

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
    abnormal_gap_indices,
    relative_time_seconds,
    safe_name,
)


FILTER_COLUMNS = ACC_COLUMNS + GYR_COLUMNS
DEFAULT_CUTOFF_HZ = 10.0
DEFAULT_ORDER = 4
REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


def contiguous_segments(table: pd.DataFrame, expected_step: int | None) -> list[np.ndarray]:
    if len(table) == 0:
        return []
    packet = table["PacketCounter"].to_numpy(dtype=int)
    sample = table["SampleTimeFine"].to_numpy(dtype=np.int64)
    breaks = []
    for index in range(len(table) - 1):
        packet_ok = packet[index + 1] - packet[index] == 1
        sample_ok = expected_step is None or sample[index + 1] - sample[index] == expected_step
        if not (packet_ok and sample_ok):
            breaks.append(index + 1)
    starts = [0] + breaks
    ends = breaks + [len(table)]
    return [np.arange(start, end) for start, end in zip(starts, ends) if end > start]


def design_filter(sample_rate_hz: float, cutoff_hz: float, order: int) -> np.ndarray:
    nyquist = sample_rate_hz / 2.0
    if not 0 < cutoff_hz < nyquist:
        raise ValueError(f"cutoff_hz must be between 0 and Nyquist ({nyquist})")
    return butter(order, cutoff_hz, btype="lowpass", fs=sample_rate_hz, output="sos")


def sos_padlen(sos: np.ndarray) -> int:
    # Mirrors scipy.signal.sosfiltfilt's default pad length well enough to decide
    # whether a short packet segment can be filtered safely.
    zeros_at_origin = (sos[:, 2] == 0).sum()
    poles_at_origin = (sos[:, 5] == 0).sum()
    return int(3 * (2 * len(sos) + 1 - min(zeros_at_origin, poles_at_origin)))


def filter_recording(
    table: pd.DataFrame,
    sample_rate_hz: float,
    cutoff_hz: float,
    order: int,
    expected_step: int | None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    processed = table.copy()
    sos = design_filter(sample_rate_hz, cutoff_hz, order)
    padlen = sos_padlen(sos)
    segments = contiguous_segments(table, expected_step)
    short_segments = []

    for column in FILTER_COLUMNS:
        filtered = np.full(len(table), np.nan, dtype=float)
        for segment in segments:
            values = table[column].iloc[segment].to_numpy(dtype=float)
            if len(segment) <= padlen or not np.isfinite(values).all():
                short_segments.append(len(segment))
                continue
            filtered[segment] = sosfiltfilt(sos, values)
        processed[f"{column}_filtered"] = filtered

    info = {
        "continuous_segment_count": len(segments),
        "continuous_segment_lengths": ";".join(map(str, [len(segment) for segment in segments])),
        "sos_padlen": padlen,
        "short_unfiltered_segment_lengths": ";".join(map(str, sorted(set(short_segments)))),
    }
    return processed, info


def spectral_summary_for_recording(
    table: pd.DataFrame,
    filename: str,
    activity: str,
    device_tag: str,
    sample_rate_hz: float,
) -> list[dict[str, object]]:
    rows = []
    for column in FILTER_COLUMNS:
        signal = table[column].to_numpy(dtype=float)
        signal = signal - np.nanmean(signal)
        frequencies, psd = welch(
            signal,
            fs=sample_rate_hz,
            nperseg=min(512, len(signal)),
        )
        total_power = float(np.trapezoid(psd, frequencies))
        area = (psd[:-1] + psd[1:]) / 2.0 * np.diff(frequencies)
        cumulative = np.cumsum(area)

        def frequency_at_power(fraction: float) -> float:
            if total_power <= 0:
                return np.nan
            index = min(np.searchsorted(cumulative, fraction * total_power), len(frequencies) - 2)
            return float(frequencies[index + 1])

        rows.append(
            {
                "filename": filename,
                "activity": activity,
                "device_tag": device_tag,
                "channel": column,
                "f90_hz": frequency_at_power(0.90),
                "f95_hz": frequency_at_power(0.95),
                "f98_hz": frequency_at_power(0.98),
                "power_fraction_gt_6hz": (
                    float(np.trapezoid(psd[frequencies >= 6.0], frequencies[frequencies >= 6.0]) / total_power)
                    if total_power > 0
                    else np.nan
                ),
                "power_fraction_gt_8hz": (
                    float(np.trapezoid(psd[frequencies >= 8.0], frequencies[frequencies >= 8.0]) / total_power)
                    if total_power > 0
                    else np.nan
                ),
                "power_fraction_gt_10hz": (
                    float(np.trapezoid(psd[frequencies >= 10.0], frequencies[frequencies >= 10.0]) / total_power)
                    if total_power > 0
                    else np.nan
                ),
            }
        )
    return rows


def y_limits(raw: pd.DataFrame, filtered: pd.DataFrame, columns: list[str]) -> tuple[float, float]:
    values = []
    for column in columns:
        values.extend(raw[column].replace([np.inf, -np.inf], np.nan).dropna().tolist())
        values.extend(filtered[f"{column}_filtered"].replace([np.inf, -np.inf], np.nan).dropna().tolist())
    if not values:
        return -1.0, 1.0
    low = float(np.nanmin(values))
    high = float(np.nanmax(values))
    if low == high:
        return low - 1.0, high + 1.0
    margin = 0.05 * (high - low)
    return low - margin, high + margin


def plot_raw_vs_processed(
    raw: pd.DataFrame,
    processed: pd.DataFrame,
    time_seconds: np.ndarray,
    filename: str,
    activity: str,
    device_tag: str,
    gap_indices: list[int],
    cutoff_hz: float,
    order: int,
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(
        f"{filename} | {device_tag} | {activity} | Butterworth order {order}, {cutoff_hz:g} Hz",
        fontsize=13,
    )

    axis_specs = [
        (axes[0], ACC_COLUMNS, "Raw acceleration", ""),
        (axes[1], ACC_COLUMNS, "Filtered acceleration", "_filtered"),
        (axes[2], GYR_COLUMNS, "Raw gyro", ""),
        (axes[3], GYR_COLUMNS, "Filtered gyro", "_filtered"),
    ]
    for axis, columns, label, suffix in axis_specs:
        for column in columns:
            axis.plot(time_seconds, processed[f"{column}{suffix}"], linewidth=0.8, label=column)
        axis.set_ylabel(label)
        axis.grid(True, linewidth=0.3, alpha=0.4)
        axis.legend(loc="upper right", ncols=len(columns), fontsize=8)

    acc_limits = y_limits(raw, processed, ACC_COLUMNS)
    gyr_limits = y_limits(raw, processed, GYR_COLUMNS)
    axes[0].set_ylim(acc_limits)
    axes[1].set_ylim(acc_limits)
    axes[2].set_ylim(gyr_limits)
    axes[3].set_ylim(gyr_limits)
    axes[3].set_xlabel("Relative time at observed 60 Hz spacing (s)")

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

    if gap_label:
        for axis in axes:
            handles, labels = axis.get_legend_handles_labels()
            unique = dict(zip(labels, handles))
            axis.legend(unique.values(), unique.keys(), loc="upper right", fontsize=8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def output_stem(path: Path, activity: str, device_tag: str) -> str:
    return f"{safe_name(activity)}__{safe_name(device_tag)}__{path.stem}"


def run(
    raw_dir: Path,
    plot_root: Path,
    processed_root: Path,
    cutoff_hz: float,
    order: int,
    spectral_summary_path: Path,
    processing_summary_path: Path,
) -> pd.DataFrame:
    recordings = [read_xsens_csv(path) for path in sorted(raw_dir.rglob("*.csv"))]
    expected_steps = infer_expected_steps(recordings)
    spectral_rows = []
    processing_rows = []

    for recording in recordings:
        table = recording.table
        activity = activity_from_filename(recording.path)
        device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))
        output_rate_hz = parse_output_rate_hz(recording.metadata.get("OutputRate", ""))
        if output_rate_hz is None:
            raise ValueError(f"Missing parseable OutputRate in {recording.path}")
        expected_step = expected_steps.get(recording.metadata.get("OutputRate", "").strip())
        time_seconds = relative_time_seconds(table["SampleTimeFine"], expected_step, output_rate_hz)

        spectral_rows.extend(
            spectral_summary_for_recording(
                table,
                recording.path.name,
                activity,
                device_tag,
                output_rate_hz,
            )
        )
        processed, info = filter_recording(
            table,
            output_rate_hz,
            cutoff_hz,
            order,
            expected_step,
        )

        stem = output_stem(recording.path, activity, device_tag)
        processed_path = processed_root / activity / device_tag / f"{stem}_processed.csv"
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        processed.to_csv(processed_path, index=False)

        plot_path = plot_root / activity / device_tag / f"{stem}_raw_vs_processed.png"
        gap_indices = abnormal_gap_indices(table, expected_step)
        plot_raw_vs_processed(
            table,
            processed,
            time_seconds,
            recording.path.name,
            activity,
            device_tag,
            gap_indices,
            cutoff_hz,
            order,
            plot_path,
        )

        processing_rows.append(
            {
                "filename": recording.path.name,
                "activity": activity,
                "device_tag": device_tag,
                "output_rate_hz": output_rate_hz,
                "filter_type": "zero-phase low-pass Butterworth",
                "filter_order": order,
                "cutoff_hz": cutoff_hz,
                "processed_csv": processed_path.as_posix(),
                "plot_path": plot_path.as_posix(),
                "gap_indices_flagged": ";".join(map(str, gap_indices)),
                **info,
            }
        )

    spectral_summary = pd.DataFrame(spectral_rows)
    spectral_summary_path.parent.mkdir(parents=True, exist_ok=True)
    spectral_summary.to_csv(spectral_summary_path, index=False)

    processing_summary = pd.DataFrame(processing_rows)
    processing_summary_path.parent.mkdir(parents=True, exist_ok=True)
    processing_summary.to_csv(processing_summary_path, index=False)
    return processing_summary


def print_summary(
    processing_summary: pd.DataFrame,
    spectral_summary_path: Path,
    processing_summary_path: Path,
    plot_root: Path,
    processed_root: Path,
    cutoff_hz: float,
    order: int,
) -> None:
    print(f"Processed recordings: {len(processing_summary)}")
    print(f"Filter: {order}th-order zero-phase low-pass Butterworth")
    print(f"Cutoff: {cutoff_hz:g} Hz")
    print(f"Plots: {plot_root}")
    print(f"Processed CSVs: {processed_root}")
    print(f"Spectral summary: {spectral_summary_path}")
    print(f"Processing summary: {processing_summary_path}")
    print()

    gap_rows = processing_summary[processing_summary["gap_indices_flagged"].astype(str) != ""]
    if not gap_rows.empty:
        print("Packet/time gaps flagged on plots:")
        for row in gap_rows.itertuples(index=False):
            print(f"- {row.filename}: indices {row.gap_indices_flagged}")

    short_rows = processing_summary[
        processing_summary["short_unfiltered_segment_lengths"].fillna("").astype(str) != ""
    ]
    if not short_rows.empty:
        print()
        print("Short continuous segments left unfiltered:")
        for row in short_rows.itertuples(index=False):
            print(f"- {row.filename}: segment lengths {row.short_unfiltered_segment_lengths}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create raw-vs-processed IMU plots and CSVs.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument(
        "--plot-root",
        type=Path,
        default=RESULTS_ROOT / "processed" / "raw_vs_processed",
    )
    parser.add_argument(
        "--processed-root",
        type=Path,
        default=RESULTS_ROOT / "processed" / "processed_csv",
    )
    parser.add_argument("--cutoff-hz", type=float, default=DEFAULT_CUTOFF_HZ)
    parser.add_argument("--order", type=int, default=DEFAULT_ORDER)
    parser.add_argument(
        "--spectral-summary",
        type=Path,
        default=RESULTS_ROOT / "processed" / "raw_vs_processed_spectral_summary.csv",
    )
    parser.add_argument(
        "--processing-summary",
        type=Path,
        default=RESULTS_ROOT / "processed" / "raw_vs_processed_summary.csv",
    )
    args = parser.parse_args()

    processing_summary = run(
        args.raw_dir,
        args.plot_root,
        args.processed_root,
        args.cutoff_hz,
        args.order,
        args.spectral_summary,
        args.processing_summary,
    )
    print_summary(
        processing_summary,
        args.spectral_summary,
        args.processing_summary,
        args.plot_root,
        args.processed_root,
        args.cutoff_hz,
        args.order,
    )


if __name__ == "__main__":
    main()
