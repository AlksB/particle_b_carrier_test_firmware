# Cell-tower geolocation, cloud side

Locates each device from the serving cell reported in Device Vitals. Nothing
runs on the device: vitals already carry MCC/MNC/LAC/CID on every cloud
connection (every 6 h on the B404X), and everything else lives in Particle
Cloud services plus one Google Geolocation API call per newly seen tower.

    vitals ──▶ geo-check ──▶ geo_lookup ──▶ webhook ──▶ Google
                  │                                       │
                  │ known tower: geolocation (cached)      ▼
                  └──────────── ledger `geo` ◀── geo-store ──▶ geolocation

Every connection ends in one `geolocation {lat, lng, accuracy, mcc, mnc, lac,
cid, cached}` event from the device: `cached: true` when the tower was
already in the device's map (no Google call), `cached: false` when Google
was just asked. A tower Google does not know produces no event.
Accuracy is ~150–200 m in a dense city, ~1 km or worse elsewhere.

Cost: ~3 Data Operations per connection (Logic run + ledger set +
geolocation event) and 3 more per tower change; Google is called once per tower per 90 days, so a fleet of
2000 stationary devices stays inside Google's free 10K requests/month.

## Files

| file | what |
|---|---|
| `geo-check.js` | Logic. Trigger `spark/device/diagnostics/update`. Decides: confirm / cached fix / ask Google. |
| `geo-store.js` | Logic. Trigger `hook-response/geo_lookup`. Writes Google's answer to the ledger, publishes `geolocation`. |
| `webhook-geo_lookup.json` | Integration template (Custom template tab). Calls Google, returns `{deviceId, lat, lng, accuracy}`. |
| `export-console-events.js` | Browser snippet: dump the virtualized console event table to `events.tsv`. |
| `geo-summary.py` | Count lookups vs connections vs tower changes in an `events.tsv`. |

## Setup (per organization / sandbox)

Logic and Ledger are org-level, not product-level: you need an org-team role
that can reach Cloud services (Developer was not enough on Basic; ask for
Administrator), and 2FA enabled on your account.

1. **Secret** — Cloud services → Secrets: `GOOGLE_GEOLOCATION_API_KEY`. The key
   must come from a Google Cloud project with billing enabled and the
   Geolocation API turned on; it does not work otherwise, even within the
   free tier.
2. **Ledger** — Cloud services → Ledger → Cloud Ledger, name `geo`, scope
   Device.
3. **Webhook** — inside the *product* (not the org): Integrations → New →
   Webhook → Custom template → paste `webhook-geo_lookup.json`, tick the
   secret. Test: publish `geo_lookup` from the product event stream with
   `{"rat":"lte","mcc":310,"mnc":410,"lac":36877,"cid":84534800,"rsrp":-95}`;
   a `hook-response/geo_lookup` JSON must follow (deviceId empty for a manual publish).
4. **Logic `geo-store`** — Event-triggered, paste `geo-store.js`. Test data
   `{"deviceId":"0a10aced202194944a060ad0","lat":41.6968,"lng":44.7945,"accuracy":154}` with a real device ID.
   Deploy with trigger `hook-response/geo_lookup` on the product.
5. **Logic `geo-check`** — paste `geo-check.js`. Test data: a trimmed vitals
   object, e.g. `{"device":{"network":{"cellular":{"radio_access_technology":"LTE","cell_global_identity":{"mobile_country_code":310,"mobile_network_code":"410","location_area_code":36877,"cell_id":84534800}},"signal":{"strengthv":-103}}}}`.
   Deploy with trigger `spark/device/diagnostics/update` on the product.
6. Reset a device (or wait for its next connection) and watch the product
   event stream for `geo_lookup → hook-sent → hook-response → geolocation`.

Numeric product ID (needed for `particle publish --product` and the REST
paths) is in the webhook error log or the product URL, not the slug.

## Things learned the hard way

- "Run code" in the Logic editor is a dry run: ledger writes are real,
  `Particle.publish` is only logged. Test publishes from the event stream.
- Logic triggers match the event name exactly; webhooks match by prefix.
  Hence `unchunked: true` (no `/0` suffix) and explicit `responseTopic`.
- `hook-response` events are not attributed to the device — the ID must go
  in the response body (`{{{PARTICLE_DEVICE_ID}}}` works once the request
  event was published with `asDeviceId`).
- A hard-coded `"radioType":"lte"` 404s on 3G towers; `{{rat}}` from vitals.
- Google's answer for a single tower does not depend on signal strength;
  the cache key is `mcc-mnc-lac-cid` only.
- The console event stream is live only; nothing is stored. Use
  `export-console-events.js` or `particle subscribe --product <id> --all`.

## Reading the result

Event: `geolocation` on the product stream (an API user with scope `events:get`
is enough for a site). Or REST:

    GET https://api.particle.io/v1/products/<id>/ledgers/geo/instances/<deviceId>

Ledger document per device:

    {
      "current": "282-1-16-4739104", "confirmedAt": …, "pending": false,
      "lat": …, "lng": …, "accuracy": …, "fetchedAt": …,
      "towers": { "<mcc-mnc-lac-cid>": { …, "lat", "lng", "accuracy", "fetchedAt", "lastSeen", "notFound" } }
    }
