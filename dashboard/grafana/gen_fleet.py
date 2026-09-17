#!/usr/bin/env python3
"""
Generates grafana/dashboards/fleet.json, the provisioned Fleet dashboard.

Grafana dashboard JSON is ~30 KB of repetitive structure; this keeps the
panel list and every SQL query in one readable place. Regenerate after
editing:

    python3 grafana/gen_fleet.py > grafana/dashboards/fleet.json

The provisioning is allowUiUpdates: true, so panels can also be tweaked in
the Grafana UI - but those edits live in Grafana's own database and are
overwritten by the file on the next restart. Either copy the change back
here, or export the dashboard JSON over fleet.json and stop using this.
"""
import json
DS = {"type": "grafana-postgresql-datasource", "uid": "fleet-pg"}
pid = [0]
def nid():
    pid[0] += 1; return pid[0]

def target(sql, fmt="time_series"):
    return {"datasource": DS, "refId": "A", "rawQuery": True, "rawSql": sql.strip(), "format": fmt, "editorMode": "code"}

def panel(type_, title, sql, x, y, w, h, fmt="time_series", unit=None, options=None, field=None, overrides=None, transformations=None, desc=None):
    p = {"id": nid(), "type": type_, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(sql, fmt)],
         "fieldConfig": {"defaults": {}, "overrides": overrides or []},
         "options": options or {}}
    if unit: p["fieldConfig"]["defaults"]["unit"] = unit
    if field: p["fieldConfig"]["defaults"].update(field)
    if transformations: p["transformations"] = transformations
    if desc: p["description"] = desc
    return p

def state_timeline(title, sql, x, y, w, h, desc=None, mappings=None):
    """One lane per device (partitionByValues on the `device` column), the
    `value` column drawn as coloured states. Strings without a mapping get
    palette colours automatically."""
    p = panel("state-timeline", title, sql, x, y, w, h, fmt="table",
              field={"custom": {"fillOpacity": 70, "lineWidth": 0}, "color": {"mode": "palette-classic"}},
              options={"mergeValues": True, "showValue": "auto", "alignValue": "left", "rowHeight": 0.8,
                       "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
              transformations=[{"id": "partitionByValues",
                                "options": {"fields": ["device"], "keepFields": False, "naming": {"asLabels": False}}}],
              desc=desc)
    if mappings:
        p["fieldConfig"]["defaults"]["mappings"] = mappings
    return p


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def thresholds(*steps):
    return {"thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}}

def stat(title, sql, x, y, unit=None, th=None, desc=None, w=4):
    f = {"color": {"mode": "thresholds"}}
    f.update(th or thresholds((None, "text")))
    return panel("stat", title, sql, x, y, w, 4, fmt="table", unit=unit, field=f, desc=desc,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": "value", "graphMode": "none", "textMode": "value"})

def ts(title, sql, x, y, w=12, h=8, unit=None, draw="line", field=None, desc=None, overrides=None):
    f = {"custom": {"drawStyle": draw, "lineWidth": 1, "fillOpacity": 0 if draw == "line" else 60,
                    "showPoints": "auto", "pointSize": 3,
                    # field devices report every 6 h and every device's samples sit on its
                    # own timestamps, so the wide frame is mostly nulls: join lines across
                    # anything shorter than two missed reports (13 h) and break beyond that
                    "spanNulls": 13 * 3600 * 1000}}
    if field: f.update(field)
    return panel("timeseries", title, sql, x, y, w, h, unit=unit, field=f, desc=desc, overrides=overrides,
                 options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "desc"}})

DEV = "device_id in ($device)"
TF = "$__timeFilter(ts)"
# every per-device series is labelled with the Particle device name, falling
# back to the id (device_label view); the $device variable filters on ids
LBL = "join device_label using (device_id)"
DEVICE_VAR_SQL = ("select case when name is null then device_id else name || '  ·  ' || device_id end as __text, "
                  "device_id as __value from device order by name nulls last, device_id")
panels = []
y = 0

# ---------------------------------------------------------------- fleet row
panels.append(row("Fleet", y)); y += 1
panels += [
    stat("Devices", "select count(*) from device", 0, y),
    stat("Reported < 2 h", "select count(*) from fleet_now where silent_sec < 7200", 4, y,
         th=thresholds((None, "red"), (1, "orange"), (15, "green")),
         desc="Devices heard from in the last two hours."),
    stat("Battery < 30 %", "select count(*) from fleet_now where battery < 30", 8, y,
         th=thresholds((None, "green"), (1, "orange"), (5, "red"))),
    stat("Connect failures", f"select count(*) from connect_failure where {TF}", 12, y,
         th=thresholds((None, "green"), (1, "orange"), (20, "red")),
         desc="connect_failure events in the selected time range."),
    stat("Hook errors", f"select count(*) from hook_event where kind='error' and {TF}", 16, y,
         th=thresholds((None, "green"), (1, "orange"), (100, "red")),
         desc="Webhook deliveries Particle reported as failed in the selected time range."),
    stat("Log freshness", "select extract(epoch from now() - max(last_seen)) from device", 20, y, unit="s",
         th=thresholds((None, "green"), (3600, "orange"), (7200, "red")),
         desc="Time since the newest event in the database. Grows if the SSE stream or the ingest stops."),
]
y += 4

fleet_overrides = [
    {"matcher": {"id": "byName", "options": "silent_sec"},
     "properties": [{"id": "unit", "value": "s"}, {"id": "displayName", "value": "silent"},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "green"), (3600, "orange"), (7200, "red"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "battery"},
     "properties": [{"id": "unit", "value": "percent"},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (30, "orange"), (50, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "rsrp_dbm"},
     "properties": [{"id": "unit", "value": "dBm"},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (-110, "orange"), (-100, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "sinr"},
     "properties": [{"id": "unit", "value": "dB"},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (0, "orange"), (5, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "success_pct"},
     "properties": [{"id": "unit", "value": "percent"},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (70, "orange"), (90, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "failures_24h"},
     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "green"), (1, "orange"), (5, "red"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "vbat_idle"}, "properties": [{"id": "unit", "value": "volt"}, {"id": "decimals", "value": 2}]},
    {"matcher": {"id": "byName", "options": "vbat_load"}, "properties": [{"id": "unit", "value": "volt"}, {"id": "decimals", "value": 2}]},
    {"matcher": {"id": "byName", "options": "last_seen"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
]
panels.append(panel("table", "Fleet now", f"""
select name, device_id, last_seen, silent_sec, status, fw_version, battery, vbat_idle, vbat_load,
       rsrp_dbm, sinr, band, operator, connect_attempts, connect_successes, success_pct, failures_24h, reed_closed
from fleet_now where {DEV} order by last_seen desc""", 0, y, 24, 11, fmt="table",
    field={"custom": {"filterable": True, "align": "auto"}}, overrides=fleet_overrides,
    options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "last_seen", "desc": True}]},
    desc="Latest report from every device. Counters are lifetime totals; success_pct is successes/attempts over the device's whole life."))
y += 11

# ----------------------------------------------------------- connectivity
panels.append(row("Connectivity", y)); y += 1
panels.append(panel("state-timeline", "Online / offline", f"""
select ts as time, label as device, status from device_status {LBL}
where {TF} and {DEV} order by ts""", 0, y, 24, 9, fmt="table",
    field={"custom": {"fillOpacity": 70, "lineWidth": 0}, "color": {"mode": "fixed"}},
    overrides=[
        {"matcher": {"id": "byValue", "options": {"op": "eq", "reducer": "allValues", "value": "online"}}, "properties": []},
    ],
    options={"mergeValues": True, "showValue": "never", "alignValue": "left", "rowHeight": 0.8,
             "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
    transformations=[{"id": "partitionByValues", "options": {"fields": ["device"], "keepFields": False, "naming": {"asLabels": False}}}],
    desc="spark/status events. Devices sleep between reports, so 'offline' is the normal resting state; what matters is the cadence."))
# value mappings for colours
panels[-1]["fieldConfig"]["defaults"]["mappings"] = [
    {"type": "value", "options": {"online": {"color": "green", "index": 0}, "offline": {"color": "dark-gray", "index": 1}, "auto-update": {"color": "blue", "index": 2}}}]
panels[-1]["fieldConfig"]["overrides"] = []
y += 9

panels += [
    ts("RSRP (Device OS diagnostics)", f"""
select ts as time, label as metric, rsrp_dbm from diagnostics {LBL}
where {TF} and {DEV} and rsrp_dbm is not null order by ts""", 0, y, unit="dBm",
       desc="Serving-cell RSRP as reported at each cloud handshake. Below about -110 dBm Cat-M1 attach gets slow."),
    ts("SINR (rf_survey)", f"""
select ts as time, label as metric, sinr from rf_survey {LBL}
where {TF} and {DEV} order by ts""", 12, y, unit="dB"),
]
y += 8
panels += [
    ts("Attach time per cycle (Δ modemSearchSec)", f"""
select ts as time, label as metric, d_search_sec from telemetry_delta {LBL}
where {TF} and {DEV} order by ts""", 0, y, unit="s", draw="points",
       field={"custom": {"drawStyle": "points", "showPoints": "always", "pointSize": 4}},
       desc="Seconds the modem spent searching for a network in each wake cycle. modemSearchSec is a lifetime counter; this is its difference between consecutive reports (null across a reboot)."),
    ts("Connect failures per hour", f"""
select $__timeGroupAlias(ts, 1h), label as metric, count(*) as failures from connect_failure {LBL}
where {TF} and {DEV} group by 1, 2 order by 1""", 12, y, draw="bars",
       field={"custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"}}}),
]
y += 8
panels.append(panel("table", "Recent connect failures", f"""
select ts, label as device, search_sec, age_sec, ceer, cgatt, cops,
       band, dl_mhz, tac, cell_id, pci, rsrp_dbm, sinr, raw
from connect_failures {LBL} where {TF} and {DEV} order by ts desc limit 200""", 0, y, 24, 8, fmt="table",
    field={"custom": {"filterable": True}},
    overrides=[{"matcher": {"id": "byName", "options": "ts"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
               {"matcher": {"id": "byName", "options": "rsrp_dbm"}, "properties": [{"id": "unit", "value": "dBm"}]},
               {"matcher": {"id": "byName", "options": "dl_mhz"}, "properties": [{"id": "unit", "value": "MHz"}, {"id": "decimals", "value": 1}]},
               {"matcher": {"id": "byName", "options": "raw"}, "properties": [{"id": "custom.width", "value": 600}]}],
    desc="AT dump captured when the connect budget ran out, taken apart: CEER is the modem's reject cause, CGATT 0 = never attached, and band/cell/RSRP are what it was camped on at that moment (empty = no cell at all)."))
y += 8

# ------------------------------------------------------------------ radio
panels.append(row("Radio", y)); y += 1
panels += [
    state_timeline("Serving cell", f"""
select ts as time, label as device, tac || ' / ' || cell_id as cell from connections {LBL}
where {TF} and {DEV} order by ts""", 0, y, 12, 9,
        desc="TAC / E-UTRAN cell id the device attached through, per report. A colour change is a different cell; the same eNodeB with another sector differs only in the last two hex digits."),
    state_timeline("Band", f"""
select ts as time, label as device, 'B' || band || ' · ' || dl_mhz || ' MHz' as band from connections {LBL}
where {TF} and {DEV} order by ts""", 12, y, 12, 9,
        desc="LTE band and downlink centre frequency (from the EARFCN) per report."),
]
y += 9

sig_overrides = [
    {"matcher": {"id": "byName", "options": "rsrp_dbm"},
     "properties": [{"id": "unit", "value": "dBm"}, {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (-110, "orange"), (-100, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "sinr"},
     "properties": [{"id": "unit", "value": "dB"}, {"id": "decimals", "value": 1}, {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                    {"id": "thresholds", "value": thresholds((None, "red"), (0, "orange"), (5, "green"))["thresholds"]}]},
    {"matcher": {"id": "byName", "options": "rsrq_db"}, "properties": [{"id": "unit", "value": "dB"}, {"id": "decimals", "value": 1}]},
    {"matcher": {"id": "byName", "options": "dl_mhz"}, "properties": [{"id": "unit", "value": "MHz"}, {"id": "decimals", "value": 1}]},
    {"matcher": {"id": "byName", "options": "bw_mhz"}, "properties": [{"id": "unit", "value": "MHz"}]},
    {"matcher": {"id": "byName", "options": "ts"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
    {"matcher": {"id": "byName", "options": "last_seen"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
]
panels.append(panel("table", "Cells seen", f"""
select tac, cell_id, enb, sector, pci, band, dl_mhz, bw_mhz, operator,
       count(*) as connections,
       count(distinct device_id) as devices,
       string_agg(distinct label, ', ' order by label) as device_names,
       round(avg(rsrp_dbm))::int as rsrp_dbm,
       min(rsrp_dbm) as rsrp_min,
       round(avg(sinr)::numeric, 1) as sinr,
       max(ts) as last_seen
from connections {LBL}
where {TF} and {DEV}
group by 1, 2, 3, 4, 5, 6, 7, 8, 9 order by connections desc""", 0, y, 24, 9, fmt="table",
    field={"custom": {"filterable": True}}, overrides=sig_overrides,
    options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "connections", "desc": True}]},
    desc="Every cell the selected devices attached through in the time range, with how often and how well. rsrp_dbm/sinr are averages over those connections."))
y += 9

panels += [
    ts("Connections per cell, per day", f"""
select $__timeGroupAlias(ts, 1d), tac || '/' || cell_id as metric, count(*) from connections
where {TF} and {DEV} group by 1, 2 order by 1""", 0, y, draw="bars",
       field={"custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"}}},
       desc="How the selected devices' connections split across cells, day by day."),
    ts("RSRP by band", f"""
select ts as time, 'B' || band as metric, rsrp_dbm from connections
where {TF} and {DEV} order by ts""", 12, y, unit="dBm", draw="points",
       field={"custom": {"drawStyle": "points", "showPoints": "always", "pointSize": 3}},
       desc="Every connection's RSRP, coloured by band. Shows whether the modem's band choice is costing signal."),
]
y += 8

panels.append(panel("table", "Connections", f"""
select ts, label as device, operator, band, dl_mhz, bw_mhz, tac, cell_id, enb, sector, pci,
       rsrp_dbm, rsrq_db, sinr, os_rsrp_dbm
from connections {LBL}
where {TF} and {DEV} order by ts desc limit 1000""", 0, y, 24, 10, fmt="table",
    field={"custom": {"filterable": True}}, overrides=sig_overrides + [
        {"matcher": {"id": "byName", "options": "os_rsrp_dbm"}, "properties": [{"id": "unit", "value": "dBm"}, {"id": "displayName", "value": "rsrp (OS)"}]}],
    options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "ts", "desc": True}]},
    desc="One row per report: serving cell and frequency from the modem's +UCGED, operator and the OS's own RSRP from the diagnostics of the same wake cycle. Newest 1000 in the range."))
y += 10

# ------------------------------------------------------------------ power
panels.append(row("Power", y)); y += 1
panels += [
    ts("Battery", f"""
select ts as time, label as metric, battery from telemetry {LBL}
where {TF} and {DEV} order by ts""", 0, y, unit="percent", field={"min": 0, "max": 100}),
    ts("Battery voltage (idle)", f"""
select ts as time, label as metric, vbat_idle from telemetry {LBL}
where {TF} and {DEV} order by ts""", 12, y, unit="volt", field={"decimals": 2}),
]
y += 8

# ---------------------------------------------------------------- backend
panels.append(row("Backend", y)); y += 1
panels += [
    ts("Webhook errors per hour, by cause", f"""
select $__timeGroupAlias(ts, 1h),
       coalesce(substring(message from 'from (.*)$'), message) as metric,
       count(*) as errors
from hook_event where kind = 'error' and {TF} group by 1, 2 order by 1""", 0, y, draw="bars",
       field={"custom": {"drawStyle": "bars", "fillOpacity": 70, "stacking": {"mode": "normal"}}},
       desc="hook-error events grouped by the host (or Particle's own message) that produced them."),
    ts("Webhook deliveries per hour", f"""
select $__timeGroupAlias(ts, 1h), kind as metric, count(*) from hook_event
where hook = 'telemetry' and {TF} group by 1, 2 order by 1""", 12, y,
       desc="For the telemetry hook: sent vs. response (delivered) vs. error. sent counts every configured integration, so it is a multiple of the telemetry rows."),
]
y += 8
panels += [
    panel("table", "Firmware versions", """
select fw_version, count(*) as devices, string_agg(label, ', ' order by label) as device_names
from device_label join device using (device_id) group by 1 order by 1 desc""", 0, y, 12, 6, fmt="table"),
    panel("table", "Resets & flashes", f"""
select ts, label as device, name as event, data from other_event {LBL}
where {TF} and {DEV} order by ts desc limit 100""", 12, y, 12, 6, fmt="table",
          overrides=[{"matcher": {"id": "byName", "options": "ts"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]}],
          desc="spark/device/last_reset, spark/flash/status and app-hash events."),
]

dash = {
    "uid": "fleet",
    "title": "Fleet",
    "tags": ["particle"],
    "timezone": "utc",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-7d", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "templating": {"list": [
        {"name": "device", "label": "Device", "type": "query", "datasource": DS,
         # the picker filters on __text, so both name and id are in it and either can be typed
         "query": DEVICE_VAR_SQL, "definition": DEVICE_VAR_SQL,
         "multi": True, "includeAll": True, "allValue": None, "refresh": 1, "sort": 1,
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}}
    ]},
    "panels": panels,
}
print(json.dumps(dash, indent=1, ensure_ascii=False))
