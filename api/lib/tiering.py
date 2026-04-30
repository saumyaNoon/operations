"""
nim-agents-ops api/lib/tiering.py

shared gate-then-tier logic. mirrors the 8-step process in PLAN.md:

  1. scope filter — caller passes in_scope rows
  2. absolute floor — metric > floor
  3. significance gate — store_contribution > 5pp of geo total OR worst 5
  4. tier 3 — passed gate AND top 3 by contribution (min 10% share)
                          AND metric > t3 critical
  5. tier 2 — passed gate AND (top driver OR above critical)
  6. tier 1 — passed gate AND neither t2/t3
  7. dedup — caller checks alert_log.was_alerted_recently
  8. consolidate — caller groups by ds_code

each agent declares a TierSpec describing its thresholds + which fields hold
metric_value, contribution_pct etc. then calls `assign_tiers(rows, spec)`.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any


@dataclass
class TierSpec:
    """declarative tier rules for a single agent / sub-tab."""
    metric_field: str = "metric_value"
    floor: float = 0.0                            # absolute floor; below = drop
    t3_metric: float = 0.0                        # t3 critical threshold
    t2_metric: float = 0.0                        # t2 watch threshold
    t1_metric: float = 0.0                        # t1 floor (gate)
    # secondary count gate (e.g. "absent_count > 10" or ">=10 jobs")
    count_field: Optional[str] = None
    t3_count: Optional[float] = None
    t2_count: Optional[float] = None
    t1_count: Optional[float] = None
    min_count_for_tier: Optional[float] = None    # universal floor (e.g. ≥10 jobs)
    # significance gate
    contrib_pct_min: float = 5.0                  # 5pp of geo total
    worst_n_floor: int = 5                        # always keep worst 5 even if below contrib
    contrib_field: str = "contribution_pct"
    # extra: trigger t3 with EITHER metric OR count (PLAN.md uses OR for absent)
    use_or_logic: bool = True


def _passes_min_count(row: Dict[str, Any], spec: TierSpec) -> bool:
    if spec.min_count_for_tier is None or not spec.count_field:
        return True
    return (row.get(spec.count_field) or 0) >= spec.min_count_for_tier


def _meets(metric: float, count, m_th, c_th, use_or: bool) -> bool:
    """does the row meet a tier's metric/count rule?"""
    m_ok = metric is not None and m_th is not None and metric >= m_th
    c_ok = count is not None and c_th is not None and count >= c_th
    if m_th is None and c_th is None:
        return False
    if c_th is None:
        return m_ok
    if m_th is None:
        return c_ok
    return (m_ok or c_ok) if use_or else (m_ok and c_ok)


def assign_tiers(rows: List[Dict[str, Any]], spec: TierSpec,
                 ds_total: Optional[float] = None) -> List[Dict[str, Any]]:
    """tag each row with a `tier` (1/2/3) or drop it. mutates rows in place
    and returns only the tier-bearing subset, ordered worst-first."""
    if not rows:
        return []

    # compute contribution_pct if not pre-populated and geo total provided
    if ds_total and ds_total > 0:
        for r in rows:
            if spec.contrib_field not in r:
                metric = r.get(spec.metric_field) or 0
                r[spec.contrib_field] = round(metric / ds_total * 100, 2)

    # absolute floor + min_count
    surviving = []
    for r in rows:
        m = r.get(spec.metric_field)
        if m is None:
            continue
        if not _passes_min_count(r, spec):
            continue
        if m < spec.floor and not _meets(m, r.get(spec.count_field), spec.t1_metric,
                                          spec.t1_count, spec.use_or_logic):
            continue
        surviving.append(r)

    if not surviving:
        return []

    # significance gate
    surviving.sort(key=lambda x: (x.get(spec.metric_field) or 0), reverse=True)
    keep_worst_n = surviving[:spec.worst_n_floor]
    above_contrib = [r for r in surviving
                     if (r.get(spec.contrib_field) or 0) >= spec.contrib_pct_min]
    gated = list({id(r): r for r in (keep_worst_n + above_contrib)}.values())

    # tier assignment — t3 first, then t2, then t1
    tiered = []
    for r in gated:
        m = r.get(spec.metric_field) or 0
        cnt = r.get(spec.count_field) if spec.count_field else None
        tier = None
        if _meets(m, cnt, spec.t3_metric, spec.t3_count, spec.use_or_logic):
            tier = 3
        elif _meets(m, cnt, spec.t2_metric, spec.t2_count, spec.use_or_logic):
            tier = 2
        elif _meets(m, cnt, spec.t1_metric, spec.t1_count, spec.use_or_logic):
            tier = 1
        if tier:
            r["tier"] = tier
            tiered.append(r)

    tiered.sort(key=lambda x: (-x["tier"], -(x.get(spec.metric_field) or 0)))
    return tiered


# ──────────────────────────────────────────────────────────────────────────────
# rolling thresholds (agents 2 + 3 — iph by opd-bucket)
# ──────────────────────────────────────────────────────────────────────────────
def opd_bucket(opd: float) -> str:
    if opd is None:
        return "small"
    if opd < 500:
        return "small"
    if opd < 1500:
        return "medium"
    return "large"


def percentiles(values: List[float], pcts=(20, 50, 80)) -> Dict[str, float]:
    if not values:
        return {f"p{p}": 0.0 for p in pcts}
    s = sorted(values)
    out = {}
    for p in pcts:
        idx = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
        out[f"p{p}"] = s[idx]
    return out
