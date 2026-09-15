# Standing Noise Status Criteria

Statuses are descriptive and dataset-relative because the CSV metadata does not document physical units.

- `STABLE`: standing magnitude variation is near the median of the standing recordings, with low drift and no robust outlier spikes.
- `MINOR_VARIATION`: variation or drift is above the central standing group but not extreme.
- `POSSIBLE_MOVEMENT`: variation or drift is a high outlier compared with other standing recordings, suggesting actual participant movement or placement disturbance may be present.
- `SUSPICIOUS_SPIKES`: a recording has both elevated standing variation and an isolated single-sample jump larger than 75% of that channel's full standing range, indicating spike-like behavior beyond gradual baseline change.

The variation score uses standing-only Acc/Gyr magnitude standard deviation and peak-to-peak values, normalized by robust median absolute deviation. Drift is the fitted linear change across the recording divided by signal peak-to-peak range. These criteria are intentionally descriptive and comparative within this dataset; they are not physical pass/fail limits.
