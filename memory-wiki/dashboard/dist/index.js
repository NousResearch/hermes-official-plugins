/* hermes-memory-wiki dashboard bundle — built from dashboard/src/index.jsx with esbuild; edit the source, run npm run build. MIT, (c) 2026 Nous Research; original by @Araja119 (hermes-agent #31244). */
(() => {
  // dashboard/src/index.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var { React } = SDK;
  var { useState, useEffect, useCallback } = SDK.hooks;
  var { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input } = SDK.components;
  var { cn } = SDK.utils;
  var PLUGIN_NAME = "memory-wiki";
  var API = "/api/plugins/memory-wiki";
  function formatTime(timestamp) {
    if (!timestamp) return "Unknown";
    return new Date(timestamp * 1e3).toLocaleString(void 0, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit"
    });
  }
  function formatDate(date) {
    try {
      return (/* @__PURE__ */ new Date(`${date}T00:00:00`)).toLocaleDateString(void 0, {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric"
      });
    } catch {
      return date;
    }
  }
  function parseHash(hash) {
    const m = /^#\/(subjects|days)\/([^/?#]+)/.exec(hash || "");
    if (!m) return { view: "overview" };
    return { view: m[1] === "subjects" ? "subject" : "day", id: decodeURIComponent(m[2]) };
  }
  function useHashRoute() {
    const [route, setRoute] = useState(() => parseHash(window.location.hash));
    useEffect(() => {
      const onChange = () => setRoute(parseHash(window.location.hash));
      window.addEventListener("hashchange", onChange);
      return () => window.removeEventListener("hashchange", onChange);
    }, []);
    const go = useCallback((hash) => {
      if (hash) {
        window.location.hash = hash;
      } else {
        window.history.pushState(null, "", window.location.pathname + window.location.search);
        setRoute({ view: "overview" });
      }
    }, []);
    return [route, go];
  }
  function subjectHash(slug) {
    return `#/subjects/${encodeURIComponent(slug)}`;
  }
  function dayHash(date) {
    return `#/days/${encodeURIComponent(date)}`;
  }
  function PageShell({ children }) {
    return /* @__PURE__ */ React.createElement("div", { className: "space-y-6" }, children);
  }
  function LoadingState() {
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement("div", { className: "flex min-h-[240px] items-center justify-center text-sm text-muted-foreground" }, "Loading memory wiki\u2026"));
  }
  function ErrorState({ message }) {
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardContent, { className: "py-8 text-sm text-destructive" }, message)));
  }
  function StatCard({ label, value }) {
    return /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardContent, { className: "py-5" }, /* @__PURE__ */ React.createElement("div", { className: "text-2xl font-semibold leading-none" }, value), /* @__PURE__ */ React.createElement("div", { className: "mt-1 text-xs uppercase tracking-wide text-muted-foreground" }, label)));
  }
  function BackButton({ go }) {
    return /* @__PURE__ */ React.createElement(Button, { ghost: true, size: "sm", className: "border border-border", onClick: () => go("") }, "\u2190 Memory Wiki");
  }
  function SessionList({ sessions }) {
    if (!sessions || sessions.length === 0) {
      return /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, "No sessions found.");
    }
    return /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, sessions.map((session) => /* @__PURE__ */ React.createElement(
      "div",
      {
        key: session.id,
        title: session.id,
        className: "block rounded-md border border-border/60 p-3"
      },
      /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap items-center gap-2" }, /* @__PURE__ */ React.createElement("span", { className: "font-medium" }, session.title || session.id), session.source && /* @__PURE__ */ React.createElement(Badge, { tone: "secondary" }, session.source), /* @__PURE__ */ React.createElement("span", { className: "text-xs text-muted-foreground" }, formatTime(session.last_active))),
      session.preview && /* @__PURE__ */ React.createElement("p", { className: "mt-2 line-clamp-2 text-sm text-muted-foreground" }, session.preview)
    )));
  }
  function LinkCard({ href, go, children, className }) {
    return /* @__PURE__ */ React.createElement(
      "a",
      {
        href,
        onClick: (event) => {
          event.preventDefault();
          go(href);
        },
        className: cn(
          "block rounded-lg outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring",
          className
        )
      },
      children
    );
  }
  function SubjectCard({ subject, go }) {
    return /* @__PURE__ */ React.createElement(LinkCard, { href: subjectHash(subject.slug), go }, /* @__PURE__ */ React.createElement(Card, { className: "h-full transition-colors hover:bg-muted/30" }, /* @__PURE__ */ React.createElement(CardHeader, { className: "pb-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex items-start justify-between gap-3" }, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, subject.name), /* @__PURE__ */ React.createElement(Badge, null, subject.session_count, " sessions"))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, subject.keywords.slice(0, 5).map((keyword) => /* @__PURE__ */ React.createElement(Badge, { key: keyword, tone: "secondary" }, keyword))), /* @__PURE__ */ React.createElement("p", { className: "text-xs text-muted-foreground" }, subject.message_count, " messages \xB7 last touched ", formatTime(subject.last_seen)), subject.snippets[0] && /* @__PURE__ */ React.createElement("p", { className: "line-clamp-3 text-sm text-muted-foreground" }, subject.snippets[0].text))));
  }
  function DayCard({ day, go }) {
    return /* @__PURE__ */ React.createElement(LinkCard, { href: dayHash(day.date), go }, /* @__PURE__ */ React.createElement(Card, { className: "transition-colors hover:bg-muted/30" }, /* @__PURE__ */ React.createElement(CardHeader, { className: "pb-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap items-center justify-between gap-3" }, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, formatDate(day.date)), /* @__PURE__ */ React.createElement(Badge, null, day.session_count, " sessions"))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, day.subjects.slice(0, 6).map((subject) => /* @__PURE__ */ React.createElement(Badge, { key: subject.slug, tone: "secondary" }, subject.name))), /* @__PURE__ */ React.createElement("ul", { className: "space-y-1 text-sm text-muted-foreground" }, day.work_items.slice(0, 3).map((item, idx) => /* @__PURE__ */ React.createElement("li", { key: `${item.session_id}-${item.timestamp}-${idx}`, className: "line-clamp-1" }, /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, item.kind, ":"), " ", item.text))))));
  }
  function NotesList({ entries }) {
    return /* @__PURE__ */ React.createElement("ul", { className: "space-y-1.5" }, entries.map((entry, i) => /* @__PURE__ */ React.createElement(
      "li",
      {
        key: i,
        className: "whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-2 text-xs"
      },
      entry
    )));
  }
  function PersistentMemoryCard({ notes }) {
    if (!notes || notes.memory_count === 0 && notes.user_count === 0) return null;
    return /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "Persistent Memory")), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-4" }, /* @__PURE__ */ React.createElement("p", { className: "text-xs text-muted-foreground" }, "The notes the agent actually carries into every session (MEMORY.md / USER.md). Audit here; edit via the memory tool or the files directly."), notes.memory.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm font-medium" }, "Agent notes (", notes.memory_count, ")"), /* @__PURE__ */ React.createElement(NotesList, { entries: notes.memory })), notes.user.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm font-medium" }, "User profile (", notes.user_count, ")"), /* @__PURE__ */ React.createElement(NotesList, { entries: notes.user }))));
  }
  function OverviewView({ go }) {
    const [overview, setOverview] = useState(null);
    const [search, setSearch] = useState("");
    const [searchResults, setSearchResults] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    useEffect(() => {
      let cancelled = false;
      const controller = new AbortController();
      SDK.fetchJSON(`${API}/overview`, { signal: controller.signal }).then((resp) => {
        if (!cancelled) setOverview(resp);
      }).catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load memory wiki");
      }).finally(() => {
        if (!cancelled) setLoading(false);
      });
      return () => {
        cancelled = true;
        controller.abort();
      };
    }, []);
    useEffect(() => {
      const trimmed = search.trim();
      if (!trimmed) return void 0;
      let cancelled = false;
      const controller = new AbortController();
      const timer = window.setTimeout(() => {
        const qs = new URLSearchParams({ q: trimmed, limit: "100" });
        SDK.fetchJSON(`${API}/subjects?${qs.toString()}`, { signal: controller.signal }).then((resp) => {
          if (!cancelled) setSearchResults({ query: search, subjects: resp.subjects });
        }).catch(() => {
          if (!cancelled) setSearchResults({ query: search, error: true });
        });
      }, 250);
      return () => {
        cancelled = true;
        controller.abort();
        window.clearTimeout(timer);
      };
    }, [search]);
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error || !overview) return /* @__PURE__ */ React.createElement(ErrorState, { message: error ?? "Memory wiki unavailable" });
    const searching = Boolean(search.trim());
    const currentResult = searchResults?.query === search ? searchResults : null;
    const searchPending = searching && !currentResult;
    const searchFailed = searching && currentResult?.error;
    const visibleSubjects = searching ? currentResult?.subjects ?? [] : overview.subjects ?? [];
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement("div", { className: "flex flex-col gap-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm text-muted-foreground" }, "Local session history"), /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, "Memory Wiki"), /* @__PURE__ */ React.createElement("p", { className: "max-w-3xl text-sm text-muted-foreground" }, "Browse deterministic subjects and daily logs generated from this Hermes profile's local session database.")), /* @__PURE__ */ React.createElement("div", { className: "grid gap-3 sm:grid-cols-3" }, /* @__PURE__ */ React.createElement(StatCard, { label: "Subjects", value: overview.subjects.length }), /* @__PURE__ */ React.createElement(StatCard, { label: "Daily logs", value: overview.daily_logs.length }), /* @__PURE__ */ React.createElement(StatCard, { label: "Recent sessions", value: overview.recent_sessions.length })), /* @__PURE__ */ React.createElement("div", { className: "max-w-xl" }, /* @__PURE__ */ React.createElement(
      Input,
      {
        value: search,
        onChange: (event) => {
          setSearchResults(null);
          setSearch(event.target.value);
        },
        placeholder: "Search subjects\u2026",
        "aria-label": "Search subjects"
      }
    )), /* @__PURE__ */ React.createElement("section", { className: "space-y-3" }, /* @__PURE__ */ React.createElement("h2", { className: "text-xl font-semibold" }, "Subjects"), /* @__PURE__ */ React.createElement("p", { role: "status", "aria-live": "polite", className: "text-sm text-muted-foreground" }, searchPending ? "Searching subjects\u2026" : ""), searchFailed && /* @__PURE__ */ React.createElement("p", { role: "alert", className: "text-sm text-destructive" }, "Could not search subjects. Change your search or clear it to try again."), /* @__PURE__ */ React.createElement("div", { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3", "aria-busy": searchPending }, visibleSubjects.map((subject) => /* @__PURE__ */ React.createElement(SubjectCard, { key: subject.slug, subject, go }))), !searchPending && !searchFailed && visibleSubjects.length === 0 && /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, search.trim() ? "No subjects match your search." : "No subjects yet \u2014 run a few sessions first.")), /* @__PURE__ */ React.createElement("section", { className: "grid gap-6 xl:grid-cols-[2fr_1fr]" }, /* @__PURE__ */ React.createElement("div", { className: "space-y-3" }, /* @__PURE__ */ React.createElement("h2", { className: "text-xl font-semibold" }, "Daily Logs"), /* @__PURE__ */ React.createElement("div", { className: "space-y-3" }, overview.daily_logs.map((day) => /* @__PURE__ */ React.createElement(DayCard, { key: day.date, day, go })), overview.daily_logs.length === 0 && /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, "No daily logs yet."))), /* @__PURE__ */ React.createElement("div", { className: "space-y-6" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "Recent Activity")), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: overview.recent_sessions }))), /* @__PURE__ */ React.createElement(PersistentMemoryCard, { notes: overview.notes }))));
  }
  function SubjectView({ slug, go }) {
    const [subject, setSubject] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    useEffect(() => {
      setLoading(true);
      setError(null);
      setSubject(null);
      SDK.fetchJSON(`${API}/subjects/${encodeURIComponent(slug)}`).then((resp) => setSubject(resp.subject)).catch((err) => setError(err instanceof Error ? err.message : "Failed to load subject")).finally(() => setLoading(false));
    }, [slug]);
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error || !subject) {
      return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement(ErrorState, { message: error ?? "Subject not found" }));
    }
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, subject.name), /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, subject.session_count, " sessions \xB7 ", subject.message_count, " messages \xB7 first seen", " ", formatTime(subject.first_seen)), /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, subject.keywords.map((keyword) => /* @__PURE__ */ React.createElement(Badge, { key: keyword, tone: "secondary" }, keyword)))), /* @__PURE__ */ React.createElement("div", { className: "grid gap-6 xl:grid-cols-[1fr_1fr]" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "Related Sessions")), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: subject.sessions }))), /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "Snippets")), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, subject.snippets.map((snippet) => /* @__PURE__ */ React.createElement(
      "blockquote",
      {
        key: `${snippet.session_id}-${snippet.message_id}-${snippet.timestamp}`,
        className: "border-l-2 border-border pl-3 text-sm text-muted-foreground"
      },
      /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, snippet.role ?? "message"),
      ":",
      " ",
      snippet.text
    ))))));
  }
  function DayView({ date, go }) {
    const [day, setDay] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    useEffect(() => {
      setLoading(true);
      setError(null);
      setDay(null);
      SDK.fetchJSON(`${API}/days/${encodeURIComponent(date)}`).then((resp) => setDay(resp.daily_log)).catch((err) => setError(err instanceof Error ? err.message : "Failed to load daily log")).finally(() => setLoading(false));
    }, [date]);
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error || !day) {
      return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement(ErrorState, { message: error ?? "Daily log not found" }));
    }
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, formatDate(day.date)), /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, day.session_count, " sessions \xB7 ", day.message_count, " messages \xB7 active until", " ", formatTime(day.last_active_max)), /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, day.subjects.map((subject) => /* @__PURE__ */ React.createElement(
      "a",
      {
        key: subject.slug,
        href: subjectHash(subject.slug),
        onClick: (event) => {
          event.preventDefault();
          go(subjectHash(subject.slug));
        }
      },
      /* @__PURE__ */ React.createElement(Badge, { tone: "secondary" }, subject.name)
    )))), /* @__PURE__ */ React.createElement("div", { className: "grid gap-6 xl:grid-cols-[1fr_1fr]" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "What we did")), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement("ul", { className: "space-y-2 text-sm text-muted-foreground" }, day.work_items.map((item, idx) => /* @__PURE__ */ React.createElement("li", { key: `${item.session_id}-${item.timestamp}-${idx}` }, /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, item.kind), ": ", item.text))))), /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, "Sessions")), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: day.sessions })))));
  }
  function MemoryWikiPage() {
    const [route, go] = useHashRoute();
    if (route.view === "subject") return /* @__PURE__ */ React.createElement(SubjectView, { slug: route.id, go });
    if (route.view === "day") return /* @__PURE__ */ React.createElement(DayView, { date: route.id, go });
    return /* @__PURE__ */ React.createElement(OverviewView, { go });
  }
  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, MemoryWikiPage);
})();
