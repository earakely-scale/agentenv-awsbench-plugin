// Shared reads of public/data/failures-t01.json (written by tools/build_hub_data.py from hub/showcase/failures-t01.json).
import F from "../../public/data/failures-t01.json";

export const DATA: any = F;
export const STATE: Record<string, string> = {
  lost: "lost points",
  knock_on: "knock-on of the family-1 decision",
  near: "near miss",
  fixed: "grader defect, fixed and regraded",
  clean: "nothing lost",
};
export const LABEL: Record<string, string> = { fair: "fair", partly_environment: "partly environment", environment: "environment" };
export const MODELS: any[] = DATA.matrix.cols.map((id: string) => DATA.models.find((m: any) => m.id === id));
export const BUILD_NOTE: Record<string, string> = {
  haiku: "final build",
  sonnet: "earlier builds",
  opus: "R11 + R12",
  fable: "final build",
  astra: "Codex CLI",
};
export const RUN = Object.fromEntries(DATA.runs.map((r: any) => [r.id, r]));
export const FAMILY = Object.fromEntries(DATA.families.map((f: any) => [f.id, f]));
export const FILM = Object.fromEntries(DATA.films.map((f: any) => [f.id, f]));
export const cell = (fam: string, model: string) => DATA.matrix.cells[fam][model];

export function squareTip(sq: any): string {
  const parts = [sq.run, STATE[sq.state] || sq.state];
  if (sq.label) parts.push(LABEL[sq.label] || sq.label);
  if (sq.checks && sq.checks.length) parts.push(sq.checks.join(", "));
  if (sq.note) parts.push(sq.note);
  return parts.join(" · ");
}

export function countLine(c: any): string {
  let s = `${c.lost} of ${c.n_runs} lost`;
  if (c.pass_lost) s += ` · pass lost in ${c.pass_lost}`;
  if (c.knock_on) s += ` · ${c.knock_on} knock-on`;
  if (c.near) s += ` · ${c.near} near`;
  if (c.fixed) s += ` · ${c.fixed} fixed`;
  return s;
}

export const sec = (hms: string | null) => {
  if (!hms) return null;
  const [h, m, s] = hms.split(":").map(Number);
  return h * 3600 + m * 60 + (s || 0);
};
export const hm = (hms: string) => hms.slice(0, 5);
export const runLink = (id: string) => (RUN[id] && RUN[id].viewer ? `/viz/flight-recorder.html?run=${id}` : null);
