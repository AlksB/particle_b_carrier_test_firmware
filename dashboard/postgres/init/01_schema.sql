-- Fleet event store. One table per event the firmware publishes, plus the
-- Device OS diagnostics and the webhook echo. Every table keys on
-- (device_id, ts) with published_at as ts, so re-running the ingest over the
-- same log is a no-op (ON CONFLICT DO NOTHING).

create table if not exists device (
    device_id     text primary key,
    name          text,            -- from the Particle device list, synced by the ingest
    fw_version    int,             -- product firmware version from the last event
    app_hash      text,            -- spark/device/app-hash
    last_reset    text,            -- spark/device/last_reset
    last_seen     timestamptz,
    first_seen    timestamptz
);

-- spark/status: online / offline / auto-update
create table if not exists device_status (
    ts         timestamptz not null,
    device_id  text        not null,
    status     text        not null,
    primary key (device_id, ts)
);

-- telemetry (firmware publish). The connect*/modem* columns are lifetime
-- counters: difference them (see telemetry_delta) before plotting.
create table if not exists telemetry (
    ts                 timestamptz not null,
    device_id          text        not null,
    fw_version         int,
    reed_closed        bool,
    battery            smallint,
    vbat_load          real,
    vbat_idle          real,
    signal_strength    smallint,
    signal_quality     smallint,
    connect_attempts   int,
    connect_successes  int,
    modem_search_sec   int,
    modem_ready_sec    int,
    modem_offgoing_sec int,
    slot_offset_sec    int,
    primary key (device_id, ts)
);

-- rf_survey (firmware publish, parsed +UCGED)
create table if not exists rf_survey (
    ts           timestamptz not null,
    device_id    text        not null,
    earfcn       int,
    band         smallint,
    ul_bw        smallint,
    dl_bw        smallint,
    tac          text,
    cell_id      text,
    pci          int,
    rsrp_idx     smallint,   -- 0..97, dBm = idx - 141
    rsrq_idx     smallint,   -- 0..34, dB  = idx/2 - 20
    sinr         real,
    rrc          smallint,
    ri           smallint,
    cqi          smallint,
    avg_rsrp_idx smallint,
    pusch_pwr    smallint,
    pucch_pwr    smallint,
    ucged_raw    text,
    primary key (device_id, ts)
);

-- connect_failure (firmware publish): AT dump at the moment the connect
-- budget ran out
create table if not exists connect_failure (
    ts          timestamptz not null,
    device_id   text        not null,
    at_epoch    bigint,
    age_sec     int,
    search_sec  int,
    raw         text,
    primary key (device_id, ts)
);

-- reed_changed (firmware publish)
create table if not exists reed_changed (
    ts          timestamptz not null,
    device_id   text        not null,
    reed_closed bool,
    device_time text,
    primary key (device_id, ts)
);

-- firmware_info (firmware publish at boot)
create table if not exists firmware_info (
    ts         timestamptz not null,
    device_id  text        not null,
    version    int,
    commit     text,
    primary key (device_id, ts)
);

-- geolocation: published as the device by the Logic functions in
-- cloud/geolocation on every connection - the serving cell's position from
-- Google's geolocation API (cached = answered from the device's tower map
-- without a Google call). accuracy_m is Google's radius: ~150-200 m in a
-- city, a kilometre or worse outside.
create table if not exists geolocation (
    ts          timestamptz not null,
    device_id   text        not null,
    lat         double precision,
    lng         double precision,
    accuracy_m  real,
    mcc         int,
    mnc         text,
    lac         int,
    cid         bigint,
    cached      bool,
    primary key (device_id, ts)
);

-- spark/device/diagnostics/update: the fields worth a column. raw holds the
-- whole payload only when the ingest runs with --keep-raw (~2 KB per row).
create table if not exists diagnostics (
    ts                  timestamptz not null,
    device_id           text        not null,
    rat                 text,
    operator            text,
    mcc                 int,
    mnc                 text,
    lac                 int,
    cell_id             bigint,
    rsrp_dbm            real,
    rsrq_db             real,
    strength_pct        real,
    quality_pct         real,
    cell_status         text,
    cell_attempts       int,
    cell_disconnects    int,
    cell_disc_reason    text,
    cloud_status        text,
    cloud_error         int,
    cloud_attempts      int,
    cloud_disconnects   int,
    cloud_disc_reason   text,
    coap_transmit       int,
    coap_retransmit     int,
    coap_round_trip     int,
    publish_rate_limited int,
    battery_state       text,
    power_source        text,
    uptime_sec          bigint,
    mem_used            int,
    mem_total           int,
    os_version          bigint,
    panic_code          int,
    raw                 jsonb,
    primary key (device_id, ts)
);

-- Webhook echo (coreid = particle-internal). kind: sent / response / error.
-- Only errors carry a message; responses carry the device the hook fired for.
-- Several hooks on the same event fire within the same millisecond now and
-- then and collapse into one row; the counts are indicative, not exact.
create table if not exists hook_event (
    ts         timestamptz not null,
    kind       text        not null,
    hook       text        not null,
    device_id  text,
    message    text,
    primary key (ts, kind, hook)
);

-- Anything not handled above (spark/flash/status, app-hash, last_reset, ...).
-- particle/device/updates/* is dropped by the ingest as pure noise.
create table if not exists other_event (
    ts         timestamptz not null,
    device_id  text        not null,
    name       text        not null,
    data       text,
    primary key (device_id, name, ts)
);

create index if not exists device_status_ts   on device_status (ts);
create index if not exists telemetry_ts       on telemetry (ts);
create index if not exists rf_survey_ts       on rf_survey (ts);
create index if not exists diagnostics_ts     on diagnostics (ts);
create index if not exists connect_failure_ts on connect_failure (ts);
create index if not exists geolocation_ts     on geolocation (ts);
create index if not exists hook_event_ts      on hook_event (ts);

-- ------------------------------------------------------------------ views
-- drop + create rather than "or replace": the ingest re-runs this file at
-- startup, and "or replace" refuses a view whose column list changed shape.
-- cascade, because fleet_now is built on the others and gets recreated
-- further down anyway.

-- Per-cycle deltas of the lifetime counters, one row per telemetry report.
-- A negative delta means the device rebooted (counters live in RAM), so it
-- is reported as null rather than a bogus number.
drop view if exists telemetry_delta cascade;
create view telemetry_delta as
select ts, device_id, fw_version,
       nullif(greatest(connect_attempts   - lag(connect_attempts)   over w, -1), -1) as d_attempts,
       nullif(greatest(connect_successes  - lag(connect_successes)  over w, -1), -1) as d_successes,
       nullif(greatest(modem_search_sec   - lag(modem_search_sec)   over w, -1), -1) as d_search_sec,
       nullif(greatest(modem_ready_sec    - lag(modem_ready_sec)    over w, -1), -1) as d_ready_sec,
       nullif(greatest(modem_offgoing_sec - lag(modem_offgoing_sec) over w, -1), -1) as d_offgoing_sec,
       extract(epoch from ts - lag(ts) over w)::int as d_wall_sec
from telemetry
window w as (partition by device_id order by ts);

-- DL centre frequency for an LTE band + EARFCN (3GPP 36.101 table 5.7.3-1):
-- F = F_DL_low + 0.1 * (N_DL - N_Offs_DL). The bands a SARA-R510 can camp on.
create or replace function earfcn_dl_mhz(band int, earfcn int) returns real
language sql immutable as $$
    select (case band
        when 1  then 2110 + 0.1 * (earfcn - 0)
        when 2  then 1930 + 0.1 * (earfcn - 600)
        when 3  then 1805 + 0.1 * (earfcn - 1200)
        when 4  then 2110 + 0.1 * (earfcn - 1950)
        when 5  then  869 + 0.1 * (earfcn - 2400)
        when 8  then  925 + 0.1 * (earfcn - 3450)
        when 12 then  729 + 0.1 * (earfcn - 5010)
        when 13 then  746 + 0.1 * (earfcn - 5180)
        when 14 then  758 + 0.1 * (earfcn - 5280)
        when 17 then  734 + 0.1 * (earfcn - 5730)
        when 18 then  860 + 0.1 * (earfcn - 5850)
        when 19 then  875 + 0.1 * (earfcn - 6000)
        when 20 then  791 + 0.1 * (earfcn - 6150)
        when 25 then 1930 + 0.1 * (earfcn - 8040)
        when 26 then  859 + 0.1 * (earfcn - 8690)
        when 28 then  758 + 0.1 * (earfcn - 9210)
        when 66 then 2110 + 0.1 * (earfcn - 66436)
        when 71 then  617 + 0.1 * (earfcn - 68586)
        when 85 then  728 + 0.1 * (earfcn - 70366)
    end)::real
$$;

-- Channel bandwidth from the PRB count UCGED reports.
create or replace function prb_to_mhz(prb int) returns real
language sql immutable as $$
    select (case prb when 6 then 1.4 when 15 then 3 when 25 then 5
                     when 50 then 10 when 75 then 15 when 100 then 20 end)::real
$$;

-- Name to show for a device: its Particle name, else the id.
drop view if exists device_label cascade;
create view device_label as
select device_id, coalesce(name, device_id) as label from device;

-- One row per connection with everything known about the serving cell: the
-- firmware's rf_survey (band, EARFCN, cell, PCI, SINR) plus the operator and
-- the OS's own RSRP from the diagnostics report of the same wake cycle,
-- matched as the nearest one within ten minutes. The E-UTRAN cell id
-- splits into eNodeB (upper 20 bits, the tower) and sector (low 8 bits).
-- RSRP/RSRQ come as 3GPP 36.133 indices: dBm = idx - 141, dB = idx/2 - 20;
-- -1 means the modem did not report them.
drop view if exists connections cascade;
create view connections as
select r.ts, r.device_id,
       d.operator, d.mcc, d.mnc,
       r.band, r.earfcn,
       earfcn_dl_mhz(r.band, r.earfcn) as dl_mhz,
       prb_to_mhz(r.dl_bw)             as bw_mhz,
       upper(r.tac)                    as tac,
       upper(lpad(r.cell_id, 8, '0'))  as cell_id,
       case when r.cell_id ~ '^[0-9a-fA-F]{1,8}$'
            then ('x' || lpad(r.cell_id, 8, '0'))::bit(32)::int >> 8 end   as enb,
       case when r.cell_id ~ '^[0-9a-fA-F]{1,8}$'
            then ('x' || lpad(r.cell_id, 8, '0'))::bit(32)::int & 255 end  as sector,
       r.pci,
       case when r.rsrp_idx between 0 and 97    then r.rsrp_idx - 141 end       as rsrp_dbm,
       case when r.rsrq_idx between -30 and 46  then (r.rsrq_idx / 2.0 - 20)::real end as rsrq_db,
       case when r.rsrp_idx >= 0 then r.sinr end                              as sinr,
       d.rsrp_dbm as os_rsrp_dbm,
       d.rsrq_db  as os_rsrq_db
from rf_survey r
left join lateral (
    select operator, mcc, mnc, rsrp_dbm, rsrq_db
    from diagnostics d
    where d.device_id = r.device_id
      and d.ts between r.ts - interval '10 minutes' and r.ts + interval '10 minutes'
    order by abs(extract(epoch from d.ts - r.ts))
    limit 1) d on true;

-- connect_failure with the AT dump taken apart: the reject cause, whether
-- the modem was attached, and the cell it was camped on (the UCGED row in
-- the dump has the same layout as rf_survey). band 255 / cell ffffffff is
-- the modem's "no cell".
drop view if exists connect_failures cascade;
create view connect_failures as
select ts, device_id, search_sec, age_sec,
       substring(raw from '\+CEER: ([^|]*)')          as ceer,
       substring(raw from '\+CGATT: ([0-9])')::int    as cgatt,
       substring(raw from '\+COPS: ([^|]*)')          as cops,
       case when band_s ~ '^\d+$' and band_s::int <> 255 then band_s::int end as band,
       case when band_s ~ '^\d+$' and band_s::int <> 255 and earfcn_s ~ '^\d+$'
            then earfcn_dl_mhz(band_s::int, earfcn_s::int) end                as dl_mhz,
       case when tac_s ~ '^[0-9a-fA-F]+$' and lower(tac_s) <> 'ffff' then upper(tac_s) end as tac,
       case when cell_s ~ '^[0-9a-fA-F]+$' and cell_s !~ '^0*$'
            then upper(lpad(cell_s, 8, '0')) end                              as cell_id,
       case when pci_s ~ '^\d+$' then pci_s::int end                         as pci,
       case when rsrp_s ~ '^\d+$' and rsrp_s::int between 0 and 97
            then rsrp_s::int - 141 end                                        as rsrp_dbm,
       case when sinr_s ~ '^-?\d+(\.\d+)?$' and rsrp_s ~ '^\d+$' and rsrp_s::int > 0
            then sinr_s::real end                                             as sinr,
       raw
from (
    select *,
           split_part(u, ',', 1)  as earfcn_s,
           split_part(u, ',', 2)  as band_s,
           split_part(u, ',', 5)  as tac_s,
           split_part(u, ',', 6)  as cell_s,
           split_part(u, ',', 7)  as pci_s,
           split_part(u, ',', 11) as rsrp_s,
           split_part(u, ',', 13) as sinr_s
    from (select *, substring(raw from '\+UCGED: 2\|[^|]*\|([^|]*)') as u
          from connect_failure) f
) f;

-- Where each device is: its newest fix, and how long it has been on that
-- tower. The geolocation event names the tower (mcc-mnc-lac-cid), so a
-- device that moved shows a new key and a new since.
drop view if exists device_position cascade;
create view device_position as
with g as (
    select *,
           mcc || '-' || mnc || '-' || lac || '-' || cid as tower,
           lag(mcc || '-' || mnc || '-' || lac || '-' || cid) over (partition by device_id order by ts) as prev_tower
    from geolocation
    where lat is not null
), runs as (
    select *, sum(case when tower is distinct from prev_tower then 1 else 0 end)
                  over (partition by device_id order by ts) as run
    from g
), latest as (
    select distinct on (device_id) * from runs order by device_id, ts desc
)
select l.device_id, l.ts as fix_at, l.lat, l.lng, l.accuracy_m, l.tower, l.cached,
       to_hex(l.cid) as cell_id,
       (select min(ts) from runs r where r.device_id = l.device_id and r.run = l.run) as on_tower_since
from latest l;

-- One row per device with its latest state, for the "fleet now" table.
drop view if exists fleet_now cascade;
create view fleet_now as
with t as (
    select distinct on (device_id) *
    from telemetry order by device_id, ts desc
), d as (
    select distinct on (device_id) device_id, ts, rsrp_dbm, rsrq_db, operator, rat
    from diagnostics order by device_id, ts desc
), r as (
    select distinct on (device_id) device_id, band, sinr, cell_id
    from rf_survey order by device_id, ts desc
), s as (
    select distinct on (device_id) device_id, status
    from device_status order by device_id, ts desc
), cf as (
    select device_id, count(*) as failures_24h
    from connect_failure where ts > now() - interval '24 hours'
    group by device_id
), g as (
    select device_id, lat, lng, accuracy_m, fix_at, on_tower_since from device_position
)
select dev.device_id,
       coalesce(dev.name, dev.device_id) as name,
       dev.last_seen,
       extract(epoch from now() - dev.last_seen)::int as silent_sec,
       s.status,
       dev.fw_version,
       t.battery, t.vbat_idle, t.vbat_load,
       t.signal_strength, t.signal_quality,
       d.rsrp_dbm, d.rsrq_db, r.sinr, r.band, r.cell_id, d.operator,
       t.connect_attempts, t.connect_successes,
       round(100.0 * t.connect_successes / nullif(t.connect_attempts, 0), 1) as success_pct,
       coalesce(cf.failures_24h, 0) as failures_24h,
       t.reed_closed,
       g.lat, g.lng, g.accuracy_m, g.fix_at, g.on_tower_since
from device dev
left join t  using (device_id)
left join d  using (device_id)
left join r  using (device_id)
left join s  using (device_id)
left join cf using (device_id)
left join g  using (device_id);
