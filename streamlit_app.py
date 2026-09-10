"""
Saratech AWS Hosting Estimator — live-priced website (Streamlit)
Secrets: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, APP_PASSCODE (optional)
IAM permission needed: pricing:GetProducts only.
"""
import io
import json
from datetime import datetime

import boto3
import pandas as pd
import streamlit as st

st.set_page_config(page_title="AWS Hosting Estimator", page_icon="☁️", layout="centered")

# ---------------- passcode gate ----------------
_pass = st.secrets.get("APP_PASSCODE", "")
if _pass and not st.session_state.get("auth_ok"):
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

# ---- FIXED shared account costs (edit here in code only) ----
FIXED = {
    "nat_gateways": 1, "nat_data_gb": 1024,      # 1 TB updates through NAT
    "public_ips": 1, "data_out_gb": 3072,        # 3 TB out to internet
    "security_mo": 30.0,
    "nat_hr": 0.045, "nat_gb_rate": 0.045, "ip_hr": 0.005, "dto_rate": 0.09,
    "markup": 0.25,
}

TERM_DISC = {"On-Demand": 1.00, "1-Year": 0.78, "3-Year": 0.60}

ENV_NAT = FIXED["nat_gateways"] * FIXED["nat_hr"] * HOURS_MO + FIXED["nat_data_gb"] * FIXED["nat_gb_rate"]
ENV_IP = FIXED["public_ips"] * FIXED["ip_hr"] * HOURS_MO
ENV_DTO = FIXED["data_out_gb"] * FIXED["dto_rate"]
ENV_SEC = FIXED["security_mo"]
ENV_TOTAL = ENV_NAT + ENV_IP + ENV_DTO + ENV_SEC




def _pricing_client():
    return boto3.client(
        "pricing", region_name="us-east-1",
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
    )


def _od_price(client, itype, sql):
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


def _storage_rate(client, api_name):
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


@st.cache_data(ttl=6 * 3600, show_spinner="Downloading AWS's complete machine catalog with today's prices… (~1 minute, then instant for everyone)")
def load_prices(catalog_version="all-v1"):
    client = _pricing_client()
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": REGION_LOCATION},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Windows"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "No License required"},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Compute Instance"},
    ]
    best = {}
    token = None
    while True:
        kw = {"ServiceCode": "AmazonEC2", "Filters": filters, "MaxResults": 100}
        if token:
            kw["NextToken"] = token
        resp = client.get_products(**kw)
        for item in resp["PriceList"]:
            prod = json.loads(item)
            attrs = prod.get("product", {}).get("attributes", {})
            itype = attrs.get("instanceType", "")
            if not itype or ".metal" in itype:
                continue
            fam = itype.split(".")[0]
            # exclude AI-training / special hardware entirely (never for client servers)
            if fam.startswith(("p2", "p3", "p4", "p5", "p6",
                               "trn", "inf", "dl", "f1", "f2", "vt1", "mac", "hpc")):
                continue
            # graphics GPU machines (NX/CAD workstations) are kept but tagged
            is_gpu = fam.startswith(("g3", "g4", "g5", "g6")) or attrs.get("gpu", "0") not in ("", "0", "NA")
            try:
                cpu = int(attrs.get("vcpu", "0"))
                ram = float(attrs.get("memory", "0").replace(" GiB", "").replace(",", ""))
            except ValueError:
                continue
            if cpu <= 0 or ram <= 0:
                continue
            price = None
            for term in prod.get("terms", {}).get("OnDemand", {}).values():
                for dim in term["priceDimensions"].values():
                    usd = float(dim["pricePerUnit"]["USD"])
                    if usd > 0:
                        price = usd
            if price is None:
                continue
            if itype not in best or price < best[itype]["win_hr"]:
                best[itype] = {"type": itype, "cpu": cpu, "ram": ram, "win_hr": price, "gpu": is_gpu}
        token = resp.get("NextToken")
        if not token:
            break
    rows = sorted(best.values(), key=lambda r: r["win_hr"])
    # SQL per-vCPU rate derived live from one known pair
    win = _od_price(client, "m6i.2xlarge", sql=False)
    wsql = _od_price(client, "m6i.2xlarge", sql=True)
    per_vcpu = (wsql - win) / 8 if (win and wsql) else 0.12
    gp3 = _storage_rate(client, "gp3") or 0.08
    return {"machines": rows, "sql_per_vcpu": per_vcpu, "gp3": gp3, "snap": 0.05,
            "fetched": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}


def match(machines, cpu, ram, gpu=False):
    fits = [m for m in machines if m["cpu"] >= cpu and m["ram"] >= ram and m.get("gpu", False) == gpu]
    return fits[0] if fits else None


def costs_all_terms(P, m, need_cpu, sql, disk_gb):
    out = {}
    sql_vcpu = max(need_cpu, 4)
    sql_mo = sql_vcpu * P["sql_per_vcpu"] * HOURS_MO if sql else 0.0
    disk_mo = disk_gb * P["gp3"]
    backup_mo = disk_gb * P["snap"]
    for term, disc in TERM_DISC.items():
        machine_mo = m["win_hr"] * disc * HOURS_MO
        out[term] = {"machine": machine_mo, "sql": sql_mo, "disk": disk_mo,
                     "backup": backup_mo, "total": machine_mo + sql_mo + disk_mo + backup_mo}
    return out, sql_vcpu


# ================= UI =================
st.title("☁️ AWS Hosting Estimator")

try:
    P = load_prices()
except Exception as e:
    st.error(f"Could not reach the AWS pricing service. Ask Mahendra to check the app settings. ({e})")
    st.stop()

st.caption(f"Live official AWS prices, fetched {P['fetched']} · Servers assumed running 24/7 · Region us-east-1")

with st.expander(f"🔍 Complete AWS catalog — {len(P['machines'])} machines with today's prices"):
    st.dataframe(pd.DataFrame([
        {"Machine": r["type"], "CPU": r["cpu"], "RAM GB": r["ram"],
         "GPU": "Yes" if r.get("gpu") else "",
         "Windows $/hr": round(r["win_hr"], 5)}
        for r in P["machines"]
    ]), use_container_width=True, hide_index=True)

if "servers" not in st.session_state:
    st.session_state.servers = []

# ---------- 1. server form ----------
st.header("1 · The server")
name = st.text_input("Server name", "Teamcenter application server")
c1, c2 = st.columns(2)
cpu = int(c1.number_input("CPUs", min_value=1, max_value=64, value=8, step=1))
ram = int(c2.number_input("Memory RAM (GB)", min_value=4, max_value=1024, value=64, step=4))
sql = st.toggle("Needs Microsoft SQL Server license (database server)", value=False)
gpu = st.toggle("Needs a graphics card (GPU) — CAD/NX workstation", value=False,
                help="Turn ON only for engineering workstations that run NX or other 3D CAD. Normal app and database servers do NOT need this.")

st.markdown("**Drives** — add every disk the server needs, like on a real Windows server:")
default_drives = pd.DataFrame([{"Drive": "C:", "Size (GB)": 150}, {"Drive": "E:", "Size (GB)": 300}])
drives = st.data_editor(default_drives, num_rows="dynamic", use_container_width=True, key="drives",
                        column_config={
                            "Drive": st.column_config.TextColumn(help="Drive letter, e.g. C:, E:, F:"),
                            "Size (GB)": st.column_config.NumberColumn(min_value=1, max_value=20000, step=50),
                        })
disk_gb = int(pd.to_numeric(drives["Size (GB)"], errors="coerce").fillna(0).sum())
st.caption(f"Total disk: **{disk_gb} GB**. Daily backups are automatic: AWS keeps rolling snapshot copies — "
           f"they use roughly the same space as the disks (≈{disk_gb} GB), priced at $0.05 per GB per month.")

# ---------- 2. machine + prices ----------
st.header("2 · What AWS offers for this")
m = match(P["machines"], cpu, ram, gpu)
if not m:
    st.error(f"AWS has no machine with {cpu} CPU / {ram} GB in our list. Reduce the size, "
             "or ask Mahendra to add larger machines.")
    st.stop()

if m["cpu"] == cpu and m["ram"] == ram:
    st.success(f"Perfect fit: **{m['type']}** — exactly {m['cpu']} CPU / {m['ram']:.0f} GB"
               + (" with GPU graphics card." if gpu else "."))
else:
    st.warning(f"AWS machine sizes are fixed (like T-shirt sizes) — there is no exact {cpu} CPU / {ram} GB. "
               f"Cheapest machine big enough: **{m['type']}** with {m['cpu']} CPU / {m['ram']} GB. "
               f"The client pays for this whole machine.")

CT, sql_vcpu = costs_all_terms(P, m, cpu, sql, disk_gb)
if sql and m["cpu"] > cpu:
    save = (m["cpu"] - sql_vcpu) * P["sql_per_vcpu"] * HOURS_MO
    st.info(f"✂️ The SQL license is charged only on the **{sql_vcpu} CPUs the client needs**, "
            f"not all {m['cpu']} — saving about **${save:,.0f}/month**.")

st.markdown("**Monthly cost of this server — three ways to pay:**")
tbl = "| | Pay monthly | 1-Year commitment | 3-Year commitment |\n|---|---:|---:|---:|\n"
tbl += "| Machine + Windows | " + " | ".join(f"${CT[t]['machine']:,.0f}" for t in TERM_DISC) + " |\n"
if sql:
    tbl += "| SQL Server license | " + " | ".join(f"${CT[t]['sql']:,.0f}" for t in TERM_DISC) + " |\n"
tbl += "| Disks (" + str(disk_gb) + " GB) | " + " | ".join(f"${CT[t]['disk']:,.0f}" for t in TERM_DISC) + " |\n"
tbl += "| Daily backups | " + " | ".join(f"${CT[t]['backup']:,.0f}" for t in TERM_DISC) + " |\n"
tbl += "| **Total / month** | " + " | ".join(f"**${CT[t]['total']:,.0f}**" for t in TERM_DISC) + " |"
tbl += "\n| **PAY AMOUNT — server + environment (environment charged once per setup)** | " + " | ".join(f"**${CT[t]['total'] + ENV_TOTAL:,.0f}**" for t in TERM_DISC) + " |"
st.markdown(tbl)
st.caption("The PAY AMOUNT row is the complete monthly cost for a one-server setup. The environment (network, internet door, security) is charged ONE time for the whole setup — adding more servers does not repeat it. "
           "Commitments discount only the machine — never the SQL license, disks or backups. "
           "A commitment must be paid for the whole period, even if the client leaves early.")

term = st.radio("Which pricing do you want in the estimate?", list(TERM_DISC.keys()), horizontal=True)
allin = CT[term]["total"] + ENV_TOTAL
if st.button(f"➕ Add '{name}' to the estimate — pay amount ${allin:,.0f}/mo everything included",
             type="primary", use_container_width=True):
    st.session_state.servers.append({"name": name, "cpu": cpu, "ram": ram, "sql": sql, "gpu": gpu,
                                     "disk": disk_gb, "term": term,
                                     "drives": ", ".join(f"{r['Drive']} {int(r['Size (GB)'])}GB"
                                                          for _, r in drives.iterrows()
                                                          if pd.notna(r["Size (GB)"]))})
    st.rerun()

# ---------- 3. estimate ----------
st.header("3 · The estimate")
if not st.session_state.servers:
    st.info("No servers added yet.")
else:
    total_rows = []
    server_total = 0.0
    for i, s in enumerate(st.session_state.servers):
        mm = match(P["machines"], s["cpu"], s["ram"], s.get("gpu", False))
        ct, sv = costs_all_terms(P, mm, s["cpu"], s["sql"], s["disk"])
        c = ct[s["term"]]
        server_total += c["total"]
        col1, col2, col3 = st.columns([7, 2, 1])
        col1.markdown(f"**{s['name']}** — {s['cpu']} CPU / {s['ram']} GB → {mm['type']} · "
                      f"{s['drives']} · {s['term']}" + (" · SQL" if s["sql"] else ""))
        col2.markdown(f"**${c['total']:,.0f}/mo**")
        if col3.button("✕", key=f"rm{i}"):
            st.session_state.servers.pop(i)
            st.rerun()
        total_rows.append({"Server": s["name"], "Needs": f"{s['cpu']} CPU / {s['ram']} GB",
                           "AWS machine": mm["type"], "Drives": s["drives"], "Term": s["term"],
                           "SQL": f"Yes (on {sv} CPUs)" if s["sql"] else "No",
                           "Machine $": round(c["machine"]), "SQL $": round(c["sql"]),
                           "Disks $": round(c["disk"]), "Backups $": round(c["backup"]),
                           "Total $/mo": round(c["total"])})

    nat, ip, dto, sec = ENV_NAT, ENV_IP, ENV_DTO, ENV_SEC
    shared = ENV_TOTAL
    aws_total = server_total + shared
    client_mo = aws_total

    st.markdown("**The complete environment** — created ONE time for the whole setup (shared by all servers):")
    ENV_ITEMS = [
        ("Dedicated AWS account for this client", "Their own isolated space, own bill", 0),
        ("Private network (VPC)", "The client's own fenced network inside AWS", 0),
        ("Private subnets for the servers", "Servers are NOT reachable from the internet directly", 0),
        ("Public subnet for the internet door", "Where the NAT gateway lives", 0),
        ("Firewall rules (security groups)", "Only allowed traffic can reach each server", 0),
        ("Routing + internet gateway", "Traffic direction inside the network", 0),
        ("User access & permissions (IAM)", "Who is allowed to manage what", 0),
        ("Internet door (NAT gateway) + 1 TB updates/mo", "Private servers can download Windows updates safely", nat),
        ("1 public internet address", "The single entry point into the environment", ip),
        ("3 TB data out to internet /mo", "Users downloading files, remote sessions", dto),
        ("Security monitoring", "AWS threat detection + full activity logging", sec),
    ]
    env_tbl = "| Included | Why | $/month |\n|---|---|---:|\n"
    for label, why, cost in ENV_ITEMS:
        env_tbl += f"| {label} | {why} | {'included' if cost == 0 else f'{cost:,.0f}'} |\n"
    env_tbl += f"| **Environment subtotal** | | **{shared:,.0f}** |"
    st.markdown(env_tbl)

    st.markdown("**Totals:**")

    st.markdown(
        f"| | $/month |\n|---|---:|\n"
        f"| All servers | {server_total:,.0f} |\n"
        f"| Internet door (NAT) + 1 TB updates — fixed | {nat:,.0f} |\n"
        f"| 1 public internet address — fixed | {ip:,.0f} |\n"
        f"| 3 TB data out to internet — fixed | {dto:,.0f} |\n"
        f"| Security monitoring — fixed | {sec:,.0f} |\n"
        f"| **Total / month** | **{aws_total:,.0f}** |\n"
        f"| Total / year | {aws_total * 12:,.0f} |"
    )

    df = pd.DataFrame(total_rows)
    summary = pd.DataFrame([
        {"Item": "All servers", "$/month": round(server_total)},
        {"Item": "NAT + 1 TB updates (fixed)", "$/month": round(nat)},
        {"Item": "1 public IP (fixed)", "$/month": round(ip)},
        {"Item": "3 TB data out (fixed)", "$/month": round(dto)},
        {"Item": "Security monitoring (fixed)", "$/month": round(sec)},
        {"Item": "TOTAL / month", "$/month": round(aws_total)},
        {"Item": "Total / year", "$/month": round(aws_total * 12)},
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Servers", index=False)
        summary.to_excel(xw, sheet_name="Totals", index=False)
    st.download_button("⬇️ Download estimate as Excel", buf.getvalue(),
                       file_name="AWS_Hosting_Estimate.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)

st.divider()
st.caption("Servers run 24/7. Disks, backups and the fixed items bill around the clock. "
           "SQL Server has a 4-CPU license minimum. Machine prices come live from AWS's official price list; "
           "1-Year/3-Year use standard commitment discounts (≈22% / ≈40% on the machine only).")
