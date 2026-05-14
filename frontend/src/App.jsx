import { useState, useEffect, useRef, useCallback } from 'react';
import Pitch from './components/Pitch.jsx';
import Timeline from './components/Timeline.jsx';
import KeyEventPopup from './components/KeyEventPopup.jsx';

const SPEEDS       = [1500, 750];       // ms per minute: ×1 and ×2
const SPEED_LABELS = ['×1', '×2'];

export default function App() {
  const [currentMinute, setCurrentMinute] = useState(0);
  const [tlData, setTlData]               = useState([]);
  const [keyEvents, setKeyEvents]         = useState([]);
  const [goals, setGoals]                 = useState([]);
  const [lineupsByName, setLineupsByName] = useState({});
  const [playing, setPlaying]             = useState(false);
  const [speedIdx, setSpeedIdx]           = useState(0);
  const [activeKeyEvent, setActiveKeyEvent] = useState(null);
  const playIntervalRef = useRef(null);
  const prevMinuteRef   = useRef(-1);
  const seenEventsRef   = useRef(new Set());

  useEffect(() => {
    async function fetchInit() {
      const [r1, r2, r3] = await Promise.all([
        fetch('/api/timeline'),
        fetch('/api/key-events'),
        fetch('/api/lineups'),
      ]);
      const [timeline, keys, lineups] = await Promise.all([
        r1.json(), r2.json(), r3.json(),
      ]);

      setTlData(timeline);
      setKeyEvents(keys);
      setGoals(
        keys
          .filter(e => e.shot_outcome === 'Goal')
          .map(e => ({ team: e.team, minute: e.minute ?? 0 }))
      );

      // Build name → { jersey, position, team } lookup from both team lineups
      const byName = {};
      for (const [team, players] of Object.entries(lineups)) {
        for (const p of players) {
          byName[p.player_name] = { jersey: p.jersey_number, position: p.position, team };
        }
      }
      setLineupsByName(byName);
    }
    fetchInit().catch(console.error);
  }, []);

  // ── Playback interval ────────────────────────────────────────────────
  useEffect(() => {
    clearInterval(playIntervalRef.current);
    if (!playing) return;
    playIntervalRef.current = setInterval(() => {
      setCurrentMinute(m => {
        if (m >= 95) { setPlaying(false); return m; }
        return m + 1;
      });
    }, SPEEDS[speedIdx]);
    return () => clearInterval(playIntervalRef.current);
  }, [playing, speedIdx]);

  // ── Detect notable shot events during playback ───────────────────────────
  useEffect(() => {
    const prev = prevMinuteRef.current;
    prevMinuteRef.current = currentMinute;
    // Only trigger during forward-playing (one step at a time), not scrubbing/jumping
    if (!playing || currentMinute - prev !== 1) return;
    const notable = keyEvents.find(
      e => (e.minute ?? 0) === currentMinute
        && e.type === 'Shot'
        && !seenEventsRef.current.has(e.event_id)
    );
    if (notable) {
      seenEventsRef.current.add(notable.event_id);
      setPlaying(false);
      setActiveKeyEvent(notable);
    }
  }, [currentMinute, playing, keyEvents]);

  const handleManualScrub = useCallback((m) => {
    setPlaying(false);
    setCurrentMinute(m);
  }, []);

  const onPlayPause = useCallback(() => setPlaying(p => !p), []);

  const onToggleSpeed = useCallback(() => setSpeedIdx(i => (i + 1) % SPEEDS.length), []);

  const onNextHotMoment = useCallback(() => {
    if (!keyEvents.length) return;
    const next = keyEvents.find(e => (e.minute ?? 0) > currentMinute);
    setCurrentMinute(next ? (next.minute ?? 0) : (keyEvents[0].minute ?? 0));
    setPlaying(false);
  }, [keyEvents, currentMinute]);

  const onPrevHotMoment = useCallback(() => {
    if (!keyEvents.length) return;
    const prev = [...keyEvents].reverse().find(e => (e.minute ?? 0) < currentMinute);
    if (prev) { setCurrentMinute(prev.minute ?? 0); setPlaying(false); }
  }, [keyEvents, currentMinute]);

  const morScore = goals.filter(g => g.team === 'Morocco'  && g.minute <= currentMinute).length;
  const porScore = goals.filter(g => g.team === 'Portugal' && g.minute <= currentMinute).length;

  return (
    <div id="app">
      {activeKeyEvent && (
        <KeyEventPopup
          event={activeKeyEvent}
          onDismiss={() => setActiveKeyEvent(null)}
        />
      )}

      <div id="main">
        <Pitch currentMinute={currentMinute} lineupsByName={lineupsByName} />

        <div id="hud-top">
          <div id="match-teams">
            <div className="team-name home">Morocco</div>
            <div id="score-display">{morScore} – {porScore}</div>
            <div className="team-name away">Portugal</div>
          </div>
          <div id="match-meta">
            2022 FIFA World Cup &nbsp;·&nbsp; Quarter-final &nbsp;·&nbsp; Dec 10, 2022
          </div>
        </div>
      </div>

      <Timeline
        tlData={tlData}
        keyEvents={keyEvents}
        currentMinute={currentMinute}
        onMinuteChange={handleManualScrub}
        playing={playing}
        speedLabel={SPEED_LABELS[speedIdx]}
        onPlayPause={onPlayPause}
        onToggleSpeed={onToggleSpeed}
        onNextHotMoment={onNextHotMoment}
        onPrevHotMoment={onPrevHotMoment}
      />
    </div>
  );
}
