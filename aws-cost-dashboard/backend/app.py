"""
AWS Cost Dashboard Backend
Fetches cost and usage data from AWS Cost Explorer, grouped by project tag.
"""

import os
import json
from datetime import date, timedelta
from functools import lru_cache

import boto3
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TAG_KEY = os.environ.get("PROJECT_TAG_KEY", "Project")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

def get_ce_client():
    return boto3.client("ce", region_name="us-east-1")  # Cost Explorer is always us-east-1

def get_date_range(months_back=3):
    end = date.today().replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    # Go back N months
    y, m = end.year, end.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

@app.route("/api/summary")
def summary():
    """Total cost + breakdown by project tag for the last 3 months."""
    ce = get_ce_client()
    start, end = get_date_range(months_back=3)

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "TAG", "Key": TAG_KEY},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ],
    )

    projects = {}
    for result in resp["ResultsByTime"]:
        month = result["TimePeriod"]["Start"][:7]
        for group in result["Groups"]:
            tag_val = group["Keys"][0].replace(f"{TAG_KEY}$", "") or "(untagged)"
            service = group["Keys"][1]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost < 0.001:
                continue

            if tag_val not in projects:
                projects[tag_val] = {"total": 0.0, "services": {}, "monthly": {}}

            projects[tag_val]["total"] += cost
            projects[tag_val]["services"][service] = (
                projects[tag_val]["services"].get(service, 0.0) + cost
            )
            projects[tag_val]["monthly"].setdefault(month, 0.0)
            projects[tag_val]["monthly"][month] += cost

    # Sort services within each project
    for p in projects.values():
        p["services"] = dict(
            sorted(p["services"].items(), key=lambda x: x[1], reverse=True)
        )

    return jsonify({
        "tag_key": TAG_KEY,
        "period": {"start": start, "end": end},
        "projects": dict(sorted(projects.items(), key=lambda x: x[1]["total"], reverse=True)),
    })


@app.route("/api/trend")
def trend():
    """Daily cost trend for the last 30 days, grouped by project."""
    ce = get_ce_client()
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "TAG", "Key": TAG_KEY}],
    )

    series = {}
    dates = []
    for result in resp["ResultsByTime"]:
        day = result["TimePeriod"]["Start"]
        dates.append(day)
        for group in result["Groups"]:
            tag_val = group["Keys"][0].replace(f"{TAG_KEY}$", "") or "(untagged)"
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            series.setdefault(tag_val, []).append({"date": day, "cost": round(cost, 4)})

    return jsonify({"dates": dates, "series": series})


@app.route("/api/forecast")
def forecast():
    """Month-to-date actual + forecasted total for current month."""
    ce = get_ce_client()
    today = date.today()
    month_start = today.replace(day=1).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1).strftime("%Y-%m-%d")

    # MTD actual
    actual_resp = ce.get_cost_and_usage(
        TimePeriod={"Start": month_start, "End": today_str},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    mtd = float(actual_resp["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"])

    # Forecast remainder
    try:
        forecast_resp = ce.get_cost_forecast(
            TimePeriod={"Start": today_str, "End": month_end},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        forecast_total = float(forecast_resp["Total"]["Amount"])
    except Exception:
        forecast_total = None

    return jsonify({
        "month": today.strftime("%B %Y"),
        "mtd": round(mtd, 2),
        "forecast": round(forecast_total, 2) if forecast_total else None,
    })


@app.route("/api/services")
def services():
    """Top services by cost across all projects, last 30 days."""
    ce = get_ce_client()
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    services_map = {}
    for result in resp["ResultsByTime"]:
        for group in result["Groups"]:
            svc = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost >= 0.01:
                services_map[svc] = services_map.get(svc, 0.0) + cost

    sorted_svcs = dict(sorted(services_map.items(), key=lambda x: x[1], reverse=True))
    return jsonify({"services": sorted_svcs})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
