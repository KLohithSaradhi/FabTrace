"""As-of-safe operational analytics derived from reconciled order state."""

from fabtrace.analytics.customer_orders import (
    CUSTOMER_PROJECTION_VERSION,
    build_customer_order_projection,
)
from fabtrace.analytics.pipeline import (
    METRIC_VERSION,
    AnalyticsError,
    AnalyticsResult,
    analyze_orders,
)

__all__ = [
    "CUSTOMER_PROJECTION_VERSION",
    "METRIC_VERSION",
    "AnalyticsError",
    "AnalyticsResult",
    "analyze_orders",
    "build_customer_order_projection",
]
