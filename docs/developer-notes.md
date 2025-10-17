# Developer Notes

## Nightly statistics import

The integration refreshes long-term statistics shortly after **01:00** in the controller's configured timezone. The nightly job executes the following steps for each of the seven history samples returned by the Beanbag API:

1. **Report-day assignment.** Convert the sample timestamp from UTC to the configured timezone, subtract one day (because the controller reports the day that just finished), and store the resulting calendar date. The helper `report_day_for_sample()` handles daylight-saving gaps and folds.
2. **Schedule analysis.** Fetch the primary and boost weekly programs, normalise them with `canonicalize_weekly()`, and build day-specific intervals via `day_intervals()`. Depending on the selected anchor strategy (`midpoint`, `start`, or `end`), choose an anchor timestamp inside the longest interval with `choose_anchor()`. If no interval matches, fall back to `safe_anchor_datetime()` using the configured anchor times.
3. **Energy calibration.** Compare the device-reported energy with the runtime-derived estimate. When the ratio matches `ln(10)` within tolerance, keep the reported value (`EnergyCalibration.use_scale=True`). Otherwise derive energy from runtime minutes and the configured fallback power.
4. **Statistic import.** Build external statistics payloads for primary and boost energy (`total_increasing`), runtime (`measurement`), and scheduled time (`measurement`). Persist the cumulative energy totals in the per-entry storage file so repeated runs remain idempotent.
5. **Sensor update.** Store the most recent daily summary inside `SecuremtrRuntimeData.statistics_state` and fire dispatcher events so the sensors reflect the latest import.

Key helpers live in `utils.py` and `schedule.py`. Update `docs/function_map.txt` whenever a new helper or public function is added.

## Logging

The nightly pipeline emits INFO-level records for each processed day, including the report date, chosen anchor, calibration method, and statistic IDs. Use these logs when diagnosing unexpected totals or confirming DST alignment.
