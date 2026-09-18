# EVAL Report

## Overall

| Metric | Value |
|--------|-------|
| First-attempt success | 58% |
| Avg cost/PR | $1.44 |
| Human edit rate | 15% |

## Per-Module Breakdown

| Module | Success | Avg Cost | PRs | Auto-merge ready? |
|--------|---------|----------|-----|--------------------|
| other | 40% | $0.48 | 5 | No |
| src/autoloop/ | 71% | $1.24 | 63 | No |

## Trend

| Date | Implementations | First-attempt | Avg Cost | Human Edits |
|------|----------------|---------------|----------|-------------|
| 2026-07-19 | 1 | 100% | $1.17 | 0% |
| 2026-07-26 | 6 | 100% | $0.76 | 0% |
| 2026-08-02 | 13 | 69% | $1.10 | 0% |
| 2026-08-09 | 1 | 100% | $0.38 | 0% |
| 2026-09-06 | 9 | 89% | $1.48 | 0% |
| 2026-09-13 | 8 | 88% | $1.71 | 0% |
| 2026-09-18 | 125 | 58% | $1.44 | 15% |
| 2026-09-20 | 53 | 57% | $2.38 | 0% |

```mermaid
xychart-beta
    title "First-Attempt Success Rate"
    x-axis ["2026-07-19", "2026-07-26", "2026-08-02", "2026-08-09", "2026-09-06", "2026-09-13", "2026-09-18", "2026-09-20"]
    y-axis "Success %" 0 --> 100
    line [100, 100, 69, 100, 89, 88, 58, 57]
```

```mermaid
xychart-beta
    title "Avg Cost/PR"
    x-axis ["2026-07-19", "2026-07-26", "2026-08-02", "2026-08-09", "2026-09-06", "2026-09-13", "2026-09-18", "2026-09-20"]
    y-axis "Cost ($)"
    line [1.17, 0.76, 1.10, 0.38, 1.48, 1.71, 1.44, 2.38]
```

```mermaid
%%{init: {'theme': 'neutral'}}%%
pie title Per-Module Success Distribution
    "other (40%, 5 PRs)" : 5
    "src/autoloop/ (71%, 63 PRs)" : 63
```
