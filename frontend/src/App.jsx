import { startTransition, useEffect, useId, useRef, useState } from 'react'
import './App.css'

const FRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1']
const ENTRY_OPTIONS = [
  { value: 'deterministic', label: 'Deterministic only' },
  { value: 'reviewed', label: 'Reviewed deterministic' },
  { value: 'independent', label: 'Independent auto' },
  { value: 'consensus', label: 'Full consensus' },
]
const PROVIDER_OPTIONS = [
  { value: 'gemini', label: 'Gemini' },
  { value: 'astra', label: 'Astra High' },
]
const RISK_TIER_PERCENT = { LOW: 10, MEDIUM: 20, HIGH: 30 }
const fmt = (n, digits = 2) => Number.isFinite(n) ? n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—'
const aiResultProviderLabel = result => {
  if (result?.selected_provider !== 'astra') return 'GEMINI / GOOGLE'
  const output = result.analysis || result.review || {}
  const model = output.model === 'gpt-6-luna' ? 'LUNA' : output.model === 'gpt-6-astra' ? 'ASTRA' : output.model?.toUpperCase() || 'CODEX'
  return `${model}${output.reasoning_effort ? ` ${output.reasoning_effort.toUpperCase()}` : ''} / CODEX CLI`
}
const proposalInstruction = change => {
  if (change.proposed_stop_after_reduction != null) return `Close ${fmt(change.volume)} lots, then ${change.required_stop_action || 'MOVE SL'} to ${fmt(change.proposed_stop_after_reduction)}`
  if (change.proposed_stop != null) return `Proposed SL ${fmt(change.proposed_stop)}`
  return `Volume ${fmt(change.volume)}`
}
async function readApiResponse(response) {
  const body = await response.text()
  if (!body.trim()) {
    throw new Error(`Scanner API returned an empty response (HTTP ${response.status}). Start the API and try again.`)
  }
  let data
  try {
    data = JSON.parse(body)
  } catch {
    throw new Error(`Scanner API returned invalid JSON (HTTP ${response.status}). Restart the API and dashboard.`)
  }
  if (!response.ok) throw new Error(data.error || `Scanner request failed (HTTP ${response.status})`)
  return data
}
function applicationServerKey(value) {
  const padding = '='.repeat((4 - value.length % 4) % 4)
  const raw = window.atob((value + padding).replaceAll('-', '+').replaceAll('_', '/'))
  return Uint8Array.from([...raw].map(char => char.charCodeAt(0)))
}
function Icon({ name, size = 18 }) {
  const paths = { grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z', chart: 'M3 20V4 M3 20h18 M6 15l4-6 4 3 6-8', shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6', pulse: 'M2 12h5l3-8 4 16 3-8h5', scan: 'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5 M5 12h14', arrow: 'M5 12h14 M14 7l5 5-5 5' }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.chart} /></svg>
}
function Panel({ title, label, children, className = '', aside }) {
  return <section className={'panel ' + className}><header className="panel-heading"><div><span className="eyebrow">{label}</span><h2>{title}</h2></div>{aside}</header>{children}</section>
}
function Chart({ bars, candles = false, id, small = false, zones = [], orderBlock, entryZone }) {
  if (!bars?.length) return <div className={'chart-empty ' + (small ? 'small' : '')}><Icon name="chart" size={28} /><span>Waiting for broker history</span><small>The automatic scanner will populate this chart</small></div>
  const min = Math.min(...bars.map(b => b.low)) - 2
  const max = Math.max(...bars.map(b => b.high)) + 2
  const y = value => 170 - (value - min) / (max - min) * 155
  const x = i => 12 + i / Math.max(1, bars.length - 1) * 568
  const line = bars.map((b, i) => (i ? 'L' : 'M') + x(i) + ',' + y(b.close)).join(' ')
  const zoneLabel = zone => {
    const active = zone.active_sources || (zone.status === 'active' ? zone.sources : []) || []
    const broken = zone.broken_sources || []
    const unknown = zone.unknown_sources || []
    const sources = [...active, ...unknown.map(frame => `${frame}?`), ...broken.map(frame => `${frame}×`)]
    const state = zone.status === 'broken' ? 'BROKEN ' : zone.status === 'unknown' ? 'UNVERIFIED ' : ''
    return `${state}${zone.kind} ${sources.join('/')} · ${fmt(zone.price)}`
  }
  const overlays = [
    ...(entryZone ? [{ ...entryZone, kind: 'LIMIT', label: 'ENTRY ZONE' }] : []),
    ...(orderBlock ? [{ ...orderBlock, kind: 'ORDER_BLOCK', label: 'ORDER BLOCK' }] : []),
    ...zones.map(zone => ({ ...zone, label: zoneLabel(zone) })),
  ].filter(zone => zone.low <= max && zone.high >= min).slice(0, 14)
  return <div className={'chart-wrap ' + (small ? 'small' : '')}><svg viewBox="0 0 650 200" role="img" aria-label={candles ? 'Broker candlestick price chart' : 'Price area chart'}>
    <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#16d8c6" stopOpacity=".48" /><stop offset="100%" stopColor="#16d8c6" stopOpacity=".015" /></linearGradient></defs>
    {[0, 1, 2, 3, 4].map(i => <g key={i}><line className="gridline" x1="10" x2="587" y1={14 + i * 39} y2={14 + i * 39} /><text x="597" y={18 + i * 39}>{fmt(max - i / 4 * (max - min), 1)}</text></g>)}
    {[0, 1, 2, 3, 4, 5, 6, 7, 8].map(i => <line key={i} className="gridline vertical" x1={12 + i * 71} x2={12 + i * 71} y1="10" y2="171" />)}
    {overlays.map((zone, index) => <g key={`${zone.kind}-${zone.low}-${index}`} className={`chart-zone ${(zone.kind || '').toLowerCase()} ${zone.status || ''}`}><title>{zone.label}{zone.status ? ` · ${zone.status}` : ''}{zone.invalidation_time ? ` · broken ${new Date(zone.invalidation_time * 1000).toISOString()}` : ''}</title><rect x="12" width="568" y={y(Math.min(max, zone.high))} height={Math.max(2, y(Math.max(min, zone.low)) - y(Math.min(max, zone.high)))} /><text className="chart-zone-label" x="18" y={y(Math.min(max, zone.high)) + 9}>{zone.label}</text></g>)}
    {candles ? bars.map((b, i) => <g key={i} className={b.close >= b.open ? 'candle-up' : 'candle-down'}><line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} /><rect x={x(i) - 2.1} y={Math.min(y(b.open), y(b.close))} width="4.2" height={Math.max(1, Math.abs(y(b.open) - y(b.close)))} /></g>) : <><path d={line + 'L580,174 L12,174Z'} fill={'url(#' + id + ')'} /><path d={line} fill="none" stroke="#20e3ce" strokeWidth="1.8" /><circle cx={x(bars.length - 1)} cy={y(bars.at(-1).close)} r="3" fill="#63ffe8" /></>}
    {[0, 16, 32, 48, bars.length - 1].filter(i => i < bars.length).map(i => <text key={i} x={x(i)} y="194" textAnchor="middle">{new Date(bars[i].time * 1000).toISOString().slice(11, 16)}</text>)}
  </svg></div>
}
function Ring({ value, label, icon, muted }) {
  const amount = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0
  return <div className={'ring-block ' + (muted ? 'muted' : '')}><div className="ring"><svg viewBox="0 0 100 100" aria-hidden="true"><circle className="ring-outer" cx="50" cy="50" r="46" /><circle className="ring-track" cx="50" cy="50" r="39" /><circle className="ring-progress" cx="50" cy="50" r="39" strokeDasharray={amount * 2.45 + ' 245'} /></svg><div className="ring-value">{icon ? <Icon name={icon} size={24} /> : <>{Number.isFinite(value) ? value : '—'}<small>/100</small></>}</div></div><span>{label}</span></div>
}
function ControlMenu({ label, value, options, onChange, title }) {
  const [open, setOpen] = useState(false)
  const menuId = useId()
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const optionRefs = useRef([])
  const focusIndex = useRef(0)
  const selectedIndex = Math.max(0, options.findIndex(option => option.value === value))

  useEffect(() => {
    if (!open) return
    optionRefs.current[focusIndex.current]?.focus()
    const closeOnOutsideClick = event => {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOnOutsideClick)
    return () => document.removeEventListener('pointerdown', closeOnOutsideClick)
  }, [open])

  function openAt(index) {
    focusIndex.current = index
    setOpen(true)
  }
  function handleMenuKeyDown(event) {
    const current = optionRefs.current.indexOf(document.activeElement)
    let next = current
    if (event.key === 'ArrowDown') next = (current + 1) % options.length
    else if (event.key === 'ArrowUp') next = (current - 1 + options.length) % options.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = options.length - 1
    else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
      return
    } else return
    event.preventDefault()
    optionRefs.current[next]?.focus()
  }

  return <div className={`control-select ${open ? 'is-open' : ''}`} ref={rootRef} title={title} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }}>
    <button type="button" className="control-select-trigger" ref={triggerRef} aria-label={`${label}: ${options[selectedIndex].label}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => open ? setOpen(false) : openAt(selectedIndex)} onKeyDown={event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        openAt(selectedIndex)
      } else if (event.key === 'Escape') setOpen(false)
    }}>
      <span className="control-select-label">{label}</span>
      <span className="control-select-value">{options[selectedIndex].label}</span>
      <span className="control-select-chevron" aria-hidden="true" />
    </button>
    {open && <div className="control-select-menu" id={menuId} role="listbox" aria-label={label} onKeyDown={handleMenuKeyDown}>
      {options.map((option, index) => <button type="button" role="option" aria-selected={option.value === value} className="control-select-option" key={option.value} ref={node => { optionRefs.current[index] = node }} onClick={() => {
        if (option.value !== value) onChange(option.value)
        setOpen(false)
        triggerRef.current?.focus()
      }}><span>{option.label}</span><span className="control-option-check" aria-hidden="true">{option.value === value ? '✓' : ''}</span></button>)}
    </div>}
  </div>
}
export default function App() {
  const [decision, setDecision] = useState(null)
  const [frame, setFrame] = useState('M5')
  const [aiBusy, setAiBusy] = useState(false)
  const [aiProvider, setAiProvider] = useState('gemini')
  const [manualReview, setManualReview] = useState(null)
  const [independentBusy, setIndependentBusy] = useState(false)
  const [independentAnalysis, setIndependentAnalysis] = useState(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('positions')
  const [chartType, setChartType] = useState('area')
  const [details, setDetails] = useState(false)
  const [pushState, setPushState] = useState('checking')
  const [pushBusy, setPushBusy] = useState(false)
  const [closeHalfBusy, setCloseHalfBusy] = useState(false)
  const [closeHalfPreview, setCloseHalfPreview] = useState(null)
  const [closeHalfResult, setCloseHalfResult] = useState(null)
  const [executionSettings, setExecutionSettings] = useState({ entry_mode: 'consensus', provider: 'gemini', auto_trailing: true, paused: false })
  const [settingsBusy, setSettingsBusy] = useState(false)
  const [settingsError, setSettingsError] = useState('')
  const desiredSettings = useRef({ entry_mode: 'consensus', provider: 'gemini', auto_trailing: true, paused: false })
  const confirmedSettings = useRef(desiredSettings.current)
  const settingsRevision = useRef(0)
  const settingsSaving = useRef(false)
  useEffect(() => {
    let active = true
    let timer
    const poll = async () => {
      try {
        const status = await readApiResponse(await fetch('/api/status', { cache: 'no-store' }))
        if (active) {
          startTransition(() => setDecision(status))
          setError('')
        }
      } catch (e) {
        if (active) setError(e instanceof TypeError ? 'Scanner API is offline.' : e.message)
      } finally {
        if (active) timer = window.setTimeout(poll, 1000)
      }
    }
    poll()
    if ('serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window) {
      navigator.serviceWorker.register('/sw.js')
        .then(() => navigator.serviceWorker.ready)
        .then(registration => registration.pushManager.getSubscription())
        .then(subscription => { if (active) setPushState(subscription ? 'enabled' : 'disabled') })
        .catch(() => { if (active) setPushState('unsupported') })
    } else {
      setPushState('unsupported')
    }
    fetch('/api/ai/status')
      .then(readApiResponse)
      .then(status => {
        if (active && ['astra', 'gemini'].includes(status.default_provider)) {
          setAiProvider(status.default_provider)
        }
      })
      .catch(() => {})
    fetch('/api/execution/settings')
      .then(readApiResponse)
      .then(settings => {
        if (active && settingsRevision.current === 0) {
          desiredSettings.current = settings
          confirmedSettings.current = settings
          setExecutionSettings(settings)
        }
      })
      .catch(() => {})
    return () => { active = false; window.clearTimeout(timer) }
  }, [])
  const d = decision
  const bars = d?.chart_data?.[frame]?.slice(-66)
  const positions = d?.positions_summary?.positions
  const orders = d?.positions_summary?.pending_orders
  const pendingChecks = d?.pending_supervision || d?.automation?.last_pending_supervision || []
  const positionRisk = d?.positions_summary?.risk_by_ticket || []
  const flags = d?.data_quality || []
  const score = d?.structure || {}
  const account = d?.account_summary
  async function reviewWithAi() {
    setAiBusy(true); setError('')
    try {
      const response = await fetch('/api/ai/review', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: aiProvider }),
      })
      const review = await readApiResponse(response)
      setManualReview(review)
    } catch (e) {
      setError(e instanceof TypeError ? 'AI review API is offline.' : e.message)
    }
    finally { setAiBusy(false) }
  }
  async function analyzeIndependently() {
    setIndependentBusy(true); setError('')
    try {
      const response = await fetch('/api/ai/independent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: aiProvider }),
      })
      setIndependentAnalysis(await readApiResponse(response))
    } catch (e) {
      setError(e instanceof TypeError ? 'Independent analysis API is offline.' : e.message)
    } finally { setIndependentBusy(false) }
  }
  async function togglePhoneAlerts() {
    setPushBusy(true); setError('')
    try {
      const registration = await navigator.serviceWorker.ready
      const existing = await registration.pushManager.getSubscription()
      if (existing) {
        await readApiResponse(await fetch('/api/notifications/subscription', {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: existing.endpoint }),
        }))
        await existing.unsubscribe()
        setPushState('disabled')
        return
      }
      const permission = await Notification.requestPermission()
      if (permission !== 'granted') throw new Error('Notification permission was not granted.')
      const config = await readApiResponse(await fetch('/api/notifications/config'))
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(config.public_key),
      })
      try {
        await readApiResponse(await fetch('/api/notifications/subscription', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(subscription.toJSON()),
        }))
      } catch (e) {
        await subscription.unsubscribe()
        throw e
      }
      setPushState('enabled')
      await registration.showNotification('Tradew phone alerts enabled', {
        body: 'You will be notified once per new BUY or SELL setup.',
        icon: '/notification-icon.svg', tag: 'tradew-alerts-enabled',
      })
    } catch (e) {
      setError(pushState === 'unsupported'
        ? 'Phone alerts are not supported in this browser.'
        : `${e.message} On iPhone, add Tradew to the Home Screen first, then enable alerts inside the installed app.`)
    } finally { setPushBusy(false) }
  }
  async function previewHalfClose() {
    setCloseHalfBusy(true); setCloseHalfResult(null); setError('')
    try {
      const result = await readApiResponse(await fetch('/api/portfolio/close-half/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      }))
      if (!result.accepted) throw new Error((result.status || 'Half-close preview unavailable').replaceAll('_', ' '))
      setCloseHalfPreview(result)
    } catch (e) {
      setError(e instanceof TypeError ? 'Portfolio action API is offline.' : e.message)
    } finally { setCloseHalfBusy(false) }
  }
  async function executeHalfClose() {
    if (!closeHalfPreview?.snapshot_token) return
    setCloseHalfBusy(true); setError('')
    try {
      const result = await readApiResponse(await fetch('/api/portfolio/close-half', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmation: 'CLOSE 50%', snapshot_token: closeHalfPreview.snapshot_token }),
      }))
      setCloseHalfPreview(null)
      setCloseHalfResult(result)
      if (!result.complete) setError(`Half close stopped: ${(result.stopped_reason || result.status).replaceAll('_', ' ')}. ${fmt(result.verified_closed_volume)} of ${fmt(result.target_close_volume)} lots was verified closed.`)
    } catch (e) {
      setError(e instanceof TypeError ? 'Portfolio action API is offline.' : e.message)
    } finally { setCloseHalfBusy(false) }
  }
  function updateExecutionSettings(update) {
    ++settingsRevision.current
    const next = { ...desiredSettings.current, ...update }
    desiredSettings.current = next
    setExecutionSettings(next)
    setSettingsBusy(true)
    setSettingsError('')
    saveExecutionSettings()
  }
  async function saveExecutionSettings() {
    if (settingsSaving.current) return
    settingsSaving.current = true
    try {
      while (true) {
        const revision = settingsRevision.current
        const next = desiredSettings.current
        try {
          const saved = await readApiResponse(await fetch('/api/execution/settings', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(next),
          }))
          confirmedSettings.current = saved
          if (revision === settingsRevision.current) {
            desiredSettings.current = saved
            setExecutionSettings(saved)
            setSettingsBusy(false)
            return
          }
        } catch (e) {
          if (revision === settingsRevision.current) {
            desiredSettings.current = confirmedSettings.current
            setExecutionSettings(confirmedSettings.current)
            setSettingsError(e instanceof TypeError ? 'Could not reach the settings API. Change was not saved.' : `Change was not saved: ${e.message}`)
            setSettingsBusy(false)
            return
          }
        }
      }
    } finally {
      settingsSaving.current = false
    }
  }
  const time = d?.snapshot_time ? new Date(d.snapshot_time * 1000).toISOString().slice(11, 19) + ' UTC' : 'No broker snapshot'
  const busy = d?.automation?.scan_in_progress
  const autoAiBusy = d?.automation?.gemini_review_in_progress
  const displayedReview = manualReview || (['reviewed', 'consensus'].includes(executionSettings.entry_mode) ? d?.llm_review : null)
  const reviewSnapshotMoved = Boolean(manualReview && d?.snapshot_id && manualReview.snapshot_id !== d.snapshot_id)
  const displayedIndependent = independentAnalysis || (['independent', 'consensus'].includes(executionSettings.entry_mode) ? d?.llm_independent_analysis : null)
  const independent = displayedIndependent?.analysis
  const independentSnapshotMoved = Boolean(independentAnalysis && d?.snapshot_id && independentAnalysis.snapshot_id !== d.snapshot_id)
  const consensus = d?.consensus || d?.automation?.last_authorization || d?.automation?.last_consensus
  const execution = d?.automatic_execution || d?.automation?.last_execution
  const executablePlan = consensus?.independent_plan || d?.entry_plan
  const ladder = executablePlan?.limit_layers || []
  const ladderText = ladder.map(layer => `${layer.quality?.toUpperCase() || `L${layer.index}`} ${fmt(layer.price)} · ${(layer.risk_weight * 100).toFixed(0)}%`).join(' / ')
  const reducedSingle = executablePlan?.ladder_execution_profile === 'single_limit_reduced_risk'
  const watchZone = d?.setup_state === 'WATCH_ZONE'
  const retainedRisk = executablePlan?.risk_fraction > 0 ? Math.round(100 * (executablePlan.limit_ladder_total_risk_fraction ?? executablePlan.risk_fraction) / executablePlan.risk_fraction) : 100
  const executionAccepted = Boolean(execution?.accepted)
  const executionAttempted = Boolean(execution?.attempted)
  const executionLabel = executionAccepted ? 'ORDER SUBMITTED' : executionAttempted ? 'BROKER REJECTED' : 'NOT SUBMITTED'
  const executionExplanations = {
    stale_execution_snapshot: 'The broker snapshot expired before submission; MT5 was not called.',
    fresh_execution_validation_failed: 'The plan failed validation against the refreshed broker snapshot; MT5 was not called.',
    independent_plan_not_ready: 'The independent analysis did not provide an executable READY plan.',
    settings_changed_during_analysis: 'Automation settings changed while the analysis was running.',
    blocked_existing_exposure: 'Existing gold exposure prevented a new order.',
    blocked_plan_expired: 'The plan expired before broker submission.',
    duplicate_setup_blocked: 'This setup was already processed and was not submitted again.',
    automation_paused: 'New AI reviews and trade submissions are paused. Existing exposure protection remains active.',
  }
  const executionExplanation = execution?.error || executionExplanations[execution?.status] || (executionAccepted ? 'MT5 accepted the protected order.' : (execution?.status || 'No execution result').replaceAll('_', ' '))
  return <div className="app-shell">
    <div className="workspace" id="overview">
      <header className="topbar">
        <div className="topbar-intro">
          <div className="brand"><span className="eyebrow">TRADEW</span><h1>Gold overview<span className="tiny-badge">XAU / USD</span></h1></div>
          <span className="connection" role="status"><i className={busy || executionSettings.paused ? 'dim' : ''} />{executionSettings.paused ? 'Automation paused · protection active' : busy ? 'Data Refresh' : autoAiBusy ? `${d?.automation?.automatic_provider === 'astra' ? 'Astra High' : 'Gemini'} analysis running` : 'Live scanner · 1s target'}</span>
        </div>
        <div className="top-actions" aria-label="Scan controls">
          <section className="control-group" aria-label="Automation settings">
            <div className="control-group-heading"><span>01 / AUTOMATION</span><span role="status">{settingsBusy ? 'SAVING' : settingsError ? 'SAVE FAILED' : 'AUTO'}</span></div>
            <div className="control-grid">
              <ControlMenu label="Entry method" value={executionSettings.entry_mode} options={ENTRY_OPTIONS} onChange={entry_mode => updateExecutionSettings({ entry_mode })} />
              <ControlMenu label="Auto provider" value={executionSettings.provider} options={PROVIDER_OPTIONS} onChange={provider => updateExecutionSettings({ provider })} title="Used for reviewed deterministic, consensus, and independent automatic plans" />
            </div>
            <div className="control-grid control-grid-toggles">
              <button className={`control-toggle ${executionSettings.auto_trailing ? 'active' : ''}`} onClick={() => updateExecutionSettings({ auto_trailing: !executionSettings.auto_trailing })} aria-pressed={executionSettings.auto_trailing}><Icon name="chart" size={16} /><span>Auto trailing</span><b>{executionSettings.auto_trailing ? 'ON' : 'OFF'}</b></button>
              <button className={`control-toggle ${pushState === 'enabled' ? 'active' : ''}`} onClick={togglePhoneAlerts} disabled={pushBusy || pushState === 'checking' || pushState === 'unsupported'} aria-pressed={pushState === 'enabled'}><Icon name="pulse" size={16} /><span>{pushBusy ? 'Updating alerts…' : 'Phone alerts'}</span><b>{pushState === 'enabled' ? 'ON' : pushState === 'unsupported' ? 'N/A' : 'OFF'}</b></button>
              <button className={`control-toggle pause-toggle ${executionSettings.paused ? 'paused' : ''}`} onClick={() => updateExecutionSettings({ paused: !executionSettings.paused })} disabled={settingsBusy} aria-pressed={executionSettings.paused} title="Stops new AI reviews and entries while keeping scanning, pending-order supervision, and trailing active"><Icon name="shield" size={16} /><span>{executionSettings.paused ? 'Resume automation' : 'Pause automation'}</span><b>{executionSettings.paused ? 'PAUSED' : 'RUNNING'}</b></button>
            </div>
            {settingsError && <p className="control-save-error" role="alert">{settingsError}</p>}
          </section>
          <section className="control-group" aria-label="On-demand analysis">
            <div className="control-group-heading"><span>02 / ON-DEMAND ANALYSIS</span><span>MANUAL</span></div>
            <div className="reviewer-select"><ControlMenu label="Reviewer" value={aiProvider} options={PROVIDER_OPTIONS} onChange={setAiProvider} /></div>
            <div className="control-grid control-grid-actions">
              <button className="control-action" onClick={reviewWithAi} disabled={aiBusy || independentBusy || !decision?.snapshot_id}><Icon name="shield" size={16} /><span>{aiBusy ? 'Reviewing…' : `Review with ${aiProvider === 'astra' ? 'Astra High' : 'Gemini'}`}</span></button>
              <button className="control-action control-action-emphasis" onClick={analyzeIndependently} disabled={independentBusy || aiBusy || !decision?.snapshot_id}><Icon name="scan" size={16} /><span>{independentBusy ? 'Analyzing…' : 'Independent scan'}</span></button>
            </div>
          </section>
        </div>
      </header>
      {error && <div className="error-banner" role="alert"><Icon name="pulse" />{error}<span>Check the local API and terminal connection.</span></div>}
      <div className="dashboard-grid">
        <Panel title="Market overview" label="01 / PRICE ACTION" className="market-panel" aside={<span className="quiet">{time}</span>}>
          <div className="market-summary"><div className="instrument"><span className="asset-icon">Au</span><div><strong>{d?.symbol || 'Gold / US Dollar'}</strong><span>Gold spot · Broker snapshot</span></div></div><div className="quote"><strong>{fmt(d?.bid)}</strong><span>Bid price <i> / {d?.risk_currency || '—'}</i></span></div><div className="quote secondary"><strong>{fmt(d?.ask)}</strong><span>Ask price</span></div></div>
          <div className="chart-toolbar"><div className="tabs">{FRAMES.map(tf => <button key={tf} className={frame === tf ? 'selected' : ''} onClick={() => setFrame(tf)}>{tf}</button>)}</div><div className="chart-switch"><button onClick={() => setChartType('area')} className={chartType === 'area' ? 'selected' : ''}>Area</button><button onClick={() => setChartType('candles')} className={chartType === 'candles' ? 'selected' : ''}>Candles</button></div></div>
          <Chart bars={bars} candles={chartType === 'candles'} id="price-gradient" zones={d?.chart_analysis?.zones || []} orderBlock={d?.order_block} entryZone={d?.planned_entry_zone} />
          <div className="market-footer"><span><i className="legend-dot" />{frame} price history</span><span>Spread <b>{fmt(d?.spread)}</b></span><span>ATR 14 <b>{fmt(score[frame]?.atr14_simple)}</b></span><span className="quiet">{`${d?.session || 'Session unresolved'} · ${score[frame]?.volatility_regime || 'regime unknown'}`}</span></div>
        </Panel>
        <Panel title="Signal intelligence" label="02 / SETUP QUALITY" aside={<span className="status-tag">{d?.action || 'WAIT'}</span>}>
          <div className="rings"><Ring value={d?.buy_setup_score} label="BUY SETUP" /><Ring icon="shield" value={d ? 100 : 0} label={executionSettings.entry_mode === 'consensus' ? '3-WAY CONSENSUS' : executionSettings.entry_mode === 'independent' ? 'LLM AUTO' : executionSettings.entry_mode === 'reviewed' ? 'REVIEWED PLAN' : 'DETERMINISTIC'} /><Ring value={d?.sell_setup_score} label="SELL SETUP" muted /></div>
          <div className="signal-state"><div><span className="eyebrow">CURRENT STATE</span><strong>{(d?.setup_lifecycle || d?.setup_state || 'NO_SETUP').replaceAll('_', ' ')}</strong></div><span className="state-glyph"><Icon name="pulse" size={28} /></span></div>
          {d?.planned_entry_zone && <div className="entry-plan"><div><span>{watchZone ? 'CONTROLLING WATCH ZONE' : reducedSingle ? 'REDUCED-RISK SINGLE LIMIT' : ladder.length > 1 ? `${ladder.length}-LAYER BROKER LIMIT` : 'BROKER LIMIT PLAN · BID ZONE'}</span><strong>{watchZone ? `${d.planned_direction} WATCH · ${fmt(d.planned_entry_zone.low)}–${fmt(d.planned_entry_zone.high)}` : reducedSingle ? `${d.planned_direction}_LIMIT ${fmt(ladder[0]?.price)} · ${(ladder[0]?.risk_weight * 100).toFixed(0)}% OF SETUP RISK` : ladder.length > 1 ? ladderText : `${d.planned_direction || d.action} · ${fmt(d.planned_entry_zone.low)}–${fmt(d.planned_entry_zone.high)}`}</strong></div><div><span>{watchZone ? 'ACTIVATION REQUIREMENT' : 'SHARED PROTECTION / OBJECTIVES'}</span><strong>{watchZone ? 'Fresh completed M5 rejection + M1 confirmation' : `SL ${fmt(executablePlan?.stop)} · ${(executablePlan?.targets || []).map(value => `TP ${fmt(value)}`).join(' / ') || 'STRUCTURAL TARGET'}`}</strong></div></div>}
          <div className="thesis">{(d?.reasons || ['Waiting for the first automatic broker snapshot.']).slice(0, 3).map((r, i) => <p key={r}><span>0{i + 1}</span>{r}</p>)}</div>
          {displayedReview && <div className="ai-review"><div className="ai-review-head"><span>{manualReview ? 'MANUAL AI CROSS-CHECK' : 'AUTOMATIC AI CROSS-CHECK'}</span><b className={displayedReview.assessment?.status === 'AGREEMENT' ? 'teal' : 'coral'}>{displayedReview.assessment?.status}</b></div><div className="ai-review-row"><strong>{aiResultProviderLabel(displayedReview)}</strong><span>{displayedReview.review?.status === 'ok' ? `${displayedReview.review.verdict} · ${displayedReview.review.summary}` : `Unavailable · ${displayedReview.review?.error}`}</span></div><small>{reviewSnapshotMoved ? 'Manual result retained from the reviewed snapshot while live data refreshes' : 'Current reviewed snapshot'} · no execution authority</small></div>}
          {displayedIndependent && <div className="independent-review"><div className="independent-head"><span>{independentAnalysis ? 'MANUAL INDEPENDENT MARKET READ' : 'AUTOMATIC INDEPENDENT MARKET READ'}</span><b className={independent?.recommended_action === 'BUY' ? 'teal' : independent?.recommended_action === 'SELL' ? 'coral' : 'quiet'}>{independent?.status === 'ok' ? independent.recommended_action : 'UNAVAILABLE'}</b></div><div className="independent-meta"><strong>{aiResultProviderLabel(displayedIndependent)}</strong><span>{independent?.market_bias || 'NO BIAS'} · {(independent?.setup_status || 'NO SETUP').replaceAll('_', ' ')}{independent?.risk_tier ? ` · ${independent.risk_tier} RISK ${RISK_TIER_PERCENT[independent.risk_tier]}%` : ''}</span></div><p className="independent-summary">{independent?.status === 'ok' ? independent.summary : independent?.error}</p>{independent?.trade_plan && <div className="entry-plan"><div><span>INDEPENDENT NATIVE LIMIT</span><strong>{independent.trade_plan.order_type} · {fmt(independent.trade_plan.entry_price)}</strong></div><div><span>PROTECTION / OBJECTIVE</span><strong>SL {fmt(independent.trade_plan.stop_loss)} · TP {fmt(independent.trade_plan.take_profit)}</strong></div></div>}{independent?.status === 'ok' && <div className="plain-summary"><span>SIMPLE EXPLANATION</span><p>{independent.plain_language_summary}</p></div>}{independent?.evidence?.length ? <ul className="independent-evidence">{independent.evidence.slice(0, 4).map(item => <li key={item}>{item}</li>)}</ul> : null}{independent?.status === 'ok' && <div className="independent-conditions"><div><span>ENTRY CONDITION</span><p>{independent.entry_condition}</p></div><div><span>INVALIDATION</span><p>{independent.invalidation_condition}</p></div>{independent.risk_tier && <div><span>RISK TIER · {independent.risk_tier} {RISK_TIER_PERCENT[independent.risk_tier]}%</span><p>{independent.risk_reason}</p></div>}</div>}<small>{independentSnapshotMoved ? 'Blind result retained from the analyzed snapshot while live data refreshes' : 'Blind input: 24 completed candles and confirmed levels per timeframe'} · broker submission remains deterministically validated</small></div>}
          {consensus && <div className="ai-review"><div className="ai-review-head"><span>AUTOMATIC TRADE AUTHORIZATION · {(consensus.mode || executionSettings.entry_mode).replaceAll('_', ' ')}</span><b className={consensus.status === 'AUTHORIZED' ? 'teal' : 'quiet'}>{consensus.status.replaceAll('_', ' ')}</b></div><div className="ai-review-row"><strong>{consensus.direction || 'NO DIRECTION'}</strong><span>{consensus.signal_origin === 'independent_llm' ? 'Independent LLM plan · deterministic event trigger only' : consensus.signal_origin === 'reviewed_deterministic' || consensus.mode === 'reviewed' ? `Deterministic plan ${consensus.deterministic || '—'} · informed review ${consensus.gemini_review || '—'} · no blind independent plan` : `Deterministic ${consensus.deterministic || '—'} · review ${consensus.gemini_review || '—'} · independent ${consensus.independent || '—'}`}</span></div>{ladder.length > 0 && <small>Executable limit layers: {ladderText} · {retainedRisk}% of setup risk retained after preflight</small>}{consensus.blockers?.length ? <small>Blocked: {consensus.blockers.map(item => item.replaceAll('_', ' ')).join(' · ')}</small> : null}{consensus.errors?.length ? <small className="coral">Validation error: {consensus.errors[0]}</small> : null}{execution && <div className={`execution-outcome ${executionAccepted ? 'submitted' : 'blocked'}`} role="status"><div><span>EXECUTION RESULT</span><b>{executionLabel}</b></div><strong>{(execution.status || 'not attempted').replaceAll('_', ' ')}</strong><p>{executionExplanation}{execution.layers_submitted ? ` ${execution.layers_submitted}/${execution.layers_requested} layers submitted.` : ''}{execution.volume ? ` ${fmt(execution.volume)} lots total.` : ''}</p></div>}</div>}
          <div className="next-trigger"><Icon name="arrow" /><div><span>NEXT TRIGGER</span><p>{d?.next_trigger || 'Request a fresh snapshot'}</p></div></div>
        </Panel>
        <Panel title="Structure & confirmation" label="03 / MULTI-TIMEFRAME" className="structure-panel" aside={<span className="quiet">7 timeframes</span>}>
          <div id="structure" className="structure-layout"><div><div className="subheading"><span><i className="legend-dot" />{frame} / Completed-candle context</span><span className="teal">{score[frame]?.trend || 'Awaiting data'}</span></div><Chart bars={bars?.slice(0, -1)} candles id="structure-gradient" small /><div className="subheading lower"><span>Volatility profile</span><span>Simple ATR 14</span></div><div className="atr-bars">{FRAMES.map(tf => { const value = score[tf]?.atr14_simple; const max = Math.max(1, ...Object.values(score).map(s => s.atr14_simple || 0)); return <div key={tf}><span>{tf}</span><div><i style={{ width: (value || 0) / max * 100 + '%' }} /></div><b>{fmt(value)}</b></div> })}</div></div>
          <div className="frame-table"><div className="table-head"><span>FRAME</span><span>STRUCTURE</span></div>{FRAMES.map(tf => <button key={tf} onClick={() => setFrame(tf)} className={frame === tf ? 'current' : ''}><b>{tf}</b><span className={score[tf]?.trend === 'bullish' ? 'teal' : score[tf]?.trend === 'bearish' ? 'coral' : 'quiet'}>{score[tf]?.trend || '—'} <i>{score[tf]?.trend === 'bullish' ? '↗' : score[tf]?.trend === 'bearish' ? '↘' : '—'}</i></span></button>)}<div className="structure-note"><Icon name="scan" /><span>Pivots confirmed after two subsequent candle closes.</span></div></div></div>
        </Panel>
        <Panel title="Portfolio & protection" label="04 / EXPOSURE CONTROL" className="portfolio-panel" aside={positions?.length ? <button className="half-close-button" onClick={previewHalfClose} disabled={closeHalfBusy || !executionSettings.paused} title={executionSettings.paused ? 'Preview closing 50% of aggregate XAU volume, including external positions' : 'Pause automation before using this manual portfolio action'}><Icon name="shield" size={14} />{closeHalfBusy ? 'Working…' : 'Close 50%'}</button> : null}>
          <div id="portfolio" className="portfolio-metrics"><div><span>OPEN POSITIONS</span><strong>{positions ? String(positions.length).padStart(2, '0') : '—'}</strong></div><div><span>PENDING ORDERS</span><strong>{orders ? String(orders.length).padStart(2, '0') : '—'}</strong></div><div><span>ACCOUNT EQUITY</span><strong>{fmt(account?.equity)}<small>{d?.risk_currency || ''}</small></strong></div></div>
          {closeHalfResult && <div className={`portfolio-action-result ${closeHalfResult.complete ? 'complete' : 'partial'}`} role="status"><strong>{closeHalfResult.complete ? '50% CLOSE COMPLETED' : '50% CLOSE INCOMPLETE'}</strong><span>{fmt(closeHalfResult.verified_closed_volume)} lots closed · {fmt(closeHalfResult.remaining_volume)} lots remain · {closeHalfResult.remaining_position_count} positions</span><button onClick={() => setCloseHalfResult(null)}>Dismiss</button></div>}
          <div className="portfolio-tabs tabs">{['positions', 'proposals', 'checks'].map(t => <button key={t} className={tab === t ? 'selected' : ''} onClick={() => setTab(t)}>{t}{t === 'checks' && flags.length ? <em>{flags.length}</em> : null}</button>)}</div>
          <div className="exposure-body">{tab === 'positions' ? (positions?.length || orders?.length) ? <div className="table-scroll"><table><thead><tr><th>Ticket / symbol</th><th>State / side</th><th>Lots</th><th>Entry</th><th>Actual SL</th><th>Risk / supervision ({d?.risk_currency})</th></tr></thead><tbody>{(positions || []).map(p => { const risk = positionRisk.find(item => item.ticket === p.ticket); const role = risk?.management_role === 'runner' ? 'RUNNER' : risk?.management_role === 'take_profit' ? 'TP1' : null; return <tr key={`position-${p.ticket}`}><td>{p.ticket}<small>{p.symbol}{p.original_setup?.setup_ref ? ` · S:${p.original_setup.setup_ref}` : ''}</small></td><td className={p.type === 0 ? 'teal' : 'coral'}>OPEN · {p.type === 0 ? 'BUY' : 'SELL'}{role ? ` · ${role}` : ''}</td><td>{fmt(p.volume)}</td><td>{fmt(p.price_open)}</td><td>{p.sl > 0 ? fmt(p.sl) : 'Unprotected'}</td><td>{risk ? risk.cash_risk == null ? 'Unknown' : `Risk ${fmt(risk.cash_risk)}` : fmt(p.profit)}</td></tr> })}{(orders || []).map(o => { const buy = [2, 4, 6].includes(o.type); const check = pendingChecks.find(item => item.ticket === o.ticket); const supervised = check?.status === 'pending_still_valid' ? 'VALID' : check?.status?.replaceAll('_', ' ').toUpperCase(); const expiry = o.time_expiration ? `Expires ${new Date(o.time_expiration * 1000).toISOString().slice(11, 16)} UTC` : 'Broker pending'; const role = /L\d+R$/.test(o.comment || '') ? 'RUNNER' : /L\d+T$/.test(o.comment || '') ? 'TP1' : null; return <tr key={`order-${o.ticket}`}><td>{o.ticket}<small>{o.symbol}{o.comment ? ` · ${o.comment}` : ''}</small></td><td className={buy ? 'teal' : 'coral'}>PENDING · {buy ? 'BUY LIMIT' : 'SELL LIMIT'}{role ? ` · ${role}` : ''}</td><td>{fmt(o.volume_current ?? o.volume_initial)}</td><td>{fmt(o.price_open)}</td><td>{o.sl > 0 ? fmt(o.sl) : 'Unprotected'}</td><td>{supervised ? `${supervised} · ${expiry}` : expiry}</td></tr> })}</tbody></table></div> : <div className="empty-state"><span><Icon name="shield" size={25} /></span><strong>{positions && orders ? 'No open positions or pending orders' : 'Exposure not yet verified'}</strong><p>{positions && orders ? 'Portfolio is clear in this snapshot.' : 'The automatic scanner is checking broker exposure.'}</p></div> : tab === 'proposals' ? d?.proposed_management_changes?.length ? <ul className="check-list">{d.proposed_management_changes.map((c, i) => <li key={i}><strong>{c.action} · #{c.ticket}</strong><span>{c.reason}</span><span>{proposalInstruction(c)}</span></li>)}</ul> : <div className="empty-state"><span><Icon name="pulse" size={25} /></span><strong>No management proposals</strong><p>Actual broker protection remains unchanged.</p></div> : <ul className="check-list">{flags.length ? flags.map(f => <li key={f}><i className="warning-dot" />{f}</li>) : <li>{decision ? 'No data-quality flags in this snapshot.' : 'Checks will appear with the first automatic scan.'}</li>}</ul>}</div>
        </Panel>
      </div>
      <footer className="page-footer"><button className="record-button" onClick={() => setDetails(!details)} aria-expanded={details}><Icon name={details ? 'arrow' : 'scan'} size={14} />{details ? 'Close Record' : 'View Record'}<span>↗</span></button><span className="quiet">{time}</span></footer>
      {details && <section className="panel record"><h2>Decision record</h2><pre>{JSON.stringify(d || { status: 'Waiting for automatic scan' }, null, 2)}</pre></section>}
    </div>
    {closeHalfPreview && <div className="portfolio-modal-backdrop" role="presentation"><section className="portfolio-modal" role="dialog" aria-modal="true" aria-labelledby="close-half-title"><span className="eyebrow">MANUAL PORTFOLIO ACTION</span><h2 id="close-half-title">Close 50% of {closeHalfPreview.symbol} exposure?</h2><p>This includes positions not opened by Tradew. Confirmation is valid only while the exact ticket and volume snapshot remains unchanged.</p><div className="portfolio-modal-grid"><div><span>POSITIONS</span><strong>{closeHalfPreview.position_count}</strong><small>{closeHalfPreview.external_position_count} external</small></div><div><span>CURRENT VOLUME</span><strong>{fmt(closeHalfPreview.total_volume)}</strong><small>lots</small></div><div><span>VOLUME TO CLOSE</span><strong>{fmt(closeHalfPreview.close_target_volume)}</strong><small>{closeHalfPreview.full_position_closes} full · {closeHalfPreview.partial_position_closes} partial</small></div><div><span>EST. PROFIT REALIZED</span><strong>{fmt(closeHalfPreview.estimated_profit_to_realize)}</strong><small>{d?.risk_currency || ''} · changes with price</small></div></div><div className="portfolio-modal-warning"><Icon name="pulse" size={17} /><span>This sends immediate market close orders. It does not cancel pending orders and cannot guarantee one price across every ticket.</span></div><div className="portfolio-modal-actions"><button onClick={() => setCloseHalfPreview(null)} disabled={closeHalfBusy}>Cancel</button><button className="confirm-half-close" onClick={executeHalfClose} disabled={closeHalfBusy}>{closeHalfBusy ? 'Closing and verifying…' : 'Confirm close 50%'}</button></div></section></div>}
  </div>
}
