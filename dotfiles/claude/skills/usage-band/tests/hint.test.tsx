import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const NOW = Date.parse('2026-11-18T05:00:00Z')
const HINT = '(shift+tab to cycle)'

const hint = (columns: number, text: string = HINT) =>
  ({
    plugin: 'usage-band',
    component: 'PromptHint',
    surface: 'terminal',
    props: { isDraft: false, isWorking: false, hint: text },
    viewport: { columns, rows: 40 },
  }) as const

const USAGE = {
  startedAt: NOW,
  context: { window: 200_000 },
  rateLimits: [
    { kind: 'seven_day', percentUsed: 41.5, resetsAt: '2026-11-21T09:00:00Z' },
    { kind: 'five_hour', percentUsed: 23, resetsAt: '2026-11-18T07:13:00Z' },
  ],
}

// Stands for the host outside tmux: no pane, so nothing is run for tmux's bar.
const outsideTmux = (on: On) => {
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/me' : undefined }))
}

test('the row shows the model bold in its color and the figures bold', async ($, on) => {
  mock.clock(on, { now: NOW })
  outsideTmux(on)
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.model', () => ({ value: 'claude-fable-5-2' }))
  on('session.usage', () => ({ value: USAGE }))
  // Stands for the engine: its own hint line.
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>{e.props.hint}</Text>
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const wide = await $.ui.mount(hint(140))
  expect((await wide.find({ type: 'Text', text: /shift/ }))?.text).toBe(HINT)
  expect((await wide.find({ type: 'Text', text: /Fable/ }))?.props).toMatchObject({
    color: 'claude',
    weight: 'bold',
  })
  expect((await wide.find({ type: 'Text', text: /^23%$/ }))?.props).toMatchObject({
    color: 'text',
    weight: 'bold',
  })
  expect(await wide.find({ type: 'Text', text: /^42%$/ })).toBeDefined()
  expect((await wide.find({ type: 'Text', text: /2h 13m/ }))?.text).toBe(' (resets in 2h 13m)')
  expect((await wide.find({ type: 'Text', text: /3d/ }))?.text).toBe(' (resets in 3d 4h)')
  await wide.unmount()

  // Too narrow for the reset times: the figures alone.
  const narrow = await $.ui.mount(hint(80))
  expect(await narrow.find({ type: 'Text', text: /^23%$/ })).toBeDefined()
  expect(await narrow.find({ type: 'Text', text: /resets/ })).toBeUndefined()
  await narrow.unmount()

  // No room after the engine's text: the row is left to the engine.
  const tiny = await $.ui.mount(hint(50))
  expect(await tiny.find({ type: 'Text', text: /Fable/ })).toBeUndefined()
  expect((await tiny.find({ type: 'Text', text: /shift/ }))?.text).toBe(HINT)
  await tiny.unmount()
})

test('a model change shows at the next poll, in the new model\'s color', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  outsideTmux(on)
  let model = 'claude-fable-5-2'
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.model', () => ({ value: model }))
  on('session.usage', () => ({ value: { ...USAGE, rateLimits: [] } }))
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>{e.props.hint}</Text>
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  model = 'claude-opus-5-5'
  await clock.advance(2_000)

  const ui = await $.ui.mount(hint(140))
  expect((await ui.find({ type: 'Text', text: /Opus/ }))?.props).toMatchObject({
    color: '#5c9dff',
    weight: 'bold',
  })
  expect(await ui.find({ type: 'Text', text: /first response/ })).toBeDefined()
  await ui.unmount()
})

test('in tmux the figures are handed to the bar\'s script, once per change', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  let percent = 42
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/me' : e.name === 'TMUX_PANE' ? '%7' : undefined }))
  on('process.run', (_, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })

    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('settings.read', () => ({ value: { modelSettings: { 'claude-fable-5-2': { effortLevel: 'high' } } } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.model', () => ({ value: 'claude-fable-5-2' }))
  on('session.usage', () => ({ value: { ...USAGE, context: { window: 200_000, tokens: 84_000, percent } } }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  expect(runs.length).toBe(1)
  expect(runs[0]?.argv).toEqual(['/home/me/.claude/statusline-tmux.sh'])
  expect(JSON.parse(runs[0]?.stdin ?? '')).toEqual({
    model: { display_name: 'Fable 5.2' },
    effort: { level: 'high' },
    context_window: { used_percentage: 42 },
    rate_limits: { five_hour: { used_percentage: 23 }, seven_day: { used_percentage: 41.5 } },
  })

  // The figures read again and found the same: the script is not run a second time.
  await clock.advance(30_000)
  expect(runs.length).toBe(1)

  // The context window fills: the bar is handed the new figure.
  percent = 57
  await clock.advance(30_000)
  expect(runs.length).toBe(2)
  expect(JSON.parse(runs[1]?.stdin ?? '').context_window).toEqual({ used_percentage: 57 })
})
