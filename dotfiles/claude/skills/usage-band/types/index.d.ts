/** One rate-limit window, ready to draw. */
export type Limit = {
  /** The window's name as shown: `5h`, `w`. */
  label: string
  /** How much of the window is used, a whole percentage. */
  percent: number
  /** Time left until the window resets (`2h 13m`); absent when unknown. */
  resetIn?: string
}

/** What the row on the hint line shows: the session's model and its rate-limit windows. */
export type Snapshot = {
  /** The model as shown: `Fable 5.2`. */
  model: string
  /** Its family, which picks the name's color: `fable`, `opus`, `sonnet`, or `` for none. */
  family: string
  limits: Limit[]
}

/** The permission mode as last reported, and a count that moves with each report. */
export type Mode = { name: string; seq: number }

declare module 'claude-code' {
  interface PluginState {
    'usage-band': { snapshot: Shaped<Snapshot>; mode: Mode }
  }
}
