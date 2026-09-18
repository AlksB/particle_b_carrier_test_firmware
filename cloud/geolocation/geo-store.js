import Particle from 'particle:core';

// Trigger: hook-response/geo_lookup — Google answered the geo_lookup webhook.
// Response template: "<deviceId>,<lat>,<lng>,<accuracy>". The device ID has to
// travel in the body: hook-response events are not attributed to the device
// (event.deviceId is "particle-internal").
//
// Stores the fix on the tower geo-check asked about (`current`), promotes it
// to the top-level fix, and republishes it as a `geo_fix` event from the
// device so the site consumes it like any other device event.

export default function process({ event }) {
  if (!event.eventData) return;

  const [deviceId, latS, lngS, accS] = event.eventData.split(',');
  const lat = Number(latS), lng = Number(lngS), accuracy = Number(accS);
  if (!deviceId || !isFinite(lat) || !isFinite(lng)) {
    console.error('bad hook response', event.eventData);
    return;
  }

  const ledger = Particle.ledger('geo', { deviceId });
  const cur = ledger.get().data || {};
  const key = cur.current;
  const towers = cur.towers || {};
  if (!key || !towers[key]) {
    console.error('no pending tower for', deviceId);
    return;
  }

  const now = Date.now();
  towers[key] = { ...towers[key], lat, lng, accuracy, fetchedAt: now, notFound: false };
  const t = towers[key];

  ledger.set({ lat, lng, accuracy, fetchedAt: now, pending: false, towers }, Particle.MERGE);

  Particle.publish('geo_fix',
    { lat, lng, accuracy, mcc: t.mcc, mnc: t.mnc, lac: t.lac, cid: t.cid, cached: false },
    { productId: event.productId, asDeviceId: deviceId });
}
