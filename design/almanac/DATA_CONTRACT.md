# Almanac overlay — live data contract

The console (data engine) writes this JSON to `wx.json`; the overlay HTML polls it
(~every 2 s) and updates text + gauges. Flat object, display-ready primitives
(numbers/strings), **not** the app's `[value, unit, ...]` lists. Missing/None → `null`;
the HTML shows an em-dash for null. Emitter converts from the app's
`Obs`/`Astro`/`Met`/`Sager`/`System` DictProperties (see the field map in the code-explorer notes / lib/properties.py).

```jsonc
{
  "ts": 1750000000,                 // epoch seconds when written (HTML shows "stale" if too old)
  "station": "Seattle",             // [Station] Name
  "date": "Fri, 31 Jul 2026",       // System['date']
  "time": "12:52",                  // System['time']  (HH:MM)

  // Temperature
  "temp": 64.0, "tempUnit": "°F",   // Obs['outTemp'][0],[1]
  "feelsLike": 64, "feelsDesc": "Warm",   // Obs['FeelsLike'][0],[2] with the core's "Feeling " prefix dropped
  "tempTrendPerHr": 4.6,            // Obs['outTempTrend'][0]  (+ = rising)
  "temp24hDelta": 4.9,             // vs this time yesterday (+ = warmer); null if unknown
  "obsLow": 49.8,  "obsLowTime": "06:15",   // Obs['outTempMin'][0],[2]
  "obsHigh": 64.8, "obsHighTime": "08:04",  // Obs['outTempMax'][0],[2]
  "fcLow": 50, "fcHigh": 82,        // forecast today low/high (Met[...] lowTemp/highTemp)
  "humidity": 67, "dewPoint": 52.9, // Obs['Humidity'][0], Obs['DewPoint'][0]

  // Conditions / short-term forecast
  "conditions": "Clear & Sunny",           // Met['Conditions']
  "conditionsNote": "Clear until 02:00 tomorrow",
  "fcHour": "10:00", "fcWind": "0 mph NW", "fcPrecipPct": 0, "fcDailyPct": 0,
  // 7-day outlook (Open-Meteo daily, hourly refresh; [] hides the band).
  // hi/lo are whole degrees in the console's own temp unit; code is the WMO
  // weather code; pp is max precipitation probability for the day.
  // The TODAY row's hi/lo are overridden with fcLow/fcHigh (WeatherFlow) when
  // known, so the hero and the band never disagree about today.
  "fcDaily": [{"day": "SAT", "date": "2026-08-29", "today": true, "hi": 64, "lo": 54, "code": 95, "pp": 95}],
  "fcStale": false,   // true when no successful forecast fetch in 24 h; the console hides the band

  // Wind  (dir in degrees; cardinal string; needle rotates to dir)
  "windSpd": 0.9, "windUnit": "mph", "windAvg": 0.1, "windGust": 2.9, "windMax": 4.3,
  "windDir": 206, "windCardinal": "SSW", "windStatus": "Calm",

  // Barometer  (needle maps slp on 980..1050)
  "slp": 1022.1, "slpUnit": "mb", "slpTrendPerHr": 0.4, "slpTrendDesc": "Rising",
  "slpSeries": [[1756400000, 1018.4], [1756401800, 1018.2]],   // 24h barograph trace, <=48 [epoch, slp] points in slpUnit; [] hides the graph
  "slp24High": 1022.1, "slp24HighTime": "08:20",
  "slp24Low": 1020.2,  "slp24LowTime": "00:00", "slpOutlook": "Unchanged",

  // Rainfall  (rainRateMm drives the tube: mm/hr mapped onto the console core's
  //  own intensity bands 0.25/1/4/16/50 mm/hr, each an equal fifth of the tube.
  //  rainRate is the same rate in display units, for the printed readout.)
  "rainToday": 0.00, "rainYest": 0.00, "rainMonth": 0.15, "rainYear": 40.0,
  //  rainRateMm is max(the sensor's raw minute, the 10-min time-weighted mean):
  //  the haptic sensor reports drizzle as an occasional trace minute with zeros
  //  between, and the window bridges those so light rain never reads as dry.
  //  rainRateInstMm is the raw minute; rainStatus takes the band word for the
  //  windowed rate when the core says "Currently Dry" inside a drizzle.)
  "rainUnit": "in", "rainRate": 0, "rainRateMm": 0, "rainRateInstMm": 0, "rainStatus": "Currently Dry",
  "drySpellDays": 12, "lastRainDate": "Sun 19 Jul", "lastRainAmt": 0.11,

  // Sun & UV  (sunFrac 0..1 = elapsed fraction of daylight → sun position on the arc)
  "uvIndex": 4.0, "uvDesc": "Moderate", "radiation": 468, "radUnit": "W/m²",
  "sunrise": "05:43", "sunset": "20:43", "sunFrac": 0.29,
  "daylight": "11h 37m", "peakSun": 0.65,

  // Air Quality  (US EPA AQI by station lat/lon, Open-Meteo; null hides the block)
  "aqi": 40, "aqiCategory": "Good", "aqiPm25": 13.8,
  // short forecast so a rising smoke event is visible before the number degrades
  "aqiForecast": [[1754247600, 40], [1754251200, 43]], "aqiPeak": 55, "aqiPeakTime": "5 PM",
  "aqiForecastCat": "Moderate", "aqiTrend": "rising", "aqiTrendText": "Moderate by 5 PM",
  "aqiStale": false,          // true if the last successful AQI fetch is > 1 h old

  // Weather alerts  (active NWS alerts by lat/lon; same-type collapsed, capped 3,
  // sorted by product level: warning > watch > advisory > alert > statement)
  "alerts": [
    { "event": "Air Quality Alert", "eventClass": "air", "level": "advisory",
      "tone": "brass",        // banner colour token: accent | brass | water | verdigris
      "priority": 2,          // level int (4 = warning, highest)
      "short": "Wildfire smoke", "areaShort": "King, Kitsap, Pierce +2",
      "onset": 1754251380, "until": 1754438400, "untilText": "Wed 5 PM",
      "headline": "Air Quality Alert issued August 3 …" }
  ],
  "alertCount": 1,            // distinct types; the HTML shows "+N more" = count-1
  "alertsStale": false, "alertsAsOf": "13:52",   // last successful fetch; stale after 1 h

  // Moon
  "moonPhase": "Waning Gibbous", "moonIllum": 78,
  "moonrise": "22:14", "moonset": "09:38", "nextFull": "Aug 8", "nextNew": "Aug 23",

  // Lightning  (distance only — no bearing; null when quiet)
  // lightningDist is the core's +/-3 km RANGE text ("13-17"); lightningDistNum is
  // its midpoint, which is what the ring geometry and big-number readouts use.
  "lightningActive": false, "lightningDist": null, "lightningDistNum": null,
  "lightningDistUnit": "miles", "lightningSinceSec": null,
  // lightningRate = strikes/min (the core's StrikeFreq); lightning3hr = the
  // rolling 3-hour count. There is no 3-min/30-min bucket in the data path.
  "lightningRate": 0, "lightning3hr": 0, "lightningToday": 0,
  "lightningLast": "3 days ago",

  // Sager
  "sagerCode": "G·2·3·D", "sagerText": "Fair, little temperature change...",
  "sagerPressure": "1022.1 rising", "sagerWind": "SSW backing", "sagerSky": "Clear"
}
```

Rules: numbers are numbers (HTML formats). Times are `"HH:MM"` strings. Angles/fractions
are numeric so the HTML can drive SVG geometry. The HTML treats any `null`/missing key as
an em-dash and leaves that gauge at a neutral position. The emitter must never write a
partial/invalid file (write to a temp path + atomic rename).
