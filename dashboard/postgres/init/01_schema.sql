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
create index if not exists hook_event_ts      on hook_event (ts);

-- ------------------------------------------------------------------ views
-- drop + create rather than "or replace": the ingest re-runs this file at
-- startup, and "or replace" refuses a view whose column list changed shape.

-- Per-cycle deltas of the lifetime counters, one row per telemetry report.
-- A negative delta means the device rebooted (counters live in RAM), so it
-- is reported as null rather than a bogus number.
drop view if exists telemetry_delta;
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

-- Name to show for a device: its Particle name, else the id.
drop view if exists device_label;
create view device_label as
select device_id, coalesce(name, device_id) as label from device;

-- One row per device with its latest state, for the "fleet now" table.
drop view if exists fleet_now;
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
       t.reed_closed
from device dev
left join t  using (device_id)
left join d  using (device_id)
left join r  using (device_id)
left join s  using (device_id)
left join cf using (device_id);
