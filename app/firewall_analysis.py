"""Conservative read-only RouterOS /export firewall analysis; never executes commands."""
from __future__ import annotations
from collections import Counter
import ipaddress
import shlex

TABLES = ("/ip firewall raw", "/ip firewall filter", "/ip firewall nat",
          "/ip firewall mangle", "/ip firewall address-list",
          "/ipv6 firewall raw", "/ipv6 firewall filter", "/ipv6 firewall address-list")

def parse_export(source: str) -> list[dict]:
    rules, positions, section = [], Counter(), None
    buffer, start = "", 0
    for number, raw in enumerate(source.splitlines() + [""], 1):
        line = raw.strip()
        if line.endswith("\\"):
            if not buffer: start = number
            buffer += line[:-1].rstrip() + " "
            continue
        if buffer:
            line, number, buffer = buffer + line, start, ""
        if not line or line.startswith("#"): continue
        if line in TABLES:
            section = line
            continue
        if line.startswith("/"):
            match = next((t for t in TABLES if line.startswith(t + " add ")), None)
            if match: section, line = match, line[len(match)+1:]
            else: section = None; continue
        if section not in TABLES or not line.startswith("add "): continue
        try: parts = shlex.split(line, posix=True)
        except ValueError: continue
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        positions[section] += 1
        rules.append(dict(table=section, line=number, position=positions[section],
                          values=fields, original=line))
    return rules

def analyze(source: str) -> dict:
    rules = parse_export(source)
    findings = []
    def warn(code, severity, msg, r=None):
        findings.append(dict(code=code, severity=severity, message=msg,
                             line=r["line"] if r else None))
    active = [r for r in rules if r["values"].get("disabled") != "yes"]
    lists = Counter()
    for r in active:
        v = r["values"]
        if r["table"].endswith("address-list") and v.get("list") and v.get("address"):
            lists[v["list"]] += 1
            try: ipaddress.ip_network(v["address"], strict=False)
            except ValueError: warn("INVALID_ADDRESS", "warning", "Nonliteral or invalid address-list entry", r)
    raw = [r for r in active if r["table"] == "/ip firewall raw"]
    threat = [r for r in raw if r["values"].get("action") == "drop" and
              r["values"].get("src-address-list") in ("rv_threat_ips", "prod_blocklist")]
    gr = [r for r in raw if r["values"].get("src-address-list") == "rv_gr_ranges"]
    for r in raw:
        v = r["values"]
        if v.get("action") == "drop" and v.get("src-address-list") == "!rv_gr_ranges":
            if not (v.get("in-interface") or v.get("in-interface-list")):
                warn("GEO_DROP_UNSCOPED", "critical", "Non-GR drop lacks ingress interface scope", r)
            if not (v.get("dst-address") or v.get("dst-address-list") or v.get("dst-port")):
                warn("GEO_DROP_BROAD", "critical", "Non-GR drop lacks destination/service scope", r)
        if v.get("action") == "accept" and v.get("src-address-list") == "rv_gr_ranges" and not (
             v.get("dst-address") or v.get("dst-address-list") or v.get("dst-port")):
            warn("BROAD_GR_ACCEPT", "warning", "GR RAW accept lacks destination scope", r)
    for r in threat:
        if any(q["position"] < r["position"] and q["values"].get("chain") == r["values"].get("chain")
               and q["values"].get("action") in ("accept", "notrack") for q in raw):
            warn("RAW_PRECEDENCE_REVIEW", "warning", "Earlier accept/notrack may bypass threat drop", r)
    if any(r["values"].get("action") == "fasttrack-connection" for r in active
           if r["table"] == "/ip firewall filter"):
        warn("FASTTRACK_PRESENT", "info", "Review FastTrack interactions with intended policy")
    return dict(tables=dict(Counter(r["table"] for r in rules)), address_lists=dict(lists),
                dstnat_rules=sum(1 for r in active if r["table"] == "/ip firewall nat" and
                                 r["values"].get("chain") == "dstnat"),
                raw_rules=len(raw), threat_drop_rules=len(threat),
                gr_raw_references=len(gr), findings=findings, rules=rules)
