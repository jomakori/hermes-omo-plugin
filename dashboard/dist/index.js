/**
 * Hermes OMO — Dashboard Plugin
 *
 * Live view of OMO (Oh My OpenAgent) orchestration runs: a run list on the
 * left, and on the right the selected run's workers plus a live-tailing
 * transcript feed. Plain IIFE, no build step. Polls two endpoints — the
 * run list every 5s and the transcript every 1.5s while the run is active —
 * and goes quiet whenever the tab is hidden or the run finishes.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;
  const { timeAgo } = SDK.utils;

  const API = "/api/plugins/omo";
  const RUNS_POLL_MS = 5000;
  const TRANSCRIPT_POLL_MS = 1500;

  // Inline colour treatment per transcript event kind; "status" and any
  // unknown kind fall through to the inherited (default) text colour.
  const KIND_STYLE = {
    tool: { opacity: 0.6 },
    result: { color: "#3fb950" },
    error: { color: "#f85149" },
  };

  // Zero-pad a clock component, e.g. 7 -> "07".
  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  // Render an event timestamp as HH:MM:SS; accepts epoch seconds, epoch ms,
  // or an ISO string, whatever shape the backend happens to send.
  function formatClock(ts) {
    let d;
    if (typeof ts === "number") {
      d = new Date(ts < 1e12 ? ts * 1000 : ts);
    } else {
      d = new Date(ts);
    }
    if (isNaN(d.getTime())) return String(ts);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // Look up a run's current `active` flag by id from the latest runs list.
  function isRunActive(runs, runId) {
    for (let i = 0; i < runs.length; i++) {
      if (runs[i].run_id === runId) return !!runs[i].active;
    }
    return false;
  }

  // Find a run object by id from the latest runs list.
  function findRun(runs, runId) {
    for (let i = 0; i < runs.length; i++) {
      if (runs[i].run_id === runId) return runs[i];
    }
    return null;
  }

  // One row in the left-hand runs list.
  function RunRow(props) {
    const run = props.run;
    const selected = props.selected;
    const workerCount = (run.workers || []).length;
    return h(
      "div",
      {
        className: "omo-run" + (selected ? " omo-run--active" : ""),
        style: { cursor: "pointer" },
        onClick: props.onSelect,
      },
      h(
        "div",
        { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px" } },
        h("span", {}, run.title || run.run_id),
        run.active ? h("span", { className: "omo-badge-live" }, "LIVE") : null
      ),
      h(
        "div",
        { style: { fontSize: "0.85em", opacity: 0.75 } },
        (run.status || "unknown") + " · " + timeAgo(run.updated_at || run.created_at) + " · " + workerCount + " worker" + (workerCount === 1 ? "" : "s")
      )
    );
  }

  // Left-hand column: run list, with loading/error/empty states handled.
  function RunsList(props) {
    if (props.error) {
      return h("div", { className: "omo-runs" }, "Could not load runs: " + props.error);
    }
    if (!props.loaded) {
      return h("div", { className: "omo-runs" }, "Loading…");
    }
    if (props.runs.length === 0) {
      return h("div", { className: "omo-runs" }, "No OMO runs yet. Dispatch one with the omo tool.");
    }
    return h(
      "div",
      { className: "omo-runs" },
      props.runs.map(function (run) {
        return h(RunRow, {
          key: run.run_id,
          run: run,
          selected: run.run_id === props.selectedId,
          onSelect: function () { props.onSelect(run.run_id); },
        });
      })
    );
  }

  // One worker status row above the transcript feed.
  function WorkerRow(props) {
    const w = props.worker;
    return h(
      "div",
      { className: "omo-worker" },
      h("span", {}, w.agent),
      h("span", {}, w.status),
      h("span", {}, (w.last_tool ? w.last_tool + " · " : "") + timeAgo(w.last_activity_at))
    );
  }

  // Worker roster panel for the currently selected run.
  function WorkersPanel(props) {
    const workers = props.workers || [];
    if (workers.length === 0) {
      return h("div", { className: "omo-workers" }, h("div", { className: "omo-worker" }, "No workers yet."));
    }
    return h(
      "div",
      { className: "omo-workers" },
      workers.map(function (w, idx) {
        return h(WorkerRow, { key: (w.task_id || w.agent) + idx, worker: w });
      })
    );
  }

  // One transcript line, colour-coded by event kind.
  function FeedLine(props) {
    const ev = props.event;
    const style = KIND_STYLE[ev.kind] || {};
    return h(
      "div",
      { className: "omo-line", style: style },
      h("span", { className: "omo-ts" }, formatClock(ev.ts)),
      " " + ev.agent + " · " + ev.kind + ": " + ev.text
    );
  }

  // Live-tailing transcript feed; auto-scrolls to bottom unless the user
  // has scrolled up to read earlier lines.
  function Feed(props) {
    const feedRef = useRef(null);
    const autoScrollRef = props.autoScrollRef;

    useEffect(function () {
      const el = feedRef.current;
      if (el && autoScrollRef.current) {
        el.scrollTop = el.scrollHeight;
      }
    }, [props.events]);

    const onScroll = useCallback(function (e) {
      const el = e.target;
      autoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    }, [autoScrollRef]);

    return h(
      "div",
      {
        className: "omo-feed",
        ref: feedRef,
        onScroll: onScroll,
        style: { fontFamily: "monospace", overflowY: "auto", maxHeight: "55vh" },
      },
      props.error ? h("div", { style: { color: "#f85149" } }, "Transcript error: " + props.error) : null,
      props.events.map(function (ev) {
        return h(FeedLine, { key: ev.seq, event: ev });
      })
    );
  }

  // Right-hand column: workers + transcript for the selected run.
  function RunDetail(props) {
    if (!props.run) {
      return h("div", {}, "Select a run to see its transcript.");
    }
    return h(
      "div",
      {},
      h(WorkersPanel, { workers: props.run.workers }),
      h(Feed, { events: props.events, error: props.feedError, autoScrollRef: props.autoScrollRef })
    );
  }

  // Top-level OMO dashboard page component.
  function OmoRunsPage() {
    const [runs, setRuns] = useState([]);
    const [runsLoaded, setRunsLoaded] = useState(false);
    const [runsError, setRunsError] = useState(null);
    const [selectedId, setSelectedId] = useState(null);
    const [events, setEvents] = useState([]);
    const [feedError, setFeedError] = useState(null);

    const runsRef = useRef([]);
    const sinceRef = useRef(0);
    const autoScrollRef = useRef(true);

    // Poll the run list every 5s; skip the network call while the tab is
    // hidden, and always clear the timer on unmount.
    useEffect(function () {
      let cancelled = false;
      function load() {
        if (document.hidden) return;
        SDK.fetchJSON(API + "/runs")
          .then(function (data) {
            if (cancelled) return;
            const list = (data && data.runs) || [];
            runsRef.current = list;
            setRuns(list);
            setRunsLoaded(true);
            setRunsError(null);
          })
          .catch(function (err) {
            if (cancelled) return;
            setRunsLoaded(true);
            setRunsError(err && err.message ? err.message : String(err));
          });
      }
      load();
      const timer = setInterval(load, RUNS_POLL_MS);
      return function () {
        cancelled = true;
        clearInterval(timer);
      };
    }, []);

    // Reset and (re)poll the transcript whenever the selected run changes.
    // Polls every 1.5s via since=next_since while the run is active; stops
    // as soon as the run goes inactive, the tab is hidden, or we unmount.
    useEffect(function () {
      if (!selectedId) {
        setEvents([]);
        return;
      }
      let cancelled = false;
      sinceRef.current = 0;
      autoScrollRef.current = true;
      setEvents([]);
      setFeedError(null);

      function poll() {
        if (document.hidden) return;
        const url = API + "/runs/" + encodeURIComponent(selectedId) + "/transcript?since=" + sinceRef.current + "&limit=200";
        SDK.fetchJSON(url)
          .then(function (data) {
            if (cancelled) return;
            const evs = (data && data.events) || [];
            if (evs.length > 0) {
              setEvents(function (prev) { return prev.concat(evs); });
            }
            if (data && typeof data.next_since === "number") {
              sinceRef.current = data.next_since;
            }
            setFeedError(null);
            if (!isRunActive(runsRef.current, selectedId)) {
              clearInterval(timer);
            }
          })
          .catch(function (err) {
            if (cancelled) return;
            setFeedError(err && err.message ? err.message : String(err));
          });
      }

      poll();
      const timer = setInterval(function () {
        if (!isRunActive(runsRef.current, selectedId)) {
          clearInterval(timer);
          return;
        }
        poll();
      }, TRANSCRIPT_POLL_MS);

      return function () {
        cancelled = true;
        clearInterval(timer);
      };
    }, [selectedId]);

    const selectedRun = selectedId ? findRun(runs, selectedId) : null;

    return h(
      "div",
      { className: "omo-root", style: { display: "flex", gap: "16px" } },
      h(RunsList, {
        runs: runs,
        loaded: runsLoaded,
        error: runsError,
        selectedId: selectedId,
        onSelect: setSelectedId,
      }),
      h(
        "div",
        { style: { flex: 1, minWidth: 0 } },
        h(RunDetail, {
          run: selectedRun,
          events: events,
          feedError: feedError,
          autoScrollRef: autoScrollRef,
        })
      )
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("omo", OmoRunsPage);
  }
})();
