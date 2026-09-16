"""
Cost insight and recommendation logic. Everything here works off aggregate
SQL queries against cur_line_items — no full-table pandas loads, so it stays
reasonably fast even with millions of rows.
"""
import datetime as _dt
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models import CurLineItem

def _base_query(db: Session, upload_id=None):
    q = db.query(CurLineItem)
    if upload_id:
        q = q.filter(CurLineItem.upload_id == upload_id)
    return q

def get_summary(db: Session, upload_id=None):
    q = _base_query(db, upload_id)
    total_cost = q.with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar()
    total_rows = q.with_entities(func.count(CurLineItem.id)).scalar()
    service_count = q.with_entities(func.count(func.distinct(CurLineItem.product_code))).scalar()
    account_count = q.with_entities(func.count(func.distinct(CurLineItem.usage_account_id))).scalar()
    date_range = q.with_entities(
        func.min(CurLineItem.usage_start_date), func.max(CurLineItem.usage_end_date)
    ).first()

    credits = q.filter(CurLineItem.line_item_type.in_(["Credit", "Refund"])) \
        .with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar()
    tax = q.filter(CurLineItem.line_item_type == "Tax") \
        .with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar()

    return {
        "total_cost": round(float(total_cost or 0), 2),
        "total_rows": total_rows,
        "distinct_services": service_count,
        "distinct_accounts": account_count,
        "period_start": date_range[0].isoformat() if date_range and date_range[0] else None,
        "period_end": date_range[1].isoformat() if date_range and date_range[1] else None,
        "credits_and_refunds": round(float(credits or 0), 2),
        "tax": round(float(tax or 0), 2),
    }

def get_cost_by_service(db: Session, upload_id=None, limit=15):
    q = _base_query(db, upload_id).with_entities(
        CurLineItem.product_code,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.product_code).order_by(func.sum(CurLineItem.unblended_cost).desc()).limit(limit)

    rows = q.all()
    total = _base_query(db, upload_id).with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar() or 1
    return [
        {
            "service": r[0] or "Unknown",
            "cost": round(float(r[1]), 2),
            "pct_of_total": round(float(r[1]) / float(total) * 100, 2),
        }
        for r in rows
    ]

def get_cost_by_account(db: Session, upload_id=None):
    q = _base_query(db, upload_id).with_entities(
        CurLineItem.usage_account_id,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.usage_account_id).order_by(func.sum(CurLineItem.unblended_cost).desc())
    return [{"account_id": r[0] or "Unknown", "cost": round(float(r[1]), 2)} for r in q.all()]

def get_cost_by_region(db: Session, upload_id=None):
    q = _base_query(db, upload_id).with_entities(
        CurLineItem.region,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.region).order_by(func.sum(CurLineItem.unblended_cost).desc())
    return [{"region": r[0] or "Global/Unspecified", "cost": round(float(r[1]), 2)} for r in q.all()]

def get_daily_trend(db: Session, upload_id=None):
    day = func.date_trunc("day", CurLineItem.usage_start_date).label("day")
    q = _base_query(db, upload_id).filter(CurLineItem.usage_start_date.isnot(None)).with_entities(
        day, func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost")
    ).group_by(day).order_by(day)
    return [{"date": r[0].date().isoformat(), "cost": round(float(r[1]), 2)} for r in q.all()]

def get_cost_by_line_item_type(db: Session, upload_id=None):
    q = _base_query(db, upload_id).with_entities(
        CurLineItem.line_item_type,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.line_item_type).order_by(func.sum(CurLineItem.unblended_cost).desc())
    return [{"type": r[0] or "Unknown", "cost": round(float(r[1]), 2)} for r in q.all()]

def get_top_resources(db: Session, upload_id=None, limit=20):
    q = _base_query(db, upload_id).filter(CurLineItem.resource_id.isnot(None)).with_entities(
        CurLineItem.resource_id,
        CurLineItem.product_code,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.resource_id, CurLineItem.product_code) \
     .order_by(func.sum(CurLineItem.unblended_cost).desc()).limit(limit)
    return [
        {"resource_id": r[0], "service": r[1] or "Unknown", "cost": round(float(r[2]), 2)}
        for r in q.all()
    ]

def get_top_usage_types(db: Session, upload_id=None, limit=2000):
    q = _base_query(db, upload_id).with_entities(
        CurLineItem.usage_type,
        CurLineItem.product_code,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.usage_type, CurLineItem.product_code) \
     .order_by(func.sum(CurLineItem.unblended_cost).desc()).limit(limit)
    return [
        {"usage_type": (r[0] or "Unknown"), "service": (r[1] or "Unknown"), "cost": round(float(r[2]), 2)}
        for r in q.all()
    ]

def get_commitment_coverage(db: Session, upload_id=None, services=("AmazonEC2", "AWSLambda", "AmazonECS")):
    """
    Cost broken down by line_item_type, restricted to commitment-eligible compute
    services. Uses CUR's own line_item_type field (Usage / DiscountedUsage /
    SavingsPlanCoveredUsage / SavingsPlanNegation / RIFee / ...) rather than
    guessing from usage_type text — this is the authoritative signal for whether
    a dollar of spend was On-Demand or already covered by a commitment.
    """
    q = _base_query(db, upload_id).filter(CurLineItem.product_code.in_(services)).with_entities(
        CurLineItem.line_item_type,
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0).label("cost"),
    ).group_by(CurLineItem.line_item_type)
    return {(r[0] or "Unknown"): float(r[1]) for r in q.all()}

def get_resource_attribution(db: Session, upload_id=None):
    """How much cost can be traced to a specific resource ID vs not (a proxy
    for tagging/chargeback readiness)."""
    q = _base_query(db, upload_id)
    total = q.with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar() or 0
    with_id = q.filter(CurLineItem.resource_id.isnot(None), CurLineItem.resource_id != "") \
        .with_entities(func.coalesce(func.sum(CurLineItem.unblended_cost), 0)).scalar() or 0
    return {"total": float(total), "with_resource_id": float(with_id)}

def _cost_matching(usage_types, service=None, any_of=(), all_of=(), none_of=()):
    """Sum cost from get_top_usage_types() rows matching simple text filters
    on (product_code, usage_type). Keeps individual rules short and readable."""
    total = 0.0
    for u in usage_types:
        if service is not None and u["service"] != service:
            continue
        ut = u["usage_type"].lower()
        if any_of and not any(k in ut for k in any_of):
            continue
        if all_of and not all(k in ut for k in all_of):
            continue
        if none_of and any(k in ut for k in none_of):
            continue
        total += u["cost"]
    return total

def _rec(title, severity, impact_cost, impact_pct, finding, recommendation):
    return {
        "title": title,
        "severity": severity,
        "impact_cost": round(impact_cost, 2),
        "impact_pct": round(abs(impact_pct), 1),
        "finding": finding,
        "recommendation": recommendation,
    }

def _rule_ec2_low_commitment(ctx):
    ec2_ondemand = _cost_matching(ctx["usage_types"], service="AmazonEC2", all_of=("boxusage",))
    ec2_discounted = _cost_matching(ctx["usage_types"], service="AmazonEC2", any_of=("reserved", "spot"))
    if ec2_ondemand > 0 and ctx["pct"](ec2_ondemand) >= 10 and ec2_discounted < ec2_ondemand * 0.3:
        p = ctx["pct"](ec2_ondemand)
        return _rec(
            "Low EC2 commitment coverage", "high" if p >= 25 else "medium",
            ec2_ondemand, p,
            f"On-Demand EC2 usage is ${ec2_ondemand:,.2f} ({p}% of total) with little evidence of "
            "Reserved Instance / Savings Plan / Spot coverage.",
            "For steady-state workloads, evaluate Compute Savings Plans or Reserved Instances "
            "(typically 20-40%+ savings vs On-Demand). For fault-tolerant/batch workloads, evaluate Spot.",
        )
    return None

def _rule_ebs_gp2_to_gp3(ctx):
    gp2 = _cost_matching(ctx["usage_types"], any_of=("volumeusage.gp2",))
    gp3 = _cost_matching(ctx["usage_types"], any_of=("volumeusage.gp3",))
    if gp2 > 0 and ctx["pct"](gp2) >= 2:
        p = ctx["pct"](gp2)
        return _rec(
            "EBS volumes on gp2 instead of gp3", "medium" if p >= 8 else "low",
            gp2, p,
            f"${gp2:,.2f} ({p}% of spend) is on gp2 EBS volumes"
            + (f", vs ${gp3:,.2f} already on gp3." if gp3 else ", with no gp3 usage detected."),
            "gp3 is ~20% cheaper per-GB than gp2 at the same baseline performance, and lets you tune "
            "IOPS/throughput independently of size. Migrating gp2 to gp3 is a live, no-downtime operation "
            "via the console or 'aws ec2 modify-volume'.",
        )
    return None

def _rule_data_transfer(ctx):
    dt_cost = _cost_matching(ctx["usage_types"], any_of=("datatransfer", "dataxfer"))
    if dt_cost > 0 and ctx["pct"](dt_cost) >= 5:
        p = ctx["pct"](dt_cost)
        return _rec(
            "High data transfer costs", "high" if p >= 15 else "medium",
            dt_cost, p,
            f"Data transfer line items account for {p}% (${dt_cost:,.2f}) of total spend.",
            "Review cross-AZ / inter-region / internet egress traffic. Consider CloudFront for public "
            "content, VPC endpoints (Gateway/Interface) to avoid NAT/internet routing for S3/DynamoDB/"
            "other AWS service traffic, and co-locating chatty services in the same AZ.",
        )
    return None

def _rule_nat_gateway(ctx):
    nat_cost = _cost_matching(ctx["usage_types"], any_of=("natgateway",))
    if nat_cost > 0 and ctx["pct"](nat_cost) >= 2:
        return _rec(
            "NAT Gateway spend", "medium",
            nat_cost, ctx["pct"](nat_cost),
            f"NAT Gateway usage is ${nat_cost:,.2f} ({ctx['pct'](nat_cost)}% of total).",
            "Add VPC Gateway/Interface endpoints for S3, DynamoDB, and other frequently-used AWS services "
            "to bypass NAT for that traffic. Consolidate NAT Gateways per-AZ instead of per-subnet where "
            "redundancy requirements allow.",
        )
    return None

def _rule_cost_spikes(ctx):
    trend = ctx["trend"]
    total = ctx["total"]
    spikes = []
    for i in range(1, len(trend)):
        prev, cur = trend[i - 1]["cost"], trend[i]["cost"]
        if prev > 0 and (cur - prev) / prev >= 0.5 and (cur - prev) >= total * 0.03:
            spikes.append((trend[i]["date"], prev, cur))
    if not spikes:
        return None
    # Surface the single largest spike as its own recommendation; the trend
    # chart shows the rest visually.
    date, prev, cur = max(spikes, key=lambda s: s[2] - s[1])
    return _rec(
        f"Cost spike on {date}", "high",
        cur - prev, ctx["pct"](cur - prev),
        f"Daily spend jumped from ${prev:,.2f} to ${cur:,.2f} ({(cur-prev)/prev*100:.0f}% increase) on {date}.",
        "Investigate what changed on this date — new resources launched, a traffic spike, misconfigured "
        "autoscaling, or an accidental large job/backfill."
        + (f" {len(spikes)-1} other day(s) also spiked this period — see the trend chart." if len(spikes) > 1 else ""),
    )

# Open Source edition: five intentionally exposed recommendations.
RULES = [
    _rule_ec2_low_commitment,
    _rule_ebs_gp2_to_gp3,
    _rule_data_transfer,
    _rule_nat_gateway,
    _rule_cost_spikes,
]


def generate_recommendations(db: Session, upload_id=None):
    total = _base_query(db, upload_id).with_entities(
        func.coalesce(func.sum(CurLineItem.unblended_cost), 0)
    ).scalar() or 0
    if total == 0:
        return []

    ctx = {
        "total": total,
        "pct": lambda cost: round(cost / total * 100, 1),
        "usage_types": get_top_usage_types(db, upload_id, limit=5000),
        "by_service": get_cost_by_service(db, upload_id, limit=50),
        "by_account": get_cost_by_account(db, upload_id),
        "by_type": get_cost_by_line_item_type(db, upload_id),
        "trend": get_daily_trend(db, upload_id),
        "coverage": get_commitment_coverage(db, upload_id),
        "resource_attribution": get_resource_attribution(db, upload_id),
    }

    recs = []
    for rule in RULES:
        try:
            result = rule(ctx)
        except Exception:
            # A single rule failing (e.g. unexpected data shape) shouldn't
            # take down the whole recommendations panel.
            continue
        if result:
            recs.append(result)

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    recs.sort(key=lambda r: (order.get(r["severity"], 9), -abs(r["impact_cost"])))
    return recs
