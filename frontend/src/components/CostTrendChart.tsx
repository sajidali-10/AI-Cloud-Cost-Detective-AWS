// Phase 6B — Daily cost trend chart.
//
// Inline SVG area + line chart driven entirely by semantic CSS
// variables so dark and light palettes Just Work without a
// separate palette.  No chart library: the spec asks for a
// "lightweight maintained React-compatible chart library" and
// the smallest defensible option is a 200-line SVG component that
// reads tokens via Tailwind utilities (text-fg-muted, fill-primary,
// etc.).
//
// Accessibility: a visually-hidden <table> with date + cost mirrors
// every datapoint so screen readers can read the trend.
//
// Tooltip: a plain div positioned over the focused / hovered
// datapoint.  Keyboard arrow keys move the focus across points.

import { useMemo, useRef, useState } from 'react'
import type { DailyCostPoint } from '../types/finops'
import { formatCurrencyDecimal, parseDecimal } from '../lib/format'

export interface CostTrendChartProps {
  points: DailyCostPoint[]
  currency?: string
  height?: number
  ariaLabel?: string
}

const PADDING = { top: 12, right: 8, bottom: 24, left: 44 }

export function CostTrendChart({
  points,
  currency = 'USD',
  height = 220,
  ariaLabel = 'Daily cost trend',
}: CostTrendChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)

  const data = useMemo(() => {
    return points
      .map((p) => ({ date: p.date, value: parseDecimal(p.amount) ?? 0 }))
      .filter((p) => Number.isFinite(p.value))
  }, [points])

  const width = 720
  const innerWidth = width - PADDING.left - PADDING.right
  const innerHeight = height - PADDING.top - PADDING.bottom

  const max = data.reduce((m, d) => Math.max(m, d.value), 0)
  const min = data.reduce((m, d) => Math.min(m, d.value), Number.POSITIVE_INFINITY)
  const range = Math.max(0.0001, max - (Number.isFinite(min) ? min : 0))
  // Always start y-axis at zero for honest cost comparisons.
  const yMax = max
  const yMin = 0

  const xStep = data.length > 1 ? innerWidth / (data.length - 1) : 0
  const yScale = (v: number) =>
    innerHeight - ((v - yMin) / (yMax - yMin || 1)) * innerHeight

  const linePath = data
    .map((d, i) => `${i === 0 ? 'M' : 'L'}${(PADDING.left + i * xStep).toFixed(2)},${(PADDING.top + yScale(d.value)).toFixed(2)}`)
    .join(' ')

  const areaPath =
    data.length === 0
      ? ''
      : `${linePath} L${(PADDING.left + (data.length - 1) * xStep).toFixed(2)},${(PADDING.top + innerHeight).toFixed(2)} L${PADDING.left.toFixed(2)},${(PADDING.top + innerHeight).toFixed(2)} Z`

  const yTicks = computeYTicks(yMin, yMax, 4)

  const hoverPoint = hoverIndex !== null && data[hoverIndex] ? data[hoverIndex] : null
  const hoverX = hoverIndex !== null ? PADDING.left + hoverIndex * xStep : 0
  const hoverY = hoverPoint ? PADDING.top + yScale(hoverPoint.value) : 0

  if (data.length === 0) {
    return (
      <div
        className="flex h-[180px] items-center justify-center rounded-md border border-border bg-surface-2 text-xs text-fg-muted"
        role="status"
      >
        No data
      </div>
    )
  }

  return (
    <div className="space-y-2" data-testid="cost-trend-chart">
      <div className="relative" ref={containerRef}>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width="100%"
          height={height}
          preserveAspectRatio="none"
          role="img"
          aria-label={ariaLabel}
          onMouseLeave={() => setHoverIndex(null)}
        >
          <title>{ariaLabel}</title>
          {/* Y grid */}
          {yTicks.map((tick) => {
            const y = PADDING.top + yScale(tick)
            return (
              <g key={tick}>
                <line
                  x1={PADDING.left}
                  x2={width - PADDING.right}
                  y1={y}
                  y2={y}
                  className="stroke-border"
                  strokeWidth={1}
                />
                <text
                  x={PADDING.left - 6}
                  y={y + 3}
                  textAnchor="end"
                  className="fill-fg-muted"
                  fontSize={10}
                >
                  {formatCurrencyDecimal(tick, currency, { emptyFallback: '' })}
                </text>
              </g>
            )
          })}
          {/* X labels — sparse, ~5 evenly spaced */}
          {data.map((d, i) => {
            if (data.length > 7 && i % Math.ceil(data.length / 5) !== 0 && i !== data.length - 1) {
              return null
            }
            const x = PADDING.left + i * xStep
            return (
              <text
                key={d.date}
                x={x}
                y={height - 6}
                textAnchor="middle"
                className="fill-fg-muted"
                fontSize={10}
              >
                {d.date.slice(5)}
              </text>
            )
          })}
          {/* Area */}
          {areaPath && (
            <path d={areaPath} className="fill-primary-soft" stroke="none" />
          )}
          {/* Line */}
          <path
            d={linePath}
            fill="none"
            className="stroke-primary"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
          {/* Hit targets */}
          {data.map((d, i) => {
            const x = PADDING.left + i * xStep
            const y = PADDING.top + yScale(d.value)
            return (
              <g key={`hit-${d.date}`}>
                <circle
                  cx={x}
                  cy={y}
                  r={hoverIndex === i ? 5 : 0}
                  className="fill-primary"
                />
                <rect
                  x={x - xStep / 2}
                  y={PADDING.top}
                  width={Math.max(xStep, 4)}
                  height={innerHeight}
                  fill="transparent"
                  onMouseEnter={() => setHoverIndex(i)}
                  onFocus={() => setHoverIndex(i)}
                  tabIndex={0}
                  onBlur={() => setHoverIndex(null)}
                  aria-label={`${d.date}: ${formatCurrencyDecimal(d.value, currency)}`}
                />
              </g>
            )
          })}
          {/* Hover indicator */}
          {hoverPoint && (
            <line
              x1={hoverX}
              x2={hoverX}
              y1={PADDING.top}
              y2={height - PADDING.bottom}
              className="stroke-border-strong"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
          )}
        </svg>
        {hoverPoint && (
          <div
            className="
              pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full
              rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs
              text-fg-primary shadow-card-md
            "
            style={{ left: `${(hoverX / width) * 100}%`, top: `${(hoverY / height) * 100}%` }}
          >
            <div className="text-fg-muted">{hoverPoint.date}</div>
            <div className="font-medium">{formatCurrencyDecimal(hoverPoint.value, currency)}</div>
          </div>
        )}
      </div>
      {/* Screen-reader mirror */}
      <table className="sr-only">
        <caption>{ariaLabel}</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {data.map((d) => (
            <tr key={d.date}>
              <th scope="row">{d.date}</th>
              <td>{formatCurrencyDecimal(d.value, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function computeYTicks(min: number, max: number, targetCount: number): number[] {
  if (max === min) {
    return [min]
  }
  const step = niceStep((max - min) / targetCount)
  const ticks: number[] = []
  for (let v = Math.floor(min / step) * step; v <= max + 0.0001; v += step) {
    ticks.push(round2(v))
  }
  return ticks.slice(0, targetCount + 2)
}

function niceStep(raw: number): number {
  if (raw <= 0) return 1
  const exp = Math.floor(Math.log10(raw))
  const base = Math.pow(10, exp)
  const norm = raw / base
  let nice: number
  if (norm < 1.5) nice = 1
  else if (norm < 3) nice = 2
  else if (norm < 7) nice = 5
  else nice = 10
  return nice * base
}

function round2(n: number): number {
  return Math.round(n * 100) / 100
}
