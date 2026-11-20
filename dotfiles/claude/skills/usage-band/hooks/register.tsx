import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionContextUsage, SessionRateLimit } from 'claude-code'

import type { Limit, Snapshot } from '../types'

// The row painted on the hint line draws from this one value; a write redraws it.
const snapshot = atom(
  { plugin: 'usage-band', key: 'snapshot' } as const,
  { model: '', family: '', limits: [] },
  { shape: 'snapshot-4' },
)
// The permission mode as a settings-hook event last reported it; `seq` moves
// on each report that should redraw the row.
const mode = atom({ plugin: 'usage-band', key: 'mode' } as const, { name: '', seq: 0 })

// A model change raises no event of its own: /model is caught as it runs, and
// the model is asked again this often for a change made any other way.
const MODEL_POLL_MS = 2_000
// Every so many polls the windows are read too, so the time to a reset moves.
const FULL_EVERY = 15
const SEPARATOR = ' · '
// The hint row opens with the permission mode's label (`⏵⏵ auto mode on`),
// which a render hook is not told. Its width is known while the mode last
// heard from a settings-hook event still stands; otherwise room is kept for
// the widest label shift+tab cycles through, and a few blank cells come first.
const MANUAL_LABEL = 16
const MODE_LABEL_CELLS: Record<string, number> = {
  auto: 15,
  default: MANUAL_LABEL,
  acceptEdits: 18,
  plan: 14,
  bypassPermissions: 24,
}
const WIDEST_CYCLED_LABEL = 18
// One cell is left untouched between the engine's text and the row, so a
// label one cell wider than reckoned is not painted over.
const GAP = 1
// Cells kept free at the row's right end.
const MARGIN = 2
const ORDER = ['five_hour', 'seven_day']
const LABELS: Record<string, string> = {
  five_hour: '5h',
  seven_day: 'w',
  spend_limit: 'spend',
}
// The model's name is drawn bold in its family's color: Fable keeps the
// theme's orange, Opus is blue, Sonnet red; any other is the plain text color.
const MODEL_COLORS: Record<string, string> = {
  fable: 'claude',
  mythos: 'claude',
  opus: '#5c9dff',
  sonnet: '#ff6b6b',
}
// A window's figure is bold in the theme's plain text color: white on a dark theme.
const PERCENT_COLOR = 'text'
// The script that shows the session's figures in tmux's status bar, under the
// home folder. It reads the JSON a status line command is given on stdin. It is
// run from here and not as the `statusLine` setting, because Claude Code keeps
// a row under the prompt for a status line even when the command prints nothing.
const TMUX_FEED = '.claude/statusline-tmux.sh'
const TMUX_FEED_TIMEOUT_MS = 5_000

function labelOf(kind: string): string {
  const known = LABELS[kind]
  if (known !== undefined) return known

  return kind.startsWith('seven_day_') ? `w(${kind.slice(10)})` : kind
}

/** `claude-fable-5-2` reads as `Fable 5.2`; any other spelling stays as it is. */
function modelName(id: string): string {
  const m = /^claude-([a-z]+)-(\d+)-(\d)(?:-\d{8})?(\[.+\])?$/.exec(id)
  if (m === null) return id
  const family = m[1] ?? ''

  return `${family.charAt(0).toUpperCase()}${family.slice(1)} ${m[2]}.${m[3]}${m[4] ?? ''}`
}

/** The model's family as its color is keyed: `opus`, or `` for one with no color of its own. */
function familyOf(id: string): string {
  return /fable|mythos|opus|sonnet|haiku/.exec(id.toLowerCase())?.[0] ?? ''
}

function resetIn(resetsAt: string | undefined, now: number): string | undefined {
  if (resetsAt === undefined) return undefined
  const ms = Date.parse(resetsAt) - now
  if (!Number.isFinite(ms) || ms <= 0) return undefined
  const minutes = Math.ceil(ms / 60_000)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)

  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes % 60}m`

  return `${minutes}m`
}

function rank(kind: string): number {
  const at = ORDER.indexOf(kind)

  return at === -1 ? ORDER.length : at
}

function limitsOf(windows: readonly SessionRateLimit[], now: number): Limit[] {
  return [...windows]
    .sort((a, b) => rank(a.kind) - rank(b.kind))
    .map(w => {
      const left = resetIn(w.resetsAt, now)
      const limit: Limit = { label: labelOf(w.kind), percent: Math.round(w.percentUsed) }

      return left === undefined ? limit : { ...limit, resetIn: left }
    })
}

/** Terminal cells a text takes: wide (CJK) characters, as a model's name may hold, take two. */
function cells(text: string): number {
  let n = 0
  for (const ch of text) {
    const c = ch.codePointAt(0) ?? 0
    const isWide =
      (c >= 0x1100 && c <= 0x115f) ||
      (c >= 0x2e80 && c <= 0xa4cf) ||
      (c >= 0xac00 && c <= 0xd7a3) ||
      (c >= 0xf900 && c <= 0xfaff) ||
      (c >= 0xff00 && c <= 0xff60)
    n += isWide ? 2 : 1
  }

  return n
}

type Piece = { text: string; color?: string; isDim?: boolean; isBold?: boolean }

/** The row's pieces: with each window's reset time, or without. */
function pieces(snap: Snapshot, hasReset: boolean): Piece[] {
  const color = MODEL_COLORS[snap.family]
  const row: Piece[] = [
    { text: '· ', isDim: true },
    color === undefined ? { text: snap.model, isBold: true } : { text: snap.model, color, isBold: true },
  ]

  if (snap.limits.length === 0) {
    row.push({ text: `${SEPARATOR}limits show after the first response`, isDim: true })
  }

  for (const limit of snap.limits) {
    row.push({ text: `${SEPARATOR}${limit.label} `, isDim: true })
    row.push({ text: `${limit.percent}%`, color: PERCENT_COLOR, isBold: true })

    if (hasReset && limit.resetIn !== undefined) {
      row.push({ text: ` (resets in ${limit.resetIn})`, isDim: true })
    }
  }

  return row
}

function width(row: readonly Piece[]): number {
  return row.reduce((n, piece) => n + cells(piece.text), 0)
}

/**
 * The pieces that fit in `columns`: with the reset times, else the figures
 * alone, else none (the row is then left to the engine).
 */
function fit(snap: Snapshot, columns: number): Piece[] {
  const full = pieces(snap, true)
  if (width(full) <= columns) return full
  const short = pieces(snap, false)

  return width(short) <= columns ? short : []
}

/**
 * Cells from the hint row's start to where the mod's row begins: past the
 * mode's label and the hint text the engine draws after it.
 *
 * The engine joins the two with a space when the hint opens with a bracket
 * (`(shift+tab to cycle)`) and with ` · ` otherwise (`? for shortcuts`).
 */
function offsetOf(hint: string, labelCells: number): number {
  if (hint === '') return labelCells
  const joint = hint.startsWith('(') ? 1 : cells(SEPARATOR)

  return labelCells + joint + cells(hint)
}

// True once the mode may have changed since it was last reported (shift+tab
// raises no event): the row then keeps room for the widest label.
let isModeStale = true

/**
 * Cells of the mode's label ahead of `hint`, as far as it can be told.
 *
 * The hint itself tells manual mode (`? for shortcuts`) from the others
 * (`(shift+tab to cycle)`). shift+tab passes through manual on its way from
 * one of the others to the next, so a hint that does not fit the reported
 * mode means the mode has moved, and stays unknown until it is reported again.
 */
function labelCellsFor(hint: string, reported: string): number {
  const isManualHint = hint.startsWith('?')
  const isCycleHint = hint.startsWith('(')

  if (reported !== '' && (isManualHint || isCycleHint)) {
    if ((reported === 'default') !== isManualHint) isModeStale = true
  }
  if (isManualHint) return MANUAL_LABEL

  return (isModeStale ? undefined : MODE_LABEL_CELLS[reported]) ?? WIDEST_CYCLED_LABEL
}

/** Takes the permission mode a settings-hook event carries. */
async function noteMode($: EngineInterface, name: string | undefined): Promise<void> {
  if (name === undefined) return
  const known = await read($, mode)

  if (isModeStale || known.name !== name) {
    isModeStale = false
    await update($, mode, last => ({ name, seq: last.seq + 1 }))
  }
}

// The model id the snapshot was last made from, to tell a change by.
let seenModel = ''
let polls = 0

/** The figures a measurement carries: the context window's fill and the rate-limit windows. */
type Figures = { context: SessionContextUsage; rateLimits: readonly SessionRateLimit[] }

// What tmux's bar is fed from: the figures as last read, the effort level a
// settings-hook event last carried and the model it was for, and the JSON the
// script was last handed, so that it runs only on a change.
let figures: Figures | undefined
let effort = { model: '', level: '' }
let fed = ''

/**
 * The effort level of `model`: the one a settings-hook event reported for it,
 * else the one the settings keep for it (`modelSettings`), else none.
 */
async function effortOf($: EngineInterface, model: string): Promise<string | undefined> {
  if (effort.model === model && effort.level !== '') return effort.level
  const kept: unknown = await $.settings.read({ key: 'modelSettings' })
  if (typeof kept !== 'object' || kept === null) return undefined
  const ofModel: unknown = (kept as Record<string, unknown>)[model]
  if (typeof ofModel !== 'object' || ofModel === null) return undefined
  const level: unknown = (ofModel as Record<string, unknown>).effortLevel

  return typeof level === 'string' && level !== '' ? level : undefined
}

/** Hands the model, the effort and the figures to tmux's bar, when in tmux and they changed. */
async function feedTmux($: EngineInterface): Promise<void> {
  if (figures === undefined || seenModel === '') return
  const [pane, home] = await Promise.all([$.env.get('TMUX_PANE'), $.env.get('HOME')])
  if (pane === undefined || pane === '' || home === undefined || home === '') return
  const level = await effortOf($, seenModel)
  const percentOf = (kind: string) => figures?.rateLimits.find(w => w.kind === kind)?.percentUsed
  // A figure not known yet is left out, and the script then draws no gauge for it.
  const payload = JSON.stringify({
    model: { display_name: modelName(seenModel) },
    effort: level === undefined ? undefined : { level },
    context_window: { used_percentage: figures.context.percent },
    rate_limits: {
      five_hour: { used_percentage: percentOf('five_hour') },
      seven_day: { used_percentage: percentOf('seven_day') },
    },
  })
  if (payload === fed) return
  const ran = await $.process.run([`${home}/${TMUX_FEED}`], {
    stdin: payload,
    timeoutMs: TMUX_FEED_TIMEOUT_MS,
  })

  if (ran.exitCode === 0) fed = payload
}

/** Takes the effort level a settings-hook event carries, and feeds tmux when it moved. */
async function noteEffort($: EngineInterface, level: string | undefined): Promise<void> {
  if (level === undefined || level === '' || seenModel === '') return
  if (effort.model === seenModel && effort.level === level) return
  effort = { model: seenModel, level }
  await feedTmux($)
}

/**
 * Reads the model and the figures (or takes the ones just measured), writes
 * the snapshot only when it changed, and feeds tmux's bar.
 */
async function refresh($: EngineInterface, measured?: Figures): Promise<void> {
  const [model, now] = await Promise.all([$.session.model(), $.clock.now()])
  const { context, rateLimits } = measured ?? (await $.session.usage())
  const fresh: Snapshot = {
    model: modelName(model),
    family: familyOf(model),
    limits: limitsOf(rateLimits, now),
  }
  const shown = await read($, snapshot)
  seenModel = model
  figures = { context, rateLimits }

  if (JSON.stringify(shown) !== JSON.stringify(fresh)) {
    await update($, snapshot, () => fresh)
  }
  // The bar is an extra: the row on the hint line stands without it.
  await feedTmux($).catch(() => undefined)
}

/** One poll: the model alone, and everything when it changed or the turn has come. */
async function poll($: EngineInterface): Promise<void> {
  polls += 1
  const isFull = polls % FULL_EVERY === 0

  if (isFull || (await $.session.model()) !== seenModel) {
    await refresh($)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    $.clock.every(MODEL_POLL_MS, () => {
      void poll($).catch(() => undefined)
    })
    await refresh($).catch(() => undefined)

    return next(e)
  })

  // After each turn, and when a window moves a whole point.
  on('session.measure', async ($, e, next) => {
    await refresh($, { context: e.context, rateLimits: e.rateLimits }).catch(() => undefined)

    return next(e)
  })

  // The events that say which permission mode the session is in, and, within
  // a turn, the effort level it runs at.
  on('classic.SessionStart', async ($, e, next) => {
    await noteMode($, e.permission_mode).catch(() => undefined)

    return next(e)
  })
  on('classic.UserPromptSubmit', async ($, e, next) => {
    await noteMode($, e.permission_mode).catch(() => undefined)
    await noteEffort($, e.effort?.level).catch(() => undefined)

    return next(e)
  })
  on('classic.PostToolUse', async ($, e, next) => {
    await noteMode($, e.permission_mode).catch(() => undefined)
    await noteEffort($, e.effort?.level).catch(() => undefined)

    return next(e)
  })
  on('classic.Stop', async ($, e, next) => {
    await noteMode($, e.permission_mode).catch(() => undefined)
    await noteEffort($, e.effort?.level).catch(() => undefined)

    return next(e)
  })

  // /model: the new model shows as soon as the command has run.
  on('command.run', { command: 'model' }, async ($, e, next) => {
    const ran = await next(e)
    await refresh($).catch(() => undefined)

    return ran
  })

  // /effort: the level reported before it no longer stands; the settings'
  // level shows until a turn reports its own.
  on('command.run', { command: 'effort' }, async ($, e, next) => {
    const ran = await next(e)
    effort = { model: '', level: '' }
    await refresh($).catch(() => undefined)

    return ran
  })

  // The hint line under the prompt. Text the engine adds to its own line is
  // dim, in one color, and a tree beside the line is laid out on a row beneath
  // it. So the mod's row, which takes a weight and a color, is pulled up onto
  // the hint row and painted over the free cells after the engine's text. The
  // engine does not say how wide its text is: the start is worked out from the
  // hint and the mode's label, and anything else the engine draws there (a
  // running task's label, a mode with a longer label) is painted over.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const snap = await read($, snapshot)

    if (snap.model === '') {
      return next(e)
    }

    const told = await read($, mode)
    const columns = e.viewport?.columns ?? 80
    const left = offsetOf(e.props.hint, labelCellsFor(e.props.hint, told.name)) + GAP
    const row = fit(snap, columns - left - MARGIN)

    if (row.length === 0) {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const line = await next(e)

    return (
      <Box flexDirection="column">
        {line}
        <Box marginTop={-1} marginLeft={left}>
          {row.map(piece => (
            <Text
              color={piece.color}
              dimColor={piece.isDim === true}
              weight={piece.isBold === true ? 'bold' : 'regular'}
              wrap="truncate-end"
            >
              {piece.text}
            </Text>
          ))}
        </Box>
      </Box>
    )
  })
}
