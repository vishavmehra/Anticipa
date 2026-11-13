"use client";

// India map: states first, then the districts of a chosen state. Real boundaries: state and UT outlines from the DataMeet based
// all India seat map (shijithpk/2024_maps_supplement, 2019 reorganisation, full J&K and Ladakh territory as in the Government of India
// map) and DataMeet Census 2011 districts; built by scripts/build-india-geo.mjs. Drawn with d3-geo in ONE svg.
// Material (owner reference): the country is tilted with CSS perspective, every shape is glass (violet gradient, white
// inner edge, top-left sheen), raised on a stepped extrusion with one soft shadow underneath. Hover or selection lifts a
// shape, glows it and drops a pulsing pin; the rest dims. All motion is CSS transform/opacity; no per-frame JS.
// prefers-reduced-motion gives the flat static version (no tilt, extrusion, lift or pulse).
// States load at once (small); the district polygons load only when a state is opened. Pilot districts carry the data,
// every other district is muted as "no coverage". Wheel up over a state opens it, wheel down goes back.

import { geoMercator, geoPath, type GeoPermissibleObjects } from "d3-geo";
import { feature } from "topojson-client";
import type { Topology } from "topojson-specification";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import { useEffect, useId, useMemo, useRef, useState } from "react";

export interface MapDistrict {
  code: string;
  district: string;
  state: string;
  value: number | null;
  display: string;
}

type Props = {
  districts: readonly MapDistrict[];
  state: string | null;
  district: string | null;
  onState: (state: string | null) => void;
  onDistrict: (code: string | null) => void;
  valueLabel: string;
};

const W = 520;
const H = 560;
const SCALE = ["var(--scale-5)", "var(--scale-4)", "var(--scale-3)", "var(--scale-2)", "var(--scale-1)"];
const EMPTY = "var(--chart-empty)";
const DEPTH = 7; // extrusion depth in screen pixels
const norm = (s: string) => s.toLowerCase().replace(/&/g, "and").replace(/[^a-z]/g, "");

function useGeo(url: string | null) {
  const [data, setData] = useState<{ url: string; topo: Topology } | null>(null);
  useEffect(() => {
    if (!url) return;
    const ac = new AbortController();
    fetch(url, { signal: ac.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((topo: Topology) => setData({ url, topo }))
      .catch(() => undefined);
    return () => ac.abort();
  }, [url]);
  return data && data.url === url ? data.topo : null;
}

export function IndiaMap({ districts, state, district, onState, onDistrict, valueLabel }: Props) {
  const uid = useId();
  const statesTopo = useGeo("/geo/india-states.json");
  const distTopo = useGeo(state ? "/geo/india-districts.json" : null);
  const [hover, setHover] = useState<string | null>(null); // state name at country level, district key in a state
  const boxRef = useRef<HTMLDivElement>(null);

  const states = useMemo(() => (statesTopo ? (feature(statesTopo, statesTopo.objects.states) as FeatureCollection<Geometry, { st_nm: string }>) : null), [statesTopo]);
  const allDistricts = useMemo(() => (distTopo ? (feature(distTopo, distTopo.objects.districts) as FeatureCollection<Geometry, { district: string; st_nm: string }>) : null), [distTopo]);
  const projection = useMemo(() => (states ? geoMercator().fitSize([W, H], states as unknown as GeoPermissibleObjects) : null), [states]);
  const path = useMemo(() => (projection ? geoPath(projection) : null), [projection]);

  const stateAgg = useMemo(() => {
    const m = new Map<string, { sum: number; n: number }>();
    for (const d of districts) if (d.value !== null) m.set(norm(d.state), { sum: (m.get(norm(d.state))?.sum ?? 0) + d.value, n: (m.get(norm(d.state))?.n ?? 0) + 1 });
    return m;
  }, [districts]);
  const values = districts.map((d) => d.value).filter((v): v is number => v !== null);
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const bucket = (v: number | null) => (v === null ? -1 : Math.min(4, Math.floor(((v - min) / span) * 5)));

  const pilot = useMemo(() => new Map(districts.map((d) => [`${norm(d.state)}|${norm(d.district)}`, d])), [districts]);
  const selFeature = states?.features.find((f) => norm(f.properties.st_nm) === norm(state ?? ""));

  // Fit the opened state into the same projection by transform, so the move animates.
  const { transform, k, tx, ty } = useMemo(() => {
    if (!path || !selFeature) return { transform: "translate(0 0) scale(1)", k: 1, tx: 0, ty: 0 };
    const [[x0, y0], [x1, y1]] = path.bounds(selFeature as Feature);
    const kk = Math.min(8, 0.86 / Math.max((x1 - x0) / W, (y1 - y0) / H));
    const x = W / 2 - (kk * (x0 + x1)) / 2;
    const y = H / 2 - (kk * (y0 + y1)) / 2;
    return { transform: `translate(${x} ${y}) scale(${kk})`, k: kk, tx: x, ty: y };
  }, [path, selFeature]);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    // Plain scrolling always scrolls the page. Holding Control or Command and scrolling zooms between levels.
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.deltaY < 0 && !state && hover) {
        e.preventDefault();
        onState(hover);
      } else if (e.deltaY > 0 && state) {
        e.preventDefault();
        if (district) onDistrict(null);
        else onState(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !state) return;
      if (district) onDistrict(null);
      else onState(null);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("keydown", onKey);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("keydown", onKey);
    };
  }, [state, hover, district, onState, onDistrict]);

  if (!states || !path) return <div className="skeleton h-[28rem] w-full" role="status" aria-label="Loading map" />;

  const stateDistricts = allDistricts?.features.filter((f) => norm(f.properties.st_nm) === norm(state ?? "")) ?? [];
  const inState = !!state;
  const focus: Feature<Geometry, { name: string; key: string }>[] = inState
    ? stateDistricts.map((f) => ({ ...f, properties: { name: f.properties.district, key: `${norm(f.properties.st_nm)}|${norm(f.properties.district)}` } }) as Feature<Geometry, { name: string; key: string }>)
    : states.features.map((f) => ({ ...f, properties: { name: f.properties.st_nm, key: f.properties.st_nm } }) as Feature<Geometry, { name: string; key: string }>);

  const info = (key: string) => {
    if (inState) return pilot.get(key) ?? null;
    const agg = stateAgg.get(norm(key));
    return agg ? { value: agg.sum / agg.n, state: key } : null;
  };
  const bucketOf = (key: string) => {
    const i = info(key);
    return i ? bucket(i.value) : -1;
  };
  const lifted = (key: string) => hover === key || (inState && !!district && pilot.get(key)?.code === district);
  const anyLift = !!hover || (inState && !!district);
  const hoverLabel = (() => {
    if (!hover) return null;
    if (inState) {
      const d = pilot.get(hover);
      return d ? `${d.district}: ${d.display} ${valueLabel}` : "No coverage";
    }
    const agg = stateAgg.get(norm(hover));
    return agg ? `${hover}: ${(agg.sum / agg.n).toFixed(1)} ${valueLabel}` : `${hover}: no coverage`;
  })();
  const pinFeature = focus.find((f) => lifted(f.properties.key));
  const pin = pinFeature ? path.centroid(pinFeature as Feature) : null;

  const open = (key: string) => {
    if (inState) {
      const d = pilot.get(key);
      if (d) onDistrict(district === d.code ? null : d.code);
    } else if (stateAgg.has(norm(key))) onState(key);
  };
  const clickable = (key: string) => (inState ? pilot.has(key) : stateAgg.has(norm(key)));

  return (
    <div ref={boxRef} className="relative">
      <nav aria-label="Map level" className="mb-1 flex flex-wrap items-center gap-1.5 text-sm">
        <button type="button" onClick={() => { onDistrict(null); onState(null); }} className={`rounded-full px-2.5 py-0.5 font-semibold ${!state ? "bg-brand-soft text-brand-ink" : "text-ink-muted hover:text-ink"}`}>
          India
        </button>
        {state && (
          <>
            <span aria-hidden="true" className="text-ink-muted">›</span>
            <button type="button" onClick={() => onDistrict(null)} className={`rounded-full px-2.5 py-0.5 font-semibold ${!district ? "bg-brand-soft text-brand-ink" : "text-ink-muted hover:text-ink"}`}>
              {state}
            </button>
          </>
        )}
        {district && (
          <>
            <span aria-hidden="true" className="text-ink-muted">›</span>
            <span className="rounded-full bg-brand-soft px-2.5 py-0.5 font-semibold text-brand-ink">{districts.find((d) => d.code === district)?.district ?? district}</span>
          </>
        )}
        <span aria-live="polite" className="ml-auto min-h-5 text-xs text-ink-soft">
          {hoverLabel ?? (state ? "Escape or the breadcrumb goes back" : "Select a state to open it")}
        </span>
      </nav>

      <div className="relative mx-auto w-full max-w-[34rem]">
        {/* halo and faint radial grid behind the country, flat */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-[4%] rounded-full"
          style={{
            background:
              "radial-gradient(circle at 50% 52%, var(--glass-hi) 0%, transparent 70%), repeating-radial-gradient(circle at 50% 52%, transparent 0 38px, var(--glass-edge) 38px 39px)",
            maskImage: "radial-gradient(circle at 50% 52%, #000 40%, transparent 72%)",
            WebkitMaskImage: "radial-gradient(circle at 50% 52%, #000 40%, transparent 72%)",
          }}
        />
        <div style={{ perspective: "1200px" }}>
          <div className="map-tilt" style={{ transform: "rotateX(20deg) rotateZ(-4deg)", transformOrigin: "50% 60%" }}>
            <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`Map of India coloured by ${valueLabel}`} className="mx-auto block h-auto max-h-[34rem] w-full touch-pan-y overflow-visible">
              <defs>
                {SCALE.map((c, i) => (
                  <linearGradient key={c} id={`${uid}-g${i}`} x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" stopColor={c} stopOpacity="0.78" />
                    <stop offset="1" stopColor={c} stopOpacity="1" />
                  </linearGradient>
                ))}
                <linearGradient id={`${uid}-ge`} x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0" stopColor={EMPTY} stopOpacity="0.8" />
                  <stop offset="1" stopColor={EMPTY} stopOpacity="1" />
                </linearGradient>
                <linearGradient id={`${uid}-sheen`} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={W * 0.8} y2={H * 0.8}>
                  <stop offset="0" stopColor="#ffffff" stopOpacity="0.55" />
                  <stop offset="0.55" stopColor="#ffffff" stopOpacity="0" />
                </linearGradient>
                <filter id={`${uid}-shadow`} x="-20%" y="-10%" width="140%" height="150%">
                  <feDropShadow dx="0" dy="16" stdDeviation="11" floodColor="#2b1b66" floodOpacity="0.3" />
                </filter>
                <g id={`${uid}-outline`}>
                  {focus.map((f) => (
                    <path key={f.properties.key} d={path(f as Feature) ?? ""} />
                  ))}
                </g>
              </defs>

              <g filter={`url(#${uid}-shadow)`}>
                <g style={{ transform, transformOrigin: "0 0", transition: "transform 650ms cubic-bezier(0.22, 1, 0.36, 1)" }} className="map-zoom">
                  {/* neighbours stay visible, flat and dim, when a state is open */}
                  {inState &&
                    states.features.map((f) => (
                      <path key={f.properties.st_nm} d={path(f as Feature) ?? ""} fill="var(--chart-grid)" fillOpacity={0.5} stroke="var(--color-surface)" strokeWidth={0.8} vectorEffect="non-scaling-stroke" />
                    ))}

                  {/* extrusion: the same outline stepped down, side walls in a darker tint */}
                  <g className="map-extrude" aria-hidden="true">
                    {[1, 0.75, 0.5, 0.25].map((f) => (
                      <use key={f} href={`#${uid}-outline`} transform={`translate(0 ${(DEPTH * f) / k})`} fill="var(--scale-1)" fillOpacity={0.55} />
                    ))}
                  </g>

                  {/* glass top faces: the interactive layer */}
                  {focus.map((f) => {
                    const key = f.properties.key;
                    const b = bucketOf(key);
                    const up = lifted(key);
                    const can = clickable(key);
                    const dim = anyLift && !up;
                    const data = inState ? pilot.get(key) : null;
                    return (
                      <path
                        key={key}
                        d={path(f as Feature) ?? ""}
                        fill={b < 0 ? `url(#${uid}-ge)` : `url(#${uid}-g${b})`}
                        stroke="rgb(255 255 255 / 0.85)"
                        strokeWidth={up ? 1.6 : 0.9}
                        vectorEffect="non-scaling-stroke"
                        opacity={dim ? 0.72 : 1}
                        className={`map-face ${can ? "cursor-pointer" : ""}`}
                        style={{
                          transformBox: "fill-box",
                          transformOrigin: "center",
                          transform: up ? `translateY(${-5 / k}px) scale(1.03)` : undefined,
                          filter: up ? "drop-shadow(0 0 5px var(--chart-teal))" : undefined,
                        }}
                        role={can ? "button" : undefined}
                        tabIndex={can ? 0 : undefined}
                        aria-label={can ? (inState ? `${data?.district}: ${valueLabel} ${data?.display}` : `Open ${f.properties.name}`) : undefined}
                        aria-pressed={can && inState ? district === data?.code : undefined}
                        onMouseEnter={() => setHover(key)}
                        onMouseLeave={() => setHover(null)}
                        onFocus={() => setHover(key)}
                        onBlur={() => setHover(null)}
                        onClick={() => open(key)}
                        onKeyDown={(e) => can && (e.key === "Enter" || e.key === " ") && (e.preventDefault(), open(key))}
                      />
                    );
                  })}

                  {/* glossy top-left sheen over every face, not interactive */}
                  <use href={`#${uid}-outline`} fill={`url(#${uid}-sheen)`} pointerEvents="none" aria-hidden="true" />

                  {pin && (
                    <g aria-hidden="true" pointerEvents="none" transform={`translate(${pin[0]} ${pin[1] - 5 / k})`}>
                      <circle r={9 / k} fill="var(--color-surface)" opacity={0.7} className="map-pin" />
                      <circle r={4.5 / k} fill="var(--color-surface)" stroke="var(--chart-violet)" strokeWidth={2.2 / k} />
                    </g>
                  )}
                </g>
              </g>

              {/* labels stay a constant size: positioned through the same zoom maths, not scaled with it */}
              {inState &&
                focus.map((f) => {
                  const d = pilot.get(f.properties.key);
                  const c = d && path.centroid(f as Feature);
                  if (!d || !c) return null;
                  return (
                    <text key={d.code} x={tx + c[0] * k} y={ty + c[1] * k + 2} textAnchor="middle" className="pointer-events-none fill-ink text-[10px] font-semibold" style={{ paintOrder: "stroke", stroke: "var(--color-surface)", strokeWidth: 3 }}>
                      {d.district}
                    </text>
                  );
                })}
            </svg>
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-center gap-2 text-xs text-ink-muted">
        <span>Lower</span>
        {SCALE.map((c) => (
          <span key={c} aria-hidden="true" className="h-3 w-7 first:rounded-l-full last:rounded-r-full" style={{ background: c }} />
        ))}
        <span>Higher</span>
        <span aria-hidden="true" className="ml-3 h-3 w-5 rounded-sm border border-line-strong/50" style={{ background: EMPTY }} />
        <span>No coverage</span>
        <span className="ml-3 hidden sm:inline">Ctrl and scroll to zoom</span>
      </div>
      <p className="mt-1 text-center text-[0.6875rem] text-ink-muted">Map data: DataMeet community, CC BY. Outlines include the 2019 reorganisation and the full territory of Jammu and Kashmir and Ladakh as in the Government of India map. Shown for orientation, not as a survey.</p>
    </div>
  );
}
