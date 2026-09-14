from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


EXPECTED_DEVICE_TAGS = {
    "DOT_Pelvis",
    "DOT_Thigh_R",
    "DOT_Thigh_L",
    "DOT_Shank_R",
    "DOT_Shank_L",
}

REQUIRED_COLUMNS = [
    "PacketCounter",
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

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = REPO_ROOT / "data" / "raw" / "xsens_imu" / "pre_pilot"
RESULTS_ROOT = REPO_ROOT / "results" / "xsens_imu" / "pre_pilot"


@dataclass(frozen=True)
class ParsedRecording:
    path: Path
    metadata: dict[str, str]
    table: pd.DataFrame


def normalize_sensor_name(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def activity_from_filename(path: Path) -> str:
    parts = path.stem.split("_")
    return parts[-1].upper() if parts else ""


def parse_output_rate_hz(value: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*Hz", value or "", flags=re.I)
    return float(match.group(1)) if match else None


def parse_start_time(value: str) -> datetime | None:
    if not value:
        return None
    match = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}:\d{2}:\d{2})_(?P<ms>\d{1,6})",
        value.strip(),
    )
    if not match:
        return None
    microseconds = match.group("ms").ljust(6, "0")
    return datetime.strptime(
        f"{match.group('date')} {match.group('time')}.{microseconds}",
        "%Y-%m-%d %H:%M:%S.%f",
    )


def find_table_header(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.startswith("PacketCounter,"):
            return index
    raise ValueError("Could not find PacketCounter CSV header")


def parse_metadata(lines: list[str], header_index: int) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for raw_line in lines[:header_index]:
        line = raw_line.strip()
        if not line:
            continue
        if line == "sep=,":
            metadata["sep"] = ","
            continue
        if "," not in line:
            continue
        key, value = line.split(",", 1)
        key = key.strip().rstrip(":")
        metadata[key] = value.strip()
    return metadata


def read_xsens_csv(path: Path) -> ParsedRecording:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header_index = find_table_header(lines)
    metadata = parse_metadata(lines, header_index)

    reader = csv.reader(lines[header_index:])
    header = next(reader)
    rows = []
    for row in reader:
        if not row:
            continue
        if len(row) == len(header) + 1 and row[-1] == "":
            row = row[:-1]
        rows.append(row)

    table = pd.DataFrame(rows, columns=header)
    for column in table.columns:
        table[column] = pd.to_numeric(table[column], errors="coerce")

    return ParsedRecording(path=path, metadata=metadata, table=table)


def packet_issues(packet_counter: pd.Series) -> tuple[int, int, list[int], list[int]]:
    packets = packet_counter.dropna().astype(int).to_numpy()
    if packets.size == 0:
        return 0, 0, [], []

    duplicates = sorted(
        packet
        for packet, count in Counter(packets.tolist()).items()
        if count > 1
    )
    expected = set(range(int(np.min(packets)), int(np.max(packets)) + 1))
    observed = set(packets.tolist())
    missing = sorted(expected - observed)
    return len(missing), len(duplicates), missing[:20], duplicates[:20]


def sample_diffs(sample_time: pd.Series) -> np.ndarray:
    values = sample_time.dropna().astype(np.int64).to_numpy()
    if values.size < 2:
        return np.array([], dtype=np.int64)
    return np.diff(values)


def modal_positive_diff(diffs: Iterable[int]) -> int | None:
    positive = [int(diff) for diff in diffs if int(diff) > 0]
    if not positive:
        return None
    return Counter(positive).most_common(1)[0][0]


def infer_expected_steps(recordings: list[ParsedRecording]) -> dict[str, int]:
    diffs_by_rate: dict[str, list[int]] = defaultdict(list)
    for recording in recordings:
        rate = recording.metadata.get("OutputRate", "").strip()
        diffs_by_rate[rate].extend(sample_diffs(recording.table["SampleTimeFine"]).tolist())
    return {
        rate: step
        for rate, diffs in diffs_by_rate.items()
        if (step := modal_positive_diff(diffs)) is not None
    }


def summarize_recording(recording: ParsedRecording, expected_step: int | None) -> dict[str, object]:
    table = recording.table
    packets = table["PacketCounter"].dropna().astype(int)
    times = table["SampleTimeFine"].dropna().astype(np.int64)
    diffs = sample_diffs(table["SampleTimeFine"])
    missing_count, duplicate_count, missing_preview, duplicate_preview = packet_issues(
        table["PacketCounter"]
    )

    abnormal_gaps = []
    if expected_step is not None:
        abnormal_gaps = [
            f"{index + 1}->{index + 2}:{int(diff)}"
            for index, diff in enumerate(diffs)
            if int(diff) != expected_step
        ]

    output_rate = recording.metadata.get("OutputRate", "")
    start_time = recording.metadata.get("StartTime", "")
    start_dt = parse_start_time(start_time)
    device_tag = normalize_sensor_name(recording.metadata.get("DeviceTag", ""))

    return {
        "filename": recording.path.name,
        "relative_path": recording.path.as_posix(),
        "device_tag": device_tag,
        "activity": activity_from_filename(recording.path),
        "start_time": start_time,
        "start_time_iso": start_dt.isoformat(timespec="milliseconds") if start_dt else "",
        "output_rate": output_rate,
        "output_rate_hz": parse_output_rate_hz(output_rate),
        "row_count": len(table),
        "packet_first": int(packets.iloc[0]) if not packets.empty else "",
        "packet_last": int(packets.iloc[-1]) if not packets.empty else "",
        "packet_min": int(packets.min()) if not packets.empty else "",
        "packet_max": int(packets.max()) if not packets.empty else "",
        "missing_packet_count": missing_count,
        "missing_packet_preview": ";".join(map(str, missing_preview)),
        "duplicate_packet_count": duplicate_count,
        "duplicate_packet_preview": ";".join(map(str, duplicate_preview)),
        "sample_time_first": int(times.iloc[0]) if not times.empty else "",
        "sample_time_last": int(times.iloc[-1]) if not times.empty else "",
        "sample_time_min": int(times.min()) if not times.empty else "",
        "sample_time_max": int(times.max()) if not times.empty else "",
        "expected_sample_time_step_for_rate": expected_step or "",
        "observed_sample_time_steps": ";".join(map(str, sorted(set(map(int, diffs.tolist()))))),
        "abnormal_time_gap_count": len(abnormal_gaps),
        "abnormal_time_gap_preview": ";".join(abnormal_gaps[:20]),
        "columns": "|".join(table.columns.tolist()),
        "schema_ok": table.columns.tolist() == REQUIRED_COLUMNS,
    }


def activity_sync_summary(manifest: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    for activity, group in manifest.groupby("activity"):
        starts = pd.to_datetime(group["start_time_iso"], errors="coerce")
        spread_ms = (
            (starts.max() - starts.min()).total_seconds() * 1000
            if starts.notna().sum() > 1
            else 0.0
        )
        first_samples = pd.to_numeric(group["sample_time_first"], errors="coerce")
        sample_spread = (
            int(first_samples.max() - first_samples.min())
            if first_samples.notna().sum() > 1
            else 0
        )
        expected_steps = pd.to_numeric(
            group["expected_sample_time_step_for_rate"], errors="coerce"
        ).dropna()
        tolerance = int(expected_steps.mode().iloc[0] * 2) if not expected_steps.empty else 0
        simultaneous = spread_ms <= 100 and (tolerance == 0 or sample_spread <= tolerance)
        status = "synchronized" if simultaneous else "not fully synchronized"
        lines.append(
            f"{activity}: {status}; files={len(group)}, start_spread_ms={spread_ms:.1f}, sample_first_spread={sample_spread}"
        )
    return lines


def build_manifest(raw_dir: Path, output_path: Path) -> pd.DataFrame:
    recordings = [read_xsens_csv(path) for path in sorted(raw_dir.rglob("*.csv"))]
    expected_steps = infer_expected_steps(recordings)
    rows = [
        summarize_recording(
            recording,
            expected_steps.get(recording.metadata.get("OutputRate", "").strip()),
        )
        for recording in recordings
    ]
    manifest = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(output_path, index=False)
    return manifest


def print_terminal_summary(manifest: pd.DataFrame) -> None:
    print(f"CSV files loaded: {len(manifest)}")
    print(f"Manifest rows: {len(manifest)}")
    print()

    expected = EXPECTED_DEVICE_TAGS
    found = set(manifest["device_tag"].dropna().astype(str))
    missing = sorted(expected - found)
    unexpected = sorted(found - expected)
    if missing:
        print("Missing expected sensors:", ", ".join(missing))
    if unexpected:
        print("Unexpected sensors:", ", ".join(unexpected))
    if not missing and not unexpected:
        print("All expected sensors present.")
    print()

    print("Packet/time warnings:")
    any_warning = False
    for row in manifest.itertuples(index=False):
        issues = []
        if row.missing_packet_count:
            issues.append(
                f"missing packets={row.missing_packet_count} [{row.missing_packet_preview}]"
            )
        if row.duplicate_packet_count:
            issues.append(
                f"duplicate packets={row.duplicate_packet_count} [{row.duplicate_packet_preview}]"
            )
        if row.abnormal_time_gap_count:
            issues.append(
                f"abnormal SampleTimeFine gaps={row.abnormal_time_gap_count} [{row.abnormal_time_gap_preview}]"
            )
        if not row.schema_ok:
            issues.append("schema mismatch")
        if issues:
            any_warning = True
            print(f"- {row.filename}: " + "; ".join(issues))
    if not any_warning:
        print("- none")
    print()

    print("Activity synchronization:")
    for line in activity_sync_summary(manifest):
        print(f"- {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an Xsens DOT CSV manifest.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DATA_DIR)
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_ROOT / "manifests" / "xsens_manifest.csv",
    )
    args = parser.parse_args()

    manifest = build_manifest(args.raw_dir, args.output)
    print_terminal_summary(manifest)
    print()
    print(f"Manifest written to: {args.output}")


if __name__ == "__main__":
    main()
