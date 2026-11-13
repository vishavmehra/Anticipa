// Builds public/geo/india-states.json and public/geo/india-districts.json.
//
//   States    : the all India Lok Sabha seat map from shijithpk/2024_maps_supplement (it builds on the DataMeet parliamentary
//               constituency maps and carries the 2019 reorganisation: Jammu and Kashmir UT and Ladakh UT). Seats are merged by
//               state or UT. The two "999" areas (territory claimed by India but administered by others) are merged into their
//               UT, so the country is drawn as in the Government of India's official map: the "Rest of J&K" area (Pakistan
//               occupied Kashmir) goes with Jammu and Kashmir, the "Rest of Ladakh" area (Gilgit-Baltistan and Aksai Chin) with Ladakh.
//   Districts : DataMeet Census 2011 district boundaries (datameet/maps, website/docs/data/geojson/dists11.geojson). Two names are
//               modernised so they match the pilot map: Bangalore to Bengaluru Urban and Mysore to Mysuru.
//
// Run: node scripts/build-india-geo.mjs <india_ls_seats_545.geojson> <dists11.geojson>
// Credits (shown in the app): "Map data: DataMeet community, CC BY". Boundaries are for orientation, not an official survey.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { bbox, feature, merge, quantize } from "topojson-client";
import { topology } from "topojson-server";
import { presimplify, simplify, quantile } from "topojson-simplify";

const [seatsPath, distPath] = process.argv.slice(2);
if (!seatsPath || !distPath) throw new Error("usage: node scripts/build-india-geo.mjs <india_ls_seats_545.geojson> <dists11.geojson>");

const clean = (s) => s.replace(/&/g, "and").replace(/\s+/g, " ").trim();

// ---- states
const seats = JSON.parse(readFileSync(seatsPath, "utf8"));
const seatFeatures = seats.features.map((f) => ({ type: "Feature", properties: { st_nm: clean(f.properties.state_ut_name) }, geometry: f.geometry }));
const seatTopo = topology({ seats: { type: "FeatureCollection", features: seatFeatures } }, 1e5);
const geoms = seatTopo.objects.seats.geometries;
const names = [...new Set(geoms.map((g) => g.properties.st_nm))].sort();
const statesFc = { type: "FeatureCollection", features: names.map((st_nm) => ({ type: "Feature", properties: { st_nm }, geometry: merge(seatTopo, geoms.filter((g) => g.properties.st_nm === st_nm)) })) };
let st = topology({ states: statesFc }, 1e4);
st = presimplify(st);
st = simplify(st, quantile(st, 0.975));
st.bbox = bbox(st);
st = quantize(st, 1e4); // simplify returns absolute floats; quantising and delta encoding keeps the file small

// ---- districts
const alias = { Bangalore: "Bengaluru Urban", Mysore: "Mysuru" };
const dists = JSON.parse(readFileSync(distPath, "utf8"));
const distFc = {
  type: "FeatureCollection",
  features: dists.features.map((f) => ({ type: "Feature", properties: { district: alias[f.properties.DISTRICT] ?? f.properties.DISTRICT, st_nm: clean(f.properties.ST_NM) }, geometry: f.geometry })),
};
let dt = topology({ districts: distFc }, 1e4);
dt = presimplify(dt);
dt = simplify(dt, quantile(dt, 0.965));
dt.bbox = bbox(dt);
dt = quantize(dt, 1e4);

mkdirSync("public/geo", { recursive: true });
writeFileSync("public/geo/india-states.json", JSON.stringify(st));
writeFileSync("public/geo/india-districts.json", JSON.stringify(dt));
console.log("states", feature(st, st.objects.states).features.length, names.join(", "));
console.log("districts", feature(dt, dt.objects.districts).features.length);
