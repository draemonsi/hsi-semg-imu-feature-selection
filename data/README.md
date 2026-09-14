# Data Directory

This directory stores thesis datasets and derived data products.

## Layout

```text
data/
  raw/
    xsens_imu/
      pre_pilot/
  processed/
    xsens_imu/
```

## Current Dataset

The current raw dataset is the Xsens DOT IMU pre-pilot dataset in `data/raw/xsens_imu/pre_pilot/`.

Raw data should be treated as read-only. Analysis scripts should write derived files to `results/` or to `data/processed/` when a processed dataset is intentionally part of the project.

The expected future sEMG dataset has not been added yet.
