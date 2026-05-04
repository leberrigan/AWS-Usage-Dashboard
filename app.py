import os
from datetime import date, timedelta, datetime
from collections import defaultdict
import boto3
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

TAG_KEY   = os.environ.get("PROJECT_TAG_KEY", "Project")
AM_BUCKET = os.environ.get("AUDIOMOTH_BUCKET", "nighthawk-raw-audio")
REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

def ce(): return boto3.client("ce", region_name="us-east-1")
def s3(): return boto3.client("s3", region_name=REGION)
def period_start(): return "2024-06-01"
def period_end(): return date.today().replace(day=1).strftime("%Y-%m-%d")
def today_str(): return date.today().strftime("%Y-%m-%d")
def thirty_days_ago(): return (date.today()-timedelta(days=30)).strftime("%Y-%m-%d")

@app.route("/api/summary")
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
def trend():
    start,end=thirty_days_ago(),today_str()
    r=ce().get_cost_and_usage(TimePeriod={"Start":start,"End":end},Granularity="DAILY",
        Metrics=["UnblendedCost"],GroupBy=[{"Type":"TAG","Key":TAG_KEY}])
    series={};dates=[]
    for result in r["ResultsByTime"]:
        day=result["TimePeriod"]["Start"]; dates.append(day)
        for g in result["Groups"]:
            tag=g["Keys"][0].replace(f"{TAG_KEY}$","") or "(untagged)"; cost=float(g["Metrics"]["UnblendedCost"]["Amount"])
            series.setdefault(tag,[]).append({"date":day,"cost":round(cost,4)})
    return jsonify({"dates":dates,"series":series})

@app.route("/api/forecast")
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

def list_all_objects(bucket,prefix=""):
    client=s3(); paginator=client.get_paginator("list_objects_v2"); objects=[]
    for page in paginator.paginate(Bucket=bucket,Prefix=prefix):
        for obj in page.get("Contents",[]):
            objects.append({"key":obj["Key"],"size":obj["Size"],"last_modified":obj["LastModified"].isoformat()})
    return objects

def parse_objects(objects):
    units=defaultdict(lambda:{"dates":set(),"monthly":defaultdict(lambda:{"count":0,"bytes":0}),"total_bytes":0,"total_files":0,"first_seen":None,"last_seen":None})
    daily_active=defaultdict(set); daily_volume=defaultdict(int)
    for obj in objects:
        parts=obj["key"].split("/")
        if len(parts)<5: continue
        unit,year,month,day=parts[0],parts[1],parts[2],parts[3]
        try:
            date_str=f"{year}-{month.zfill(2)}-{day.zfill(2)}"; datetime.strptime(date_str,"%Y-%m-%d")
        except ValueError: continue
        month_key=f"{year}-{month.zfill(2)}"; size=obj["size"]
        u=units[unit]
        u["dates"].add(date_str); u["monthly"][month_key]["count"]+=1; u["monthly"][month_key]["bytes"]+=size
        u["total_bytes"]+=size; u["total_files"]+=1
        if u["first_seen"] is None or date_str<u["first_seen"]: u["first_seen"]=date_str
        if u["last_seen"] is None or date_str>u["last_seen"]: u["last_seen"]=date_str
        daily_active[date_str].add(unit); daily_volume[date_str]+=size
    return units,daily_active,daily_volume

@app.route("/api/audiomoth/overview")
def audiomoth_overview():
    objects=list_all_objects(AM_BUCKET); units,daily_active,daily_volume=parse_objects(objects)
    cutoff=(date.today()-timedelta(days=7)).strftime("%Y-%m-%d")
    currently_active=len([u for u,d in units.items() if d["last_seen"] and d["last_seen"]>=cutoff])
    all_dates=sorted(daily_active.keys())
    daily_series=[{"date":d,"active_units":len(daily_active[d]),"volume_bytes":daily_volume[d]} for d in all_dates]
    return jsonify({"total_units":len(units),"currently_active":currently_active,
        "total_bytes":sum(u["total_bytes"] for u in units.values()),
        "total_files":sum(u["total_files"] for u in units.values()),"daily_series":daily_series})

@app.route("/api/audiomoth/units")
def audiomoth_units():
    objects=list_all_objects(AM_BUCKET); units,_,_=parse_objects(objects)
    result=[{"unit":n,"first_seen":u["first_seen"],"last_seen":u["last_seen"],"total_files":u["total_files"],
        "total_bytes":u["total_bytes"],"active_days":len(u["dates"]),"monthly":dict(sorted(u["monthly"].items()))}
        for n,u in sorted(units.items())]
    result.sort(key=lambda x:x["last_seen"] or "",reverse=True)
    return jsonify({"units":result})

@app.route("/api/audiomoth/monthly")
def audiomoth_monthly():
    objects=list_all_objects(AM_BUCKET); units,_,_=parse_objects(objects)
    monthly=defaultdict(lambda:{"units":set(),"files":0,"bytes":0})
    for unit_name,u in units.items():
        for month_key,m in u["monthly"].items():
            monthly[month_key]["units"].add(unit_name); monthly[month_key]["files"]+=m["count"]; monthly[month_key]["bytes"]+=m["bytes"]
    result=[{"month":k,"active_units":len(v["units"]),"total_files":v["files"],"total_bytes":v["bytes"]} for k,v in sorted(monthly.items())]
    return jsonify({"monthly":result})

@app.route("/health")
def health(): return jsonify({"status":"ok"})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000)); app.run(host="0.0.0.0",port=port,debug=False)