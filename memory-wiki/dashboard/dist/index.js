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
  var LOCALE_KEY = "memory-wiki.locale";
  var ZH_SOURCES = {
    session: "\u4F1A\u8BDD",
    cli: "\u547D\u4EE4\u884C",
    desktop: "\u684C\u9762\u7AEF",
    web: "\u7F51\u9875\u7AEF",
    local: "\u672C\u5730",
    telegram: "\u7535\u62A5",
    discord: "Discord",
    whatsapp: "WhatsApp",
    whatsapp_cloud: "WhatsApp \u4E91",
    slack: "Slack",
    signal: "Signal",
    mattermost: "Mattermost",
    matrix: "Matrix",
    email: "\u7535\u5B50\u90AE\u4EF6",
    sms: "\u77ED\u4FE1",
    dingtalk: "\u9489\u9489",
    api_server: "API \u670D\u52A1",
    webhook: "Webhook \u63A5\u5165",
    msgraph_webhook: "\u5FAE\u8F6F Graph \u901A\u77E5",
    feishu: "\u98DE\u4E66",
    wecom: "\u4F01\u4E1A\u5FAE\u4FE1",
    wecom_callback: "\u4F01\u4E1A\u5FAE\u4FE1\u56DE\u8C03",
    weixin: "\u5FAE\u4FE1",
    bluebubbles: "BlueBubbles",
    qqbot: "QQ \u673A\u5668\u4EBA",
    yuanbao: "\u5143\u5B9D",
    relay: "\u4E2D\u7EE7\u5E73\u53F0"
  };
  var ZH = {
    "Memory Wiki": "\u8BB0\u5FC6\u767E\u79D1",
    "Local session history": "\u672C\u5730\u4F1A\u8BDD\u5386\u53F2",
    "Subjects": "\u4E3B\u9898",
    "Daily logs": "\u6BCF\u65E5\u8BB0\u5F55",
    "Recent sessions": "\u6700\u8FD1\u4F1A\u8BDD",
    "Persistent Memory": "\u56FA\u5B9A\u8BB0\u5FC6",
    "Related Sessions": "\u76F8\u5173\u4F1A\u8BDD",
    "Snippets": "\u539F\u6587\u7247\u6BB5",
    "Sessions": "\u4F1A\u8BDD",
    "What we did": "\u6D3B\u52A8\u8BB0\u5F55",
    "Agent notes": "\u52A9\u624B\u7B14\u8BB0",
    "User profile": "\u7528\u6237\u8D44\u6599",
    "Browse deterministic subjects and daily logs from this Hermes profile's local session database. Not AI-generated and not Hindsight data.": "\u6309\u786E\u5B9A\u6027\u89C4\u5219\u6D4F\u89C8\u6B64 Hermes \u914D\u7F6E\u6863\u6848\u672C\u5730\u4F1A\u8BDD\u6570\u636E\u5E93\u4E2D\u7684\u4E3B\u9898\u4E0E\u6BCF\u65E5\u8BB0\u5F55\uFF1B\u975E AI \u751F\u6210\uFF0C\u4E5F\u4E0D\u662F Hindsight \u6570\u636E\u3002",
    "The notes the agent carries into every session (MEMORY.md / USER.md). Edit via the memory tool.": "\u52A9\u624B\u5728\u6BCF\u6B21\u4F1A\u8BDD\u4E2D\u643A\u5E26\u7684\u7B14\u8BB0\uFF08MEMORY.md / USER.md\uFF09\u3002\u8BF7\u901A\u8FC7\u8BB0\u5FC6\u5DE5\u5177\u4FEE\u6539\u3002",
    "Search subjects\u2026": "\u641C\u7D22\u4E3B\u9898\u2026",
    "Search subjects": "\u641C\u7D22\u4E3B\u9898",
    "Searching\u2026": "\u6B63\u5728\u641C\u7D22\u2026",
    "No subjects match your search.": "\u6CA1\u6709\u5339\u914D\u7684\u4E3B\u9898\u3002",
    "No subjects yet \u2014 run a few sessions first.": "\u6682\u65E0\u4E3B\u9898\u2014\u2014\u5148\u8FDB\u884C\u51E0\u6B21\u4F1A\u8BDD\u3002",
    "No daily logs yet.": "\u6682\u65E0\u6BCF\u65E5\u8BB0\u5F55\u3002",
    "No sessions found.": "\u672A\u627E\u5230\u4F1A\u8BDD\u3002",
    "No snippets found.": "\u6682\u65E0\u539F\u6587\u7247\u6BB5\u3002",
    "No activity found.": "\u6682\u65E0\u6D3B\u52A8\u8BB0\u5F55\u3002",
    "Loading memory wiki\u2026": "\u6B63\u5728\u52A0\u8F7D\u8BB0\u5FC6\u767E\u79D1\u2026",
    "Failed to load memory wiki": "\u8BB0\u5FC6\u767E\u79D1\u52A0\u8F7D\u5931\u8D25",
    "Failed to load subject": "\u4E3B\u9898\u52A0\u8F7D\u5931\u8D25",
    "Failed to load daily log": "\u6BCF\u65E5\u8BB0\u5F55\u52A0\u8F7D\u5931\u8D25",
    "Search failed": "\u641C\u7D22\u5931\u8D25",
    "Retry": "\u91CD\u8BD5",
    "Technical details": "\u6280\u672F\u8BE6\u60C5",
    "Unknown": "\u672A\u77E5",
    "last touched": "\u6700\u8FD1\u66F4\u65B0",
    "first seen": "\u9996\u6B21\u51FA\u73B0",
    "active until": "\u6700\u540E\u6D3B\u8DC3",
    "Memory Wiki language": "\u8BB0\u5FC6\u767E\u79D1\u8BED\u8A00",
    "Automatic": "\u81EA\u52A8",
    "user": "\u7528\u6237",
    "assistant": "\u52A9\u624B",
    "system": "\u7CFB\u7EDF",
    "tool": "\u5DE5\u5177",
    "developer": "\u5F00\u53D1\u8005",
    "message": "\u6D88\u606F",
    "request": "\u8BF7\u6C42",
    "response": "\u56DE\u590D",
    "result": "\u7ED3\u679C",
    "coding": "\u7F16\u7A0B",
    "research": "\u7814\u7A76",
    "planning": "\u89C4\u5212",
    "conversation": "\u5BF9\u8BDD"
  };
  var LocaleContext = React.createContext("en");
  function useI18n() {
    const locale = React.useContext(LocaleContext);
    const t = (text) => locale === "zh-CN" && Object.prototype.hasOwnProperty.call(ZH, text) ? ZH[text] : text;
    const number = (n) => new Intl.NumberFormat(locale).format(n);
    const count = (n, kind) => locale === "zh-CN" ? `${number(n)} ${kind === "session" ? "\u4E2A\u4F1A\u8BDD" : "\u6761\u6D88\u606F"}` : `${number(n)} ${kind}${n === 1 ? "" : "s"}`;
    const sourceLabel = (value) => locale === "zh-CN" && Object.prototype.hasOwnProperty.call(ZH_SOURCES, value) ? ZH_SOURCES[value] : value;
    const activityText = (item) => locale === "zh-CN" && item.kind === "tool" && typeof item.text === "string" && item.text.startsWith("Tool: ") ? item.text.slice(6) === "tool call" ? "\u5DE5\u5177\u8C03\u7528" : item.text.slice(6) : item.text;
    return { locale, t, number, count, sourceLabel, activityText };
  }
  function validPreference(value) {
    return ["auto", "zh-CN", "en"].includes(value) ? value : "auto";
  }
  function readPreference() {
    try {
      return validPreference(window.localStorage.getItem(LOCALE_KEY));
    } catch {
      return "auto";
    }
  }
  function hostLocale() {
    const language = document.documentElement.lang.trim() || window.navigator.languages?.[0] || window.navigator.language || "en";
    return /^zh(?:-|$)/i.test(language) ? "zh-CN" : "en";
  }
  function useLocale() {
    const [preference, setPreference] = useState(readPreference);
    const [automatic, setAutomatic] = useState(hostLocale);
    useEffect(() => {
      const update = () => setAutomatic(hostLocale());
      const observer = new MutationObserver(update);
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
      window.addEventListener("languagechange", update);
      const storage = (event) => {
        if (event.key === LOCALE_KEY || event.key === null) setPreference(readPreference());
      };
      window.addEventListener("storage", storage);
      return () => {
        observer.disconnect();
        window.removeEventListener("languagechange", update);
        window.removeEventListener("storage", storage);
      };
    }, []);
    const select = (value) => {
      const next = validPreference(value);
      setPreference(next);
      try {
        window.localStorage.setItem(LOCALE_KEY, next);
      } catch {
      }
    };
    return [preference === "auto" ? automatic : preference, preference, select];
  }
  function LanguagePicker({ preference, select }) {
    const { t } = useI18n();
    return /* @__PURE__ */ React.createElement("label", { className: "flex items-center justify-end gap-2 text-sm text-muted-foreground" }, t("Memory Wiki language"), /* @__PURE__ */ React.createElement(
      "select",
      {
        "aria-label": t("Memory Wiki language"),
        value: preference,
        onChange: (e) => select(e.target.value),
        className: "rounded-md border border-border bg-background p-2 text-foreground"
      },
      /* @__PURE__ */ React.createElement("option", { value: "auto" }, t("Automatic")),
      /* @__PURE__ */ React.createElement("option", { value: "zh-CN" }, "\u4E2D\u6587"),
      /* @__PURE__ */ React.createElement("option", { value: "en" }, "English")
    ));
  }
  function formatTime(timestamp, locale, unknown) {
    if (!timestamp) return unknown;
    const date = new Date(timestamp * 1e3);
    if (Number.isNaN(date.getTime())) return unknown;
    return date.toLocaleString(locale, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function formatDate(value, locale) {
    const date = /* @__PURE__ */ new Date(`${value}T00:00:00`);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(locale, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric"
    });
  }
  function parseHash(hash) {
    const match = /^#\/(subjects|days)\/([^/?#]+)/.exec(hash || "");
    if (!match) return { view: "overview" };
    try {
      return { view: match[1] === "subjects" ? "subject" : "day", id: decodeURIComponent(match[2]) };
    } catch {
      return { view: "overview" };
    }
  }
  function useHashRoute() {
    const [route, setRoute] = useState(() => parseHash(window.location.hash));
    useEffect(() => {
      const onChange = () => setRoute(parseHash(window.location.hash));
      window.addEventListener("hashchange", onChange);
      return () => window.removeEventListener("hashchange", onChange);
    }, []);
    const go = useCallback((hash) => {
      if (hash) window.location.hash = hash;
      else {
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
  function useResource(path, field) {
    const [state, setState] = useState({ path, loading: true, data: null, error: null });
    const [attempt, setAttempt] = useState(0);
    useEffect(() => {
      let cancelled = false;
      setState({ path, loading: true, data: null, error: null });
      SDK.fetchJSON(path).then((response) => {
        if (!cancelled) setState({ path, loading: false, data: field ? response[field] : response, error: null });
      }).catch((error) => {
        if (!cancelled) setState({ path, loading: false, data: null, error: error?.message || "" });
      });
      return () => {
        cancelled = true;
      };
    }, [path, field, attempt]);
    return { ...state.path === path ? state : { loading: true, data: null, error: null }, retry: () => setAttempt((n) => n + 1) };
  }
  function PageShell({ children }) {
    return /* @__PURE__ */ React.createElement("div", { className: "space-y-6" }, children);
  }
  function LoadingState() {
    const { t } = useI18n();
    return /* @__PURE__ */ React.createElement("div", { role: "status", className: "flex min-h-[240px] items-center justify-center text-sm text-muted-foreground" }, t("Loading memory wiki\u2026"));
  }
  function ErrorState({ message, detail, retry }) {
    const { t } = useI18n();
    return /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3 py-8 text-sm" }, /* @__PURE__ */ React.createElement("p", { role: "alert", className: "text-destructive" }, t(message)), detail && /* @__PURE__ */ React.createElement("details", null, /* @__PURE__ */ React.createElement("summary", null, t("Technical details")), /* @__PURE__ */ React.createElement("p", { className: "whitespace-pre-wrap" }, detail)), /* @__PURE__ */ React.createElement(Button, { onClick: retry }, t("Retry"))));
  }
  function StatCard({ label, value }) {
    const { t, number } = useI18n();
    return /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardContent, { className: "py-5" }, /* @__PURE__ */ React.createElement("div", { className: "text-2xl font-semibold leading-none" }, number(value)), /* @__PURE__ */ React.createElement("div", { className: "mt-1 text-xs uppercase tracking-wide text-muted-foreground" }, t(label))));
  }
  function BackButton({ go }) {
    const { t } = useI18n();
    return /* @__PURE__ */ React.createElement(Button, { ghost: true, size: "sm", className: "border border-border", onClick: () => go("") }, "\u2190 ", t("Memory Wiki"));
  }
  function SessionList({ sessions }) {
    const { t, locale, sourceLabel } = useI18n();
    if (!sessions?.length) return /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, t("No sessions found."));
    return /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, sessions.map((session) => /* @__PURE__ */ React.createElement("div", { key: session.id, title: session.id, className: "block rounded-md border border-border/60 p-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap items-center gap-2" }, /* @__PURE__ */ React.createElement("span", { className: "font-medium" }, session.title || session.id), session.source && /* @__PURE__ */ React.createElement(Badge, { tone: "secondary" }, sourceLabel(session.source)), /* @__PURE__ */ React.createElement("span", { className: "text-xs text-muted-foreground" }, formatTime(session.last_active, locale, t("Unknown")))), session.preview && /* @__PURE__ */ React.createElement("p", { className: "mt-2 line-clamp-2 text-sm text-muted-foreground" }, session.preview))));
  }
  function LinkCard({ href, go, children, className }) {
    return /* @__PURE__ */ React.createElement(
      "a",
      {
        href,
        onClick: (e) => {
          e.preventDefault();
          go(href);
        },
        className: cn("block rounded-lg outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring", className)
      },
      children
    );
  }
  function SubjectCard({ subject, go }) {
    const { t, locale, count } = useI18n();
    return /* @__PURE__ */ React.createElement(LinkCard, { href: subjectHash(subject.slug), go }, /* @__PURE__ */ React.createElement(Card, { className: "h-full transition-colors hover:bg-muted/30" }, /* @__PURE__ */ React.createElement(CardHeader, { className: "pb-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex items-start justify-between gap-3" }, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, subject.name), /* @__PURE__ */ React.createElement(Badge, null, count(subject.session_count, "session")))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, subject.keywords.slice(0, 5).map((keyword) => /* @__PURE__ */ React.createElement(Badge, { key: keyword, tone: "secondary" }, keyword))), /* @__PURE__ */ React.createElement("p", { className: "text-xs text-muted-foreground" }, count(subject.message_count, "message"), " \xB7 ", t("last touched"), " ", formatTime(subject.last_seen, locale, t("Unknown"))), subject.snippets[0] && /* @__PURE__ */ React.createElement("p", { className: "line-clamp-3 text-sm text-muted-foreground" }, subject.snippets[0].text))));
  }
  function DayCard({ day, go }) {
    const { t, locale, count, activityText } = useI18n();
    return /* @__PURE__ */ React.createElement(LinkCard, { href: dayHash(day.date), go }, /* @__PURE__ */ React.createElement(Card, { className: "transition-colors hover:bg-muted/30" }, /* @__PURE__ */ React.createElement(CardHeader, { className: "pb-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap items-center justify-between gap-3" }, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, formatDate(day.date, locale)), /* @__PURE__ */ React.createElement(Badge, null, count(day.session_count, "session")))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, day.subjects.slice(0, 6).map((subject) => /* @__PURE__ */ React.createElement(Badge, { key: subject.slug, tone: "secondary" }, subject.name))), /* @__PURE__ */ React.createElement("ul", { className: "space-y-1 text-sm text-muted-foreground" }, day.work_items.slice(0, 3).map((item, idx) => /* @__PURE__ */ React.createElement("li", { key: `${item.session_id}-${item.timestamp}-${idx}`, className: "line-clamp-1" }, /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, t(item.kind), ":"), " ", activityText(item)))))));
  }
  function NotesList({ entries }) {
    return /* @__PURE__ */ React.createElement("ul", { className: "space-y-1.5" }, entries.map((entry, i) => /* @__PURE__ */ React.createElement("li", { key: i, className: "whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-2 text-xs" }, entry)));
  }
  function PersistentMemoryCard({ notes }) {
    const { t, number } = useI18n();
    if (!notes || notes.memory_count === 0 && notes.user_count === 0) return null;
    return /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("Persistent Memory"))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-4" }, /* @__PURE__ */ React.createElement("p", { className: "text-xs text-muted-foreground" }, t("The notes the agent carries into every session (MEMORY.md / USER.md). Edit via the memory tool.")), notes.memory.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm font-medium" }, t("Agent notes"), " (", number(notes.memory_count), ")"), /* @__PURE__ */ React.createElement(NotesList, { entries: notes.memory })), notes.user.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm font-medium" }, t("User profile"), " (", number(notes.user_count), ")"), /* @__PURE__ */ React.createElement(NotesList, { entries: notes.user }))));
  }
  function OverviewView({ go, search, setSearch }) {
    const { t } = useI18n();
    const { data: overview, loading, error, retry } = useResource(`${API}/overview`);
    const [searchResults, setSearchResults] = useState(null);
    const [searchError, setSearchError] = useState(false);
    const [searchLoading, setSearchLoading] = useState(false);
    const [searchAttempt, setSearchAttempt] = useState(0);
    useEffect(() => {
      const trimmed = search.trim();
      setSearchError(false);
      setSearchResults(null);
      setSearchLoading(!!trimmed);
      if (!trimmed) return;
      let cancelled = false;
      const timer = window.setTimeout(() => {
        const qs = new URLSearchParams({ q: trimmed, limit: "100" });
        SDK.fetchJSON(`${API}/subjects?${qs.toString()}`).then((response) => {
          if (!cancelled) setSearchResults(response.subjects);
        }).catch(() => {
          if (!cancelled) setSearchError(true);
        }).finally(() => {
          if (!cancelled) setSearchLoading(false);
        });
      }, 250);
      return () => {
        cancelled = true;
        window.clearTimeout(timer);
      };
    }, [search, searchAttempt]);
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error !== null || !overview) return /* @__PURE__ */ React.createElement(ErrorState, { message: "Failed to load memory wiki", detail: error, retry });
    const visibleSubjects = searchResults ?? overview.subjects ?? [];
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement("div", { className: "flex flex-col gap-2" }, /* @__PURE__ */ React.createElement("div", { className: "text-sm text-muted-foreground" }, t("Local session history")), /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, t("Memory Wiki")), /* @__PURE__ */ React.createElement("p", { className: "max-w-3xl text-sm text-muted-foreground" }, t("Browse deterministic subjects and daily logs from this Hermes profile's local session database. Not AI-generated and not Hindsight data."))), /* @__PURE__ */ React.createElement("div", { className: "grid gap-3 sm:grid-cols-3" }, /* @__PURE__ */ React.createElement(StatCard, { label: "Subjects", value: overview.subjects.length }), /* @__PURE__ */ React.createElement(StatCard, { label: "Daily logs", value: overview.daily_logs.length }), /* @__PURE__ */ React.createElement(StatCard, { label: "Recent sessions", value: overview.recent_sessions.length })), /* @__PURE__ */ React.createElement("div", { className: "max-w-xl" }, /* @__PURE__ */ React.createElement(Input, { value: search, onChange: (e) => setSearch(e.target.value), placeholder: t("Search subjects\u2026"), "aria-label": t("Search subjects") })), /* @__PURE__ */ React.createElement("section", { className: "space-y-3" }, /* @__PURE__ */ React.createElement("h2", { className: "text-xl font-semibold" }, t("Subjects")), searchLoading ? /* @__PURE__ */ React.createElement("p", { role: "status" }, t("Searching\u2026")) : searchError ? /* @__PURE__ */ React.createElement(ErrorState, { message: "Search failed", retry: () => setSearchAttempt((n) => n + 1) }) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3" }, visibleSubjects.map((subject) => /* @__PURE__ */ React.createElement(SubjectCard, { key: subject.slug, subject, go }))), visibleSubjects.length === 0 && /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, t(search.trim() ? "No subjects match your search." : "No subjects yet \u2014 run a few sessions first.")))), /* @__PURE__ */ React.createElement("section", { className: "grid gap-6 xl:grid-cols-[2fr_1fr]" }, /* @__PURE__ */ React.createElement("div", { className: "space-y-3" }, /* @__PURE__ */ React.createElement("h2", { className: "text-xl font-semibold" }, t("Daily logs")), /* @__PURE__ */ React.createElement("div", { className: "space-y-3" }, overview.daily_logs.map((day) => /* @__PURE__ */ React.createElement(DayCard, { key: day.date, day, go })), overview.daily_logs.length === 0 && /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, t("No daily logs yet.")))), /* @__PURE__ */ React.createElement("div", { className: "space-y-6" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("Recent sessions"))), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: overview.recent_sessions }))), /* @__PURE__ */ React.createElement(PersistentMemoryCard, { notes: overview.notes }))));
  }
  function SubjectView({ slug, go }) {
    const { t, locale, count } = useI18n();
    const { data: subject, loading, error, retry } = useResource(`${API}/subjects/${encodeURIComponent(slug)}`, "subject");
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error !== null || !subject) return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement(ErrorState, { message: "Failed to load subject", detail: error, retry }));
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, subject.name), /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, count(subject.session_count, "session"), " \xB7 ", count(subject.message_count, "message"), " \xB7 ", t("first seen"), " ", formatTime(subject.first_seen, locale, t("Unknown"))), /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, subject.keywords.map((keyword) => /* @__PURE__ */ React.createElement(Badge, { key: keyword, tone: "secondary" }, keyword)))), /* @__PURE__ */ React.createElement("div", { className: "grid gap-6 xl:grid-cols-[1fr_1fr]" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("Related Sessions"))), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: subject.sessions }))), /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("Snippets"))), /* @__PURE__ */ React.createElement(CardContent, { className: "space-y-3" }, subject.snippets.map((snippet) => /* @__PURE__ */ React.createElement("blockquote", { key: `${snippet.session_id}-${snippet.message_id}-${snippet.timestamp}`, className: "border-l-2 border-border pl-3 text-sm text-muted-foreground" }, /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, t(snippet.role ?? "message")), ": ", snippet.text)), !subject.snippets.length && /* @__PURE__ */ React.createElement("p", null, t("No snippets found."))))));
  }
  function DayView({ date, go }) {
    const { t, locale, count, activityText } = useI18n();
    const { data: day, loading, error, retry } = useResource(`${API}/days/${encodeURIComponent(date)}`, "daily_log");
    if (loading) return /* @__PURE__ */ React.createElement(LoadingState, null);
    if (error !== null || !day) return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement(ErrorState, { message: "Failed to load daily log", detail: error, retry }));
    return /* @__PURE__ */ React.createElement(PageShell, null, /* @__PURE__ */ React.createElement(BackButton, { go }), /* @__PURE__ */ React.createElement("div", { className: "space-y-2" }, /* @__PURE__ */ React.createElement("h1", { className: "text-3xl font-semibold tracking-tight" }, formatDate(day.date, locale)), /* @__PURE__ */ React.createElement("p", { className: "text-sm text-muted-foreground" }, count(day.session_count, "session"), " \xB7 ", count(day.message_count, "message"), " \xB7 ", t("active until"), " ", formatTime(day.last_active_max, locale, t("Unknown"))), /* @__PURE__ */ React.createElement("div", { className: "flex flex-wrap gap-1.5" }, day.subjects.map((subject) => /* @__PURE__ */ React.createElement("a", { key: subject.slug, href: subjectHash(subject.slug), onClick: (e) => {
      e.preventDefault();
      go(subjectHash(subject.slug));
    } }, /* @__PURE__ */ React.createElement(Badge, { tone: "secondary" }, subject.name))))), /* @__PURE__ */ React.createElement("div", { className: "grid gap-6 xl:grid-cols-[1fr_1fr]" }, /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("What we did"))), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement("ul", { className: "space-y-2 text-sm text-muted-foreground" }, day.work_items.map((item, idx) => /* @__PURE__ */ React.createElement("li", { key: `${item.session_id}-${item.timestamp}-${idx}` }, /* @__PURE__ */ React.createElement("span", { className: "font-medium text-foreground/80" }, t(item.kind)), ": ", activityText(item)))), !day.work_items.length && /* @__PURE__ */ React.createElement("p", null, t("No activity found.")))), /* @__PURE__ */ React.createElement(Card, null, /* @__PURE__ */ React.createElement(CardHeader, null, /* @__PURE__ */ React.createElement(CardTitle, { className: "text-base" }, t("Sessions"))), /* @__PURE__ */ React.createElement(CardContent, null, /* @__PURE__ */ React.createElement(SessionList, { sessions: day.sessions })))));
  }
  function MemoryWikiPage() {
    const [route, go] = useHashRoute();
    const [locale, preference, select] = useLocale();
    const [search, setSearch] = useState("");
    return /* @__PURE__ */ React.createElement(LocaleContext.Provider, { value: locale }, /* @__PURE__ */ React.createElement("div", { lang: locale, className: "space-y-6" }, /* @__PURE__ */ React.createElement(LanguagePicker, { preference, select }), route.view === "subject" ? /* @__PURE__ */ React.createElement(SubjectView, { slug: route.id, go }) : route.view === "day" ? /* @__PURE__ */ React.createElement(DayView, { date: route.id, go }) : /* @__PURE__ */ React.createElement(OverviewView, { go, search, setSearch })));
  }
  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, MemoryWikiPage);
})();
