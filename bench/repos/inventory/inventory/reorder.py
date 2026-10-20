import math


def reorder_point(avg_daily_demand: float, lead_time_days: int, safety_days: int = 0) -> int:
    """Units on hand at which to reorder: demand over the lead time plus safety stock,
    rounded up to whole units."""
    return math.ceil(avg_daily_demand * (lead_time_days + safety_days))


def needs_reorder(on_hand: int, avg_daily_demand: float, lead_time_days: int,
                  safety_days: int = 0) -> bool:
    return on_hand <= reorder_point(avg_daily_demand, lead_time_days, safety_days)
