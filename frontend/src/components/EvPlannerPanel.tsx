import { useState, type ChangeEvent, type FormEvent } from "react";
import type { EvConfigureBody } from "../api";
import { evCard } from "../cards";
import type { Context } from "../types";
import { EvSummary } from "./Cards";

type Props = {
  context: Context | null;
  busy: boolean;
  onConfigure: (body: EvConfigureBody) => void;
};

type Form = { range: string; battery: string; capacity: string; reserve: string; target: string; corridor: string; model: string };

function formFrom(context: Context | null): Form {
  const v = context?.vehicle;
  const text = (n: number | null | undefined, fallback = "") => (n == null ? fallback : String(n));
  return {
    range: text(v?.max_range_km),
    battery: text(v?.current_battery_percent),
    capacity: text(v?.battery_capacity_kwh),
    reserve: text(context?.reserve_percent, "15"),
    target: text(context?.target_charge_percent, "80"),
    corridor: text(context?.corridor_km, "5"),
    model: [v?.make, v?.model].filter(Boolean).join(" "),
  };
}

function num(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? n : NaN;
}

/** Client-side checks mirror the server's limits so the user gets a precise message before any request. */
function validate(form: Form): string | null {
  const range = num(form.range), battery = num(form.battery), capacity = num(form.capacity);
  const reserve = num(form.reserve), target = num(form.target), corridor = num(form.corridor);
  if (range === undefined || Number.isNaN(range) || range <= 0 || range > 2000) return "Enter your vehicle's full-charge range in km (up to 2000).";
  if (battery === undefined || Number.isNaN(battery) || battery < 0 || battery > 100) return "Enter your current battery level, 0 to 100%.";
  if (capacity !== undefined && (Number.isNaN(capacity) || capacity <= 0 || capacity > 500)) return "Battery capacity must be between 1 and 500 kWh, or left blank.";
  if (reserve !== undefined && (Number.isNaN(reserve) || reserve < 0 || reserve >= 100)) return "Reserve must be 0 to 99%.";
  if (target !== undefined && (Number.isNaN(target) || target <= 0 || target > 100)) return "Charging target must be 1 to 100%.";
  if (reserve !== undefined && target !== undefined && target <= reserve) return "Charging target must be higher than the reserve.";
  if (corridor !== undefined && (Number.isNaN(corridor) || corridor <= 0 || corridor > 50)) return "Search corridor must be between 0 and 50 km.";
  return null;
}

/** Remounts the form whenever the server's vehicle or settings change, so it always shows what the server holds (including values typed in chat). */
export default function EvPlannerPanel(props: Props) {
  const { context } = props;
  const key = JSON.stringify([context?.vehicle, context?.reserve_percent, context?.target_charge_percent, context?.corridor_km, context?.session_id]);
  return <EvPlannerForm key={key} {...props} />;
}

function EvPlannerForm({ context, busy, onConfigure }: Props) {
  const on = Boolean(context?.ev_mode);
  const [form, setForm] = useState<Form>(() => formFrom(context));
  const [problem, setProblem] = useState<string | null>(null);

  const set = (key: keyof Form) => (event: ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [key]: event.target.value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const message = validate(form);
    setProblem(message);
    if (message) return;
    const parts = form.model.trim().split(/\s+/).filter(Boolean);
    const vehicle: NonNullable<EvConfigureBody["vehicle"]> = {
      max_range_km: num(form.range),
      current_battery_percent: num(form.battery),
    };
    const capacity = num(form.capacity);
    if (capacity !== undefined) vehicle.battery_capacity_kwh = capacity;
    if (parts.length) {
      vehicle.make = parts[0];
      if (parts.length > 1) vehicle.model = parts.slice(1).join(" ");
    }
    onConfigure({
      enabled: true,
      vehicle,
      settings: { reserve_percent: num(form.reserve), target_charge_percent: num(form.target), corridor_km: num(form.corridor) },
    });
  };

  const card = context ? evCard(context) : null;

  return (
    <div className="panel-body">
      <section aria-labelledby="ev-heading" className="panel-section">
        <div className="panel-title-row">
          <h2 id="ev-heading" className="panel-title">EV Planner</h2>
          <span className="pill pill-ev">Optional</span>
        </div>
        <p className="hint">
          For electric vehicles only. It adds your vehicle's range and battery to a trip and plans charging stops.
          Ordinary trips never show battery or charging information.
        </p>
        <label className="switch">
          <input
            type="checkbox"
            role="switch"
            checked={on}
            disabled={busy}
            onChange={(event) => onConfigure(event.target.checked ? { enabled: true } : { enabled: false })}
          />
          <span>{on ? "EV Planner is on" : "Turn on EV Planner"}</span>
        </label>
      </section>

      {on && (
        <>
          <form className="panel-section" onSubmit={submit} noValidate aria-labelledby="ev-vehicle-heading">
            <h3 id="ev-vehicle-heading" className="panel-subtitle">Vehicle and settings</h3>
            <div className="field-grid">
              <div className="field field-wide">
                <label htmlFor="ev-model">Vehicle (optional)</label>
                <input id="ev-model" type="text" maxLength={80} placeholder="e.g. Tata Nexon EV" value={form.model} onChange={set("model")} disabled={busy} />
              </div>
              <div className="field">
                <label htmlFor="ev-range">Full-charge range (km)</label>
                <input id="ev-range" type="number" inputMode="decimal" min={1} max={2000} value={form.range} onChange={set("range")} disabled={busy} required />
              </div>
              <div className="field">
                <label htmlFor="ev-battery">Battery now (%)</label>
                <input id="ev-battery" type="number" inputMode="decimal" min={0} max={100} value={form.battery} onChange={set("battery")} disabled={busy} required />
              </div>
              <div className="field">
                <label htmlFor="ev-capacity">Battery size (kWh, optional)</label>
                <input id="ev-capacity" type="number" inputMode="decimal" min={1} max={500} value={form.capacity} onChange={set("capacity")} disabled={busy} />
              </div>
              <div className="field">
                <label htmlFor="ev-reserve">Reserve (%)</label>
                <input id="ev-reserve" type="number" inputMode="decimal" min={0} max={99} value={form.reserve} onChange={set("reserve")} disabled={busy} />
              </div>
              <div className="field">
                <label htmlFor="ev-target">Charge up to (%)</label>
                <input id="ev-target" type="number" inputMode="decimal" min={1} max={100} value={form.target} onChange={set("target")} disabled={busy} />
              </div>
              <div className="field">
                <label htmlFor="ev-corridor">Search corridor (km)</label>
                <input id="ev-corridor" type="number" inputMode="decimal" min={1} max={50} value={form.corridor} onChange={set("corridor")} disabled={busy} />
              </div>
            </div>
            {problem && <p className="form-error" role="alert">{problem}</p>}
            <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Working…" : "Apply and plan charging"}</button>
          </form>

          <section className="panel-section" aria-labelledby="ev-result-heading">
            <h3 id="ev-result-heading" className="panel-subtitle">Charging plan</h3>
            {!context?.route && <p className="card-state card-state-info">Plan a trip in the chat first. The charging plan follows that route.</p>}
            {context?.route && card && <EvSummary card={card} />}
            {context?.route && context.ev_status === "calculated" && (
              <p className="card-foot">
                {context.chargers.length} charging station{context.chargers.length === 1 ? "" : "s"} within {context.corridor_km} km of the route.
                Source: Tata EV Charging Dataset (bundled), a static list rather than live availability.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
