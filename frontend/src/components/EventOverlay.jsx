import { useEffect, useMemo } from 'react';

// ── Confetti ──────────────────────────────────────────────────────────────────
const CONFETTI_COLORS = ['#FF9900','#FFD700','#FF4444','#55EE88','#66AAFF','#FF66CC','#FFFFFF','#FFAAAA'];

function Confetti() {
  const pieces = useMemo(() =>
    Array.from({ length: 72 }, (_, i) => ({
      id: i,
      x:        Math.random() * 100,
      delay:    Math.random() * 1.8,
      duration: 2.6 + Math.random() * 2.2,
      color:    CONFETTI_COLORS[i % CONFETTI_COLORS.length],
      w:        5 + Math.random() * 11,
      h:        4 + Math.random() * 6,
      isRound:  Math.random() > 0.55,
      endRot:   360 + Math.random() * 720,
    }))
  , []);

  return (
    <div className="ev-confetti" aria-hidden>
      {pieces.map(p => (
        <span
          key={p.id}
          className="ev-confetti-piece"
          style={{
            left:              `${p.x}%`,
            width:             `${p.w}px`,
            height:            `${p.h}px`,
            background:        p.color,
            borderRadius:      p.isRound ? '50%' : '2px',
            animationDelay:    `${p.delay}s`,
            animationDuration: `${p.duration}s`,
            '--end-rot':       `${p.endRot}deg`,
          }}
        />
      ))}
    </div>
  );
}

// ── Goal overlay ──────────────────────────────────────────────────────────────
function GoalOverlay({ event, onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5500);
    return () => clearTimeout(t);
  }, [onDismiss]);

  return (
    <div className="ev-backdrop ev-goal" onClick={onDismiss}>
      <Confetti />
      <div className="ev-glow-ring" />
      <div className="ev-content">
        <div className="ev-ball ev-ball--goal">⚽</div>
        <div className="ev-headline ev-headline--goal">GOAL!</div>
        <div className="ev-sub">
          <span className="ev-player">{event.player}</span>
          <span className="ev-sep">·</span>
          <span className="ev-team">{event.team}</span>
          <span className="ev-sep">·</span>
          <span className="ev-minute">{event.minute}'</span>
        </div>
        <div className="ev-hint">tap anywhere to continue</div>
      </div>
    </div>
  );
}

// ── Shot (saved / blocked / off target / post) overlay ───────────────────────
const SHOT_CFG = {
  Saved:          { label: 'SAVED',          ballCls: 'ev-ball--saved',   dim: false },
  'Saved To Post':{ label: 'SAVED TO POST',  ballCls: 'ev-ball--saved',   dim: false },
  Blocked:        { label: 'BLOCKED',        ballCls: 'ev-ball--blocked', dim: true  },
  'Off T':        { label: 'OFF TARGET',     ballCls: 'ev-ball--miss',    dim: true  },
  Wayward:        { label: 'WAYWARD',        ballCls: 'ev-ball--miss',    dim: true  },
  Post:           { label: 'OFF THE POST',   ballCls: 'ev-ball--post',    dim: true  },
};

function ShotOverlay({ event, onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 3200);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const cfg = SHOT_CFG[event?.shot_outcome] || { label: event?.shot_outcome, ballCls: 'ev-ball--miss', dim: true };

  return (
    <div className={`ev-backdrop ev-shot${cfg.dim ? ' ev-shot--dim' : ''}`} onClick={onDismiss}>
      <div className="ev-content">
        <div className={`ev-ball ${cfg.ballCls}`}>⚽</div>
        <div className="ev-headline ev-headline--shot">{cfg.label}</div>
        <div className="ev-sub">
          <span className="ev-player">{event.player}</span>
          <span className="ev-sep">·</span>
          <span className="ev-team">{event.team}</span>
          <span className="ev-sep">·</span>
          <span className="ev-minute">{event.minute}'</span>
        </div>
      </div>
    </div>
  );
}

// ── Card overlay ──────────────────────────────────────────────────────────────
function CardOverlay({ event, onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 4500);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const isRed = /red/i.test(event?.foul_committed_card || '');

  return (
    <div className={`ev-backdrop ev-card ${isRed ? 'ev-card--red' : 'ev-card--yellow'}`} onClick={onDismiss}>
      <div className="ev-content">
        <div className={`ev-card-shape ${isRed ? 'ev-card-shape--red' : 'ev-card-shape--yellow'}`} />
        <div className="ev-headline ev-headline--card">{isRed ? 'RED CARD' : 'YELLOW CARD'}</div>
        <div className="ev-sub">
          <span className="ev-player">{event.player}</span>
          <span className="ev-sep">·</span>
          <span className="ev-team">{event.team}</span>
          <span className="ev-sep">·</span>
          <span className="ev-minute">{event.minute}'</span>
        </div>
        <div className="ev-hint">tap anywhere to continue</div>
      </div>
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────
export default function EventOverlay({ event, onDismiss }) {
  if (!event) return null;
  if (event.shot_outcome === 'Goal')           return <GoalOverlay  event={event} onDismiss={onDismiss} />;
  if (event.type === 'Foul Committed' && event.foul_committed_card)
                                               return <CardOverlay  event={event} onDismiss={onDismiss} />;
  if (event.type === 'Shot')                   return <ShotOverlay  event={event} onDismiss={onDismiss} />;
  return null;
}
