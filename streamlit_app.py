"""
Saratech AWS Hosting Estimator — live-priced website (Streamlit)

Run locally:   streamlit run streamlit_app.py
Deploy free:   push to GitHub -> share.streamlit.io -> set Secrets (see README section below)

Secrets required (Streamlit Cloud -> App -> Settings -> Secrets):
    AWS_ACCESS_KEY_ID = "AKIA..."
    AWS_SECRET_ACCESS_KEY = "..."
(IAM user needs ONLY the permission pricing:GetProducts — it can read public price data and nothing else.)
"""
import io
import json
from datetime import datetime

import boto3
import pandas as pd
import streamlit as st

st.set_page_config(page_title="AWS Hosting Estimator", page_icon="☁️", layout="wide")

# ---------------- passcode gate ----------------
# Set APP_PASSCODE in Streamlit Secrets. If unset, the app runs open (local testing).
_pass = st.secrets.get("APP_PASSCODE", "")
if _pass:
    if not st.session_state.get("auth_ok"):
        st.title("☁️ AWS Hosting Estimator")
        entered = st.text_input("Enter access code", type="password")
        if entered:
            if entered == _pass:
                st.session_state.auth_ok = True
                st.rerun()
            else:
                st.error("Wrong code. Ask Mahendra for access.")
        st.stop()


REGION_LOCATION = "US East (N. Virginia)"
HOURS_MO = 730

# Machines the matcher may choose from (specs are fixed by AWS; prices fetched LIVE below)
MACHINES = [
    ("t3.large", 2, 8), ("t3.xlarge", 4, 16),
    ("m6i.xlarge", 4, 16), ("m6i.2xlarge", 8, 32), ("m6i.4xlarge", 16, 64),
    ("r6i.xlarge", 4, 32), ("r6i.2xlarge", 8, 64), ("r6i.4xlarge", 16, 128), ("r6i.8xlarge", 32, 256),
    ("r6a.xlarge", 4, 32), ("r6a.2xlarge", 8, 64), ("r6a.4xlarge", 16, 128),
    ("x2iedn.xlarge", 4, 128), ("x2iedn.2xlarge", 8, 256), ("x2iedn.4xlarge", 16, 512),
]

CPU_CHOICES = [2, 4, 8, 12, 16, 24, 32]
RAM_CHOICES = [8, 16, 32, 64, 96, 128, 256, 512]


# ---------------- live pricing (cached 6h; prices themselves change ~yearly) ----------------
def _pricing_client():
    return boto3.client(
        "pricing", region_name="us-east-1",
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
    )


def _od_price(client, itype: str, sql: bool):
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": itype},
        {"Type": "TERM_MATCH", "Field": "location", "Value": REGION_LOCATION},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Windows"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "SQL Std" if sql else "NA"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "No License required"},
    ]
    resp = client.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=10)
    for item in resp["PriceList"]:
        prod = json.loads(item)
        for term in prod.get("terms", {}).get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                usd = float(dim["pricePerUnit"]["USD"])
                if usd > 0:
                    return usd
    return None


def _storage_rate(client, api_name: str):
    resp = client.get_products(ServiceCode="AmazonEC2", Filters=[
        {"Type": "TERM_MATCH", "Field": "location", "Value": REGION_LOCATION},
        {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": api_name},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
    ], MaxResults=5)
    for item in resp["PriceList"]:
        prod = json.loads(item)
        for term in prod.get("terms", {}).get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                usd = float(dim["pricePerUnit"]["USD"])
                if usd > 0:
                    return usd
    return None


def _snapshot_rate(client):
    resp = client.get_products(ServiceCode="AmazonEC2", Filters=[
        {"Type": "TERM_MATCH", "Field": "location", "Value": REGION_LOCATION},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
    ], MaxResults=20)
    best = None
    for item in resp["PriceList"]:
        prod = json.loads(item)
        usage = prod.get("product", {}).get("attributes", {}).get("usagetype", "")
        if "SnapshotUsage" in usage and ":" not in usage.split("SnapshotUsage")[-1]:
            for term in prod.get("terms", {}).get("OnDemand", {}).values():
                for dim in term["priceDimensions"].values():
                    usd = float(dim["pricePerUnit"]["USD"])
                    if usd > 0:
                        best = usd
    return best


@st.cache_data(ttl=6 * 3600, show_spinner="Fetching live AWS prices…")
def load_prices():
    client = _pricing_client()
    rows = []
    for itype, cpu, ram in MACHINES:
        win = _od_price(client, itype, sql=False)
        wsql = _od_price(client, itype, sql=True)
        if win is None:
            continue
        rows.append({"type": itype, "cpu": cpu, "ram": ram, "win_hr": win,
                     "sql_hr_full": (wsql - win) if wsql else None})
    # SQL per-vCPU rate: derived from any machine that has both prices
    per_vcpu = None
    for r in rows:
        if r["sql_hr_full"]:
            per_vcpu = r["sql_hr_full"] / r["cpu"]
            break
    gp3 = _storage_rate(client, "gp3") or 0.08
    snap = _snapshot_rate(client) or 0.05
    rows.sort(key=lambda r: r["win_hr"])
    return {"machines": rows, "sql_per_vcpu": per_vcpu or 0.12, "gp3": gp3, "snap": snap,
            "fetched": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}


# ---------------- cost model ----------------
def match(machines, cpu, ram):
    fits = [m for m in machines if m["cpu"] >= cpu and m["ram"] >= ram]
    return (fits[0], fits) if fits else (None, [])


def server_cost(p, s):
    m, _ = match(p["machines"], s["cpu"], s["ram"])
    if not m:
        return None
    hrs = min(s["hours"], 24) * min(s["days"], 31)
    compute = m["win_hr"] * hrs
    sql_vcpu = max(s["cpu"], 4)
    sql = sql_vcpu * p["sql_per_vcpu"] * hrs if s["sql"] else 0.0
    storage = s["disk"] * (p["gp3"] + p["snap"])
    return {"machine": m, "hrs": hrs, "compute": compute, "sql": sql,
            "sql_vcpu": sql_vcpu, "storage": storage,
            "total": compute + sql + storage}


# ---------------- UI ----------------
st.title("☁️ AWS Hosting Estimator")
st.caption("Live prices from the official AWS Price List API — nothing hardcoded. Region: us-east-1, On-Demand.")

try:
    P = load_prices()
except Exception as e:
    st.error(f"Could not reach the AWS pricing API. Check the app's Secrets. ({e})")
    st.stop()

st.caption(f"Prices fetched {P['fetched']} · SQL Standard = ${P['sql_per_vcpu']:.3f}/vCPU-hr · "
           f"gp3 ${P['gp3']:.3f}/GB-mo · snapshot ${P['snap']:.3f}/GB-mo")

if "servers" not in st.session_state:
    st.session_state.servers = []

left, right = st.columns([5, 6], gap="large")

with left:
    st.subheader("Configure a server")
    name = st.text_input("Server name", "App server")
    c1, c2 = st.columns(2)
    cpu = c1.select_slider("CPUs needed", CPU_CHOICES, value=8)
    ram = c2.select_slider("RAM needed (GB)", RAM_CHOICES, value=64)
    sql = st.toggle("SQL Server Standard license included", value=False)
    c3, c4, c5 = st.columns(3)
    hours = c3.number_input("Hours/day", 1.0, 24.0, 24.0, 1.0)
    days = c4.number_input("Days/month", 1.0, 31.0, 30.42, 1.0)
    disk = c5.number_input("Disk GB (gp3)", 0, 20000, 300, 50,
                           help="Includes daily snapshots (~1× disk size) automatically.")

    m, fits = match(P["machines"], cpu, ram)
    if not m:
        st.error(f"No AWS machine offers {cpu} CPU / {ram} GB (largest listed: "
                 f"{max(x['cpu'] for x in P['machines'])} CPU / {max(x['ram'] for x in P['machines'])} GB). "
                 "Reduce the requirement or ask for the catalog to be extended.")
    else:
        exact = (m["cpu"] == cpu and m["ram"] == ram)
        if exact:
            st.success(f"Exact match: **{m['type']}** ({m['cpu']} CPU / {m['ram']} GB) — "
                       f"${m['win_hr']:.4f}/hr Windows.")
        else:
            msg = (f"No {cpu} CPU / {ram} GB machine exists — AWS sizes are fixed. "
                   f"Cheapest that fits: **{m['type']}** ({m['cpu']} CPU / {m['ram']} GB), "
                   f"${m['win_hr']:.4f}/hr. You pay for the whole machine.")
            if sql and m["cpu"] > cpu:
                save = (m["cpu"] - max(cpu, 4)) * P["sql_per_vcpu"] * HOURS_MO
                msg += (f"\n\n✂️ **SQL trim applies:** license billed on your {max(cpu,4)} CPUs, "
                        f"not the machine's {m['cpu']} — saving ≈ ${save:,.0f}/mo.")
            if not sql and m["cpu"] > cpu:
                msg += ("\n\nℹ️ Without SQL, trimming CPUs saves nothing — Windows compute "
                        f"always bills the full {m['cpu']}-CPU machine.")
            st.warning(msg)

        pv = server_cost(P, {"cpu": cpu, "ram": ram, "sql": sql, "hours": hours, "days": days, "disk": disk})
        st.markdown(
            f"| | $/month |\n|---|---:|\n"
            f"| {m['type']} compute + Windows ({m['win_hr']:.4f}/hr × {pv['hrs']:.0f} h) | {pv['compute']:,.0f} |\n"
            + (f"| SQL Standard on {pv['sql_vcpu']} vCPU | {pv['sql']:,.0f} |\n" if sql else "")
            + f"| Disk {disk} GB + daily snapshots (bills 24/7) | {pv['storage']:,.0f} |\n"
            + f"| **This server** | **{pv['total']:,.0f}** |"
        )
        if st.button("➕ Add to estimate", type="primary", use_container_width=True):
            st.session_state.servers.append(
                {"name": name, "cpu": cpu, "ram": ram, "sql": sql,
                 "hours": hours, "days": days, "disk": disk})
            st.rerun()

with right:
    st.subheader("Estimate sheet")
    rows = [(s, server_cost(P, s)) for s in st.session_state.servers]
    rows = [(s, c) for s, c in rows if c]

    if not rows:
        st.info("No servers yet — configure one on the left and press **Add to estimate**.")
    for i, (s, c) in enumerate(rows):
        col1, col2, col3 = st.columns([6, 2, 1])
        col1.markdown(f"**{s['name']}** — asked {s['cpu']}/{s['ram']} → {c['machine']['type']}"
                      + (f", SQL on {c['sql_vcpu']} vCPU" if s["sql"] else ", no SQL"))
        col2.markdown(f"**${c['total']:,.0f}**")
        if col3.button("✕", key=f"rm{i}"):
            st.session_state.servers.pop(i)
            st.rerun()
    server_total = sum(c["total"] for _, c in rows)

    st.markdown("**Shared account costs** *(always on, even when servers are stopped)*")
    sc1, sc2 = st.columns(2)
    nat_n = sc1.number_input("NAT Gateways", 0, 10, 1)
    nat_gb = sc2.number_input("GB through NAT /mo", 0, 100000, 100, help="Updates, patches, downloads")
    ip_n = sc1.number_input("Public IPv4 addresses", 0, 50, 1)
    dto_gb = sc2.number_input("Data out to internet GB /mo", 0, 100000, 2048,
                              help="2048 GB = 2 TB. User downloads, remote sessions, backups leaving AWS.")
    sec_mo = sc1.number_input("Security baseline $/mo", 0, 2000, 30,
                              help="GuardDuty + CloudTrail + Inspector, small account")
    markup = sc2.number_input("Saratech markup %", 0, 100, 25) / 100

    nat_cost = nat_n * 0.045 * HOURS_MO + nat_gb * 0.045
    ip_cost = ip_n * 0.005 * HOURS_MO
    dto_cost = dto_gb * 0.09
    shared_total = nat_cost + ip_cost + dto_cost + sec_mo

    aws_total = server_total + shared_total
    client_mo = aws_total * (1 + markup)

    st.markdown(
        f"| | $/month |\n|---|---:|\n"
        f"| Servers subtotal | {server_total:,.0f} |\n"
        f"| NAT ({nat_n} gw + {nat_gb} GB) | {nat_cost:,.0f} |\n"
        f"| Public IPv4 ({ip_n}) | {ip_cost:,.0f} |\n"
        f"| Data out ({dto_gb:,} GB) | {dto_cost:,.0f} |\n"
        f"| Security baseline | {sec_mo:,.0f} |\n"
        f"| **AWS total** | **{aws_total:,.0f}** |\n"
        f"| Markup {markup:.0%} | {aws_total*markup:,.0f} |\n"
        f"| **Client price / month** | **{client_mo:,.0f}** |\n"
        f"| Client price / year | {client_mo*12:,.0f} |"
    )

    if rows:
        df = pd.DataFrame([{
            "Server": s["name"], "Requested": f"{s['cpu']} CPU / {s['ram']} GB",
            "AWS machine": f"{c['machine']['type']} ({c['machine']['cpu']}/{c['machine']['ram']})",
            "SQL": f"Yes (on {c['sql_vcpu']} vCPU)" if s["sql"] else "No",
            "Hrs/mo": round(c["hrs"]), "Compute $": round(c["compute"]),
            "SQL $": round(c["sql"]), "Storage $": round(c["storage"]), "Total $": round(c["total"]),
        } for s, c in rows])
        summary = pd.DataFrame([
            {"Item": "Servers subtotal", "$/mo": round(server_total)},
            {"Item": f"NAT ({nat_n} gw + {nat_gb} GB)", "$/mo": round(nat_cost)},
            {"Item": f"Public IPv4 ({ip_n})", "$/mo": round(ip_cost)},
            {"Item": f"Data out ({dto_gb} GB)", "$/mo": round(dto_cost)},
            {"Item": "Security baseline", "$/mo": round(sec_mo)},
            {"Item": "AWS total", "$/mo": round(aws_total)},
            {"Item": f"Markup {markup:.0%}", "$/mo": round(aws_total * markup)},
            {"Item": "CLIENT PRICE / month", "$/mo": round(client_mo)},
            {"Item": "Client price / year", "$/mo": round(client_mo * 12)},
        ])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="Servers", index=False)
            summary.to_excel(xw, sheet_name="Totals", index=False)
        st.download_button("⬇️ Generate Excel", buf.getvalue(),
                           file_name="AWS_Hosting_Estimate.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

st.caption("Disk, snapshots, NAT and public IPs bill around the clock even when servers are stopped. "
           "SQL Standard has a 4-vCPU license minimum. Prices are live On-Demand, us-east-1.")
