# Final Xsens IMU Pre-Pilot Summary

## Dataset

The finalized Xsens IMU pre-pilot analysis uses the curated dataset in `data/raw/xsens_imu/pre_pilot/`. The dataset contains 41 usable CSV files from three anonymized participants (`P01`, `P02`, `P03`) across Standing, Squat, High Knee Hops, and Jogging. Sensors are available as `pelvis`, `thigh_l`, `thigh_r`, `shank_l`, and `shank_r` depending on participant/activity completeness. All included files are sampled at 60 Hz.

The original upload remains unchanged in `PrePilot_Test/`. The previous official pre-pilot dataset is preserved in `data/raw/xsens_imu/archive/pre_pilot_v1/` and its numerical results are superseded by the current curated analysis.

## Methods

The pre-pilot workflow first inspected raw accelerometer and gyroscope behavior for every valid curated CSV. Raw plots were generated for each sensor/activity/participant, including acceleration and gyroscope magnitudes.

Processed signal plots used the established IMU preprocessing method:

- 60 Hz sampling rate
- 4th-order Butterworth low-pass filter
- 10 Hz cutoff
- zero-phase `scipy.signal.sosfiltfilt`

The 10 Hz cutoff was used to preserve the main movement envelope while reducing high-frequency variation for visual comparison and exploratory cycle detection. Raw signals remain important for impact/transient inspection, especially in High Knee Hops and Jogging.

Knee-flexion analysis used synchronized thigh-shank pairs only. Quaternion samples were normalized, aligned using `SampleTimeFine`, and used to compute relative thigh-to-shank orientation. Relative rotations were converted to rotation vectors and projected onto a data-derived dominant flexion-like axis. Each activity/leg was zeroed using the lowest-motion 0.5 s paired window within the first 2 s.

The derived variable is labeled:

```text
IMU-derived relative knee flexion estimate
```

It is not an anatomical or clinically validated knee angle.

## Signal Behavior

Standing analysis showed that P01 has early settling/movement, particularly in pelvis and thigh signals. P02 and P03 available standing sensors are more stable and better represent quiet standing behavior.

Squat recordings show repetitive movement patterns, with P02 giving the clearest bilateral example. High Knee Hops show repeated impact/motion cycles. Jogging shows strong periodicity over the full recording where synchronized thigh-shank pairs exist. No obvious clipping or saturation was identified in the signal-behavior outputs.

## Knee-Flexion Findings

### Squat

| Participant | Left ROM | Right ROM | Left cycles | Right cycles | Bilateral correlation | Lag | Interpretation |
|---|---:|---:|---:|---:|---:|---:|---|
| P01 | ~118.1° | ~124.2° | 10 | 9 | -0.242 | -21 samples | Unstable / investigate |
| P02 | ~80.6° | ~82.0° | 5 | 5 | 0.998 | 0 samples | Strong bilateral consistency |
| P03 | N/A | N/A | N/A | N/A | N/A | N/A | Required sensor pairs missing |

P02 squat is the strongest proof-of-method case. The left and right estimates are closely matched in range, timing, and shape. P01 squat should not be used as evidence of reliable bilateral squat tracking because the bilateral estimates are unstable and poorly correlated.

### High Knee Hops

P01 produced approximately 5 cycles with strong bilateral agreement (`r = 0.995`, lag `0` samples). P02 produced approximately 6 cycles per leg, with ROM estimates of ~120.0° left and ~112.7° right. P02 showed negative bilateral correlation with an approximately 26-sample lag, consistent with alternating-leg behavior rather than synchronized bilateral movement. P03 was not computable due to missing required thigh-shank pairs.

### Jogging

P01 was computable on the right side only: ~90.3° range, 128 peaks, and mean interval ~0.943 s. P02 was computable bilaterally: ~86.0° left and ~85.0° right, 129/128 peaks, and mean intervals ~0.929/~0.927 s. P02 bilateral correlation was `r = -0.785` with a 27-sample lag, consistent with alternating leg motion. P03 jogging was not recorded / not computable for knee-flexion analysis.

## Limitations

- P03 has incomplete sensor coverage and is not usable for knee-flexion analysis.
- P01 jogging is missing the synchronized left shank required for left-knee estimation.
- P01 squat produces unstable bilateral estimates.
- No anatomical sensor-to-segment calibration was performed.
- No external motion-capture ground truth was available.
- The flexion axis is data-derived and activity/pair-specific.
- The estimate should not be interpreted as a validated anatomical knee angle.

## Protocol Recommendations

For the actual pilot:

- Confirm all five IMUs are recording before each trial.
- Confirm synchronized thigh-shank pairs before movement starts.
- Include a short quiet baseline period at the beginning of each activity.
- Give a clear countdown and delay movement until recording is stable.
- Repeat any trial immediately if a sensor is missing, empty, or visibly desynchronized.
- Preserve raw signals for impact/transient inspection in addition to filtered signals.
- Add anatomical calibration and external validation if clinically interpretable knee angles are required.

## Conclusion

The pre-pilot supports feasibility of the IMU processing workflow: the curated dataset can be loaded, quality-checked, filtered, synchronized, and used for quaternion-based relative thigh-shank motion estimates. P02 provides the strongest validation evidence, especially in squat and jogging. Protocol consistency must be improved before the actual pilot, particularly complete sensor coverage, stable trial starts, and calibration/validation if anatomical knee angles are required.
