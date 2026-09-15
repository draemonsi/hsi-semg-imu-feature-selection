# Data Directory

This directory stores thesis datasets and derived data products.

## Layout

```text
data/
  raw/
    xsens_imu/
      archive/
        pre_pilot_v1/
      pre_pilot/
      pre_pilot_manifest.csv
  processed/
    xsens_imu/
```

## Current Dataset

The current official raw dataset is the curated Xsens DOT IMU pre-pilot dataset in `data/raw/xsens_imu/pre_pilot/`.

`PrePilot_Test/` is the original uploaded source folder. Treat it as immutable source material and do not edit, rename, move, or delete files inside it during analysis.

The curated official dataset uses anonymized participant IDs only:

```text
P01
P02
P03
```

The public dataset and manifests must not contain participant names. Any local name-to-ID mapping belongs under `private/`, which is excluded by `.gitignore`.

The curated pre-pilot layout is:

```text
data/raw/xsens_imu/pre_pilot/
  standing/
  squat/
  high_knee_hops/
  jogging/
```

Within each activity, files are grouped by anonymized participant ID and use canonical sensor filenames:

```text
pelvis.csv
thigh_l.csv
thigh_r.csv
shank_l.csv
shank_r.csv
```

Empty, ambiguous, duplicate, or unsynchronized source exports are excluded through `data/raw/xsens_imu/pre_pilot_manifest.csv` rather than silently deleted from the source upload.

The previous official pre-pilot dataset is preserved unchanged in `data/raw/xsens_imu/archive/pre_pilot_v1/`.

Raw data should be treated as read-only. Analysis scripts should write derived files to `results/` or to `data/processed/` when a processed dataset is intentionally part of the project.

The expected future sEMG dataset has not been added yet.
