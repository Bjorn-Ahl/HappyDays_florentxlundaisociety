/*
 * HappyDays — Swedish city lookup.
 *
 * Keyboard-accessible fallback for the map: typing (or autocompleting) a
 * city name snaps the marker to these coordinates. Covers all four
 * electricity price zones (SE1–SE4) from Kiruna down to Malmö.
 *
 * Loaded as a plain <script>; exposes window.SWEDISH_CITIES.
 */
(function (global) {
  "use strict";

  const SWEDISH_CITIES = [
    { name: "Kiruna",    lat: 67.8558, lng: 20.2253 },
    { name: "Luleå",     lat: 65.5848, lng: 22.1547 },
    { name: "Umeå",      lat: 63.8258, lng: 20.2630 },
    { name: "Östersund", lat: 63.1792, lng: 14.6357 },
    { name: "Sundsvall", lat: 62.3908, lng: 17.3069 },
    { name: "Uppsala",   lat: 59.8586, lng: 17.6389 },
    { name: "Stockholm", lat: 59.3293, lng: 18.0686 },
    { name: "Örebro",    lat: 59.2741, lng: 15.2066 },
    { name: "Karlstad",  lat: 59.3793, lng: 13.5036 },
    { name: "Linköping", lat: 58.4108, lng: 15.6214 },
    { name: "Göteborg",  lat: 57.7089, lng: 11.9746 },
    { name: "Visby",     lat: 57.6348, lng: 18.2948 },
    { name: "Jönköping", lat: 57.7826, lng: 14.1618 },
    { name: "Malmö",     lat: 55.6050, lng: 13.0038 },
    { name: "Lund",      lat: 55.7047, lng: 13.1910 },
  ];

  global.SWEDISH_CITIES = SWEDISH_CITIES;
})(window);
