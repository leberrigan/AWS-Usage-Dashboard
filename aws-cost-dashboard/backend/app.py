import os, json, threading, functools
from datetime import date, timedelta, datetime
from collections import defaultdict
import boto3
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TAG_KEY    = os.environ.get("PROJECT_TAG_KEY", "Project")
AM_BUCKET  = os.environ.get("AUDIOMOTH_BUCKET", "nighthawk-raw-audio")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
CACHE_FILE = "/tmp/audiomoth_cache.json"
ADMIN_USER = os.environ.get("DASHBOARD_USER", "admin")
ADMIN_PASS = os.environ.get("DASHBOARD_PASS", "changeme")

def ce(): return boto3.client("ce", region_name="us-east-1")
def s3(): return boto3.client("s3", region_name=REGION)

def period_start():
    d=date.today(); m,y=d.month-12,d.year
    if m<=0: m+=12; y-=1
    return f"{y}-{m:02d}-01"
def period_end(): return date.today().replace(day=1).strftime("%Y-%m-%d")
def today_str(): return date.today().strftime("%Y-%m-%d")
def thirty_days_ago(): return (date.today()-timedelta(days=30)).strftime("%Y-%m-%d")

# ── Basic Auth ────────────────────────────────────────────────────────────────

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                'Authentication required', 401,
                {'WWW-Authenticate': 'Basic realm="Nighthawk Admin"'})
        return f(*args, **kwargs)
    return decorated

@app.route("/api/auth/check")
def auth_check():
    auth = request.authorization
    if auth and check_auth(auth.username, auth.password):
        return jsonify({"authenticated": True})
    return jsonify({"authenticated": False}), 401

# ── Cost Explorer (protected) ─────────────────────────────────────────────────

@app.route("/api/summary")
@requires_auth
def summary():
    start,end=period_start(),period_end()
    r=ce().get_cost_and_usage(TimePeriod={"Start":start,"End":end},Granularity="MONTHLY",
        Metrics=["UnblendedCost"],GroupBy=[{"Type":"TAG","Key":TAG_KEY},{"Type":"DIMENSION","Key":"SERVICE"}])
    projects={}
    for result in r["ResultsByTime"]:
        month=result["TimePeriod"]["Start"][:7]
        for g in result["Groups"]:
            tag=g["Keys"][0].replace(f"{TAG_KEY}$","") or "(untagged)"
            svc=g["Keys"][1]; cost=float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost<0.001: continue
            if tag not in projects: projects[tag]={"total":0.0,"services":{},"monthly":{}}
            projects[tag]["total"]+=cost
            projects[tag]["services"][svc]=projects[tag]["services"].get(svc,0.0)+cost
            projects[tag]["monthly"].setdefault(month,0.0); projects[tag]["monthly"][month]+=cost
    for p in projects.values():
        p["services"]=dict(sorted(p["services"].items(),key=lambda x:x[1],reverse=True))
    return jsonify({"tag_key":TAG_KEY,"period":{"start":start,"end":end},
        "projects":dict(sorted(projects.items(),key=lambda x:x[1]["total"],reverse=True))})

@app.route("/api/trend")
@requires_auth
def trend():
    """
    Accepts ?range=7d|30d|3m|1y|all
    Uses DAILY granularity for <=60 days, MONTHLY for longer ranges.
    Returns unified {dates, series} where each date is the period start.
    """
    range_param = request.args.get("range", "30d")
    today = date.today()

    if range_param == "7d":
        start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        gran = "DAILY"
    elif range_param == "30d":
        start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        gran = "DAILY"
    elif range_param == "3m":
        start = (today - timedelta(days=91)).strftime("%Y-%m-%d")
        gran = "DAILY"
    elif range_param == "1y":
        m, y = today.month - 12, today.year
        if m <= 0: m += 12; y -= 1
        start = f"{y}-{m:02d}-01"
        gran = "MONTHLY"
    else:  # all
        start = "2024-06-01"
        gran = "MONTHLY"

    end = today_str() if gran == "DAILY" else period_end()

    r = ce().get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity=gran,
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "TAG", "Key": TAG_KEY}]
    )
    series = {}; dates = []
    for result in r["ResultsByTime"]:
        day = result["TimePeriod"]["Start"]; dates.append(day)
        for g in result["Groups"]:
            tag = g["Keys"][0].replace(f"{TAG_KEY}$", "") or "(untagged)"
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            series.setdefault(tag, []).append({"date": day, "cost": round(cost, 4)})
    return jsonify({"dates": dates, "series": series, "granularity": gran})

@app.route("/api/forecast")
@requires_auth
def forecast():
    today=date.today(); ms=today.replace(day=1).strftime("%Y-%m-%d"); ts=today_str()
    me=(today.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("%Y-%m-%d")
    ar=ce().get_cost_and_usage(TimePeriod={"Start":ms,"End":ts},Granularity="MONTHLY",Metrics=["UnblendedCost"])
    mtd=float(ar["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"])
    try:
        fr=ce().get_cost_forecast(TimePeriod={"Start":ts,"End":me},Metric="UNBLENDED_COST",Granularity="MONTHLY")
        fc=round(float(fr["Total"]["Amount"]),2)
    except: fc=None
    return jsonify({"month":today.strftime("%B %Y"),"mtd":round(mtd,2),"forecast":fc})

@app.route("/api/services")
@requires_auth
def services():
    start,end=thirty_days_ago(),today_str()
    r=ce().get_cost_and_usage(TimePeriod={"Start":start,"End":end},Granularity="MONTHLY",
        Metrics=["UnblendedCost"],GroupBy=[{"Type":"DIMENSION","Key":"SERVICE"}])
    svcs={}
    for result in r["ResultsByTime"]:
        for g in result["Groups"]:
            svc=g["Keys"][0]; cost=float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost>=0.01: svcs[svc]=svcs.get(svc,0.0)+cost
    return jsonify({"services":dict(sorted(svcs.items(),key=lambda x:x[1],reverse=True))})

# ── AudioMoth (public) ────────────────────────────────────────────────────────

_scan_running = False

def load_cache():
    try:
        with open(CACHE_FILE) as f: return json.load(f)
    except: return None

def save_cache(data):
    with open(CACHE_FILE,"w") as f: json.dump(data,f)

def run_scan():
    global _scan_running
    if _scan_running: return
    _scan_running = True
    try:
        client=s3()
        resp=client.list_objects_v2(Bucket=AM_BUCKET,Delimiter="/")
        units=[p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes",[])]
        daily_active=defaultdict(set); daily_volume=defaultdict(int)
        unit_data=[]; cutoff=(date.today()-timedelta(days=7)).strftime("%Y-%m-%d")
        for unit in units:
            dates=set(); size_by_date={}
            yr=client.list_objects_v2(Bucket=AM_BUCKET,Prefix=f"{unit}/",Delimiter="/")
            for yp in yr.get("CommonPrefixes",[]):
                year=yp["Prefix"].rstrip("/").split("/")[-1]
                mr=client.list_objects_v2(Bucket=AM_BUCKET,Prefix=yp["Prefix"],Delimiter="/")
                for mp in mr.get("CommonPrefixes",[]):
                    month=mp["Prefix"].rstrip("/").split("/")[-1]
                    dr=client.list_objects_v2(Bucket=AM_BUCKET,Prefix=mp["Prefix"],Delimiter="/")
                    for dp in dr.get("CommonPrefixes",[]):
                        day=dp["Prefix"].rstrip("/").split("/")[-1]
                        try:
                            ds=f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                            datetime.strptime(ds,"%Y-%m-%d")
                            dates.add(ds)
                            fp=client.list_objects_v2(Bucket=AM_BUCKET,Prefix=dp["Prefix"],MaxKeys=1000)
                            count=len(fp.get("Contents",[])); sz=sum(o["Size"] for o in fp.get("Contents",[]))
                            size_by_date[ds]={"count":count,"bytes":sz}
                        except ValueError: pass
            if not dates: continue
            monthly=defaultdict(lambda:{"count":0,"bytes":0})
            tf=0; tb=0
            for ds,info in size_by_date.items():
                mk=ds[:7]; monthly[mk]["count"]+=info["count"]; monthly[mk]["bytes"]+=info["bytes"]
                tf+=info["count"]; tb+=info["bytes"]
            for ds in dates:
                daily_active[ds].add(unit)
                daily_volume[ds]+=size_by_date.get(ds,{}).get("bytes",0)
            unit_data.append({"unit":unit,"first_seen":min(dates),"last_seen":max(dates),
                "total_files":tf,"total_bytes":tb,"active_days":len(dates),
                "monthly":dict(sorted({k:dict(v) for k,v in monthly.items()}.items()))})
        unit_data.sort(key=lambda x:x["last_seen"] or "",reverse=True)
        monthly_agg=defaultdict(lambda:{"units":set(),"files":0,"bytes":0})
        for u in unit_data:
            for mk,m in u["monthly"].items():
                monthly_agg[mk]["units"].add(u["unit"])
                monthly_agg[mk]["files"]+=m["count"]; monthly_agg[mk]["bytes"]+=m["bytes"]
        all_dates=sorted(daily_active.keys())
        currently_active=sum(1 for u in unit_data if u["last_seen"]>=cutoff)
        cache={
            "scanned_at": datetime.utcnow().isoformat(),
            "overview":{"total_units":len(unit_data),"currently_active":currently_active,
                "total_bytes":sum(u["total_bytes"] for u in unit_data),
                "total_files":sum(u["total_files"] for u in unit_data),
                "daily_series":[{"date":d,"active_units":len(daily_active[d]),"volume_bytes":daily_volume[d]} for d in all_dates]},
            "units": unit_data,
            "monthly":[{"month":k,"active_units":len(v["units"]),"total_files":v["files"],"total_bytes":v["bytes"]}
                for k,v in sorted(monthly_agg.items())]
        }
        save_cache(cache)
    finally:
        _scan_running = False

def get_cache_or_trigger():
    cache=load_cache()
    if cache is None:
        threading.Thread(target=run_scan,daemon=True).start()
        return None
    return cache

@app.route("/api/audiomoth/status")
def audiomoth_status():
    cache=load_cache()
    if cache: return jsonify({"status":"ready","scanned_at":cache.get("scanned_at")})
    if _scan_running: return jsonify({"status":"scanning"})
    return jsonify({"status":"not_started"})

@app.route("/api/audiomoth/scan", methods=["POST"])
@requires_auth
def audiomoth_scan():
    threading.Thread(target=run_scan,daemon=True).start()
    return jsonify({"status":"scan_started"})

@app.route("/api/audiomoth/overview")
def audiomoth_overview():
    cache=get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}),202
    return jsonify(cache["overview"])

@app.route("/api/audiomoth/units")
def audiomoth_units():
    cache=get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}),202
    return jsonify({"units":cache["units"]})

@app.route("/api/audiomoth/monthly")
def audiomoth_monthly():
    cache=get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}),202
    return jsonify({"monthly":cache["monthly"]})

@app.route("/health")
def health(): return jsonify({"status":"ok"})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000)); app.run(host="0.0.0.0",port=port,debug=False)