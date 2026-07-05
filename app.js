// Amperstrand Unified Test Dashboard — Nostr reader for GitHub Pages
// Fetches DVM-native events (kind 5900/6900/7000) + kind 1063 file metadata
// AND kind 30078 parameterized-replaceable run summaries from ALL pubkeys.
// Renders per-project views (tollgate / fips / BLE / microfips / generic).
// Pure vanilla JS, no build step.

// === CLIENT-SIDE ERROR CAPTURE ================================================
// Must run before any other code to catch initialization errors.
// Queues errors in localStorage so they survive page reloads.
(function () {
  var KEY = "tollgate_dashboard_errors";
  var MAX = 10;
  function queue(err) {
    try {
      var q = JSON.parse(localStorage.getItem(KEY) || "[]");
      q.push(Object.assign({ ts: Date.now() }, err));
      if (q.length > MAX) q.shift();
      localStorage.setItem(KEY, JSON.stringify(q));
    } catch (e) {}
  }
  window.addEventListener("error", function (e) {
    queue({
      type: "error",
      msg: e.message || "(unknown error)",
      src: (e.filename || "?") + ":" + (e.lineno || 0) + ":" + (e.colno || 0),
      stack:
        e.error && e.error.stack
          ? e.error.stack.split("\n").slice(0, 5).join("\n")
          : "",
    });
  });
  window.addEventListener("unhandledrejection", function (e) {
    queue({
      type: "promise",
      msg: e.reason && e.reason.message ? e.reason.message : String(e.reason),
      stack:
        e.reason && e.reason.stack
          ? e.reason.stack.split("\n").slice(0, 5).join("\n")
          : "",
    });
  });
})();

// === EVENT CONTRACTS =========================================================
//
//   kind 5900 (DVM job request):
//     tags: ["param", key, value], ["e", request_id]
//
//   kind 6900 (DVM job result — legacy tollgate run parser):
//     tags: ["param", key, value], ["e", request_id], ["file", url]
//     content: JSON with pass/fail counts, file URLs, metadata
//
//   kind 7000 (DVM job feedback):
//     tags: ["status", "processing|success|error"], ["e", request_id]
//
//   kind 1063 (NIP-94 file metadata, per file, BlossomFS):
//     tags: ["url", ...], ["x", sha256], ["m", mime], ["filename", ...], ["size", ...]
//
//   kind 30078 (NIP-78 parameterized replaceable — unified run summary):
//     tags: ["d", run_id], ["t", project_tag], ["file", blossom_url], ...
//     content: JSON with project-specific summary (scenario, counts, metrics)

// === CONFIGURATION ==========================================================
const RELAYS = [
  "wss://relay.cashu.email",
  "wss://relay.damus.io",
  "wss://nos.lol",
  "wss://relay.contextvm.org",
  "wss://relay2.contextvm.org",
  "wss://cvm.otherstuff.ai",
];
const FETCH_TIMEOUT_MS = 12000;
const FETCH_SINCE_DAYS = 90;

// === STATE ==================================================================
let allRuns = [];
let selectedRunId = null;
let imgObserver = null;
let activeImgLoads = 0;
const MAX_CONCURRENT_IMG_LOADS = 3;
const imgLoadQueue = [];

const CACHE_KEY = "prta:runs:v6";
const filterState = { search: "", status: "all", sort: "newest", runner: "", project: "ours", maxAge: 604800 };
let detailLoadId = 0;
let currentTestFilter = "all";
let currentTestSearch = "";
let currentHierarchy = null;
let liveSockets = [];
let liveConnectedCount = 0;
let displayIdCache = new Map();
let renderRunsListTimer = null;

// ===========================================================================
// WebSocket: Fetch kind 30078 (primary) + legacy DVM events (5900/6900/7000) + 1063
// DVM kinds are deprecated per ADR-007 — kept for historical run visibility only.
// ===========================================================================

function fetchDvmEvents(kinds = [30078, 5900, 6900, 7000, 1063], limit = 200) {
  return new Promise((resolve) => {
    const events = new Map();
    let resolved = false;
    let closedRelays = 0;
    let connectedCount = 0;

    const timeout = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        resolve({ events: [...events.values()], connected: connectedCount });
      }
    }, FETCH_TIMEOUT_MS);

    RELAYS.forEach((relayUrl) => {
      let ws;
      try {
        ws = new WebSocket(relayUrl);
      } catch (e) {
        closedRelays++;
        checkDone();
        return;
      }

      const subId = "prta-dvm-" + Math.random().toString(36).slice(2, 8);

      ws.onopen = () => {
        connectedCount++;
        ws.send(JSON.stringify([
          "REQ", subId,
          {
            kinds,
            limit,
            since: Math.floor(Date.now() / 1000) - 86400 * FETCH_SINCE_DAYS,
          },
        ]));
        updateConnectionStatus(connectedCount, RELAYS.length);
      };

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          if (data[0] === "EVENT" && data[1] === subId && data[2]) {
            const evt = data[2];
            events.set(evt.id, evt);
          } else if (data[0] === "EOSE" && data[1] === subId) {
            ws.send(JSON.stringify(["CLOSE", subId]));
            ws.close();
          }
        } catch (e) { /* ignore parse errors */ }
      };

      ws.onerror = () => {
        closedRelays++;
        checkDone();
      };

      ws.onclose = () => {
        closedRelays++;
        checkDone();
      };
    });

    function checkDone() {
      if (closedRelays >= RELAYS.length && !resolved) {
        clearTimeout(timeout);
        resolved = true;
        resolve({ events: [...events.values()], connected: connectedCount });
      }
    }
  });
}

function fetchKind30078Events(limit = 200) {
  return new Promise((resolve) => {
    const events = new Map();
    let resolved = false;
    let closedRelays = 0;
    let connectedCount = 0;

    const timeout = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        resolve([...events.values()]);
      }
    }, FETCH_TIMEOUT_MS);

    RELAYS.forEach((relayUrl) => {
      let ws;
      try {
        ws = new WebSocket(relayUrl);
      } catch (e) {
        closedRelays++;
        checkDone();
        return;
      }

      const subId = "prta-k30078-" + Math.random().toString(36).slice(2, 8);

      ws.onopen = () => {
        connectedCount++;
        ws.send(JSON.stringify([
          "REQ", subId,
          {
            kinds: [30078],
            limit,
            since: Math.floor(Date.now() / 1000) - 86400 * FETCH_SINCE_DAYS,
          },
        ]));
      };

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          if (data[0] === "EVENT" && data[1] === subId && data[2]) {
            const evt = data[2];
            events.set(evt.id, evt);
          } else if (data[0] === "EOSE" && data[1] === subId) {
            ws.send(JSON.stringify(["CLOSE", subId]));
            ws.close();
          }
        } catch (e) { /* ignore */ }
      };

      ws.onerror = () => {
        closedRelays++;
        checkDone();
      };

      ws.onclose = () => {
        closedRelays++;
        checkDone();
      };
    });

    function checkDone() {
      if (closedRelays >= RELAYS.length && !resolved) {
        clearTimeout(timeout);
        resolved = true;
        resolve([...events.values()]);
      }
    }
  });
}

// ===========================================================================
// Real-time WebSocket subscription for live DVM events
// ===========================================================================

function subscribeToRealtimeUpdates() {
  liveSockets = [];
  liveConnectedCount = 0;

  RELAYS.forEach((relayUrl) => {
    let ws;
    try {
      ws = new WebSocket(relayUrl);
    } catch (e) {
      return;
    }

    ws.onopen = () => {
      liveSockets.push(ws);
      liveConnectedCount++;
      updateLiveIndicator();
      ws.send(JSON.stringify(["REQ", "prta-live", {
        kinds: [5900, 6900, 7000, 30078],
        since: Math.floor(Date.now() / 1000),
      }]));
    };

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data[0] === "EVENT" && data[1] === "prta-live" && data[2]) {
          handleLiveEvent(data[2]);
        }
      } catch (e) { /* ignore parse errors */ }
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onclose = () => {
      const idx = liveSockets.indexOf(ws);
      if (idx >= 0) liveSockets.splice(idx, 1);
      liveConnectedCount = Math.max(0, liveConnectedCount - 1);
      updateLiveIndicator();
    };
  });
}

function handleLiveEvent(event) {
  if (!event || !event.kind) return;

  if (event.kind === 7000) {
    const tags = event.tags || [];
    const status = getTag(tags, "status") || "processing";
    const requestId = getTag(tags, "e");
    if (!requestId) return;

    const run = allRuns.find(
      (r) => r.runId === requestId || r.eventId === requestId
    );
    if (run && run.feedbackStatus !== status) {
      run.feedbackStatus = status;
      scheduleRenderRunsList();
    }
    return;
  }

  if (event.kind === 6900) {
    const run = parseRunFromKind6900(event, new Map());
    if (!run) return;
    if (allRuns.find((r) => r.runId === run.runId && r.source === "dvm")) return;

    allRuns.unshift(run);
    displayIdCache.clear();
    saveCachedRuns(allRuns);
    populateRunnerFilter();
    scheduleRenderRunsList();
    return;
  }

  if (event.kind === 30078) {
    const run = parseRunFromKind30078(event, new Map());
    if (!run) return;
    const existing = allRuns.find((r) => r.runId === run.runId && r.source === "k30078");
    if (existing) {
      if (run.timestamp > existing.timestamp) {
        Object.assign(existing, run);
      }
      return;
    }

    allRuns.unshift(run);
    displayIdCache.clear();
    saveCachedRuns(allRuns);
    populateRunnerFilter();
    scheduleRenderRunsList();
    return;
  }
}

function updateLiveIndicator() {
  const el = document.getElementById("live-indicator");
  if (!el) return;
  if (liveConnectedCount > 0) {
    el.classList.add("live");
    el.classList.remove("idle");
    el.title = "Live — " + liveConnectedCount + "/" + RELAYS.length + " relays connected";
  } else {
    el.classList.remove("live");
    el.classList.add("idle");
    el.title = "Connecting\u2026";
  }
}

// ===========================================================================
// Parsing helpers
// ===========================================================================

function getTag(tags, name) {
  const t = (tags || []).find((tg) => tg[0] === name);
  return t ? t[1] : null;
}

function getAllTags(tags, name) {
  return (tags || []).filter((tg) => tg[0] === name);
}

function getTagNum(tags, name) {
  const v = getTag(tags, name);
  if (v == null || v === "") return null;
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? null : n;
}

const PROJECT_TAG_MAP = {
  "tollgate": "tollgate",
  "boltcard": "boltcard",
  "fips": "fips",
  "fips-test": "fips",
  "fips-interop": "fips",
  "fips-rekey": "fips",
  "fips-throughput": "fips",
  "fips-dashboard": "fips",
  "fips-ble": "ble",
  "ble-experiment": "ble",
  "microfips": "microfips",
  "conwrt": "conwrt",
  "conwrt-sqm": "conwrt",
  "conwrt-bufferbloat": "conwrt",
  "silent-energy": "silent-energy",
  "fips-benchmark": "fips-benchmark",
};

function determineProjectTag(tTags) {
  for (const t of tTags) {
    if (PROJECT_TAG_MAP[t]) return PROJECT_TAG_MAP[t];
  }
  return null;
}

function getRunProject(run) {
  if (run.projectTag) return run.projectTag;
  if (run.source === "dvm") return "tollgate";
  return "unknown";
}

const PROJECT_LABELS = {
  tollgate: "Tollgate",
  boltcard: "Boltcard",
  fips: "FIPS",
  ble: "BLE",
  microfips: "Microfips",
  conwrt: "conwrt",
  "silent-energy": "Silent Energy",
  unknown: "Other",
};

const PROJECT_COLORS = {
  tollgate: "var(--accent)",
  boltcard: "var(--purple)",
  fips: "var(--blue)",
  ble: "var(--green)",
  microfips: "var(--yellow)",
  conwrt: "var(--cyan, #22d3ee)",
  "silent-energy": "var(--orange, #f59e0b)",
  unknown: "var(--text-dim)",
};

function guessMimeFromPath(path) {
  if (!path) return "application/octet-stream";
  const ext = path.split(".").pop()?.toLowerCase();
  const map = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
    gif: "image/gif", svg: "image/svg+xml", webp: "image/webp",
    html: "text/html", htm: "text/html",
    json: "application/json", xml: "application/xml",
    txt: "text/plain", log: "text/plain", csv: "text/csv",
    mp4: "video/mp4", webm: "video/webm",
  };
  return map[ext] || "application/octet-stream";
}

// Build a URL -> metadata map from kind 1063 NIP-94 events.
function buildFileMeta(events) {
  const meta = new Map();
  for (const evt of events) {
    if (evt.kind !== 1063) continue;
    const tags = evt.tags || [];
    const url = getTag(tags, "url");
    if (!url) continue;
    meta.set(url, {
      url,
      sha256: getTag(tags, "x"),
      mime: getTag(tags, "m") || "application/octet-stream",
      filename: getTag(tags, "filename"),
      size: getTagNum(tags, "size"),
      eventId: evt.id,
    });
  }
  return meta;
}

// ===========================================================================
// NIP-19 npub encoding (bech32) for runner pubkey display
// ===========================================================================

const BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

function bech32Polymod(values) {
  const gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  let chk = 1;
  for (const v of values) {
    const b = chk >> 25;
    chk = (chk & 0x1ffffff) << 5 ^ v;
    for (let i = 0; i < 5; i++) {
      if (((b >> i) & 1) === 1) chk ^= gen[i];
    }
  }
  return chk;
}

function bech32HrpExpand(hrp) {
  return [...hrp].map((c) => c.charCodeAt(0) >> 5)
    .concat([...hrp].map((c) => c.charCodeAt(0) & 31));
}

function bech32CreateChecksum(hrp, data) {
  const values = bech32HrpExpand(hrp).concat(data, [0, 0, 0, 0, 0, 0]);
  const mod = bech32Polymod(values) ^ 1;
  const ret = [];
  for (let i = 0; i < 6; i++) {
    ret.push((mod >> (5 * (5 - i))) & 31);
  }
  return ret;
}

function convertBits(data, fromBits, toBits, pad) {
  let acc = 0, bits = 0;
  const ret = [];
  const maxv = (1 << toBits) - 1;
  for (const v of data) {
    if (v < 0 || (v >> fromBits) !== 0) return null;
    acc = (acc << fromBits) | v;
    bits += fromBits;
    while (bits >= toBits) {
      bits -= toBits;
      ret.push((acc >> bits) & maxv);
    }
  }
  if (pad) {
    if (bits > 0) ret.push((acc << (toBits - bits)) & maxv);
  } else if (bits >= fromBits || ((acc << (toBits - bits)) & maxv)) {
    return null;
  }
  return ret;
}

function hexToBytes(hex) {
  const bytes = [];
  for (let i = 0; i < hex.length; i += 2) {
    bytes.push(parseInt(hex.substr(i, 2), 16));
  }
  return bytes;
}

function hexToNpub(hex) {
  if (!hex || hex.length !== 64) return "";
  const hrp = "npub";
  const data = convertBits(hexToBytes(hex), 8, 5, true);
  if (!data) return "";
  const checksum = bech32CreateChecksum(hrp, data);
  const combined = data.concat(checksum);
  return hrp + "1" + combined.map((v) => BECH32_CHARSET[v]).join("");
}

function shortNpub(hex) {
  const npub = hexToNpub(hex);
  if (!npub) return "";
  return npub.slice(0, 10) + "\u2026";
}

// ===========================================================================
// NIP-90 DVM event parsing (kind 5900/6900/7000)
// ===========================================================================

// Parse kind 6900 (DVM job result) -> run object.
// The event carries test results as a DVM job completion. Content may be
// JSON with pass/fail counts and file URLs. Tags carry param values and
// file references. The linked 5900 request event provides the run_id via
// its d-tag.
function parseRunFromKind6900(event, fileMeta) {
  const tags = event.tags || [];

  let payload = null;
  try {
    payload = JSON.parse(event.content || "{}");
  } catch (e) { /* non-JSON content */ }

  const contentFiles = (payload && Array.isArray(payload.files)) ? payload.files : [];
  const passed = payload && payload.passed != null ? payload.passed : getTagNum(tags, "passed");
  const failed = payload && payload.failed != null ? payload.failed : getTagNum(tags, "failed");
  const skipped = payload && payload.skipped != null ? payload.skipped : getTagNum(tags, "skipped");
  const total = payload && payload.total != null
    ? payload.total
    : (passed != null || failed != null || skipped != null
      ? (passed || 0) + (failed || 0) + (skipped || 0)
      : null);

  const branch = (payload && payload.branch)
    || getParam(tags, "branch")
    || getTag(tags, "branch");
  const backend = (payload && payload.backend)
    || getParam(tags, "backend")
    || getTag(tags, "backend");
  const router = (payload && payload.router)
    || getParam(tags, "router")
    || getTag(tags, "router");
  const pr = (payload && payload.pr) || getParam(tags, "pr") || getTag(tags, "pr");

  let files = contentFiles;
  if (files.length === 0) {
    files = getAllTags(tags, "file").map((t) => ({ url: t[1] || "" }));
  }

  files = files.map((f) => {
    if (typeof f === "string") f = { url: f };
    const fm = fileMeta.get(f.url) || {};
    return {
      path: f.path || fm.filename || "",
      url: f.url,
      sha256: f.sha256 || fm.sha256 || "",
      mime: f.mime || fm.mime || "application/octet-stream",
      size: f.size != null ? f.size : (fm.size != null ? fm.size : null),
      redacted: !!f.redacted,
    };
  });

  const screenshots = files.filter((f) => (f.mime || "").startsWith("image/"));
  const nonScreenshotFiles = files.filter(
    (f) => !(f.mime || "").startsWith("image/")
  );

  let runId = (payload && payload.run_id) || getTag(tags, "d") || event.id;

  let status = "success";
  if (failed != null && failed > 0) status = "error";
  else if (passed != null && passed === 0 && total != null && total > 0) status = "error";

  return {
    id: event.id,
    eventId: event.id,
    runId,
    timestamp: event.created_at,
    status,
    passed,
    failed,
    skipped,
    total,
    branch: branch || null,
    pr: pr || null,
    repo: (payload && payload.repo) || null,
    commit: (payload && payload.commit) || getTag(tags, "commit") || null,
    requestEventId: getTag(tags, "e") || null,
    router: router || null,
    backend: backend || null,
    clientType: (payload && payload.client_type) || getTag(tags, "client_type") || null,
    viewport: (payload && payload.viewport) || getTag(tags, "viewport") || null,
    blossomServer: payload ? payload.blossom_server : null,
    scanSummary: (payload && payload.scan_summary) ? payload.scan_summary : {},
    files: nonScreenshotFiles,
    screenshots,
    content: event.content || "",
    rawEvent: event,
    source: "dvm",
    runnerNpub: event.pubkey,
    feedbackStatus: null,
  };
}

function getParam(tags, name) {
  const t = (tags || []).find((tg) => tg[0] === "param" && tg[1] === name);
  return t ? t[2] : null;
}

function parseRunFromKind30078(event, fileMeta) {
  const tags = event.tags || [];
  const dTag = getTag(tags, "d");
  const tTags = getAllTags(tags, "t").map((t) => t[1]);
  const fileUrls = getAllTags(tags, "file").map((t) => t[1]);

  let payload = null;
  try {
    payload = JSON.parse(event.content || "{}");
  } catch (e) { /* non-JSON content */ }

  const projectTag = determineProjectTag(tTags);

  let files = [];
  if (payload && Array.isArray(payload.files)) {
    files = payload.files.map((f) => {
      if (typeof f === "string") f = { url: f };
      const fm = fileMeta.get(f.url) || {};
      return {
        path: f.path || fm.filename || "",
        url: f.url,
        sha256: f.sha256 || fm.sha256 || "",
        mime: f.mime || fm.mime || guessMimeFromPath(f.path || f.url),
        size: f.size != null ? f.size : (fm.size != null ? fm.size : null),
        redacted: !!f.redacted,
      };
    });
  }
  for (const url of fileUrls) {
    if (!files.find((f) => f.url === url)) {
      const fm = fileMeta.get(url) || {};
      files.push({
        path: fm.filename || url.slice(-40),
        url,
        sha256: fm.sha256 || "",
        mime: fm.mime || guessMimeFromPath(url),
        size: fm.size != null ? fm.size : null,
        redacted: false,
      });
    }
  }

  const screenshots = files.filter((f) => (f.mime || "").startsWith("image/"));
  const nonScreenshotFiles = files.filter(
    (f) => !(f.mime || "").startsWith("image/")
  );

  const runId = dTag || (payload && payload.run_id) || event.id;

  const passed = payload ? (payload.passed ?? payload.counts?.passed ?? payload.metadata?.passed ?? null) : null;
  const failed = payload ? (payload.failed ?? payload.counts?.failed ?? payload.metadata?.failed ?? null) : null;
  const skipped = payload ? (payload.skipped ?? payload.counts?.skipped ?? payload.metadata?.skipped ?? null) : null;
  const total = payload && payload.total != null
    ? payload.total
    : payload && payload.counts?.total != null
      ? payload.counts.total
      : ((passed ?? 0) + (failed ?? 0) + (skipped ?? 0)) || null;

  let status = "success";
  if (failed != null && failed > 0) status = "error";
  else if (passed != null && passed === 0 && total != null && total > 0) status = "error";

  return {
    id: event.id,
    eventId: event.id,
    runId,
    projectTag,
    subTags: tTags,
    timestamp: event.created_at,
    status,
    passed,
    failed,
    skipped,
    total,
    scenario: payload?.scenario || payload?.mode || null,
    fipsRef: payload?.fips_ref || payload?.["fips-ref"] || null,
    nodes: payload?.nodes ?? null,
    phase: payload?.phase || null,
    noiseMode: payload?.noise_mode || null,
    branch: payload?.branch || null,
    pr: payload?.pr || null,
    repo: payload?.repo || null,
    commit: payload?.commit || null,
    requestEventId: null,
    router: payload?.router || null,
    backend: payload?.backend || null,
    clientType: payload?.client_type || null,
    viewport: payload?.viewport || null,
    blossomServer: payload?.blossom_server || null,
    scanSummary: payload?.scan_summary || {},
    files: nonScreenshotFiles,
    screenshots,
    content: event.content || "",
    rawEvent: event,
    source: "k30078",
    summary: payload,
    runnerNpub: event.pubkey,
    feedbackStatus: null,
  };
}

// Parse kind 7000 (DVM job feedback) -> status object.
function parseFeedbackFromKind7000(event) {
  const tags = event.tags || [];
  const status = getTag(tags, "status") || "processing";
  const requestId = getTag(tags, "e");
  return {
    status,
    requestId,
    runnerNpub: event.pubkey,
    timestamp: event.created_at,
    eventId: event.id,
  };
}

function dedupeDvmRuns(events, fileMeta, feedback) {
  const k6900 = events.filter((e) => e.kind === 6900);

  const byRunId = new Map();

  for (const evt of k6900) {
    try {
      const run = parseRunFromKind6900(evt, fileMeta);
      const existing = byRunId.get(run.runId);
      if (!existing || run.timestamp > existing.timestamp) {
        byRunId.set(run.runId, run);
      }
    } catch (e) {
      console.warn("[PRTA] Failed to parse 6900", evt.id, e);
    }
  }

  if (feedback && feedback.length > 0) {
    const fbByRun = new Map();
    for (const fb of feedback) {
      const runId = fb.requestId || fb.eventId;
      const prev = fbByRun.get(runId);
      if (!prev || fb.timestamp > prev.timestamp) {
        fbByRun.set(runId, fb);
      }
    }
    for (const run of byRunId.values()) {
      const fb = fbByRun.get(run.requestEventId) || fbByRun.get(run.runId) || fbByRun.get(run.eventId);
      if (fb) run.feedbackStatus = fb.status;
    }
  }

  return [...byRunId.values()].sort((a, b) => b.timestamp - a.timestamp);
}

function dedupeKind30078Runs(events, fileMeta) {
  const byDtag = new Map();

  for (const evt of events) {
    if (evt.kind !== 30078) continue;
    try {
      const run = parseRunFromKind30078(evt, fileMeta);
      const existing = byDtag.get(run.runId);
      if (!existing || run.timestamp > existing.timestamp) {
        byDtag.set(run.runId, run);
      }
    } catch (e) {
      console.warn("[PRTA] Failed to parse 30078", evt.id, e);
    }
  }

  return [...byDtag.values()].sort((a, b) => b.timestamp - a.timestamp);
}

// ===========================================================================
// Formatting helpers
// ===========================================================================

function formatTimestamp(unixSeconds) {
  if (!unixSeconds) return "Unknown";
  const d = new Date(unixSeconds * 1000);
  return d.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }) + " UTC";
}

function formatDateShort(unixSeconds) {
  if (!unixSeconds) return "?";
  const d = new Date(unixSeconds * 1000);
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });
}

function formatRelative(unixSeconds) {
  if (!unixSeconds) return "";
  const diff = Date.now() / 1000 - unixSeconds;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function shortRunId(runId) {
  if (!runId) return "?";
  if (runId.length <= 20) return runId;
  return runId.slice(0, 16) + "\u2026";
}

function shortCommit(commit) {
  if (!commit) return null;
  const c = commit.replace(/^g/, "");
  return c.slice(0, 7);
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatBytes(bytes) {
  if (bytes == null) return "?";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  return (ms / 1000).toFixed(1) + "s";
}

function statusIcon(status) {
  if (status === "success") return `<span class="status-dot status-success" title="All tests passed"></span>`;
  if (status === "error") return `<span class="status-dot status-error" title="Failures"></span>`;
  if (status === "partial") return `<span class="status-dot status-partial" title="Partial"></span>`;
  return "";
}

function statusBadge(run) {
  if (run.passed == null && run.failed == null) {
    return `<span class="status-badge unknown">NO DATA</span>`;
  }
  if (run.status === "error") {
    return `<span class="status-badge failed">FAILED</span>`;
  }
  if (run.status === "success" && run.passed != null && run.passed > 0) {
    return `<span class="status-badge passed">PASSED</span>`;
  }
  return `<span class="status-badge unknown">NO DATA</span>`;
}

function passFailBar(run) {
  const passed = run.passed || 0;
  const failed = run.failed || 0;
  const skipped = run.skipped || 0;
  const total = passed + failed + skipped;
  if (total === 0) return "";
  const pct = (val) => (val / total) * 100;
  return `<div class="pf-bar">
    ${passed > 0 ? `<div class="pf-seg pf-pass" style="width:${pct(passed)}%" title="${passed} passed"></div>` : ""}
    ${failed > 0 ? `<div class="pf-seg pf-fail" style="width:${pct(failed)}%" title="${failed} failed"></div>` : ""}
    ${skipped > 0 ? `<div class="pf-seg pf-skip" style="width:${pct(skipped)}%" title="${skipped} skipped"></div>` : ""}
  </div>`;
}

function loadCachedRuns() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const runs = JSON.parse(raw);
    return Array.isArray(runs) ? runs : null;
  } catch (e) {
    return null;
  }
}

function saveCachedRuns(runs) {
  try {
    const stripped = runs.map(({ rawEvent, content, ...rest }) => rest);
    localStorage.setItem(CACHE_KEY, JSON.stringify(stripped));
  } catch (e) {
    console.warn("[PRTA] Cache save failed:", e);
  }
}

// ===========================================================================
// Rendering: sidebar run list
// ===========================================================================

function updateConnectionStatus(connected, total) {
  const el = document.getElementById("conn-status");
  if (!el) return;
  if (connected === 0) {
    el.textContent = "Offline";
    el.className = "conn-badge offline";
  } else if (connected < total) {
    el.textContent = connected + "/" + total + " relays";
    el.className = "conn-badge partial";
  } else {
    el.textContent = connected + "/" + total + " relays";
    el.className = "conn-badge online";
  }
}

function getFilteredRuns() {
  let runs = allRuns.slice();

  if (filterState.maxAge && filterState.maxAge > 0) {
    const cutoff = Math.floor(Date.now() / 1000) - filterState.maxAge;
    runs = runs.filter((r) => (r.timestamp || 0) >= cutoff);
  }

  if (filterState.project && filterState.project !== "all") {
    if (filterState.project === "ours") {
      runs = runs.filter((r) => {
        const p = getRunProject(r);
        return p === "tollgate" || p === "boltcard" || p === "fips" || p === "ble" || p === "microfips" || p === "conwrt" || p === "silent-energy";
      });
    } else {
      runs = runs.filter((r) => getRunProject(r) === filterState.project);
    }
  }

  if (filterState.runner) {
    runs = runs.filter((r) => r.runnerNpub === filterState.runner);
  }

  if (filterState.search) {
    const q = filterState.search;
    runs = runs.filter((r) => {
      const hay = [r.runId, r.branch, r.router, r.backend, r.pr,
        r.runnerNpub ? hexToNpub(r.runnerNpub) : null,
      ].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
  }

  if (filterState.status === "passed") {
    runs = runs.filter((r) => r.status === "success" && r.passed != null && r.passed > 0);
  } else if (filterState.status === "failed") {
    runs = runs.filter((r) => r.status === "error");
  }

  if (filterState.sort === "oldest") {
    runs.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  } else if (filterState.sort === "most") {
    runs.sort((a, b) => {
      const ta = a.total != null ? a.total : (a.passed || 0) + (a.failed || 0) + (a.skipped || 0);
      const tb = b.total != null ? b.total : (b.passed || 0) + (b.failed || 0) + (b.skipped || 0);
      return tb - ta;
    });
  } else {
    runs.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  }

  return runs;
}

function computeDisplayId(run) {
  if (!run.runnerNpub) return shortRunId(run.runId);
  const cacheKey = run.runId + ":" + run.runnerNpub;
  if (displayIdCache.has(cacheKey)) return displayIdCache.get(cacheKey);

  const runnerRuns = allRuns
    .filter((r) => r.runnerNpub === run.runnerNpub)
    .sort((a, b) => b.timestamp - a.timestamp);
  const idx = runnerRuns.findIndex((r) => r.runId === run.runId);
  const displayId = `${shortNpub(run.runnerNpub)} #${runnerRuns.length - idx}`;
  displayIdCache.set(cacheKey, displayId);
  return displayId;
}

function populateRunnerFilter() {
  const select = document.getElementById("runner-filter");
  if (!select) return;

  const runners = [...new Set(
    allRuns.map((r) => r.runnerNpub).filter(Boolean)
  )].sort((a, b) => {
    const aCount = allRuns.filter((r) => r.runnerNpub === a).length;
    const bCount = allRuns.filter((r) => r.runnerNpub === b).length;
    if (bCount !== aCount) return bCount - aCount;
    const aNewest = allRuns.filter((r) => r.runnerNpub === a).reduce((mx, r) => Math.max(mx, r.timestamp || 0), 0);
    const bNewest = allRuns.filter((r) => r.runnerNpub === b).reduce((mx, r) => Math.max(mx, r.timestamp || 0), 0);
    return bNewest - aNewest;
  }).slice(0, 20);

  const currentValue = filterState.runner;
  select.innerHTML = `<option value="">All runners</option>` +
    runners.map((npub) => {
      const count = allRuns.filter((r) => r.runnerNpub === npub).length;
      const label = shortNpub(npub);
      return `<option value="${escapeHtml(npub)}">${escapeHtml(label)} (${count})</option>`;
    }).join("");

  if (runners.includes(currentValue)) {
    select.value = currentValue;
  } else {
    filterState.runner = "";
    select.value = "";
  }
}

function buildSidebar() {
  const aside = document.getElementById("runs-list");
  aside.innerHTML = `
    <div class="sidebar-controls">
      <div class="project-tabs" id="project-tabs">
        <button class="project-tab active" data-project="ours" type="button">Our Projects</button>
        <button class="project-tab" data-project="tollgate" type="button">Tollgate</button>
        <button class="project-tab" data-project="fips" type="button">FIPS</button>
        <button class="project-tab" data-project="ble" type="button">BLE</button>
        <button class="project-tab" data-project="microfips" type="button">Microfips</button>
        <button class="project-tab" data-project="conwrt" type="button">conwrt</button>
        <button class="project-tab" data-project="silent-energy" type="button">Silent Energy</button>
        <button class="project-tab" data-project="boltcard" type="button">Boltcard</button>
        <button class="project-tab project-tab-secondary" data-project="all" type="button">All Nostr</button>
      </div>
      <input type="text" id="search-input" class="search-input" placeholder="Search runs\u2026" autocomplete="off" />
      <div class="filter-toggles">
        <button class="filter-btn active" data-filter="all" type="button">All</button>
        <button class="filter-btn" data-filter="passed" type="button">Passed</button>
        <button class="filter-btn" data-filter="failed" type="button">Failed</button>
      </div>
      <select id="runner-filter" class="runner-filter">
        <option value="">All runners</option>
      </select>
      <select id="sort-select" class="sort-select">
        <option value="newest">Newest</option>
        <option value="oldest">Oldest</option>
        <option value="most">Most tests</option>
      </select>
      <select id="age-filter" class="age-filter">
        <option value="86400">Last 24h</option>
        <option value="604800" selected>Last 7 days</option>
        <option value="2592000">Last 30 days</option>
        <option value="0">All time</option>
      </select>
    </div>
    <div class="runs-scroll" id="runs-scroll"></div>
  `;
  wireSidebarControls();
}

function wireSidebarControls() {
  document.querySelectorAll(".project-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".project-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      filterState.project = btn.dataset.project;
      scheduleRenderRunsList();
    });
  });

  const searchInput = document.getElementById("search-input");
  if (searchInput) {
    let timer = null;
    searchInput.addEventListener("input", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        filterState.search = e.target.value.toLowerCase().trim();
        scheduleRenderRunsList();
      }, 200);
    });
  }

  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      filterState.status = btn.dataset.filter;
      scheduleRenderRunsList();
    });
  });

  const sortSelect = document.getElementById("sort-select");
  if (sortSelect) {
    sortSelect.addEventListener("change", (e) => {
      filterState.sort = e.target.value;
      scheduleRenderRunsList();
    });
  }

  const ageFilter = document.getElementById("age-filter");
  if (ageFilter) {
    ageFilter.addEventListener("change", (e) => {
      filterState.maxAge = parseInt(e.target.value, 10);
      scheduleRenderRunsList();
    });
  }

  const runnerFilter = document.getElementById("runner-filter");
  if (runnerFilter) {
    runnerFilter.addEventListener("change", (e) => {
      filterState.runner = e.target.value;
      scheduleRenderRunsList();
    });
  }
}

function scheduleRenderRunsList() {
  if (renderRunsListTimer) return;
  renderRunsListTimer = setTimeout(() => {
    renderRunsListTimer = null;
    renderRunsList();
  }, 500);
}

function renderRunsList() {
  const container = document.getElementById("runs-scroll");
  if (!container) return;
  container.innerHTML = "";

  if (allRuns.length === 0) {
    container.innerHTML = `
      <div class="runs-empty">
        <div class="runs-empty-icon">\u26A0</div>
        <p>No test runs found.</p>
        <p class="hint">Make sure the publisher has emitted<br>kind 6900 DVM result events.</p>
      </div>`;
    return;
  }

  const runs = getFilteredRuns();

  if (runs.length === 0) {
    container.innerHTML = `<div class="no-match">No runs match your filters.</div>`;
    return;
  }

  const countEl = document.createElement("div");
  countEl.className = "runs-count";
  countEl.textContent = runs.length + " run" + (runs.length !== 1 ? "s" : "");
  container.appendChild(countEl);

  runs.forEach((run, index) => {
    const card = document.createElement("div");
    card.className = "run-card";
    card.dataset.runId = run.runId;
    if (run.runId === selectedRunId) card.classList.add("active");
    if (index < 10) {
      card.style.animationDelay = (index * 30) + "ms";
    } else {
      card.style.animationDelay = "0ms";
      card.style.animationDuration = "0s";
    }

    const noData = run.passed == null && run.failed == null;

    const feedbackBadge = run.feedbackStatus
      ? `<span class="dvm-status-badge dvm-status-${escapeHtml(run.feedbackStatus)}">${escapeHtml(run.feedbackStatus)}</span>`
      : "";

    const npubLabel = run.runnerNpub && run.source === "dvm"
      ? `<span class="runner-npub" title="${escapeHtml(hexToNpub(run.runnerNpub))}">${escapeHtml(shortNpub(run.runnerNpub))}</span>`
      : "";

    const displayId = computeDisplayId(run);

    card.innerHTML = `
      <div class="run-card-header">
        <span class="run-id" title="${escapeHtml(run.runId)}">${escapeHtml(displayId)}</span>
        <div class="run-card-pf">
          ${feedbackBadge}
          ${statusIcon(run.status)}
          ${noData
            ? `<span class="pf-text pf-no-data">No data</span>`
            : `<span class="pf-text"><span class="pf-pass-num">${run.passed != null ? run.passed : "?"}</span>pass <span class="pf-fail-num">${run.failed != null ? run.failed : "?"}</span>fail</span>`
          }
        </div>
      </div>
      ${passFailBar(run)}
      <div class="run-card-meta">
        <span class="meta-chip meta-chip-project" style="--project-color: ${PROJECT_COLORS[getRunProject(run)] || "var(--text-dim)"}">${escapeHtml(PROJECT_LABELS[getRunProject(run)] || "?")}</span>
        ${run.source === "dvm" ? `<span class="meta-chip meta-chip-legacy" title="Legacy NIP-90 DVM event">DVM</span>` : ""}
        ${run.router ? `<span class="meta-chip">${escapeHtml(run.router)}</span>` : ""}
        ${run.scenario ? `<span class="meta-chip">${escapeHtml(run.scenario)}</span>` : ""}
        ${run.branch ? `<span class="meta-chip meta-chip-branch">${escapeHtml(run.branch)}</span>` : ""}
        ${run.pr ? `<span class="meta-chip meta-chip-pr">#${escapeHtml(run.pr)}</span>` : ""}
      </div>
      <div class="run-card-footer">
        <span class="timestamp">${escapeHtml(formatDateShort(run.timestamp))}</span>
        <div class="run-card-footer-right">
          ${npubLabel}
          <span class="relative">${escapeHtml(formatRelative(run.timestamp))}</span>
        </div>
      </div>
    `;

    card.addEventListener("click", () => {
      selectRun(run);
    });

    container.appendChild(card);
  });
}

// ===========================================================================
// Lazy image loading (Intersection Observer + request throttle)
// ===========================================================================

function lazyLoadScreenshots(container) {
  if (imgObserver) imgObserver.disconnect();
  imgLoadQueue.length = 0;
  activeImgLoads = 0;

  imgObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        const img = entry.target;
        imgObserver.unobserve(img);
        enqueueImageLoad(img);
      }
    });
  }, { rootMargin: "300px" });

  container.querySelectorAll(".shot-thumb[data-src]").forEach((img) => {
    imgObserver.observe(img);
  });
}

function enqueueImageLoad(img) {
  imgLoadQueue.push(img);
  processImageQueue();
}

function processImageQueue() {
  while (activeImgLoads < MAX_CONCURRENT_IMG_LOADS && imgLoadQueue.length > 0) {
    const img = imgLoadQueue.shift();
    activeImgLoads++;
    const url = img.dataset.src;

    fetch(url)
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.blob(); })
      .then((blob) => {
        const objUrl = URL.createObjectURL(blob);
        img.addEventListener("load", () => {
          img.classList.add("loaded");
          activeImgLoads--;
          processImageQueue();
        }, { once: true });
        img.addEventListener("error", () => {
          showExpiredPlaceholder(img);
          activeImgLoads--;
          processImageQueue();
        }, { once: true });
        img.src = objUrl;
      })
      .catch(() => {
        showExpiredPlaceholder(img);
        activeImgLoads--;
        processImageQueue();
      });

    img.removeAttribute("data-src");
  }
}

function showExpiredPlaceholder(img) {
  const card = img.parentElement;
  card.classList.add("shot-expired");
  card.innerHTML = '<div class="shot-expired-icon"> expired</div><div class="shot-name" title="' + (img.dataset.filename || '') + '">' + (img.dataset.filename || 'screenshot') + '</div>';
}

function observeNewThumbnails(container) {
  if (!imgObserver) return;
  container.querySelectorAll(".shot-thumb[data-src]").forEach((img) => {
    imgObserver.observe(img);
  });
}

// ===========================================================================
// Rendering: detail view
// ===========================================================================

let currentRun = null;

// ===========================================================================
// Test hierarchy: fetch summary.json, group artifacts by test
// ===========================================================================

async function fetchTestSummary(run) {
  const summaryFile = [...(run.files || []), ...(run.screenshots || [])]
    .find((f) => f.path === "summary.json");
  if (!summaryFile) return null;
  try {
    const resp = await fetch(summaryFile.url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

function buildTestHierarchy(run, summary) {
  if (!summary || !Array.isArray(summary.tests)) return null;

  const allFiles = [...(run.screenshots || []), ...(run.files || [])];
  const matchedUrls = new Set();
  let debugLogsUrl = null;

  const testsByName = new Map();
  for (const t of summary.tests) {
    testsByName.set(t.name, t);
  }

  const testArtifacts = new Map();
  const suiteFiles = {};
  for (const file of allFiles) {
    const path = file.path || "";
    if (path === "summary.json") continue;
    let matched = false;

    const m = path.match(/^(?:.*\/)?(.+)-(passed|failed|skipped|error)\.[^.]+$/);
    if (m && testsByName.has(m[1])) {
      const name = m[1];
      if (!testArtifacts.has(name)) {
        testArtifacts.set(name, { screenshots: [], videos: [], html: [] });
      }
      const mime = file.mime || "";
      if (mime.startsWith("image/")) {
        testArtifacts.get(name).screenshots.push(file);
      } else if (mime.startsWith("video/")) {
        testArtifacts.get(name).videos.push(file);
      } else if (mime.includes("html")) {
        testArtifacts.get(name).html.push(file);
      }
      matchedUrls.add(file.url);
      matched = true;
    }

    if (!matched && m) {
      const mime = file.mime || "";
      if (mime.startsWith("video/")) {
        const name = m[1];
        if (!testArtifacts.has(name)) {
          testArtifacts.set(name, { screenshots: [], videos: [], html: [] });
        }
        testArtifacts.get(name).videos.push(file);
        testArtifacts.get(name)._standalone = true;
        matchedUrls.add(file.url);
        matched = true;
      }
    }

    if (!matched) {
      const vm = path.match(/^raw\/([^/]+)\//);
      if (vm) {
        const suiteName = vm[1];
        if (!suiteFiles[suiteName]) suiteFiles[suiteName] = [];
        suiteFiles[suiteName].push(file);
      }
    }
    if (path === "debug-logs.json") {
      debugLogsUrl = file.url;
      matchedUrls.add(file.url);
    }
  }

  const suiteMap = new Map();
  for (const test of summary.tests) {
    const runner = test.runner || "ungrouped";
    if (!suiteMap.has(runner)) {
      const runnerInfo = (summary.runners || []).find((r) => r.name === runner);
      suiteMap.set(runner, {
        name: runner,
        status: runnerInfo ? runnerInfo.status : null,
        counts: runnerInfo ? runnerInfo.counts : null,
        tests: [],
      });
    }
    const artifacts = testArtifacts.get(test.name) || { screenshots: [], videos: [], html: [] };
    suiteMap.get(runner).tests.push({ ...test, artifacts });
  }

  const standaloneArtifacts = [];
  for (const [name, arts] of testArtifacts) {
    if (arts._standalone && (arts.videos.length > 0 || arts.screenshots.length > 0)) {
      standaloneArtifacts.push({
        name,
        outcome: "unknown",
        runner: "artifacts",
        artifacts: arts,
      });
    }
  }
  if (standaloneArtifacts.length > 0) {
    suiteMap.set("artifacts", {
      name: "Artifacts",
      status: null,
      counts: null,
      tests: standaloneArtifacts,
    });
  }

  for (const [suiteName, files] of Object.entries(suiteFiles)) {
    const suite = suiteMap.get(suiteName);
    if (!suite) continue;
    const passedTest = suite.tests.find(t => t.outcome === "passed") || suite.tests[0];
    if (!passedTest) continue;
    for (const file of files) {
      const mime = file.mime || "";
      if (mime.startsWith("image/")) {
        passedTest.artifacts.screenshots.push(file);
      } else if (mime.startsWith("video/")) {
        passedTest.artifacts.videos.push(file);
      } else if (mime.includes("html")) {
        passedTest.artifacts.html.push(file);
      }
      matchedUrls.add(file.url);
    }
  }

  const featuredVideos = [];
  for (const suite of suiteMap.values()) {
    for (const test of suite.tests) {
      if (test.artifacts.videos && test.artifacts.videos.length > 0) {
        featuredVideos.push({
          name: test.name,
          outcome: test.outcome,
          suite: suite.name,
          videos: test.artifacts.videos,
        });
      }
    }
  }

  const generalArtifacts = { screenshots: [], html: [], other: [] };
  const reports = [];

  for (const file of allFiles) {
    if (matchedUrls.has(file.url)) continue;
    const path = file.path || "";
    if (path === "summary.json") continue;

    const mime = file.mime || "";
    const isReport = /\.(json|xml|txt|log|csv)$/i.test(path) || mime.includes("json") || mime.includes("xml");

    if (mime.startsWith("image/")) {
      generalArtifacts.screenshots.push(file);
    } else if (mime.includes("html")) {
      generalArtifacts.html.push(file);
    } else if (isReport) {
      reports.push(file);
    } else {
      generalArtifacts.other.push(file);
    }
  }

  return { suites: [...suiteMap.values()], generalArtifacts, reports, featuredVideos, debugLogsUrl };
}

function outcomeIcon(outcome) {
  switch (outcome) {
    case "passed": return "\u2713";
    case "failed":
    case "error": return "\u2717";
    case "skipped": return "\u2298";
    default: return "?";
  }
}

function suiteStatusClass(suite) {
  const tests = suite.tests || [];
  if (tests.some((t) => t.outcome === "failed" || t.outcome === "error")) return "error";
  if (tests.some((t) => t.outcome === "passed")) return "success";
  return "partial";
}

function renderFilterBar(summary) {
  const counts = summary.counts || {};
  const total = counts.total || 0;
  const passed = counts.passed || 0;
  const failed = counts.failed || 0;
  const skipped = counts.skipped || 0;

  return `
    <div class="filter-bar">
      <div class="filter-buttons">
        <button class="filter-btn active" data-filter="all">All <span class="filter-count">${total}</span></button>
        <button class="filter-btn" data-filter="passed">Passed <span class="filter-count">${passed}</span></button>
        <button class="filter-btn" data-filter="failed">Failed <span class="filter-count">${failed}</span></button>
        <button class="filter-btn" data-filter="skipped">Skipped <span class="filter-count">${skipped}</span></button>
      </div>
      <input type="search" class="test-search" placeholder="Search tests\u2026" />
    </div>
  `;
}

function renderFeaturedVideos(hierarchy) {
  const vids = hierarchy.featuredVideos || [];
  if (vids.length === 0 || vids.length > 5) return "";
  return `
    <section class="featured-videos">
      <h3 class="featured-videos-title">\u{1F4F9} Featured Videos <span class="featured-videos-count">${vids.length}</span></h3>
      <div class="featured-videos-grid">
        ${vids.map((v) => `
          <button class="featured-video-card test-status-${v.outcome}" data-test-name="${escapeHtml(v.name)}">
            <span class="featured-video-play">\u25B6</span>
            <span class="featured-video-name">${escapeHtml(v.name)}</span>
            <span class="featured-video-suite">${escapeHtml(v.suite)}</span>
          </button>
        `).join("")}
      </div>
    </section>
  `;
}

function renderTestTree(hierarchy) {
  if (!hierarchy || hierarchy.suites.length === 0) return "";

  const suiteHtml = hierarchy.suites.map((suite) => {
    const tests = suite.tests || [];
    const passed = tests.filter((t) => t.outcome === "passed").length;
    const failed = tests.filter((t) => t.outcome === "failed" || t.outcome === "error").length;
    const skipped = tests.filter((t) => t.outcome === "skipped").length;

    return `
      <div class="test-suite" data-suite="${escapeHtml(suite.name)}">
        <div class="test-suite-header">
          <span class="test-suite-toggle">\u25BC</span>
          <span class="test-suite-name">${escapeHtml(suite.name)}</span>
          <span class="test-suite-badges">
            ${passed > 0 ? `<span class="suite-badge suite-badge-passed">${passed}</span>` : ""}
            ${failed > 0 ? `<span class="suite-badge suite-badge-failed">${failed}</span>` : ""}
            ${skipped > 0 ? `<span class="suite-badge suite-badge-skipped">${skipped}</span>` : ""}
          </span>
          <span class="status-dot status-${suiteStatusClass(suite)}"></span>
        </div>
        <div class="test-suite-body">
          ${tests.map((test) => renderTestCase(test)).join("")}
        </div>
      </div>
    `;
  }).join("");

  return `<div class="test-tree">${suiteHtml}</div>`;
}

function renderTestCase(test) {
  const outcome = test.outcome || "unknown";

  return `
    <div class="test-case" data-test-name="${escapeHtml(test.name)}" data-outcome="${escapeHtml(outcome)}">
      <div class="test-case-header">
        <span class="test-toggle">\u25B8</span>
        <span class="test-status-icon test-status-${escapeHtml(outcome)}">${outcomeIcon(outcome)}</span>
        <span class="test-name">${escapeHtml(test.name)}</span>
        ${test.duration_ms != null ? `<span class="test-duration">${formatDuration(test.duration_ms)}</span>` : ""}
        ${test.failure_message ? `<span class="test-failure" title="${escapeHtml(test.failure_message)}">\u26A0</span>` : ""}
      </div>
    </div>
  `;
}

function renderTestArtifacts(test) {
  const a = test.artifacts;
  if (!a) return "";
  const parts = [];

  parts.push(`
    <div class="test-detail-meta">
      ${test.description ? `<div class="test-description">${escapeHtml(test.description)}</div>` : ""}
      ${test.human_failure ? `<div class="test-failure-detail">\u26A0 ${escapeHtml(test.human_failure)}</div>` : ""}
      ${test.markers && test.markers.length ? `<div class="test-markers">${test.markers.map(m => `<span class="test-marker">${escapeHtml(m)}</span>`).join("")}</div>` : ""}
      <div class="test-detail-info">
        <span>Framework: ${escapeHtml(test.framework || "unknown")}</span>
        ${test.file ? `<span>File: ${escapeHtml(test.file)}</span>` : ""}
      </div>
    </div>
  `);

  if (a.screenshots.length > 0) {
    parts.push(`
      <div class="test-artifact-group">
        <h4 class="test-artifact-title">Screenshots <span class="test-artifact-count">${a.screenshots.length}</span></h4>
        ${renderScreenshots(a.screenshots)}
      </div>
    `);
  }

  if (a.videos.length > 0) {
    parts.push(`
      <div class="test-artifact-group">
        <h4 class="test-artifact-title">Videos <span class="test-artifact-count">${a.videos.length}</span></h4>
        <div class="test-video-grid">
          ${a.videos.map((v) => `
            <div class="test-video-card">
              <video controls preload="metadata" src="${escapeHtml(v.url)}" class="test-video"></video>
              <div class="test-video-name" title="${escapeHtml(v.path)}">${escapeHtml(v.path)}</div>
            </div>
          `).join("")}
        </div>
      </div>
    `);
  }

  if (a.html.length > 0) {
    parts.push(`
      <div class="test-artifact-group">
        <h4 class="test-artifact-title">HTML Snapshots <span class="test-artifact-count">${a.html.length}</span></h4>
        ${a.html.map((h) => `
          <div class="html-inline-row">
            <button class="html-view-btn" data-url="${escapeHtml(h.url)}" data-name="${escapeHtml(h.path)}">View Inline</button>
            <a href="${escapeHtml(h.url)}" target="_blank" rel="noopener" class="html-open-link">${escapeHtml(h.path)} \u2197</a>
          </div>
        `).join("")}
      </div>
    `);
  }

  if (currentHierarchy && currentHierarchy.debugLogsUrl) {
    parts.push(`
      <div class="test-artifact-group">
        <h4 class="test-artifact-title">Debug Output</h4>
        <details class="debug-log-section">
          <summary class="debug-log-toggle">Show debug output</summary>
          <pre class="debug-log-content" data-debug-url="${escapeHtml(currentHierarchy.debugLogsUrl)}" data-test-name="${escapeHtml(test.name)}">Loading\u2026</pre>
        </details>
      </div>
    `);
  }

  if (a.screenshots.length === 0 && a.videos.length === 0 && a.html.length === 0 && !(currentHierarchy && currentHierarchy.debugLogsUrl)) {
    parts.push(`<p class="test-no-artifacts">No screenshots or videos captured for this test.</p>`);
  }

  return `<div class="test-case-body-inner">${parts.join("")}</div>`;
}

function findTestInHierarchy(hierarchy, testName) {
  if (!hierarchy) return null;
  for (const suite of hierarchy.suites) {
    for (const test of suite.tests) {
      if (test.name === testName) return test;
    }
  }
  return null;
}

function applyTestFilters() {
  const view = document.getElementById("run-view");
  const filter = currentTestFilter;
  const search = currentTestSearch.toLowerCase().trim();

  view.querySelectorAll(".test-case").forEach((el) => {
    const outcome = el.dataset.outcome;
    const name = (el.dataset.testName || "").toLowerCase();

    let visible = true;
    if (filter === "passed") visible = outcome === "passed";
    else if (filter === "failed") visible = outcome === "failed" || outcome === "error";
    else if (filter === "skipped") visible = outcome === "skipped";

    if (visible && search) {
      visible = name.includes(search);
    }

    el.classList.toggle("hidden", !visible);
  });

  view.querySelectorAll(".test-suite").forEach((suite) => {
    const hasVisible = suite.querySelector(".test-case:not(.hidden)");
    suite.classList.toggle("hidden", !hasVisible);
  });
}

function renderGeneralArtifacts(hierarchy) {
  const parts = [];
  const ga = hierarchy.generalArtifacts;

  if (ga.screenshots.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">General Screenshots <span class="section-count">${ga.screenshots.length}</span></h3>
        ${renderScreenshots(ga.screenshots)}
      </section>
    `);
  }

  if (ga.html.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">HTML Snapshots <span class="section-count">${ga.html.length}</span></h3>
        <div class="html-list">
          ${ga.html.map((h) => `
            <div class="html-inline-row">
              <button class="html-view-btn" data-url="${escapeHtml(h.url)}" data-name="${escapeHtml(h.path)}">View Inline</button>
              <a href="${escapeHtml(h.url)}" target="_blank" rel="noopener" class="html-open-link">${escapeHtml(h.path)} \u2197</a>
            </div>
          `).join("")}
        </div>
      </section>
    `);
  }

  if (hierarchy.reports.length > 0) {
    parts.push(`
      <details class="advanced-section">
        <summary class="advanced-header">Advanced \u2014 Raw Reports &amp; Data <span class="section-count">${hierarchy.reports.length}</span></summary>
        <div class="advanced-body">
          ${renderFileList(hierarchy.reports)}
        </div>
      </details>
    `);
  }

  if (ga.other.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">Other Files <span class="section-count">${ga.other.length}</span></h3>
        ${renderFileList(ga.other)}
      </section>
    `);
  }

  return parts.join("");
}

function wireUpTestTree(view) {
  view.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      view.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentTestFilter = btn.dataset.filter;
      applyTestFilters();
    });
  });

  const search = view.querySelector(".test-search");
  if (search) {
    search.addEventListener("input", () => {
      currentTestSearch = search.value;
      applyTestFilters();
    });
  }

  view.querySelectorAll(".test-suite-header").forEach((header) => {
    header.addEventListener("click", () => {
      header.parentElement.classList.toggle("collapsed");
    });
  });

  view.querySelectorAll(".featured-video-card").forEach((card) => {
    card.addEventListener("click", () => {
      const testName = card.dataset.testName;
      const tc = view.querySelector(`.test-case[data-test-name="${CSS.escape(testName)}"]`);
      if (tc) {
        if (!tc.classList.contains("expanded")) {
          tc.querySelector(".test-case-header")?.click();
        }
        tc.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  });

  view.querySelectorAll(".test-case-header").forEach((header) => {
    header.addEventListener("click", () => {
      const testCase = header.parentElement;

      const expanded = testCase.classList.toggle("expanded");
      if (expanded) {
        const testName = testCase.dataset.testName;
        const test = findTestInHierarchy(currentHierarchy, testName);
        if (test) {
          const body = document.createElement("div");
          body.className = "test-case-body";
          body.innerHTML = renderTestArtifacts(test);
          testCase.appendChild(body);

          body.querySelectorAll(".shot-thumb").forEach((img) => {
            img.addEventListener("click", () => openLightbox(img.dataset.fullUrl, img.dataset.filename));
          });

          body.querySelectorAll(".html-view-btn").forEach((btn) => {
            btn.addEventListener("click", () => openHtmlViewer(btn.dataset.url, btn.dataset.name));
          });

          body.querySelectorAll(".debug-log-section").forEach((details) => {
            details.addEventListener("toggle", async () => {
              if (!details.open) return;
              const pre = details.querySelector(".debug-log-content");
              if (!pre || pre.dataset.loaded) return;
              try {
                const resp = await fetch(pre.dataset.debugUrl);
                const data = await resp.json();
                const testName = pre.dataset.testName;
                const entry = data[testName];
                if (!entry) {
                  pre.textContent = "No debug data for this test.";
                  pre.dataset.loaded = "1";
                  return;
                }
                const lines = [
                  `Outcome: ${entry.outcome || "unknown"}`,
                  `Duration: ${(entry.duration_ms / 1000).toFixed(1)}s`,
                ];
                if (entry.failure_message) lines.push(`\nFailure: ${entry.failure_message}`);
                if (entry.stdout) lines.push(`\n=== stdout ===\n${entry.stdout}`);
                if (entry.stderr) lines.push(`\n=== stderr ===\n${entry.stderr}`);
                if (entry.logs) lines.push(`\n=== logs ===\n${entry.logs}`);
                if (entry.traceback) lines.push(`\n=== traceback ===\n${entry.traceback}`);
                pre.textContent = lines.join("\n") || "No output captured.";
                pre.dataset.loaded = "1";
              } catch {
                pre.textContent = "Failed to load debug data.";
              }
            });
          });

          observeNewThumbnails(body);
        }
      } else {
        const body = testCase.querySelector(".test-case-body");
        if (body) body.remove();
      }
    });
  });

  // Auto-expand failed/error tests so screenshots are immediately visible
  view.querySelectorAll('.test-case[data-outcome="failed"], .test-case[data-outcome="error"]').forEach((tc) => {
    const header = tc.querySelector(".test-case-header");
    if (header && !tc.classList.contains("expanded")) header.click();
  });

  // Auto-expand first suite so test tree isn't fully collapsed on load
  const firstSuite = view.querySelector(".test-suite:not(.collapsed)");
  if (!firstSuite) {
    const anySuite = view.querySelector(".test-suite");
    if (anySuite && anySuite.classList.contains("collapsed")) {
      anySuite.classList.remove("collapsed");
    }
  }

  view.querySelectorAll(".general-section .html-view-btn").forEach((btn) => {
    btn.addEventListener("click", () => openHtmlViewer(btn.dataset.url, btn.dataset.name));
  });

  view.querySelectorAll(".general-section .shot-thumb").forEach((img) => {
    img.addEventListener("click", () => openLightbox(img.dataset.fullUrl, img.dataset.filename));
  });

  lazyLoadScreenshots(view);
}

function renderFlatBody(view, run) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  body.innerHTML = `
    <section class="screenshot-section">
      <h3 class="section-title">Screenshots <span class="section-count">${run.screenshots.length}</span></h3>
      ${renderScreenshots(run.screenshots)}
    </section>
    <section class="files-section">
      <h3 class="section-title">Files <span class="section-count">${run.files.length}</span></h3>
      ${renderFileList(run.files)}
    </section>
  `;

  view.querySelectorAll(".shot-thumb").forEach((img) => {
    img.addEventListener("click", () => openLightbox(img.dataset.fullUrl, img.dataset.filename));
  });

  lazyLoadScreenshots(view);
  checkBlobAvailability(view);
}

async function renderFipsRun(view, run, myLoadId) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  const allFiles = [...(run.screenshots || []), ...(run.files || [])];

  const metricsBar = (() => {
    const parts = [];
    if (run.passed != null) parts.push(`<div class="metric metric-green"><span class="metric-value">${run.passed}</span><span class="metric-label">Passed</span></div>`);
    if (run.failed != null) parts.push(`<div class="metric${run.failed > 0 ? " metric-red" : ""}"><span class="metric-value">${run.failed}</span><span class="metric-label">Failed</span></div>`);
    if (run.skipped != null) parts.push(`<div class="metric"><span class="metric-value">${run.skipped}</span><span class="metric-label">Skipped</span></div>`);
    if (run.total != null) parts.push(`<div class="metric"><span class="metric-value">${run.total}</span><span class="metric-label">Total</span></div>`);
    const summaryText = run.summary?.metadata?.summary || run.summary?.summary || "";
    if (summaryText) parts.push(`<div class="metric metric-wide"><span class="metric-value-text">${escapeHtml(summaryText.substring(0, 200))}</span></div>`);
    return parts.length ? `<div class="detail-metrics">${parts.join("")}</div>` : "";
  })();

  if (allFiles.length === 0 && !metricsBar) {
    body.innerHTML = `<p class="section-empty">No artifacts in this run.</p>`;
    return;
  }

  const sorted = [...allFiles].sort((a, b) => {
    const pri = (p) => {
      if (p.endsWith("report.html")) return 0;
      if (p.endsWith(".gif")) return 1;
      if (p.endsWith(".html") || p.endsWith(".htm")) return 2;
      if (p.endsWith("analysis.txt") || p.endsWith(".txt")) return 3;
      if (p.endsWith(".json")) return 4;
      return 5;
    };
    return pri(a.path || "") - pri(b.path || "");
  });

  const summaryContent = (() => {
    const md = run.summary?.metadata?.summary || "";
    const payload = run.summary || {};
    let html = "";
    if (md) html += `<div class="md-viewer">${renderMarkdown(md)}</div>`;
    const prettyJson = JSON.stringify(payload, null, 2);
    html += `<details class="advanced-section"><summary class="advanced-header">Raw Event Content (JSON)</summary><pre class="json-viewer-pre">${escapeHtml(prettyJson)}</pre></details>`;
    return html;
  })();

  body.innerHTML = `
    ${metricsBar}
    <div class="fips-run-info">
      ${run.scenario ? `<span class="meta-chip meta-chip-branch">${escapeHtml(run.scenario)}</span>` : ""}
      ${run.fipsRef ? `<span class="meta-chip">${escapeHtml(run.fipsRef)}</span>` : ""}
      ${run.nodes != null ? `<span class="meta-chip">${run.nodes} nodes</span>` : ""}
    </div>
    <div class="file-nav-bar" id="fips-file-nav">
      <button class="file-nav-tab active" data-idx="-1">Summary</button>
      ${sorted.map((f, i) => {
        const name = (f.path || "").split("/").pop() || f.url.slice(-20);
        return `<button class="file-nav-tab" data-idx="${i}">${escapeHtml(name)}</button>`;
      }).join("")}
    </div>
    <div class="fips-viewer" id="fips-viewer">
      <div class="test-tree-loading"><div class="spinner"></div><p>Loading\u2026</p></div>
    </div>
  `;

  const viewer = body.querySelector("#fips-viewer");
  const navBar = body.querySelector("#fips-file-nav");

  navBar.querySelectorAll(".file-nav-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      navBar.querySelectorAll(".file-nav-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const idx = parseInt(tab.dataset.idx, 10);
      if (idx === -1) {
        viewer.innerHTML = summaryContent;
      } else {
        showFipsFile(sorted[idx], viewer, myLoadId, run);
      }
    });
  });

  viewer.innerHTML = summaryContent;
}

function renderMarkdown(md) {
  let html = escapeHtml(md);
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/^\| (.+)$/gm, (match) => {
    const cells = match.split('|').filter(c => c.trim());
    if (cells.every(c => /^[\s-:]+$/.test(c))) return '';
    const tds = cells.map(c => `<td>${c.trim()}</td>`).join('');
    return `<tr>${tds}</tr>`;
  });
  html = html.replace(/(<tr>[\s\S]*?<\/tr>)(?!\s*<tr>)/g, '<table border="1" cellpadding="4" style="border-collapse:collapse">$1</table>');
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  html = html.replace(/^(?!<[hutol])(.+)$/gm, '<p>$1</p>');
  html = html.replace(/<\/p>\s*<p>/g, '</p><p>');
  return html;
}

async function showFipsFile(file, viewer, myLoadId, run) {
  const name = (file.path || "").split("/").pop() || file.url;
  const ext = (name.split(".").pop() || "").toLowerCase();

  viewer.innerHTML = `<div class="test-tree-loading"><div class="spinner"></div><p>Loading ${escapeHtml(name)}\u2026</p></div>`;

  try {
    const resp = await fetch(file.url);
    if (myLoadId !== detailLoadId) return;
    const text = await resp.text();

    if (text.includes('"error"') && text.includes('Not found in storage')) {
      const fallback = run?.summary?.metadata?.summary || "";
      viewer.innerHTML = `<div class="fips-download-state">
        <p>File expired from Blossom storage</p>
        ${fallback ? `<div class="md-viewer" style="text-align:left;margin-top:16px">${renderMarkdown(fallback)}</div>` : ""}
        <a href="${escapeHtml(file.url)}" target="_blank" rel="noopener" class="detail-link">Try direct link \u2197</a>
      </div>`;
      return;
    }

    if (ext === "html" || ext === "htm") {
      viewer.innerHTML = `<iframe sandbox="allow-same-origin" class="fips-report-frame"></iframe>`;
      viewer.querySelector("iframe").srcdoc = text;

    } else if (ext === "json") {
      try {
        const data = JSON.parse(text);
        viewer.innerHTML = `<pre class="json-viewer-pre">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
      } catch {
        viewer.innerHTML = `<pre class="json-viewer-pre">${escapeHtml(text)}</pre>`;
      }

    } else if (["gif", "png", "jpg", "jpeg", "svg", "webp"].includes(ext)) {
      viewer.innerHTML = `<div class="fips-image-viewer"><img alt="${escapeHtml(name)}"></div>`;
      const blob = await resp.blob();
      if (myLoadId !== detailLoadId) return;
      viewer.querySelector("img").src = URL.createObjectURL(blob);

    } else if (ext === "md") {
      const truncated = text.length > 100000 ? text.slice(0, 100000) + "\n\n... truncated" : text;
      viewer.innerHTML = `<div class="md-viewer">${renderMarkdown(truncated)}</div>`;

    } else if (["log", "txt", "env", "yaml", "yml", "csv"].includes(ext) || name === "DONE") {
      const truncated = text.length > 50000 ? text.slice(0, 50000) + "\n\n... truncated" : text;
      viewer.innerHTML = `<pre class="json-viewer-pre">${escapeHtml(truncated)}</pre>`;

    } else {
      viewer.innerHTML = `<div class="fips-download-state"><p>${escapeHtml(name)}</p><a href="${escapeHtml(file.url)}" target="_blank" rel="noopener" class="detail-link">Download from Blossom \u2197</a></div>`;
    }
  } catch (e) {
    if (myLoadId !== detailLoadId) return;
    const fallback = run?.summary?.metadata?.summary || "";
    viewer.innerHTML = `<div class="fips-download-state">
      <p>Failed to load ${escapeHtml(name)}</p>
      ${fallback ? `<div class="md-viewer" style="text-align:left;margin-top:16px">${renderMarkdown(fallback)}</div>` : ""}
      <a href="${escapeHtml(file.url)}" target="_blank" rel="noopener" class="detail-link">Open directly \u2197</a>
    </div>`;
  }
}

async function renderBleRun(view, run, myLoadId) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  const summary = run.summary || {};
  const allFiles = [...(run.screenshots || []), ...(run.files || [])];

  const metricsHtml = renderBleMetrics(summary);
  const filesHtml = await renderBleFiles(allFiles, body, myLoadId);

  if (myLoadId !== detailLoadId) return;

  body.innerHTML = `
    ${metricsHtml}
    ${filesHtml}
  `;

  wireGenericFileButtons(body);
  lazyLoadScreenshots(body);
  checkBlobAvailability(body);
}

function renderBleMetrics(summary) {
  const parts = [];

  if (summary.best_mode || summary.metrics?.best_mode) {
    const best = summary.best_mode || summary.metrics?.best_mode;
    const bestRtt = summary.metrics?.best_rtt_p95 || summary.best_rtt_p95;
    parts.push(`
      <div class="metric metric-green">
        <span class="metric-value">${escapeHtml(best)}</span>
        <span class="metric-label">Best Mode</span>
      </div>
      ${bestRtt != null ? `<div class="metric"><span class="metric-value">${bestRtt}ms</span><span class="metric-label">RTT p95</span></div>` : ""}
    `);
  }

  if (summary.platform_pair) {
    parts.push(`<div class="metric"><span class="metric-value">${escapeHtml(summary.platform_pair)}</span><span class="metric-label">Platform Pair</span></div>`);
  }
  if (summary.mode) {
    parts.push(`<div class="metric"><span class="metric-value">${escapeHtml(summary.mode)}</span><span class="metric-label">Mode</span></div>`);
  }
  if (summary.phase) {
    parts.push(`<div class="metric"><span class="metric-value">${escapeHtml(summary.phase)}</span><span class="metric-label">Phase</span></div>`);
  }
  if (Array.isArray(summary.modes)) {
    parts.push(`<div class="metric"><span class="metric-value">${summary.modes.length}</span><span class="metric-label">Modes Tested</span></div>`);
  }
  if (Array.isArray(summary.payload_sizes)) {
    parts.push(`<div class="metric"><span class="metric-value">${summary.payload_sizes.join("/")}</span><span class="metric-label">Payload Sizes</span></div>`);
  }

  if (parts.length === 0) return "";

  return `<div class="detail-metrics ble-metrics">${parts.join("")}</div>`;
}

async function renderBleFiles(files, body, myLoadId) {
  if (files.length === 0) return "";

  const images = files.filter((f) => (f.mime || "").startsWith("image/") || /\.(png|jpg|jpeg|gif|svg|webp)$/i.test(f.path || ""));
  const jsonFiles = files.filter((f) => (f.mime || "").includes("json") || f.path?.endsWith(".json"));
  const htmlFiles = files.filter((f) => (f.mime || "").includes("html") || /\.(html|htm)$/i.test(f.path || ""));
  const others = files.filter((f) => !images.includes(f) && !jsonFiles.includes(f) && !htmlFiles.includes(f));

  const parts = [];

  if (images.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">Charts <span class="section-count">${images.length}</span></h3>
        ${renderScreenshots(images)}
      </section>
    `);
  }

  if (htmlFiles.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">Reports <span class="section-count">${htmlFiles.length}</span></h3>
        <div class="html-list">
          ${htmlFiles.map((h) => `
            <div class="html-inline-row">
              <button class="html-view-btn" data-url="${escapeHtml(h.url)}" data-name="${escapeHtml(h.path)}">View Inline</button>
              <a href="${escapeHtml(h.url)}" target="_blank" rel="noopener" class="html-open-link">${escapeHtml(h.path)} \u2197</a>
            </div>
          `).join("")}
        </div>
      </section>
    `);
  }

  if (jsonFiles.length > 0 || others.length > 0) {
    parts.push(`
      <section class="general-section">
        <h3 class="section-title">Data Files <span class="section-count">${jsonFiles.length + others.length}</span></h3>
        ${renderFileList([...jsonFiles, ...others])}
      </section>
    `);
  }

  return parts.join("");
}

async function renderMicrofipsRun(view, run, myLoadId) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  const summary = run.summary || {};
  const allFiles = [...(run.screenshots || []), ...(run.files || [])];

  const passed = summary.tests_passed ?? summary.passed ?? run.passed;
  const failed = summary.tests_failed ?? summary.failed ?? run.failed;

  const statusColor = failed != null && failed > 0 ? "var(--red)" : "var(--green)";
  const statusText = failed != null && failed > 0 ? "FAILED" : "PASSED";

  body.innerHTML = `
    <div class="microfips-status-bar" style="border-color: ${statusColor};">
      <span class="microfips-status-icon" style="color: ${statusColor};">${failed != null && failed > 0 ? "\u2717" : "\u2713"}</span>
      <span class="microfips-status-text" style="color: ${statusColor};">${statusText}</span>
      ${passed != null ? `<span class="microfips-status-count">${passed} passed</span>` : ""}
      ${failed != null ? `<span class="microfips-status-count">${failed} failed</span>` : ""}
    </div>
    ${run.fipsRef ? `<div class="detail-meta-grid"><div class="meta-item"><span class="meta-label">FIPS Ref</span><span class="meta-value">${escapeHtml(run.fipsRef)}</span></div>${run.noiseMode ? `<div class="meta-item"><span class="meta-label">Noise Mode</span><span class="meta-value">${escapeHtml(run.noiseMode)}</span></div>` : ""}</div>` : ""}
    ${allFiles.length > 0 ? `
      <section class="general-section">
        <h3 class="section-title">Artifacts <span class="section-count">${allFiles.length}</span></h3>
        ${renderFileList(allFiles)}
      </section>
    ` : ""}
    ${run.content ? `
      <details class="advanced-section">
        <summary class="advanced-header">Summary JSON</summary>
        <div class="advanced-body">
          <pre class="json-viewer-pre">${escapeHtml(JSON.stringify(summary, null, 2))}</pre>
        </div>
      </details>
    ` : ""}
  `;

  wireGenericFileButtons(body);
}

async function renderBenchmarkRun(view, run, myLoadId) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  body.innerHTML = `<div class="test-tree-loading"><div class="spinner"></div><p>Loading benchmark\u2026</p></div>`;

  const files = run.files || [];
  const benchFile =
    files.find((f) => /benchmark-results\.json$/i.test(f.path || "")) ||
    files.find((f) => /benchmark/i.test((f.path || "") + " " + (f.url || "")));

  if (!benchFile) {
    body.innerHTML = `<p class="section-empty">No benchmark results file found in this run.</p>`;
    return;
  }

  let data;
  try {
    const resp = await fetch(benchFile.url);
    if (myLoadId !== detailLoadId) return;
    data = JSON.parse(await resp.text());
  } catch (e) {
    if (myLoadId !== detailLoadId) return;
    body.innerHTML = `<p class="section-empty">Failed to load benchmark results: ${escapeHtml(String(e))}</p>`;
    return;
  }
  if (myLoadId !== detailLoadId) return;

  const results = Array.isArray(data.results) ? data.results : [];
  const echoResults = results.filter((r) => r.test === "echo");
  const tputResults = results.filter((r) => r.test === "throughput");
  const pairs = [...new Set(results.map((r) => r.pair).filter(Boolean))].sort();

  const summaryCards = [
    `<div class="metric"><span class="metric-value">${results.length}</span><span class="metric-label">Total Tests</span></div>`,
    `<div class="metric"><span class="metric-value">${echoResults.length}</span><span class="metric-label">Echo Runs</span></div>`,
    `<div class="metric"><span class="metric-value">${tputResults.length}</span><span class="metric-label">Throughput Runs</span></div>`,
    `<div class="metric"><span class="metric-value">${pairs.length}</span><span class="metric-label">Device Pairs</span></div>`,
  ].join("");

  const echoMax = echoResults.reduce((m, r) => Math.max(m, r.median_us || 0), 0) || 1;
  const tputMax = tputResults.reduce((m, r) => Math.max(m, r.achieved_bps || 0), 0) || 1;

  const groupBars = (rows, max, accessor, formatVal, color) => {
    if (!rows.length) return "";
    const byPair = {};
    for (const r of rows) {
      const p = r.pair || "?";
      (byPair[p] = byPair[p] || []).push(r);
    }
    return Object.keys(byPair).sort().map((pair) => {
      const items = byPair[pair].slice().sort((a, b) => (accessor.sortKey(a) ?? 0) - (accessor.sortKey(b) ?? 0));
      const bars = items.map((r) => {
        const val = accessor.value(r) || 0;
        const pct = Math.max(2, (val / max) * 100);
        return `<div class="bench-bar-row">
          <span class="bench-bar-label">${escapeHtml(accessor.label(r))}</span>
          <div class="bench-bar-track"><div class="bench-bar-fill" style="width:${pct.toFixed(1)}%;background:${color};"></div></div>
          <span class="bench-bar-value">${escapeHtml(formatVal(r))}</span>
        </div>`;
      }).join("");
      return `<div class="bench-chart-group">
        <div class="bench-chart-group-label">${escapeHtml(pair)}</div>
        <div class="bench-chart-bars">${bars}</div>
      </div>`;
    }).join("");
  };

  const fmtUs = (r) => `${(r.median_us || 0).toLocaleString()}\u00b5s`;
  const fmtKbps = (r) => `${Math.round((r.achieved_bps || 0) / 1000).toLocaleString()} kbps`;

  const echoChart = groupBars(
    echoResults, echoMax,
    {
      sortKey: (r) => r.payload_size,
      label: (r) => (r.payload_size != null ? r.payload_size + "B" : "?"),
      value: (r) => r.median_us,
    },
    fmtUs, "var(--blue)"
  );

  const tputChart = groupBars(
    tputResults, tputMax,
    {
      sortKey: (r) => r.frame_size,
      label: (r) => (r.frame_size != null ? r.frame_size + "B" : "?"),
      value: (r) => r.achieved_bps,
    },
    fmtKbps, "var(--green)"
  );

  const echoSection = echoResults.length
    ? `<section class="general-section">
        <h3 class="section-title">Echo RTT <span class="section-count">${echoResults.length}</span></h3>
        <div class="bench-chart">${echoChart}</div>
      </section>`
    : `<p class="section-empty">No echo results.</p>`;

  const tputSection = tputResults.length
    ? `<section class="general-section">
        <h3 class="section-title">Throughput <span class="section-count">${tputResults.length}</span></h3>
        <div class="bench-chart">${tputChart}</div>
      </section>`
    : `<p class="section-empty">No throughput results.</p>`;

  const tableRows = results.map((r) => {
    const isEcho = r.test === "echo";
    const size = isEcho ? (r.payload_size ?? "?") : (r.frame_size ?? "?");
    const value = isEcho ? fmtUs(r) : fmtKbps(r);
    const loss = isEcho
      ? (r.loss_count != null ? `${r.loss_count}/${r.count ?? "?"}` : "?")
      : (r.frame_loss_rate != null ? `${(r.frame_loss_rate * 100).toFixed(3)}%` : "?");
    return `<tr>
      <td>${escapeHtml(r.test || "?")}</td>
      <td>${escapeHtml(r.pair || "?")}</td>
      <td>${escapeHtml(String(size))}</td>
      <td>${escapeHtml(value)}</td>
      <td>${escapeHtml(loss)}</td>
    </tr>`;
  }).join("");

  const rawTable = results.length
    ? `<details class="advanced-section">
        <summary class="advanced-header">Raw Data <span class="section-count">${results.length}</span></summary>
        <div class="bench-table-wrap">
          <table class="bench-table">
            <thead><tr><th>Test</th><th>Pair</th><th>Size</th><th>Value</th><th>Loss/Rate</th></tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </details>`
    : "";

  body.innerHTML = `
    <style>
      .bench-chart { display:flex; flex-direction:column; gap:18px; }
      .bench-chart-group { display:flex; flex-direction:column; gap:6px; }
      .bench-chart-group-label { font-size:12px; color:var(--text-muted); font-weight:600; letter-spacing:.02em; text-transform:uppercase; }
      .bench-chart-bars { display:flex; flex-direction:column; gap:5px; }
      .bench-bar-row { display:grid; grid-template-columns:48px 1fr 120px; align-items:center; gap:10px; }
      .bench-bar-label { font-size:12px; color:var(--text-muted); font-variant-numeric:tabular-nums; text-align:right; }
      .bench-bar-track { height:14px; background:var(--bg-elevated); border-radius:7px; overflow:hidden; border:1px solid var(--border-light); }
      .bench-bar-fill { height:100%; border-radius:7px; transition:width .4s ease; }
      .bench-bar-value { font-size:12px; color:var(--text); font-variant-numeric:tabular-nums; }
      .bench-table-wrap { overflow-x:auto; }
      .bench-table { width:100%; border-collapse:collapse; font-size:13px; }
      .bench-table th, .bench-table td { padding:7px 10px; text-align:left; border-bottom:1px solid var(--border-light); }
      .bench-table th { color:var(--text-muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.02em; }
      .bench-table td { color:var(--text); font-variant-numeric:tabular-nums; }
    </style>
    <div class="detail-metrics">${summaryCards}</div>
    ${data.scenario ? `<div class="fips-run-info"><span class="meta-chip meta-chip-branch">${escapeHtml(data.scenario)}</span>${data.timestamp ? `<span class="meta-chip">${escapeHtml(data.timestamp)}</span>` : ""}</div>` : ""}
    ${echoSection}
    ${tputSection}
    ${rawTable}
  `;
}

async function renderGenericFileBrowser(view, run, myLoadId) {
  const body = view.querySelector(".detail-body");
  if (!body) return;

  const allFiles = [...(run.screenshots || []), ...(run.files || [])];

  const images = allFiles.filter((f) => (f.mime || "").startsWith("image/") || /\.(png|jpg|jpeg|gif|svg|webp)$/i.test(f.path || ""));
  const htmlFiles = allFiles.filter((f) => (f.mime || "").includes("html") || /\.(html|htm)$/i.test(f.path || ""));
  const others = allFiles.filter((f) => !images.includes(f) && !htmlFiles.includes(f));

  const sections = [];

  if (images.length > 0) {
    sections.push(`
      <section class="general-section">
        <h3 class="section-title">Images <span class="section-count">${images.length}</span></h3>
        ${renderScreenshots(images)}
      </section>
    `);
  }

  if (htmlFiles.length > 0) {
    sections.push(`
      <section class="general-section">
        <h3 class="section-title">HTML <span class="section-count">${htmlFiles.length}</span></h3>
        <div class="html-list">
          ${htmlFiles.map((h) => `
            <div class="html-inline-row">
              <button class="html-view-btn" data-url="${escapeHtml(h.url)}" data-name="${escapeHtml(h.path)}">View Inline</button>
              <a href="${escapeHtml(h.url)}" target="_blank" rel="noopener" class="html-open-link">${escapeHtml(h.path)} \u2197</a>
            </div>
          `).join("")}
        </div>
      </section>
    `);
  }

  if (others.length > 0) {
    sections.push(`
      <section class="general-section">
        <h3 class="section-title">Files <span class="section-count">${others.length}</span></h3>
        ${renderFileList(others)}
      </section>
    `);
  }

  if (run.content) {
    let prettyContent;
    try { prettyContent = JSON.stringify(JSON.parse(run.content), null, 2); }
    catch { prettyContent = run.content; }
    sections.push(`
      <details class="advanced-section">
        <summary class="advanced-header">Event Content (JSON)</summary>
        <div class="advanced-body">
          <pre class="json-viewer-pre">${escapeHtml(prettyContent)}</pre>
        </div>
      </details>
    `);
  }

  if (sections.length === 0) {
    sections.push(`<p class="section-empty">No artifacts in this run.</p>`);
  }

  body.innerHTML = sections.join("");
  wireGenericFileButtons(body);
  lazyLoadScreenshots(body);
}

function wireGenericFileButtons(container) {
  container.querySelectorAll(".html-view-btn").forEach((btn) => {
    btn.addEventListener("click", () => openHtmlViewer(btn.dataset.url, btn.dataset.name));
  });
  container.querySelectorAll(".shot-thumb").forEach((img) => {
    img.addEventListener("click", () => openLightbox(img.dataset.fullUrl, img.dataset.filename));
  });
}

// ===========================================================================
// HTML viewer modal
// ===========================================================================

async function openHtmlViewer(url, filename) {
  const modal = document.getElementById("html-viewer");
  if (!modal) return;
  const frame = modal.querySelector(".html-viewer-frame");
  const title = modal.querySelector(".html-viewer-title");
  const open = modal.querySelector(".html-viewer-open");

  title.textContent = filename || "";
  open.href = url;

  try {
    const resp = await fetch(url);
    const html = await resp.text();
    const blob = new Blob([html], { type: "text/html" });
    frame.src = URL.createObjectURL(blob);
  } catch {
    frame.src = url;
  }

  modal.hidden = false;
  requestAnimationFrame(() => modal.classList.add("open"));
}

function closeHtmlViewer() {
  const modal = document.getElementById("html-viewer");
  if (!modal) return;
  const frame = modal.querySelector(".html-viewer-frame");
  modal.classList.remove("open");
  setTimeout(() => {
    modal.hidden = true;
    if (frame.src.startsWith("blob:")) URL.revokeObjectURL(frame.src);
    frame.src = "about:blank";
  }, 200);
}

// ===========================================================================
// Detail view: selectRun
// ===========================================================================

function selectRunFromHash() {
  const hashRunId = location.hash.slice(1);
  if (!hashRunId) {
    if (allRuns.length > 0 && !selectedRunId) {
      const newest = [...allRuns].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))[0];
      selectRun(newest);
    }
    return;
  }
  if (selectedRunId === hashRunId) return;
  const run = allRuns.find((r) => r.runId === hashRunId);
  if (run) selectRun(run);
}

function showPlaceholder() {
  selectedRunId = null;
  currentRun = null;
  currentHierarchy = null;
  document.getElementById("app").classList.remove("mobile-view-detail");
  document.querySelectorAll(".run-card").forEach((el) => el.classList.remove("active"));

  const view = document.getElementById("run-view");
  view.scrollTop = 0;
  view.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon empty-icon-arrow">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
          <line x1="8" y1="21" x2="16" y2="21"/>
          <line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
      </div>
      <p class="empty-title">Select a test run</p>
      <p class="empty-hint empty-hint-desktop">&larr; Select a run from the list</p>
      <p class="empty-hint empty-hint-mobile">Tap a run to view details</p>
      <p class="hint">Runs are fetched live from Nostr relays.<br>
      Screenshots load on demand to be gentle on <a href="https://blossom.psbt.me" target="_blank" rel="noopener">blossom.psbt.me</a></p>
    </div>
  `;
}

async function selectRun(run) {
  selectedRunId = run.runId;
  currentRun = run;
  currentHierarchy = null;
  const myLoadId = ++detailLoadId;

  if (location.hash !== "#" + run.runId) {
    history.pushState({ runId: run.runId }, "", "#" + run.runId);
  }

  document.querySelectorAll(".run-card").forEach((el) => {
    el.classList.toggle("active", el.dataset.runId === run.runId);
  });

  if (window.innerWidth <= 768) {
    document.getElementById("app").classList.add("mobile-view-detail");
  }

  const view = document.getElementById("run-view");
  view.scrollTop = 0;

  const metaItems = [];
  if (run.branch) metaItems.push(metaItem("Branch", escapeHtml(run.branch)));
  if (run.pr) metaItems.push(metaItem("PR", "#" + escapeHtml(run.pr)));

  const repo = run.repo || (run.backend === "rust" ? "Amperstrand/tollgate-rs-ai-research-and-experiments" : "OpenTollGate/tollgate-module-basic-go");
  if (repo) {
    metaItems.push(metaItem("Repo", `<a href="https://github.com/${escapeHtml(repo)}" target="_blank" rel="noopener" class="meta-link">${escapeHtml(repo)} \u2197</a>`));
  }
  if (run.commit && run.commit !== "(branch head)" && run.commit.length >= 7) {
    const c = shortCommit(run.commit);
    metaItems.push(metaItem("Commit", `<a href="https://github.com/${escapeHtml(repo)}/commit/${escapeHtml(run.commit)}" target="_blank" rel="noopener" class="meta-link"><code>${escapeHtml(c)}</code> \u2197</a>`));
  }
  if (run.portal && run.portal !== "builtin") {
    metaItems.push(metaItem("Portal", `<span class="meta-chip meta-chip-portal">${escapeHtml(run.portal)}</span>`));
  }
  if (run.router) metaItems.push(metaItem("Router", escapeHtml(run.router)));
  if (run.backend) metaItems.push(metaItem("Backend", escapeHtml(run.backend)));
  if (run.clientType) metaItems.push(metaItem("Client", escapeHtml(run.clientType)));
  if (run.viewport) metaItems.push(metaItem("Viewport", escapeHtml(run.viewport)));
  if (run.runnerNpub) {
    metaItems.push(metaItem("Runner", `<code class="runner-code">${escapeHtml(shortNpub(run.runnerNpub))}</code>`));
  }

  if (run.scanSummary && run.scanSummary.scanned != null) {
    metaItems.push(metaItem("Scanned", String(run.scanSummary.scanned)));
  }
  if (run.scanSummary && run.scanSummary.blocked) {
    metaItems.push(metaItem("Blocked", String(run.scanSummary.blocked)));
  }

  const metrics = [];
  if (run.total != null) metrics.push(metric(run.total, "Total"));
  if (run.passed != null) metrics.push(metric(run.passed, "Passed", "green"));
  if (run.failed != null) metrics.push(metric(run.failed, "Failed", run.failed > 0 ? "red" : "green"));
  if (run.skipped != null && run.skipped > 0) metrics.push(metric(run.skipped, "Skipped", "yellow"));

  view.innerHTML = `
    <div class="detail-header">
      <button id="back-to-list" class="back-to-list" aria-label="Back to runs list">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="15 18 9 12 15 6"></polyline>
        </svg>
        <span>Runs</span>
      </button>
      <div class="detail-titles">
        <div class="detail-run">
          ${statusBadge(run)}
          ${statusIcon(run.status)}
          <span class="run-id-lg">${escapeHtml(run.runId)}</span>
        </div>
      </div>
      ${metrics.length > 0 ? `<div class="detail-metrics">${metrics.join("")}</div>` : ""}
      ${metaItems.length > 0 ? `<div class="detail-meta-grid">${metaItems.join("")}</div>` : ""}
      <div class="detail-links">
        <a href="https://njump.me/${run.eventId}" target="_blank" class="detail-link" rel="noopener">Nostr event \u2197</a>
      </div>
      ${renderNostrEventsSection(run)}
    </div>
    <div class="detail-body">
      <div class="test-tree-loading">
        <div class="spinner"></div>
        <p>Loading test results\u2026</p>
      </div>
    </div>
  `;

  const summary = await fetchTestSummary(run);

  const backBtn = view.querySelector("#back-to-list");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (history.state && history.state.runId) {
        history.back();
      } else {
        showPlaceholder();
      }
    });
  }

  if (myLoadId !== detailLoadId) return;

  const body = view.querySelector(".detail-body");
  if (!body) return;

  const project = getRunProject(run);

  if (project === "fips") {
    await renderFipsRun(view, run, myLoadId);
    return;
  }
  if (project === "ble") {
    await renderBleRun(view, run, myLoadId);
    return;
  }
  if (project === "microfips") {
    await renderMicrofipsRun(view, run, myLoadId);
    return;
  }
  if (project === "fips-benchmark") {
    await renderBenchmarkRun(view, run, myLoadId);
    return;
  }
  if (project === "conwrt") {
    await renderConwrtRun(view, run, myLoadId);
    return;
  }
  if (project === "unknown") {
    await renderGenericFileBrowser(view, run, myLoadId);
    return;
  }

  if (project === "tollgate" && run.source === "k30078" && (!summary || !summary.tests)) {
    await renderFipsRun(view, run, myLoadId);
    return;
  }

  if (summary && summary.tests) {
    const hierarchy = buildTestHierarchy(run, summary);
    if (hierarchy) {
      currentHierarchy = hierarchy;
      currentTestFilter = "all";
      currentTestSearch = "";

      body.innerHTML = `
        ${renderFilterBar(summary)}
        ${renderFeaturedVideos(hierarchy)}
        ${renderTestTree(hierarchy)}
        ${renderGeneralArtifacts(hierarchy)}
      `;

      wireUpTestTree(view);
      return;
    }
  }

  renderFlatBody(view, run);
}

function renderNostrEventsSection(run) {
  const parts = [];

  const k6900 = run.rawEvent || null;
  let requestEvent = null;
  if (k6900) {
    const reqTag = (k6900.tags || []).find((t) => t[0] === "request");
    if (reqTag && reqTag[1]) {
      try { requestEvent = JSON.parse(reqTag[1]); } catch (e) {}
    }
  }
  const reqId = run.requestEventId || (requestEvent && requestEvent.id);

  if (requestEvent) {
    parts.push(renderEventBlock("Kind 5900 \u2014 DVM Job Request", requestEvent, "request"));
  } else if (reqId) {
    parts.push(`<div class="nostr-event-block"><div class="nostr-event-kind">Kind 5900 \u2014 DVM Job Request</div><div class="nostr-event-body"><a href="https://njump.me/${escapeHtml(reqId)}" target="_blank" rel="noopener" class="detail-link">${escapeHtml(reqId.slice(0,16))}\u2026 \u2197</a></div></div>`);
  }

  if (k6900) {
    parts.push(renderEventBlock("Kind 6900 \u2014 DVM Job Result", k6900, "result"));
  }

  if (run.feedbackStatus) {
    parts.push(`<div class="nostr-event-block nostr-event-feedback"><div class="nostr-event-kind">Kind 7000 \u2014 DVM Feedback: <span class="feedback-${escapeHtml(run.feedbackStatus)}">${escapeHtml(run.feedbackStatus)}</span></div>${reqId ? `<div class="nostr-event-body"><a href="https://njump.me/${escapeHtml(reqId)}" target="_blank" rel="noopener" class="detail-link">View on njump \u2197</a></div>` : ""}</div>`);
  }

  if (parts.length === 0) return "";

  return `<details class="nostr-events-section"><summary>Nostr DVM Events (${parts.length})</summary><div class="nostr-events-list">${parts.join("")}</div></details>`;
}

function renderEventBlock(title, event, cls) {
  const time = event.created_at ? new Date(event.created_at * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "?";
  const paramTags = (event.tags || []).filter((t) => t[0] === "param").map((t) => `${t[1]}=${t[2]}`).join(", ");
  let contentPreview = "";
  try {
    const parsed = JSON.parse(event.content || "{}");
    const display = {...parsed};
    if (display.files) display.files = `[${display.files.length} files]`;
    contentPreview = JSON.stringify(display, null, 2);
  } catch (e) {
    contentPreview = event.content || "";
  }
  const eventId = event.id || "?";
  return `<div class="nostr-event-block nostr-event-${cls}">
    <div class="nostr-event-kind">${escapeHtml(title)}</div>
    <div class="nostr-event-meta">
      <span>id: <code>${escapeHtml(eventId.slice(0,16))}\u2026</code></span>
      <span>time: ${escapeHtml(time)}</span>
      ${paramTags ? `<span>params: ${escapeHtml(paramTags)}</span>` : ""}
      <a href="https://njump.me/${escapeHtml(eventId)}" target="_blank" rel="noopener" class="detail-link">njump \u2197</a>
    </div>
    <pre class="nostr-event-content">${escapeHtml(contentPreview)}</pre>
  </div>`;
}

function metric(value, label, cls) {
  return `<div class="metric metric-${cls || ""}">
    <span class="metric-value">${value}</span>
    <span class="metric-label">${escapeHtml(label)}</span>
  </div>`;
}

function metaItem(label, valueHtml) {
  return `<div class="meta-item">
    <span class="meta-label">${escapeHtml(label)}</span>
    <span class="meta-value">${valueHtml}</span>
  </div>`;
}

function renderScreenshots(screenshots) {
  if (screenshots.length === 0) {
    return `<p class="section-empty">No screenshots in this run.</p>`;
  }
  return `<div class="shot-grid">` + screenshots.map((s) => `
    <div class="shot-card">
      <img class="shot-thumb"
           data-src="${escapeHtml(s.url)}"
           data-full-url="${escapeHtml(s.url)}"
           data-filename="${escapeHtml(s.path)}"
           alt="${escapeHtml(s.path)}">
      <div class="shot-name" title="${escapeHtml(s.path)}">${escapeHtml(s.path)}</div>
    </div>
  `).join("") + `</div>`;
}

function renderFileList(files) {
  if (files.length === 0) {
    return `<p class="section-empty">No additional files in this run.</p>`;
  }
  const rows = files.map((f, i) => {
    const name = (f.path || "").split("/").pop() || f.url;
    const icon = fileIcon(f.mime);
    return `<div class="file-row" data-blossom-url="${escapeHtml(f.url)}" data-file-idx="${i}">
      <span class="file-icon">${icon}</span>
      <span class="file-name" title="${escapeHtml(f.path)}">${escapeHtml(name)}</span>
      <span class="file-path">${escapeHtml(f.path)}</span>
      <span class="file-size">${escapeHtml(formatBytes(f.size))}</span>
      <span class="file-ext">${escapeHtml(extOf(f.path || name))}</span>
      <span class="file-status file-status-checking" title="Checking availability...">...</span>
      <a class="file-download" href="${escapeHtml(f.url)}" target="_blank" rel="noopener" title="Open">open</a>
    </div>`;
  }).join("");
  return `<div class="file-list" data-file-list>${rows}</div>`;
}

function checkBlobAvailability(container) {
  if (!container) return;
  const rows = container.querySelectorAll(".file-row[data-blossom-url]");
  rows.forEach((row) => {
    const url = row.dataset.blossomUrl;
    const status = row.querySelector(".file-status");
    if (!url || !status) return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    fetch(url, { method: "GET", headers: { "Range": "bytes=0-0" }, signal: ctrl.signal })
      .then((resp) => {
        clearTimeout(timer);
        if (resp.ok || resp.status === 206) {
          status.className = "file-status file-status-ok";
          status.textContent = "ok";
          status.title = "Available";
        } else {
          status.className = "file-status file-status-gone";
          status.textContent = "gone";
          status.title = `HTTP ${resp.status}`;
        }
      })
      .catch(() => {
        clearTimeout(timer);
        status.className = "file-status file-status-gone";
        status.textContent = "gone";
        status.title = "Request failed";
      });
  });
}

function fileIcon(mime) {
  if (!mime) return "\u{1F4C4}";
  if (mime.includes("html")) return "\u{1F4C4}";
  if (mime.includes("json")) return "{}";
  if (mime.includes("xml")) return "\u{1F4C4}";
  if (mime.includes("text")) return "\u{1F4C4}";
  if (mime.includes("image")) return "\u{1F5BC}";
  return "\u{1F4C4}";
}

function extOf(path) {
  const dot = path.lastIndexOf(".");
  return dot >= 0 ? path.slice(dot + 1).toUpperCase() : "?";
}

// ===========================================================================
// Lightbox
// ===========================================================================

function openLightbox(url, filename) {
  const lb = document.getElementById("lightbox");
  const img = lb.querySelector(".lightbox-img");
  const open = lb.querySelector(".lightbox-open");
  img.src = url;
  open.href = url;
  lb.hidden = false;
  requestAnimationFrame(() => lb.classList.add("open"));
}

function closeLightbox() {
  const lb = document.getElementById("lightbox");
  lb.classList.remove("open");
  setTimeout(() => { lb.hidden = true; }, 200);
}

// ===========================================================================
// Error states
// ===========================================================================

function showGlobalError(message) {
  const container = document.getElementById("runs-scroll") || document.getElementById("runs-list");
  if (!container) return;
  container.innerHTML = `
    <div class="runs-empty">
      <div class="runs-empty-icon">\u26A0</div>
      <p>${escapeHtml(message)}</p>
      <p class="hint">Check your connection and try<br>refreshing the page.</p>
    </div>`;
}

// ===========================================================================
// View switching: Test Runs / CVM Jobs / CVM Services
// ===========================================================================

const BACK_ARROW_SVG = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>`;

let currentView = "runs";

const CVM_JOBS_CACHE_KEY = "prta:cvm-jobs:v1";
const CVM_JOBS_MAX = 200;
let cvmJobs = [];
let cvmJobsById = new Map();
let cvmJobsSockets = [];
let cvmJobsConnected = 0;
let cvmJobsSubStarted = false;
let cvmJobsRenderTimer = null;
let selectedJobId = null;
const cvmJobsFilter = { search: "", dir: "all" };

let cvmServices = new Map();
let cvmServicesFetched = false;
let selectedServicePubkey = null;

function switchView(view) {
  if (view === currentView) return;
  currentView = view;

  document.querySelectorAll(".view-toggle-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });

  const main = document.querySelector("main");
  const aside = document.getElementById("runs-list");
  document.getElementById("app").classList.remove("mobile-view-detail");

  if (view === "runs") {
    main.classList.remove("view-services");
    aside.style.display = "";
    buildSidebar();
    renderRunsList();
    const sel = allRuns.find((r) => r.runId === selectedRunId);
    if (sel) selectRun(sel);
    else selectRunFromHash();
  } else if (view === "jobs") {
    main.classList.remove("view-services");
    aside.style.display = "";
    buildCvmJobsSidebar();
    startCvmJobsSubscription();
    renderCvmJobsList();
    showCvmJobsPlaceholder();
  } else if (view === "services") {
    main.classList.add("view-services");
    aside.style.display = "none";
    fetchCvmServices();
  }
}

// ===========================================================================
// JSON viewer (renderJson) — shared by CVM Jobs + Services
// ===========================================================================

const jvDataStore = new Map();

function renderJson(obj) {
  const id = "jv-" + Math.random().toString(36).slice(2, 10);
  let pretty;
  try { pretty = JSON.stringify(obj, null, 2); } catch (e) { pretty = String(obj); }
  jvDataStore.set(id, pretty);
  return `<div class="json-viewer"><button class="jv-copy" type="button" data-jv="${id}">Copy</button><div class="jv-root">${jsonValueHtml(obj, 0)}</div></div>`;
}

function jsonValueHtml(val, depth) {
  if (val === null) return `<span class="jv-null">null</span>`;
  if (typeof val === "boolean") return `<span class="jv-bool">${val}</span>`;
  if (typeof val === "number") return `<span class="jv-num">${val}</span>`;
  if (typeof val === "string") return jvStr(val);
  if (Array.isArray(val)) {
    if (val.length === 0) return `<span class="jv-bracket">[]</span>`;
    return jsonArrayHtml(val, depth);
  }
  if (typeof val === "object") {
    const keys = Object.keys(val);
    if (keys.length === 0) return `<span class="jv-bracket">{}</span>`;
    return jsonObjectHtml(val, keys, depth);
  }
  return escapeHtml(String(val));
}

function jvStr(s) {
  const max = 280;
  let d = s;
  if (s.length > max) d = s.slice(0, max) + "\u2026 [" + s.length + " chars]";
  return `<span class="jv-str">"${escapeHtml(d)}"</span>`;
}

function jsonObjectHtml(obj, keys, depth) {
  const items = keys.map((k, i) => {
    const comma = i < keys.length - 1 ? `<span class="jv-colon">,</span>` : "";
    return `<div class="jv-line"><span class="jv-key">"${escapeHtml(k)}"</span><span class="jv-colon">: </span>${jsonValueHtml(obj[k], depth + 1)}${comma}</div>`;
  }).join("");
  const openAttr = depth < 2 ? " open" : "";
  return `<details class="jv-block"${openAttr}><summary class="jv-summary">{<span class="jv-bracket"> ${keys.length} key${keys.length !== 1 ? "s" : ""}</span>}</summary><div class="jv-content">${items}<div class="jv-line"><span class="jv-bracket">}</span></div></div></details>`;
}

function jsonArrayHtml(arr, depth) {
  const items = arr.map((v, i) => {
    const comma = i < arr.length - 1 ? `<span class="jv-colon">,</span>` : "";
    return `<div class="jv-line"><span class="jv-colon">${i}: </span>${jsonValueHtml(v, depth + 1)}${comma}</div>`;
  }).join("");
  const openAttr = depth < 2 ? " open" : "";
  return `<details class="jv-block"${openAttr}><summary class="jv-summary">[<span class="jv-bracket"> ${arr.length} </span>]</summary><div class="jv-content">${items}<div class="jv-line"><span class="jv-bracket">]</span></div></div></details>`;
}

// ===========================================================================
// CVM Jobs — kind 25910 (ephemeral MCP JSON-RPC) live feed
// ===========================================================================

function dirArrow(d) {
  return d === "request" ? "\u2192" : d === "response" ? "\u2190" : "\u2022";
}

function parseCvmJob(event) {
  const tags = event.tags || [];
  let payload = null;
  try { payload = JSON.parse(event.content || "{}"); } catch (e) { /* non-JSON */ }
  const serverPubkey = getTag(tags, "p");
  const correlationId = getTag(tags, "e");
  let direction = "unknown";
  let method = null;
  if (payload) {
    if (payload.method) {
      direction = "request";
      method = payload.method;
    } else if ("result" in payload || "error" in payload) {
      direction = "response";
    }
  }
  let toolName = null;
  if (payload && payload.params) {
    if (typeof payload.params.name === "string") toolName = payload.params.name;
    else if (typeof payload.params === "string") toolName = payload.params;
  }
  return {
    requestId: event.id,
    timestamp: event.created_at,
    direction,
    method,
    toolName,
    serverPubkey,
    correlationId,
    payload,
    raw: event,
  };
}

function startCvmJobsSubscription() {
  if (cvmJobsSubStarted) return;
  cvmJobsSubStarted = true;

  try {
    const cached = JSON.parse(localStorage.getItem(CVM_JOBS_CACHE_KEY) || "[]");
    if (Array.isArray(cached)) {
      cvmJobs = cached.slice(0, CVM_JOBS_MAX);
      cvmJobsById = new Map(cvmJobs.map((j) => [j.requestId, j]));
    }
  } catch (e) { /* ignore */ }

  cvmJobsSockets = [];
  cvmJobsConnected = 0;

  RELAYS.forEach((relayUrl) => {
    let ws;
    try { ws = new WebSocket(relayUrl); } catch (e) { return; }
    const subId = "cvm-jobs-" + Math.random().toString(36).slice(2, 8);

    ws.onopen = () => {
      cvmJobsSockets.push(ws);
      cvmJobsConnected++;
      updateCvmJobsLive();
      ws.send(JSON.stringify(["REQ", subId, { kinds: [25910], limit: 100 }]));
    };

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data[0] === "EVENT" && data[2] && data[2].kind === 25910) {
          handleCvmJobEvent(data[2]);
        }
      } catch (e) { /* ignore */ }
    };

    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onclose = () => {
      const i = cvmJobsSockets.indexOf(ws);
      if (i >= 0) cvmJobsSockets.splice(i, 1);
      cvmJobsConnected = Math.max(0, cvmJobsConnected - 1);
      updateCvmJobsLive();
    };
  });
}

function handleCvmJobEvent(event) {
  if (cvmJobsById.has(event.id)) return;
  const job = parseCvmJob(event);
  cvmJobs.unshift(job);
  cvmJobsById.set(event.id, job);
  while (cvmJobs.length > CVM_JOBS_MAX) {
    const removed = cvmJobs.pop();
    cvmJobsById.delete(removed.requestId);
  }
  saveCvmJobsCache();
  scheduleCvmJobsRender();
}

function scheduleCvmJobsRender() {
  if (cvmJobsRenderTimer) return;
  cvmJobsRenderTimer = setTimeout(() => {
    cvmJobsRenderTimer = null;
    if (currentView === "jobs") renderCvmJobsList();
  }, 250);
}

function saveCvmJobsCache() {
  try {
    localStorage.setItem(CVM_JOBS_CACHE_KEY, JSON.stringify(cvmJobs.slice(0, CVM_JOBS_MAX)));
  } catch (e) { /* quota */ }
}

function updateCvmJobsLive() {
  const el = document.getElementById("cvm-jobs-live");
  if (!el) return;
  const txt = el.querySelector(".live-text");
  if (cvmJobsConnected > 0) {
    el.className = "live-indicator live";
    el.title = "Live \u2014 " + cvmJobsConnected + "/" + RELAYS.length + " relays";
    if (txt) txt.textContent = "Live";
  } else {
    el.className = "live-indicator idle";
    el.title = "Connecting\u2026";
    if (txt) txt.textContent = "Connecting\u2026";
  }
}

function buildCvmJobsSidebar() {
  const aside = document.getElementById("runs-list");
  aside.innerHTML = `
    <div class="sidebar-controls">
      <div class="cvm-live-bar">
        <span id="cvm-jobs-live" class="live-indicator idle"><span class="live-dot"></span><span class="live-text">Connecting\u2026</span></span>
        <span id="cvm-jobs-count" class="cvm-count">0 events</span>
      </div>
      <input type="text" id="cvm-jobs-search" class="search-input" placeholder="Filter by method or server\u2026" autocomplete="off" />
      <div class="filter-toggles">
        <button class="filter-btn active" data-jfilter="all" type="button">All</button>
        <button class="filter-btn" data-jfilter="request" type="button">Requests \u2192</button>
        <button class="filter-btn" data-jfilter="response" type="button">Responses \u2190</button>
      </div>
    </div>
    <div class="runs-scroll" id="cvm-jobs-scroll"></div>
  `;

  const search = document.getElementById("cvm-jobs-search");
  if (search) {
    let t = null;
    search.addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(() => {
        cvmJobsFilter.search = e.target.value.toLowerCase().trim();
        renderCvmJobsList();
      }, 200);
    });
  }

  aside.querySelectorAll(".filter-btn[data-jfilter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      aside.querySelectorAll(".filter-btn[data-jfilter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      cvmJobsFilter.dir = btn.dataset.jfilter;
      renderCvmJobsList();
    });
  });

  updateCvmJobsLive();
}

function renderCvmJobsList() {
  const container = document.getElementById("cvm-jobs-scroll");
  if (!container) return;
  const countEl = document.getElementById("cvm-jobs-count");

  let jobs = cvmJobs.slice();
  if (cvmJobsFilter.dir !== "all") jobs = jobs.filter((j) => j.direction === cvmJobsFilter.dir);
  if (cvmJobsFilter.search) {
    const q = cvmJobsFilter.search;
    jobs = jobs.filter((j) => {
      const hay = [
        j.method, j.toolName, j.serverPubkey,
        j.serverPubkey ? shortNpub(j.serverPubkey) : "",
      ].filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
  }

  if (countEl) countEl.textContent = jobs.length + " event" + (jobs.length !== 1 ? "s" : "");
  container.innerHTML = "";

  if (jobs.length === 0) {
    const live = cvmJobs.length > 0;
    container.innerHTML = `<div class="no-match">${live ? "No events match your filters." : "Waiting for events\u2026"}<span class="hint">Live kind 25910 MCP JSON-RPC feed.<br>Events accumulate in memory as they arrive.</span></div>`;
    return;
  }

  jobs.forEach((job, i) => {
    const card = document.createElement("div");
    card.className = "cvm-job-item";
    card.dataset.jobId = job.requestId;
    if (job.requestId === selectedJobId) card.classList.add("active");
    if (i < 10) card.style.animationDelay = (i * 25) + "ms";
    else { card.style.animationDelay = "0ms"; card.style.animationDuration = "0s"; }

    const label = job.method
      ? (job.toolName ? `${job.method}: ${job.toolName}` : job.method)
      : (job.direction === "response" ? "response" : "event");

    card.innerHTML = `
      <div class="cvm-job-top">
        <span class="cvm-dir cvm-dir-${job.direction}" title="${escapeHtml(job.direction)}">${dirArrow(job.direction)}</span>
        <span class="cvm-method" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
      </div>
      <div class="cvm-job-meta">
        <span class="cvm-job-pubkey" title="${escapeHtml(job.serverPubkey || "")}">${job.serverPubkey ? escapeHtml(shortNpub(job.serverPubkey)) : "\u2014"}</span>
        <span class="cvm-job-time">${escapeHtml(formatRelative(job.timestamp))}</span>
      </div>
    `;

    card.addEventListener("click", () => selectCvmJob(job.requestId));
    container.appendChild(card);
  });
}

function showCvmJobsPlaceholder() {
  const view = document.getElementById("run-view");
  view.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon empty-icon-arrow">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 12h16M14 6l6 6-6 6"/>
        </svg>
      </div>
      <p class="empty-title">Select a CVM job</p>
      <p class="hint">Live kind 25910 MCP JSON-RPC feed.<br>Click an event to inspect its payload, params and correlated response.</p>
    </div>
  `;
}

function renderCvmJobResultMedia(payload) {
  if (!payload) return "";
  const result = payload.result;
  if (!result) return "";
  const content = Array.isArray(result.content)
    ? result.content
    : (Array.isArray(result) ? result : null);
  if (!content) return "";

  const parts = [];
  for (const item of content) {
    if (!item || typeof item !== "object") continue;
    if (item.type === "image" && item.data) {
      const mime = item.mimeType || item.mime || "image/png";
      parts.push(`<div class="cvm-result-media"><img alt="result image" src="data:${escapeHtml(mime)};base64,${escapeHtml(item.data)}"></div>`);
    } else if (item.type === "text" && item.text != null) {
      parts.push(`<div class="cvm-result-text">${escapeHtml(item.text)}</div>`);
    } else if (item.type === "resource" && item.resource && item.resource.text != null) {
      parts.push(`<div class="cvm-result-text">${escapeHtml(item.resource.text)}</div>`);
    }
  }
  return parts.join("");
}

function selectCvmJob(jobId) {
  selectedJobId = jobId;
  const job = cvmJobsById.get(jobId);
  document.querySelectorAll(".cvm-job-item").forEach((el) => el.classList.toggle("active", el.dataset.jobId === jobId));

  const view = document.getElementById("run-view");
  view.scrollTop = 0;

  if (window.innerWidth <= 768) document.getElementById("app").classList.add("mobile-view-detail");

  if (!job) {
    view.innerHTML = `<div class="empty-state"><p class="empty-title">Job not found</p></div>`;
    return;
  }

  const payload = job.payload || {};

  let correlated = null;
  if (job.direction === "response") {
    if (job.correlationId) correlated = cvmJobsById.get(job.correlationId) || null;
    if (!correlated && payload && payload.id != null) {
      correlated = cvmJobs.find((j) => j.direction === "request" && j.payload && j.payload.id === payload.id) || null;
    }
  } else if (job.direction === "request") {
    if (job.correlationId) correlated = cvmJobsById.get(job.correlationId) || null;
    if (!correlated) correlated = cvmJobs.find((j) => j.correlationId === job.requestId) || null;
  }

  const metaItems = [];
  metaItems.push(metaItem("Direction", escapeHtml(job.direction)));
  if (job.method) metaItems.push(metaItem("Method", escapeHtml(job.method)));
  if (job.toolName) metaItems.push(metaItem("Tool", escapeHtml(job.toolName)));
  if (job.serverPubkey) metaItems.push(metaItem("Server", `<code class="runner-code">${escapeHtml(shortNpub(job.serverPubkey))}</code>`));
  if (job.correlationId) metaItems.push(metaItem("Correlation", `<code>${escapeHtml(job.correlationId.slice(0, 16))}\u2026</code>`));
  metaItems.push(metaItem("Time", escapeHtml(formatTimestamp(job.timestamp))));

  const titleText = job.method
    ? (job.toolName ? `${job.method}: ${job.toolName}` : job.method)
    : (job.direction === "response" ? "Response" : "Event");

  view.innerHTML = `
    <div class="cvm-detail-header">
      <button id="cvm-back" class="back-to-list" type="button" aria-label="Back to jobs">${BACK_ARROW_SVG}<span>Jobs</span></button>
      <div class="detail-titles">
        <div class="detail-run">
          <span class="cvm-dir cvm-dir-${job.direction}">${dirArrow(job.direction)}</span>
          <span class="run-id-lg">${escapeHtml(titleText)}</span>
        </div>
      </div>
      <div class="detail-meta-grid">${metaItems.join("")}</div>
      <div class="detail-links">
        <a href="https://njump.me/${escapeHtml(job.requestId)}" target="_blank" class="detail-link" rel="noopener">Nostr event \u2197</a>
      </div>
    </div>
    <div class="cvm-detail">
      ${renderCvmJobResultMedia(payload)}
      ${correlated ? `<div class="section-title">Correlated ${escapeHtml(correlated.direction)} <span class="section-count">${escapeHtml(correlated.method || "")}</span></div>${renderJson(correlated.payload || {})}` : ""}
      <div class="section-title">JSON-RPC Payload</div>
      ${renderJson(payload)}
      <div class="section-title" style="margin-top:24px">Raw Event</div>
      ${renderJson(job.raw)}
    </div>
  `;

  const back = view.querySelector("#cvm-back");
  if (back) {
    back.addEventListener("click", () => {
      document.getElementById("app").classList.remove("mobile-view-detail");
      selectedJobId = null;
      document.querySelectorAll(".cvm-job-item").forEach((el) => el.classList.remove("active"));
    });
  }
}

// ===========================================================================
// CVM Services — kind 11316 (server directory)
// ===========================================================================

function parseCvmService(event) {
  let info = {};
  try { info = JSON.parse(event.content || "{}"); } catch (e) { /* non-JSON */ }
  const name = info.name || info.server_name || info.serverName || shortNpub(event.pubkey);
  const description = info.description || info.about || info.summary || "";
  return {
    pubkey: event.pubkey,
    name,
    description,
    info,
    timestamp: event.created_at,
    raw: event,
  };
}

function fetchCvmServices() {
  renderCvmServicesLoading();
  if (cvmServicesFetched) { renderCvmServicesGrid(); return; }

  const events = new Map();
  let closed = 0;
  let resolved = false;

  const finish = () => {
    if (resolved) return;
    resolved = true;
    for (const evt of events.values()) {
      cvmServices.set(evt.pubkey, parseCvmService(evt));
    }
    cvmServicesFetched = true;
    if (currentView === "services") renderCvmServicesGrid();
  };

  const timeout = setTimeout(finish, FETCH_TIMEOUT_MS);

  RELAYS.forEach((relayUrl) => {
    let ws;
    try { ws = new WebSocket(relayUrl); } catch (e) { closed++; checkDone(); return; }
    const subId = "cvm-svc-" + Math.random().toString(36).slice(2, 8);

    ws.onopen = () => {
      ws.send(JSON.stringify(["REQ", subId, { kinds: [11316] }]));
    };

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data[0] === "EVENT" && data[2] && data[2].kind === 11316) {
          const evt = data[2];
          const ex = events.get(evt.pubkey);
          if (!ex || evt.created_at > ex.created_at) events.set(evt.pubkey, evt);
        } else if (data[0] === "EOSE" && data[1] === subId) {
          ws.send(JSON.stringify(["CLOSE", subId]));
          ws.close();
        }
      } catch (e) { /* ignore */ }
    };

    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onclose = () => { closed++; checkDone(); };
  });

  function checkDone() {
    if (closed >= RELAYS.length && !resolved) {
      clearTimeout(timeout);
      finish();
    }
  }
}

function renderCvmServicesLoading() {
  const view = document.getElementById("run-view");
  view.innerHTML = `
    <div class="empty-state">
      <div class="connecting">
        <div class="spinner"></div>
        <p>Discovering CVM services\u2026</p>
      </div>
    </div>
  `;
}

function renderCvmServicesGrid() {
  const view = document.getElementById("run-view");
  view.scrollTop = 0;
  const services = [...cvmServices.values()].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));

  if (services.length === 0) {
    view.innerHTML = `
      <div class="empty-state">
        <p class="empty-title">No CVM services found</p>
        <p class="hint">Waiting for kind 11316 server announcements from relays.</p>
      </div>
    `;
    return;
  }

  view.innerHTML = `
    <div class="cvm-services-wrap">
      <div class="cvm-services-header">
        <h2>ContextVM Services</h2>
        <p>${services.length} server${services.length !== 1 ? "s" : ""} discovered \u00b7 kind 11316</p>
      </div>
      <div class="cvm-services-grid">
        ${services.map((s) => `
          <div class="cvm-service-card" data-pubkey="${escapeHtml(s.pubkey)}">
            <div class="cvm-service-name">${escapeHtml(s.name)}</div>
            ${s.description ? `<div class="cvm-service-desc">${escapeHtml(s.description)}</div>` : ""}
            <div class="cvm-service-foot">
              <span class="cvm-service-pubkey" title="${escapeHtml(s.pubkey)}">${escapeHtml(shortNpub(s.pubkey))}</span>
              <span class="cvm-service-pubkey">${escapeHtml(formatRelative(s.timestamp))}</span>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;

  view.querySelectorAll(".cvm-service-card").forEach((card) => {
    card.addEventListener("click", () => selectCvmService(card.dataset.pubkey));
  });
}

function selectCvmService(pubkey) {
  selectedServicePubkey = pubkey;
  const svc = cvmServices.get(pubkey);
  if (!svc) return;

  const view = document.getElementById("run-view");
  view.scrollTop = 0;
  if (window.innerWidth <= 768) document.getElementById("app").classList.add("mobile-view-detail");

  const metaItems = [];
  metaItems.push(metaItem("Pubkey", `<code class="runner-code">${escapeHtml(shortNpub(pubkey))}</code>`));
  if (svc.description) metaItems.push(metaItem("Description", escapeHtml(svc.description)));
  metaItems.push(metaItem("Last seen", escapeHtml(formatRelative(svc.timestamp))));
  metaItems.push(metaItem("Announced", escapeHtml(formatTimestamp(svc.timestamp))));

  view.innerHTML = `
    <div class="cvm-detail-header">
      <button id="cvm-svc-back" class="back-to-list" type="button" aria-label="Back to services">${BACK_ARROW_SVG}<span>Services</span></button>
      <div class="detail-titles">
        <div class="detail-run">
          <span class="run-id-lg">${escapeHtml(svc.name)}</span>
        </div>
      </div>
      <div class="detail-meta-grid">${metaItems.join("")}</div>
    </div>
    <div class="cvm-detail">
      <div id="cvm-svc-extra" class="section-empty">Loading tools, resources and relays\u2026</div>
      <div class="section-title" style="margin-top:24px">Server Info</div>
      ${renderJson(svc.info)}
      <div class="section-title" style="margin-top:24px">Raw Event</div>
      ${renderJson(svc.raw)}
    </div>
  `;

  const back = view.querySelector("#cvm-svc-back");
  if (back) {
    back.addEventListener("click", () => {
      document.getElementById("app").classList.remove("mobile-view-detail");
      selectedServicePubkey = null;
      renderCvmServicesGrid();
    });
  }

  fetchCvmServiceExtra(pubkey);
}

function fetchCvmServiceExtra(pubkey) {
  const got = { tools: null, resources: null, relays: null };
  let closed = 0;
  let done = false;

  const finish = () => {
    if (done) return;
    done = true;
    renderCvmServiceExtra(got);
  };
  const timeout = setTimeout(finish, 9000);

  RELAYS.forEach((relayUrl) => {
    let ws;
    try { ws = new WebSocket(relayUrl); } catch (e) { closed++; checkDone(); return; }
    const subId = "cvm-extra-" + Math.random().toString(36).slice(2, 8);

    ws.onopen = () => {
      ws.send(JSON.stringify(["REQ", subId, { kinds: [11317, 11318, 10002], authors: [pubkey] }]));
    };

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data[0] === "EVENT" && data[2]) {
          const e = data[2];
          if (e.kind === 11317 && (!got.tools || e.created_at > got.tools.created_at)) got.tools = e;
          else if (e.kind === 11318 && (!got.resources || e.created_at > got.resources.created_at)) got.resources = e;
          else if (e.kind === 10002 && (!got.relays || e.created_at > got.relays.created_at)) got.relays = e;
        } else if (data[0] === "EOSE" && data[1] === subId) {
          ws.send(JSON.stringify(["CLOSE", subId]));
          ws.close();
        }
      } catch (e) { /* ignore */ }
    };

    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onclose = () => { closed++; checkDone(); };
  });

  function checkDone() {
    if (closed >= RELAYS.length && !done) {
      clearTimeout(timeout);
      finish();
    }
  }
}

function renderCvmServiceExtra(got) {
  const el = document.getElementById("cvm-svc-extra");
  if (!el) return;
  const parts = [];

  let tools = [];
  if (got.tools) {
    try {
      const c = JSON.parse(got.tools.content || "[]");
      tools = Array.isArray(c) ? c : (c.tools || []);
    } catch (e) { /* ignore */ }
  }
  if (tools.length) {
    parts.push(`<div class="section-title">Tools <span class="section-count">${tools.length}</span></div>`);
    parts.push(`<div class="cvm-tools-list">${tools.map((t) => `
      <div class="cvm-tool">
        <span class="cvm-tool-name">${escapeHtml(t.name || "?")}</span>
        ${t.description ? `<span class="cvm-tool-desc">${escapeHtml(t.description)}</span>` : ""}
      </div>
    `).join("")}</div>`);
  }

  let resources = [];
  if (got.resources) {
    try {
      const c = JSON.parse(got.resources.content || "[]");
      resources = Array.isArray(c) ? c : (c.resources || []);
    } catch (e) { /* ignore */ }
  }
  if (resources.length) {
    parts.push(`<div class="section-title">Resources <span class="section-count">${resources.length}</span></div>`);
    parts.push(renderJson(resources));
  }

  let relays = [];
  if (got.relays) {
    relays = (got.relays.tags || []).filter((tg) => tg[0] === "r").map((tg) => tg[1]);
  }
  if (relays.length) {
    parts.push(`<div class="section-title">Relays <span class="section-count">${relays.length}</span></div>`);
    parts.push(`<div class="relay-list">${relays.map((r) => `<span class="relay-chip">${escapeHtml(r)}</span>`).join("")}</div>`);
  }

  if (!parts.length) {
    el.className = "section-empty";
    el.textContent = "No tools, resources or relay metadata found for this server.";
    return;
  }
  el.className = "";
  el.innerHTML = parts.join("");
}

// ===========================================================================
// Init
// ===========================================================================

(async function init() {
  console.log("[PRTA] Initializing\u2026");
  console.log("[PRTA] Relays:", RELAYS);
  console.log("[PRTA] Fetching [5900, 6900, 7000, 1063] DVM + [30078] run summaries from all pubkeys");

  document.querySelectorAll(".view-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".jv-copy");
    if (!btn) return;
    const text = jvDataStore.get(btn.dataset.jv);
    if (text == null) return;
    const markDone = () => {
      btn.textContent = "Copied!";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(markDone).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); markDone(); } catch (err) {}
      document.body.removeChild(ta);
    }
  });

  const lb = document.getElementById("lightbox");
  lb.querySelector(".lightbox-backdrop").addEventListener("click", closeLightbox);
  lb.querySelector(".lightbox-close").addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeLightbox();
      closeHtmlViewer();
    }
  });

  const hv = document.getElementById("html-viewer");
  if (hv) {
    hv.querySelector(".html-viewer-backdrop").addEventListener("click", closeHtmlViewer);
    hv.querySelector(".html-viewer-close").addEventListener("click", closeHtmlViewer);
  }

  window.addEventListener("popstate", () => {
    if (location.hash) {
      selectRunFromHash();
    } else {
      showPlaceholder();
    }
  });

  const menuBtn = document.getElementById("menu-toggle");
  const backdrop = document.getElementById("sidebar-backdrop");
  const app = document.getElementById("app");
  if (menuBtn) {
    menuBtn.addEventListener("click", () => app.classList.toggle("sidebar-open"));
  }
  if (backdrop) {
    backdrop.addEventListener("click", () => app.classList.remove("sidebar-open"));
  }

  buildSidebar();

  const scrollEl = document.getElementById("runs-scroll");
  if (scrollEl) {
    scrollEl.innerHTML = `
      <div class="loading">
        <div class="connecting">
          <div class="spinner"></div>
          <p>Connecting to relays\u2026</p>
          <div class="relay-list">${RELAYS.map((r) => `<span class="relay-chip">${r.replace("wss://", "")}</span>`).join("")}</div>
        </div>
      </div>`;
  }

  const cached = loadCachedRuns();
  if (cached && cached.length > 0) {
    allRuns = cached;
    populateRunnerFilter();
    renderRunsList();
    console.log("[PRTA] Rendered " + cached.length + " runs from cache (instant)");
    selectRunFromHash();
  }

  try {
    const [dvmResult, k30078Events] = await Promise.all([
      fetchDvmEvents([5900, 6900, 7000, 1063], 200),
      fetchKind30078Events(200),
    ]);
    const { events, connected } = dvmResult;

    const n5900 = events.filter((e) => e.kind === 5900).length;
    const n6900 = events.filter((e) => e.kind === 6900).length;
    const n7000 = events.filter((e) => e.kind === 7000).length;
    const n1063 = events.filter((e) => e.kind === 1063).length;
    console.log("[PRTA] Connected to " + connected + "/" + RELAYS.length + " relays");
    console.log("[PRTA] DVM events: " + events.length + " (5900: " + n5900 + ", 6900: " + n6900 + ", 7000: " + n7000 + ", 1063: " + n1063 + ")");
    console.log("[PRTA] Kind 30078 events: " + k30078Events.length);

    const fileMeta = buildFileMeta(events);
    console.log("[PRTA] File metadata map: " + fileMeta.size + " entries");

    const feedback = events
      .filter((e) => e.kind === 7000)
      .map(parseFeedbackFromKind7000);

    const dvmRuns = dedupeDvmRuns(events, fileMeta, feedback);
    const k30078Runs = dedupeKind30078Runs(k30078Events, fileMeta);
    const freshRuns = [...dvmRuns, ...k30078Runs].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
    console.log("[PRTA] " + freshRuns.length + " total runs (" + dvmRuns.length + " DVM, " + k30078Runs.length + " kind 30078)");

    if (connected === 0 && events.length === 0 && k30078Events.length === 0) {
      if (!cached || cached.length === 0) {
        showGlobalError("Could not connect to any relay.");
      }
      return;
    }

    if (freshRuns.length > 0 || (!cached || cached.length === 0)) {
      const freshKeys = new Set(freshRuns.map((r) => r.runId + ":" + r.source));
      const keptCached = (cached || []).filter((r) => !freshKeys.has(r.runId + ":" + (r.source || "dvm")));
      allRuns = [...freshRuns, ...keptCached];
      displayIdCache.clear();
      saveCachedRuns(allRuns);
      populateRunnerFilter();
      renderRunsList();
      selectRunFromHash();
    }
  } catch (e) {
    console.error("[PRTA] Init error:", e);
    if (!cached || cached.length === 0) {
      showGlobalError("Initialization failed: " + e.message);
    }
  }

  subscribeToRealtimeUpdates();

  (function showErrorBanner() {
    var KEY = "tollgate_dashboard_errors";
    var q;
    try { q = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return; }
    if (!q.length) return;
    var banner = document.getElementById("error-banner");
    if (!banner) return;
    var recent = q[q.length - 1];
    var count = q.length;
    var text = banner.querySelector(".error-banner-text");
    var age = Math.round((Date.now() - recent.ts) / 1000);
    var ageStr = age < 60 ? age + "s ago" : age < 3600 ? Math.round(age / 60) + "m ago" : Math.round(age / 3600) + "h ago";
    text.textContent = count === 1
      ? "JS error (" + ageStr + "): " + recent.msg
      : count + " JS errors (latest " + ageStr + "): " + recent.msg;
    banner.querySelector(".error-banner-copy").addEventListener("click", function () {
      var dump = q.map(function (e) {
        return "[" + new Date(e.ts).toISOString() + "] " + e.type + ": " + e.msg +
          (e.src ? "\n  at " + e.src : "") +
          (e.stack ? "\n" + e.stack : "");
      }).join("\n\n");
      navigator.clipboard.writeText(dump).then(function () {
        banner.querySelector(".error-banner-copy").textContent = "Copied!";
        setTimeout(function () {
          banner.querySelector(".error-banner-copy").textContent = "Copy";
        }, 2000);
      });
    });
    banner.querySelector(".error-banner-dismiss").addEventListener("click", function () {
      localStorage.removeItem(KEY);
      banner.hidden = true;
    });
    banner.hidden = false;
  })();
})();

async function renderConwrtRun(view, run, myLoadId) {
  var body = view.querySelector(".detail-body");
  if (!body) return;

  var CONWRT_UC = [
    "ssh-hardening","sqm","doh","wireguard-client","nodns","wireguard-server",
    "vpn-node","adguard","guest-wifi","auto-sqm","ssl","travelmate",
    "openclash","tollgate","mwan3","pbr"
  ];

  var conwrtRuns = allRuns.filter(function(r) {
    return getRunProject(r) === "conwrt";
  });

  var ucMap = {};
  CONWRT_UC.forEach(function(uc) { ucMap[uc] = null; });

  conwrtRuns.forEach(function(r) {
    (r.files || []).forEach(function(f) {
      var path = f.path || "";
      var parts = path.split("/");
      if (parts.length >= 2 && ucMap.hasOwnProperty(parts[0]) && !ucMap[parts[0]]) {
        ucMap[parts[0]] = { run: r, files: [] };
      }
      if (parts.length >= 2 && ucMap[parts[0]]) {
        ucMap[parts[0]].files.push(f);
      }
    });
  });

  var passCount = 0;
  CONWRT_UC.forEach(function(uc) { if (ucMap[uc]) passCount++; });

  var html = '<div style="margin-bottom:1.5rem">';
  html += '<div style="font-size:1rem;font-weight:600;margin-bottom:.5rem;color:var(--text)">Use Case Matrix';
  html += ' <span style="color:var(--text-dim);font-size:.8rem;font-weight:400">(' + passCount + '/' + CONWRT_UC.length + ' tested)</span></div>';
  html += '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:.85rem">';
  html += '<thead><tr><th style="text-align:left;padding:.4rem;border-bottom:1px solid var(--border)">Use Case</th>';
  html += '<th style="text-align:left;padding:.4rem;border-bottom:1px solid var(--border)">Status</th>';
  html += '<th style="text-align:left;padding:.4rem;border-bottom:1px solid var(--border)">Evidence</th></tr></thead><tbody>';

  CONWRT_UC.forEach(function(uc) {
    var info = ucMap[uc];
    if (info) {
      var date = new Date(info.run.timestamp * 1000).toLocaleDateString();
      var links = info.files.slice(0, 5).map(function(f) {
        var name = (f.path || "").split("/").pop();
        return '<a href="' + escapeHtml(f.url) + '" target="_blank" rel="noopener" style="display:inline-block;margin-right:.4rem;color:var(--link);text-decoration:none">' + escapeHtml(name) + '</a>';
      }).join("");
      html += '<tr style="border-bottom:1px solid var(--border-dim)">';
      html += '<td style="padding:.4rem"><strong>' + escapeHtml(uc) + '</strong></td>';
      html += '<td style="padding:.4rem"><span style="background:rgba(35,134,54,.2);color:var(--green);padding:2px 8px;border-radius:12px;font-size:.75rem;font-weight:600">PASS</span>';
      html += ' <span style="color:var(--text-dim);font-size:.75rem">' + date + '</span></td>';
      html += '<td style="padding:.4rem">' + links + '</td></tr>';
    } else {
      html += '<tr style="border-bottom:1px solid var(--border-dim)">';
      html += '<td style="padding:.4rem;color:var(--text-dim)">' + escapeHtml(uc) + '</td>';
      html += '<td style="padding:.4rem"><span style="background:rgba(139,148,158,.15);color:var(--text-dim);padding:2px 8px;border-radius:12px;font-size:.75rem">PENDING</span></td>';
      html += '<td style="padding:.4rem;color:var(--text-dim)">—</td></tr>';
    }
  });

  html += '</tbody></table></div></div>';
  html += '<div style="font-size:1rem;font-weight:600;margin-bottom:.5rem;color:var(--text)">Run Details</div>';

  body.innerHTML = html;

  var fileDiv = document.createElement("div");
  body.appendChild(fileDiv);

  var subView = { querySelector: function(sel) {
    if (sel === ".detail-body") return fileDiv;
    return view.querySelector(sel);
  }};
  
  try {
    await renderFipsRun(subView, run, myLoadId);
  } catch(e) {
    fileDiv.innerHTML = '<p style="color:var(--text-dim);padding:1rem">Select a file from the run details above.</p>';
  }
}
