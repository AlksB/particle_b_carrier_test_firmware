import Particle from 'particle:core';

// Trigger: spark/device/diagnostics/update (Device Vitals), published on every
// cloud connection. Vitals carry the serving cell identity, so no firmware
// change is needed.
//
// The device's `geo` ledger keeps a map of recently seen towers with their
// fixes. Same tower as last time -> bump confirmedAt. A different but already
// known tower -> republish its cached fix, no Google call. Unknown or stale
// tower -> publish geo_lookup; the webhook asks Google and geo-store writes the
// answer back. A lookup that never got an answer is marked notFound here.

const STALE_DAYS = 90;      // tower coordinates don't move; re-ask rarely
const NOTFOUND_DAYS = 7;    // but Google keeps adding towers; retry misses sooner
const LOOKUP_TIMEOUT_MS = 10 * 60e3;   // no answer by then = miss
const MAX_TOWERS = 20;      // per device; evict least recently seen

// Device OS radio_access_technology -> Google radioType
const RAT_MAP = {
  'LTE': 'lte', 'LTE Cat-M1': 'lte', 'LTE Cat-1': 'lte', 'LTE Cat-M1/NB1': 'lte',
  '3G': 'wcdma', 'UMTS': 'wcdma',
  '2G': 'gsm', 'GSM': 'gsm',
};

export default function process({ event }) {
  if (!event.eventData) return;

  const v = JSON.parse(event.eventData);
  const cell = v?.device?.network?.cellular;
  const cgi = cell?.cell_global_identity;
  if (!cgi || !cgi.cell_id) return;   // Wi-Fi device, or the modem didn't answer

  const tower = {
    rat: RAT_MAP[cell.radio_access_technology] || 'lte',
    mcc: Number(cgi.mobile_country_code),
    mnc: Number(cgi.mobile_network_code),   // a string in vitals ("01")
    lac: Number(cgi.location_area_code),    // TAC on LTE
    cid: Number(cgi.cell_id),
    rsrp: v?.device?.network?.signal?.strengthv ?? -100,   // dBm
  };
  const key = `${tower.mcc}-${tower.mnc}-${tower.lac}-${tower.cid}`;

  const ledger = Particle.ledger('geo');
  const cur = ledger.get().data || {};
  const towers = cur.towers || {};
  const now = Date.now();

  // A lookup we asked for earlier and never heard back about: record the miss
  // on that tower so it isn't re-asked on every vitals update.
  if (cur.pending && cur.current && towers[cur.current]
      && now - (cur.lookupAt || 0) > LOOKUP_TIMEOUT_MS) {
    towers[cur.current].notFound = true;
    towers[cur.current].fetchedAt = cur.lookupAt || now;
    cur.pending = false;
    console.log('no answer for', cur.current, '- marked notFound');
  }

  const known = towers[key];
  const staleDays = known?.notFound ? NOTFOUND_DAYS : STALE_DAYS;
  const fresh = known?.fetchedAt && (now - known.fetchedAt) < staleDays * 86400e3;

  towers[key] = { ...(known || {}), ...tower, lastSeen: now };
  evict(towers, key);

  if (fresh) {
    const changed = cur.current !== key;
    const update = { current: key, confirmedAt: now, pending: false, towers };
    if (changed && known.lat != null) {
      // Back on a tower we already located: serve the fix from cache.
      update.lat = known.lat; update.lng = known.lng; update.accuracy = known.accuracy;
      Particle.publish('geo_fix',
        { lat: known.lat, lng: known.lng, accuracy: known.accuracy,
          mcc: tower.mcc, mnc: tower.mnc, lac: tower.lac, cid: tower.cid, cached: true },
        { productId: event.productId, asDeviceId: event.deviceId });
      console.log('cached fix for', key);
    }
    ledger.set(update, Particle.MERGE);
    return;
  }

  // Unknown or stale tower: ask Google. geo-store resolves `current` to the
  // tower key when the answer arrives.
  console.log('lookup', key);
  ledger.set({ current: key, confirmedAt: now, lookupAt: now, pending: true, towers },
             Particle.MERGE);
  Particle.publish('geo_lookup', tower,
                   { productId: event.productId, asDeviceId: event.deviceId });
}

// Keep the map bounded: drop the least recently seen towers, never `keep`.
function evict(towers, keep) {
  const keys = Object.keys(towers);
  if (keys.length <= MAX_TOWERS) return;
  keys.filter(k => k !== keep)
      .sort((a, b) => (towers[a].lastSeen || 0) - (towers[b].lastSeen || 0))
      .slice(0, keys.length - MAX_TOWERS)
      .forEach(k => delete towers[k]);
}
