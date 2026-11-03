/**
 * The Softnix `WeatherCard`: current conditions drawn as a sky-tinted card with
 * an illustrated icon. Every value comes from the weather_now tool (Open-Meteo);
 * this file only draws. Icons are inline SVG so the card loads nothing remote.
 */
import { useId } from "react";
import { CARD_UI, type CardLang } from "./protocol";

export const WEATHER_CONDITIONS = ["clear", "partlyCloudy", "cloudy", "fog", "drizzle", "rain", "snow", "thunder"] as const;
export type WeatherCondition = (typeof WEATHER_CONDITIONS)[number];

type Props = {
  condition: WeatherCondition;
  isDay: boolean;
  temperature: number;
  unit: "C" | "F";
  label: string;
  date?: string;
  location: string;
  updated?: string;
  outlook?: string;
  hours?: { time: string; chance: number }[];
  lang: CardLang;
};

/** Card background: night wins, then storm, rain, grey skies; sunny skies stay blue. */
function skyOf(condition: WeatherCondition, isDay: boolean): string {
  if (!isDay) return "night";
  if (condition === "thunder") return "storm";
  if (condition === "rain" || condition === "drizzle") return "rain";
  if (condition === "cloudy" || condition === "fog" || condition === "snow") return "overcast";
  return "day";
}

function Cloud({ fill, x = 0, y = 0, scale = 1 }: { fill: string; x?: number; y?: number; scale?: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${scale})`}>
      <circle cx="56" cy="80" r="44" fill={fill} />
      <circle cx="116" cy="54" r="60" fill={fill} />
      <circle cx="174" cy="86" r="40" fill={fill} />
      <rect x="26" y="80" width="182" height="46" rx="23" fill={fill} />
    </g>
  );
}

function WeatherIcon({ condition, isDay }: { condition: WeatherCondition; isDay: boolean }) {
  const id = useId().replace(/:/g, "");
  const g = (name: string) => `${id}-${name}`;
  const url = (name: string) => `url(#${g(name)})`;
  const dark = condition === "rain" || condition === "thunder";
  const cloudFill = url(dark ? "cloud-dark" : "cloud");
  const orb = condition === "clear" || condition === "partlyCloudy";
  const orbAt = condition === "clear" ? { cx: 150, cy: 110, r: 64 } : { cx: 112, cy: 82, r: 52 };
  return (
    <svg className="a2ui-weather-icon" viewBox="0 0 300 230" aria-hidden="true">
      <defs>
        <radialGradient id={g("sun")} cx="38%" cy="32%" r="70%">
          <stop offset="0%" stopColor="#fff6b8" />
          <stop offset="45%" stopColor="#ffcc3d" />
          <stop offset="100%" stopColor="#f08b14" />
        </radialGradient>
        <radialGradient id={g("moon")} cx="35%" cy="30%" r="75%">
          <stop offset="0%" stopColor="#fffbe6" />
          <stop offset="60%" stopColor="#f6e7a8" />
          <stop offset="100%" stopColor="#d9b85a" />
        </radialGradient>
        <radialGradient id={g("glow")} cx="50%" cy="50%" r="50%">
          <stop offset="55%" stopColor="#ffffff" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={g("cloud")} x1="0" y1="0" x2="0.6" y2="1">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="100%" stopColor="#d9e1ec" />
        </linearGradient>
        <linearGradient id={g("cloud-dark")} x1="0" y1="0" x2="0.6" y2="1">
          <stop offset="0%" stopColor="#eef2f8" />
          <stop offset="100%" stopColor="#9eacc2" />
        </linearGradient>
        <linearGradient id={g("drop")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#c4e6ff" />
          <stop offset="100%" stopColor="#4aa3ff" />
        </linearGradient>
        <linearGradient id={g("bolt")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#fff3a0" />
          <stop offset="100%" stopColor="#ffb21a" />
        </linearGradient>
        <filter id={g("shadow")} x="-20%" y="-20%" width="140%" height="160%">
          <feDropShadow dx="0" dy="10" stdDeviation="10" floodColor="#0b2a66" floodOpacity="0.28" />
        </filter>
        <mask id={g("crescent")}>
          <rect width="300" height="230" fill="#ffffff" />
          <circle cx={orbAt.cx + orbAt.r * 0.45} cy={orbAt.cy - orbAt.r * 0.3} r={orbAt.r * 0.85} fill="#000000" />
        </mask>
      </defs>
      {orb ? (
        <>
          <circle cx={orbAt.cx} cy={orbAt.cy} r={orbAt.r * 1.6} fill={url("glow")} />
          {isDay ? (
            <circle cx={orbAt.cx} cy={orbAt.cy} r={orbAt.r} fill={url("sun")} />
          ) : (
            <circle cx={orbAt.cx} cy={orbAt.cy} r={orbAt.r} fill={url("moon")} mask={url("crescent")} />
          )}
        </>
      ) : null}
      {condition === "clear" ? null : (
        <g filter={url("shadow")} opacity="0.97">
          {condition === "cloudy" || condition === "fog" ? <Cloud fill={url("cloud-dark")} x={110} y={14} scale={0.62} /> : null}
          <Cloud fill={cloudFill} x={condition === "partlyCloudy" ? 70 : 40} y={condition === "partlyCloudy" ? 70 : 40} />
        </g>
      )}
      {condition === "rain" || condition === "drizzle" ? (
        <g fill={url("drop")}>
          {(condition === "rain" ? [92, 142, 192] : [112, 172]).map((x) => (
            <path key={x} d={`M${x} 184 q-9 16 0 22 q9 -6 0 -22z`} />
          ))}
        </g>
      ) : null}
      {condition === "thunder" ? <path d="M150 168 l-22 34 h18 l-10 26 l34 -40 h-18 l12 -20z" fill={url("bolt")} /> : null}
      {condition === "snow" ? (
        <g stroke="#ffffff" strokeWidth="4" strokeLinecap="round">
          {[100, 150, 200].map((x) => (
            <g key={x} transform={`translate(${x} 196)`}>
              <line x1="-8" y1="0" x2="8" y2="0" />
              <line x1="-4" y1="-7" x2="4" y2="7" />
              <line x1="-4" y1="7" x2="4" y2="-7" />
            </g>
          ))}
        </g>
      ) : null}
      {condition === "fog" ? (
        <g stroke="#ffffff" strokeOpacity="0.85" strokeWidth="8" strokeLinecap="round">
          <line x1="60" y1="190" x2="220" y2="190" />
          <line x1="90" y1="212" x2="250" y2="212" />
        </g>
      ) : null}
    </svg>
  );
}

export function WeatherView({ condition, isDay, temperature, unit, label, date, location, updated, outlook, hours, lang }: Props) {
  const temp = Math.round(temperature);
  const unitWord = unit === "F" ? "Fahrenheit" : lang === "th" ? "เซลเซียส" : "Celsius";
  return (
    <section className="a2ui-weather" data-sky={skyOf(condition, isDay)}>
      <div className="a2ui-weather-body">
        <div className="a2ui-weather-now">
          <span className="a2ui-weather-label">{label}</span>
          <span className="a2ui-weather-temp" aria-label={`${temp} ${CARD_UI[lang].degrees} ${unitWord}`}>
            {temp}°<span className="a2ui-weather-unit">{unit}</span>
          </span>
        </div>
        <div className="a2ui-weather-sep" aria-hidden="true" />
        <div className="a2ui-weather-meta">
          {date ? <span className="a2ui-weather-date">{date}</span> : null}
          <span className="a2ui-weather-place">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z" />
              <circle cx="12" cy="9.5" r="2.5" />
            </svg>
            <span>{location}</span>
          </span>
          {updated ? <span className="a2ui-weather-updated">{updated}</span> : null}
        </div>
      </div>
      {hours?.length ? (
        <div className="a2ui-weather-hours">
          {outlook ? <span className="a2ui-weather-outlook">{outlook}</span> : null}
          <ol>
            {hours.map((h, i) => (
              <li key={i} aria-label={`${h.time} ${CARD_UI[lang].rainChance} ${Math.round(h.chance)}%`}>
                <span aria-hidden="true">{Math.round(h.chance)}%</span>
                <span className="a2ui-weather-bar" aria-hidden="true">
                  <span style={{ height: `${Math.max(4, h.chance)}%` }} />
                </span>
                <span aria-hidden="true">{h.time}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      <WeatherIcon condition={condition} isDay={isDay} />
    </section>
  );
}
