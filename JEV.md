# JEV Report

**Verdict:** Jev agreed on 73% (n=11 live) of confident calls

## Charts

```mermaid
xychart-beta
    title "Triage: Jev vs Incumbent (n=109 live)"
    x-axis ["Agreed", "Disagreed", "Uncertain (fell back)"]
    y-axis "Decisions" 0 --> 99
    bar [8, 3, 98]
```

```mermaid
xychart-beta
    title "Auto-Merge Confidence by Action Zone (n=205 live)"
    x-axis ["Act: reject (below 0.35)", "Pipeline (0.35-0.65)", "Act: merge (0.65+)"]
    y-axis "Count" 0 --> 109
    bar [15, 108, 82]
```

```mermaid
xychart-beta
    title "Weekly Triage Agreement When Confident"
    x-axis ["9/21 (n=2 live)", "9/28 (n=6 live)", "10/5 (n=1 live)", "10/26 (n=2 live)"]
    y-axis "% agreed" 0 --> 100
    bar [50, 67, 100, 100]
```

## Counts

- **Total entries:** 442
- **Live entries:** 330
- **Backfilled entries:** 112
- **Date range:** 2026-07-17 to 2026-11-08

## Triage Agreement

- **Agreed:** 8 of 109
- **Disagreed:** 3 of 109
- **Uncertain (fell back):** 98 of 109
- **When confident, agreed:** 8 of 11 — 73% (n=11 live)
- **Disagreement breakdown:**
  - ready-vs-rejected: 3

### Weekly Trends

| Week | Agreed | Disagreed | Uncertain | n |
|------|--------|-----------|-----------|---|
| 9/21 | 1 | 1 | 19 | 21 |
| 9/28 | 4 | 2 | 31 | 37 |
| 10/5 | 1 | 0 | 5 | 6 |
| 10/26 | 2 | 0 | 38 | 40 |
| 11/2 | 0 | 0 | 5 | 5 |

## Auto-Merge Agreement

- **Mean confidence:** 0.590 (n=205 live)
- **Median confidence:** 0.590 (n=205 live)
- **Agreement with incumbent decision:** 49/69 — 71% (n=69 live)

### Per-Zone Agreement

| Zone | Agreed | Total | Agreement |
|------|--------|-------|-----------|
| Act: reject (below 0.35) | 2 | 4 | 50% (n=4 live) |
| Pipeline (0.35-0.65) | 25 | 39 | 64% (n=39 live) |
| Act: merge (0.65+) | 22 | 26 | 85% (n=26 live) |

## Backfill

```mermaid
xychart-beta
    title "Triage: Jev vs Incumbent (n=28 backfill)"
    x-axis ["Agreed", "Disagreed", "Uncertain (fell back)"]
    y-axis "Decisions" 0 --> 23
    bar [22, 0, 6]
```

```mermaid
xychart-beta
    title "Auto-Merge Confidence by Action Zone (n=66 backfill)"
    x-axis ["Act: reject (below 0.35)", "Pipeline (0.35-0.65)", "Act: merge (0.65+)"]
    y-axis "Count" 0 --> 40
    bar [39, 9, 18]
```

```mermaid
xychart-beta
    title "Weekly Triage Agreement When Confident"
    x-axis ["9/21 (n=22 backfill)"]
    y-axis "% agreed" 0 --> 100
    bar [100]
```

### Backfill Triage Agreement

- **Agreed:** 22 of 28
- **Disagreed:** 0 of 28
- **Uncertain (fell back):** 6 of 28
- **When confident, agreed:** 22 of 22 — 100% (n=22 backfill)

### Weekly Trends

| Week | Agreed | Disagreed | Uncertain | n |
|------|--------|-----------|-----------|---|
| 9/21 | 22 | 0 | 6 | 28 |

### Backfill Auto-Merge Agreement

- **Mean confidence:** 0.412 (n=66 backfill)
- **Median confidence:** 0.310 (n=66 backfill)
- **Agreement with incumbent decision:** 9/33 — 27% (n=33 backfill)

### Per-Zone Agreement

| Zone | Agreed | Total | Agreement |
|------|--------|-------|-----------|
| Act: reject (below 0.35) | 1 | 19 | 5% (n=19 backfill) |
| Pipeline (0.35-0.65) | 3 | 9 | 33% (n=9 backfill) |
| Act: merge (0.65+) | 5 | 5 | 100% (n=5 backfill) |

## Legend

- **Incumbent decision:** the human/pipeline decision that was actually used
- **Fell back:** Jev returned an uncertain result and deferred to the incumbent
- **Pipeline (0.35–0.65):** confidence too close to call; Jev defers to the incumbent
- **Backfilled:** a decision reconstructed from historical data, not made live
- **n=… live / n=… backfill:** the sample behind a number; live and backfilled decisions are never pooled

---

*Generated at 2026-11-08 21:37 UTC*