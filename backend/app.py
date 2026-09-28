import os, json, threading, functools, re, time
from datetime import date, timedelta, datetime
from collections import defaultdict
import boto3
import requests
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
# Flask's jsonify() alphabetises dict keys by default, which silently undoes
# our cost-descending `sorted(...)` calls below (e.g. summary()/services()) —
# "Top Project" and "Top Services" would end up picking whatever project/
# service name sorts first alphabetically instead of the highest-cost one.
app.json.sort_keys = False

TAG_KEY    = os.environ.get("PROJECT_TAG_KEY", "Project")
AM_BUCKET  = os.environ.get("AUDIOMOTH_BUCKET", "nighthawk-raw-audio")
REGION     = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
CACHE_FILE = "/tmp/audiomoth_cache.json"
USER_MGMT_URL = os.environ.get("USER_MGMT_URL", "http://127.0.0.1:5004")
TOOL_SLUG  = "aws-costs"

def ce(): return boto3.client("ce", region_name="us-east-1")
def s3(): return boto3.client("s3", region_name=REGION)

def period_start():
    d=date.today(); m,y=d.month-12,d.year
    if m<=0: m+=12; y-=1
    return f"{y}-{m:02d}-01"
def period_end(): return date.today().replace(day=1).strftime("%Y-%m-%d")
def next_month_start():
    d=date.today()
    if d.month==12: return f"{d.year+1}-01-01"
    return f"{d.year}-{d.month+1:02d}-01"
def today_str(): return date.today().strftime("%Y-%m-%d")
def thirty_days_ago(): return (date.today()-timedelta(days=30)).strftime("%Y-%m-%d")

# ── Centralized auth ──────────────────────────────────────────────────────────
# oauth2-proxy sits in front of nginx for the whole host and handles sign-in;
# by the time a request reaches us, nginx has set X-Auth-Request-Email for a
# verified identity. What we check ourselves is authorization: is this email
# allowed to use aws-costs, and with what role. That decision lives in the
# shared User Management service (Baserow-backed), not in this app.

_access_cache = {}   # email -> (expires_at, allowed, role, name)
_CACHE_TTL = 30       # seconds; matches Asset Management's auth.py

def _check_access(email):
    now = time.time()
    cached = _access_cache.get(email)
    if cached and cached[0] > now:
        return cached[1], cached[2], cached[3]
    try:
        r = requests.get(f"{USER_MGMT_URL}/internal/check",
                          params={"email": email, "tool": TOOL_SLUG}, timeout=5)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        # Fail closed, and don't cache the failure — a transient outage of
        # the user-management service should self-heal on the next request
        # rather than locking everyone out for the full cache TTL.
        return False, None, None
    allowed, role, name = data.get("allowed", False), data.get("role"), data.get("name")
    _access_cache[email] = (now + _CACHE_TTL, allowed, role, name)
    return allowed, role, name

def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        email = request.headers.get("X-Auth-Request-Email")
        if not email:
            return jsonify({"error": "Not signed in"}), 401
        allowed, role, _ = _check_access(email)
        if not allowed or role != "admin":
            return jsonify({"error": "Not authorized for this tool"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route("/api/auth/check")
def auth_check():
    email = request.headers.get("X-Auth-Request-Email")
    if not email:
        return jsonify({"authenticated": False, "name": None})
    allowed, role, name = _check_access(email)
    return jsonify({"authenticated": bool(allowed and role == "admin"), "name": name})

# ── FLAC metadata parsing ─────────────────────────────────────────────────────

def parse_flac_tags(data):
    """Extract Vorbis comment tags from raw FLAC binary data (no external deps)."""
    if len(data) < 4 or data[:4] != b'fLaC':
        return {}
    pos, tags = 4, {}
    while pos < len(data) - 4:
        header = data[pos:pos+4]
        if len(header) < 4: break
        block_type = header[0] & 0x7F
        is_last    = bool(header[0] & 0x80)
        block_len  = (header[1] << 16) | (header[2] << 8) | header[3]
        pos += 4
        if pos + block_len > len(data): break
        if block_type == 4:  # VORBIS_COMMENT
            block = data[pos:pos+block_len]
            try:
                bp = 0
                vl = int.from_bytes(block[bp:bp+4], 'little'); bp += 4 + vl
                if bp + 4 > len(block): pass
                else:
                    nc = int.from_bytes(block[bp:bp+4], 'little'); bp += 4
                    for _ in range(nc):
                        if bp + 4 > len(block): break
                        cl = int.from_bytes(block[bp:bp+4], 'little'); bp += 4
                        if bp + cl > len(block): break
                        c = block[bp:bp+cl].decode('utf-8', errors='ignore'); bp += cl
                        if '=' in c:
                            k, v = c.split('=', 1)
                            tags[k.upper().strip()] = v.strip()
            except Exception:
                pass
        pos += block_len
        if is_last: break
    return tags

def extract_unit_metadata(tags):
    """Return (lat, lon, am_serno, pi_serno) from parsed FLAC Vorbis tags.
    Tag names may include literal angle brackets, e.g. <LAT>, so we normalise
    by stripping <> before matching."""
    norm = {k.strip('<>'):v for k,v in tags.items()}
    lat = lon = am_serno = pi_serno = None
    for k in ['LAT', 'LATITUDE', 'GPS_LATITUDE']:
        if k in norm:
            try: lat = float(norm[k]); break
            except: pass
    for k in ['LON', 'LONG', 'LNG', 'LONGITUDE', 'GPS_LONGITUDE']:
        if k in norm:
            try: lon = float(norm[k]); break
            except: pass
    am_serno = norm.get('AM_SERNO') or norm.get('AUDIOMOTH_SERNO')
    pi_serno = norm.get('PI_SERNO') or norm.get('RASPI_SERNO')
    return lat, lon, am_serno, pi_serno

def get_unit_metadata(s3_client, key):
    """Download first 64 KB of an S3 FLAC and return (lat, lon, am_serno, pi_serno)."""
    try:
        resp = s3_client.get_object(Bucket=AM_BUCKET, Key=key, Range="bytes=0-65535")
        data = resp["Body"].read()
        return extract_unit_metadata(parse_flac_tags(data))
    except Exception:
        return None, None, None, None

# ── Cost Explorer helpers ─────────────────────────────────────────────────────

def _ce_query_paginated(start, end, gran, group_by):
    """Run a paginated GetCostAndUsage and return all ResultsByTime entries."""
    results = []
    kwargs = dict(TimePeriod={"Start": start, "End": end},
                  Granularity=gran, Metrics=["UnblendedCost"], GroupBy=group_by)
    while True:
        r = ce().get_cost_and_usage(**kwargs)
        results.extend(r["ResultsByTime"])
        if "NextPageToken" not in r:
            break
        kwargs["NextPageToken"] = r["NextPageToken"]
    return results

def _ce_query_all(end, gran, group_by):
    """Query from the furthest-back date CE allows, handling the retention
    limit automatically whether it is 14, 38, or any other number of months.
    Strategy: try from 2006-01-01; if CE rejects it with a ValidationException
    it tells us the limit in the error message (e.g. '14 months').  We then
    back off by (limit - 1) months so we stay comfortably inside the window."""
    
    d = date.today()
    # Subtract 1 so we land one full month inside the allowed window
    months_back = 37
    mo, yr = d.month - months_back, d.year
    while mo <= 0: mo += 12; yr -= 1
    try:
        return _ce_query_paginated(f"{yr}-{mo:02d}-01", end, gran, group_by)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ValidationException":
            raise
        months_back = 13
        mo, yr = d.month - months_back, d.year
        while mo <= 0: mo += 12; yr -= 1
        return _ce_query_paginated(f"{yr}-{mo:02d}-01", end, gran, group_by)

def _ce_query_paginated_filtered(start, end, gran, filter_def):
    """Like _ce_query_paginated but with an optional Filter (None = no filter, returns total)."""
    results = []
    kwargs = dict(TimePeriod={"Start": start, "End": end},
                  Granularity=gran, Metrics=["UnblendedCost"])
    if filter_def is not None:
        kwargs["Filter"] = filter_def
    while True:
        r = ce().get_cost_and_usage(**kwargs)
        results.extend(r["ResultsByTime"])
        if "NextPageToken" not in r:
            break
        kwargs["NextPageToken"] = r["NextPageToken"]
    return results

def _ce_query_all_filtered(end, gran, filter_def):
    """Like _ce_query_all but with a Filter instead of GroupBy."""
    d = date.today()
    months_back = 37
    mo, yr = d.month - months_back, d.year
    while mo <= 0: mo += 12; yr -= 1
    try:
        return _ce_query_paginated_filtered(f"{yr}-{mo:02d}-01", end, gran, filter_def)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ValidationException":
            raise
        months_back = 13
        mo, yr = d.month - months_back, d.year
        while mo <= 0: mo += 12; yr -= 1
        return _ce_query_paginated_filtered(f"{yr}-{mo:02d}-01", end, gran, filter_def)

# ── Cost Explorer (protected) ─────────────────────────────────────────────────

@app.route("/api/summary")
@requires_auth
def summary():
    start,end=period_start(),period_end()
    # Must paginate: GroupBy TAG×SERVICE over 12 months produces a group per
    # (month, project, service) combo, which for a project touching many
    # services easily exceeds one page — an un-paginated call here silently
    # truncates that project's totals (and always the same way, since results
    # come back in the same order each time), understating it.
    results=_ce_query_paginated(start,end,"MONTHLY",
        [{"Type":"TAG","Key":TAG_KEY},{"Type":"DIMENSION","Key":"SERVICE"}])
    projects={}
    for result in results:
        month=result["TimePeriod"]["Start"][:7]
        for g in result["Groups"]:
            tag=g["Keys"][0].replace(f"{TAG_KEY}$","") or "ampi"
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
    range_param = request.args.get("range", "30d")
    group_param = request.args.get("group", "project")  # "project" or "service"
    today = date.today()
    if range_param == "7d":
        start = (today - timedelta(days=7)).strftime("%Y-%m-%d"); gran = "DAILY"
    elif range_param == "30d":
        start = (today - timedelta(days=30)).strftime("%Y-%m-%d"); gran = "DAILY"
    elif range_param == "3m":
        start = (today - timedelta(days=91)).strftime("%Y-%m-%d"); gran = "DAILY"
    elif range_param == "1y":
        m, y = today.month - 12, today.year
        if m <= 0: m += 12; y -= 1
        start = f"{y}-{m:02d}-01"; gran = "MONTHLY"
    else:  # all — probe CE to find the actual retention boundary
        gran = "MONTHLY"
        start = None
    end = today_str() if gran == "DAILY" else period_end()
    if group_param == "service":
        group_by = [{"Type": "DIMENSION", "Key": "SERVICE"}]
    else:
        group_by = [{"Type": "TAG", "Key": TAG_KEY}]
    if range_param == "all":
        all_results = _ce_query_all(end, gran, group_by)
    else:
        all_results = _ce_query_paginated(start, end, gran, group_by)
    totals = defaultdict(lambda: defaultdict(float)); dates = []
    for result in all_results:
        day = result["TimePeriod"]["Start"]; dates.append(day)
        for g in result["Groups"]:
            if group_param == "service":
                name = g["Keys"][0]
            else:
                # Untagged resources come back as their own group (key "Project$",
                # empty value) alongside any explicitly-tagged "ampi" group for the
                # same day — both normalise to "ampi" here, so sum them per day
                # rather than keeping separate points, or one silently overwrites
                # the other on the frontend.
                name = g["Keys"][0].replace(f"{TAG_KEY}$", "") or "ampi"
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            totals[name][day] += cost
    series = {name: [{"date": day, "cost": round(cost, 4)} for day, cost in sorted(by_day.items())]
              for name, by_day in totals.items()}
    return jsonify({"dates": dates, "series": series, "granularity": gran, "group": group_param})

@app.route("/api/forecast")
@requires_auth
def forecast():
    today=date.today(); ms=today.replace(day=1).strftime("%Y-%m-%d"); ts=today_str()
    me=(today.replace(day=28)+timedelta(days=4)).replace(day=1).strftime("%Y-%m-%d")
    # CE requires Start < End; on the 1st of the month ms == ts, so MTD is 0
    if ms < ts:
        ar=ce().get_cost_and_usage(TimePeriod={"Start":ms,"End":ts},Granularity="MONTHLY",Metrics=["UnblendedCost"])
        mtd=float(ar["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"])
    else:
        mtd=0.0
    try:
        fr=ce().get_cost_forecast(TimePeriod={"Start":ts,"End":me},Metric="UNBLENDED_COST",Granularity="MONTHLY")
        fc=round(float(fr["Total"]["Amount"]),2)
    except: fc=None
    return jsonify({"month":today.strftime("%B %Y"),"mtd":round(mtd,2),"forecast":fc})

@app.route("/api/services")
@requires_auth
def services():
    start,end=thirty_days_ago(),today_str()
    results=_ce_query_paginated(start,end,"MONTHLY",[{"Type":"DIMENSION","Key":"SERVICE"}])
    svcs={}
    for result in results:
        for g in result["Groups"]:
            svc=g["Keys"][0]; cost=float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost>=0.01: svcs[svc]=svcs.get(svc,0.0)+cost
    return jsonify({"services":dict(sorted(svcs.items(),key=lambda x:x[1],reverse=True))})

@app.route("/api/projects")
@requires_auth
def projects():
    start, end = thirty_days_ago(), today_str()
    results = _ce_query_paginated(start, end, "MONTHLY", [{"Type": "TAG", "Key": TAG_KEY}])
    projs = {}
    for result in results:
        for g in result["Groups"]:
            tag = g["Keys"][0].replace(f"{TAG_KEY}$", "") or "ampi"
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if cost >= 0.01:
                projs[tag] = projs.get(tag, 0.0) + cost
    return jsonify({"projects": dict(sorted(projs.items(), key=lambda x: x[1], reverse=True))})

@app.route("/api/ampi/costs")
@requires_auth
def ampi_costs():
    # Costs tagged 'ampi' OR with no Project tag (all pre-tagging history is untagged)
    ampi_filter = {"Or": [
        {"Tags": {"Key": TAG_KEY, "Values": ["ampi"]}},
        {"Tags": {"Key": TAG_KEY, "MatchOptions": ["ABSENT"]}}
    ]}
    today = date.today()

    monthly_results = []
    try:
        monthly_results = _ce_query_all_filtered(period_end(), "MONTHLY", ampi_filter)
    except Exception:
        pass

    daily_results = []
    try:
        start_3m = (today - timedelta(days=91)).strftime("%Y-%m-%d")
        daily_results = _ce_query_paginated_filtered(start_3m, today_str(), "DAILY", ampi_filter)
    except Exception:
        pass

    monthly_costs = {}
    for result in monthly_results:
        month = result["TimePeriod"]["Start"][:7]
        cost = float(result["Total"]["UnblendedCost"]["Amount"])
        if cost > 0.001:
            monthly_costs[month] = monthly_costs.get(month, 0) + cost

    total_cost = sum(monthly_costs.values())
    d3m = (today - timedelta(days=91)).strftime("%Y-%m")
    three_month_cost = sum(v for k, v in monthly_costs.items() if k >= d3m)

    # Active-unit counts: cache["monthly"][].active_units is a union of every
    # unit that uploaded *at all* during that month, which saturates near the
    # full fleet size for any completed month and looks constant month-to-month.
    # Use the true per-day active-unit counts instead (same source the
    # "AudioMoth units" overlay on the project/service trend uses), taking the
    # day's own count for the daily series and the average across the month's
    # days for the monthly series.
    cache = load_cache()
    daily_active_units = {d["date"]: d["active_units"]
                           for d in cache.get("overview", {}).get("daily_series", [])} if cache else {}

    def avg_active_units(month_key):
        vals = [v for d, v in daily_active_units.items() if d.startswith(month_key)]
        return round(sum(vals) / len(vals)) if vals else 0

    daily_series = []
    for result in daily_results:
        day = result["TimePeriod"]["Start"]
        cost = float(result["Total"]["UnblendedCost"]["Amount"])
        stations = daily_active_units.get(day, 0)
        cps = round(cost / stations, 6) if stations > 0 else None
        daily_series.append({"date": day, "cost": round(cost, 4),
                              "active_stations": stations, "cost_per_station": cps})

    monthly_series = []
    for k, v in sorted(monthly_costs.items()):
        stations = avg_active_units(k)
        monthly_series.append({"month": k, "cost": round(v, 4),
                                "active_stations": stations,
                                "cost_per_station": round(v / stations, 4) if stations > 0 else None})
    return jsonify({
        "total_cost": round(total_cost, 2),
        "three_month_cost": round(three_month_cost, 2),
        "daily": daily_series,
        "monthly": monthly_series
    })

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
        client = s3()
        resp = client.list_objects_v2(Bucket=AM_BUCKET, Delimiter="/")
        units = [p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes",[])]
        daily_active = defaultdict(set); daily_volume = defaultdict(int)
        unit_data = []; cutoff = (date.today()-timedelta(days=7)).strftime("%Y-%m-%d")
        for unit in units:
            dates = set(); size_by_date = {}; gps_candidates = []  # (date, key) recent FLACs
            yr = client.list_objects_v2(Bucket=AM_BUCKET, Prefix=f"{unit}/", Delimiter="/")
            for yp in yr.get("CommonPrefixes",[]):
                year = yp["Prefix"].rstrip("/").split("/")[-1]
                mr = client.list_objects_v2(Bucket=AM_BUCKET, Prefix=yp["Prefix"], Delimiter="/")
                for mp in mr.get("CommonPrefixes",[]):
                    month = mp["Prefix"].rstrip("/").split("/")[-1]
                    dr = client.list_objects_v2(Bucket=AM_BUCKET, Prefix=mp["Prefix"], Delimiter="/")
                    for dp in dr.get("CommonPrefixes",[]):
                        day = dp["Prefix"].rstrip("/").split("/")[-1]
                        try:
                            ds = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                            datetime.strptime(ds, "%Y-%m-%d")
                            dates.add(ds)
                            fp = client.list_objects_v2(Bucket=AM_BUCKET, Prefix=dp["Prefix"], MaxKeys=1000)
                            contents = fp.get("Contents",[])
                            count = len(contents)
                            sz = sum(o["Size"] for o in contents)
                            size_by_date[ds] = {"count": count, "bytes": sz}
                            # Collect recent FLAC candidates (keep 5 newest dates)
                            for obj in contents:
                                if obj["Key"].upper().endswith(".FLAC"):
                                    gps_candidates.append((ds, obj["Key"]))
                                    gps_candidates.sort(key=lambda x: x[0], reverse=True)
                                    gps_candidates = gps_candidates[:5]
                                    break
                        except ValueError: pass
            if not dates: continue
            monthly = defaultdict(lambda: {"count":0,"bytes":0})
            tf = tb = 0
            daily_list = []
            for ds in sorted(dates):
                info = size_by_date.get(ds, {"count":0,"bytes":0})
                daily_list.append({"date":ds,"count":info["count"],"bytes":info["bytes"]})
                mk = ds[:7]
                monthly[mk]["count"] += info["count"]; monthly[mk]["bytes"] += info["bytes"]
                tf += info["count"]; tb += info["bytes"]
                daily_active[ds].add(unit); daily_volume[ds] += info["bytes"]
            # Try recent FLACs newest-first until GPS coordinates are found
            lat = lon = am_serno = pi_serno = None
            for _, ckey in gps_candidates:
                lat, lon, am_serno, pi_serno = get_unit_metadata(client, ckey)
                if lat is not None and lon is not None:
                    break
            unit_data.append({
                "unit": unit, "first_seen": min(dates), "last_seen": max(dates),
                "total_files": tf, "total_bytes": tb, "active_days": len(dates),
                "monthly": dict(sorted({k:dict(v) for k,v in monthly.items()}.items())),
                "daily": daily_list,
                "lat": lat, "lon": lon,
                "am_serno": am_serno, "pi_serno": pi_serno
            })
        unit_data.sort(key=lambda x: x["last_seen"] or "", reverse=True)
        monthly_agg = defaultdict(lambda: {"units":set(),"files":0,"bytes":0})
        for u in unit_data:
            for mk, m in u["monthly"].items():
                monthly_agg[mk]["units"].add(u["unit"])
                monthly_agg[mk]["files"] += m["count"]; monthly_agg[mk]["bytes"] += m["bytes"]
        all_dates = sorted(daily_active.keys())
        currently_active = sum(1 for u in unit_data if u["last_seen"] >= cutoff)
        cache = {
            "scanned_at": datetime.utcnow().isoformat(),
            "overview": {
                "total_units": len(unit_data), "currently_active": currently_active,
                "total_bytes": sum(u["total_bytes"] for u in unit_data),
                "total_files": sum(u["total_files"] for u in unit_data),
                "daily_series": [{"date":d,"active_units":len(daily_active[d]),"volume_bytes":daily_volume[d]} for d in all_dates]
            },
            "units": unit_data,
            "monthly": [{"month":k,"active_units":len(v["units"]),"total_files":v["files"],"total_bytes":v["bytes"]}
                for k,v in sorted(monthly_agg.items())]
        }
        save_cache(cache)
    finally:
        _scan_running = False

def get_cache_or_trigger():
    cache = load_cache()
    if cache is None:
        threading.Thread(target=run_scan, daemon=True).start()
        return None
    return cache

@app.route("/api/audiomoth/status")
def audiomoth_status():
    cache = load_cache()
    if cache: return jsonify({"status":"ready","scanned_at":cache.get("scanned_at")})
    if _scan_running: return jsonify({"status":"scanning"})
    return jsonify({"status":"not_started"})

@app.route("/api/audiomoth/scan", methods=["POST"])
@requires_auth
def audiomoth_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status":"scan_started"})

@app.route("/api/audiomoth/overview")
def audiomoth_overview():
    cache = get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}), 202
    return jsonify(cache["overview"])

@app.route("/api/audiomoth/units")
def audiomoth_units():
    cache = get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}), 202
    # Omit daily array from bulk response (use /unit/<name> for that); keep everything else
    slim = [{k:v for k,v in u.items() if k != 'daily'} for u in cache["units"]]
    return jsonify({"units": slim})

@app.route("/api/audiomoth/unit/<name>")
def audiomoth_unit_detail(name):
    cache = load_cache()
    if not cache: return jsonify({"error":"not ready"}), 404
    for u in cache["units"]:
        if u["unit"] == name:
            return jsonify({"unit": name, "daily": u.get("daily",[]),
                            "lat": u.get("lat"), "lon": u.get("lon"),
                            "am_serno": u.get("am_serno"), "pi_serno": u.get("pi_serno")})
    return jsonify({"error":"not found"}), 404

@app.route("/api/audiomoth/locations")
def audiomoth_locations():
    cache = get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}), 202
    locs = [
        {"unit": u["unit"], "lat": u.get("lat"), "lon": u.get("lon"),
         "last_seen": u["last_seen"], "total_files": u["total_files"],
         "active_days": u["active_days"],
         "am_serno": u.get("am_serno"), "pi_serno": u.get("pi_serno")}
        for u in cache["units"]
        if u.get("lat") is not None and u.get("lon") is not None
    ]
    return jsonify({"locations": locs})

@app.route("/api/audiomoth/monthly")
def audiomoth_monthly():
    cache = get_cache_or_trigger()
    if cache is None: return jsonify({"status":"scanning"}), 202
    return jsonify({"monthly": cache["monthly"]})

@app.route("/health")
def health(): return jsonify({"status":"ok"})

def _auto_rescan_loop():
    import time
    while True:
        time.sleep(3600)
        run_scan()

threading.Thread(target=_auto_rescan_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
