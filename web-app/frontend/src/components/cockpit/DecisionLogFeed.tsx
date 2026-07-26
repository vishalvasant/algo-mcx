import { useEffect, useMemo, useState } from "react";
import { ScrollText } from "lucide-react";
import { Link } from "react-router-dom";
import { fetchDecisionLogs } from "../../api/client";
import type { DecisionLogEvent } from "../../types";
import { COCKPIT_DUMMY, COCKPIT_PREVIEW_MODE, dummyDecisionEvents } from "../../utils/cockpitDummyData";
import { formatHumanDecision } from "../../utils/decisionLogFormat";

function formatTs(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function DecisionLogFeed() {
  const [events, setEvents] = useState<DecisionLogEvent[]>([]);

  useEffect(() => {
    const load = () => {
      fetchDecisionLogs({ limit: 12 })
        .then((res) => setEvents(res.events ?? []))
        .catch(() => setEvents([]));
    };
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
  }, []);

  const displayEvents = useMemo(() => {
    if (COCKPIT_PREVIEW_MODE) return dummyDecisionEvents();
    if (events.length >= 4) return events.slice(0, 8);
    const dummy = dummyDecisionEvents();
    const merged = [...events];
    for (const d of dummy) {
      if (merged.length >= 6) break;
      if (!merged.some((e) => e.message === d.message)) merged.push(d);
    }
    return merged.length ? merged : dummy;
  }, [events]);

  return (
    <section className="cockpit-panel decision-feed-panel">
      <header className="cockpit-panel-head">
        <ScrollText size={14} />
        <h3>AI Decision Log</h3>
        <Link to="/logs" className="decision-feed-link">
          View all
        </Link>
      </header>
      <ul className="decision-feed-list">
        {displayEvents.map((ev) => {
          const human = formatHumanDecision(ev);
          const dummyMatch = COCKPIT_DUMMY.decisions.find((d) => d.title === ev.message);
          const title =
            COCKPIT_PREVIEW_MODE || ev.id.startsWith("dummy-")
              ? (dummyMatch?.title ?? ev.message)
              : human.title || ev.message;
          const summary = dummyMatch?.summary ?? human.summary;
          return (
            <li key={ev.id} className={`decision-feed-item sev-${ev.severity}`}>
              <span className="decision-feed-time mono">{formatTs(ev.ts)}</span>
              <strong className="decision-feed-title">{title}</strong>
              <span className="decision-feed-msg">{summary}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
