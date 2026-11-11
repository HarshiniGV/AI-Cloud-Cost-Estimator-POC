// exportExcel.js
// Builds the multi-sheet Excel report from the /api/estimate response that
// page.js already holds in state. Nothing here touches the backend.
//
//   Sheet 1  "Summary"  -> monthly / yearly cost per cloud, cost ranking,
//                          star-rating comparison table (AI-generated via the
//                          backend /api/ratings, with a built-in fallback),
//                          "why OCI" notes
//   Sheet 2+ "Azure", "OCI", "AWS", "GCP" -> line-item costing per cloud
//
// Requires:  npm install exceljs

const PROVIDERS = [
  { key: "azure", label: "Azure", color: "0078D4" },
  { key: "oci", label: "OCI", color: "C74634" },
  { key: "aws", label: "AWS", color: "FF9900" },
  { key: "gcp", label: "GCP", color: "34A853" },
];

// ---------------------------------------------------------------------------
// FALLBACK star ratings (1-5), used only when the AI ratings request fails.
// These are opinion-based, NOT calculated from your uploaded sheet.
// ---------------------------------------------------------------------------
const DEFAULT_RATINGS = [
  {
    category: "Oracle Database (Exadata / Autonomous / RAC)",
    azure: 3, oci: 5, aws: 3, gcp: 3,
    why: "Native home of Oracle Database: Exadata, Autonomous DB and RAC run on OCI first-party, with full feature parity.",
  },
  {
    category: "Oracle licence portability (BYOL)",
    azure: 4, oci: 5, aws: 3, gcp: 2,
    why: "Existing Oracle licences can be brought over, and Oracle's own licensing rules are applied most favourably on OCI.",
  },
  {
    category: "Price / performance",
    azure: 3, oci: 5, aws: 3, gcp: 4,
    why: "Low list prices on compute and block storage, with the same price in every region.",
  },
  {
    category: "Data egress cost",
    azure: 2, oci: 5, aws: 2, gcp: 2,
    why: "Large free outbound data allowance each month, then low per-GB rates.",
  },
  {
    category: "Compute flexibility",
    azure: 4, oci: 4, aws: 5, gcp: 4,
    why: "Flexible shapes let you choose OCPU and memory independently, though the instance catalogue is smaller than AWS.",
  },
  {
    category: "Service catalogue breadth",
    azure: 5, oci: 3, aws: 5, gcp: 4,
    why: "Covers the core services well but has a smaller catalogue than AWS or Azure.",
  },
  {
    category: "Kubernetes / containers",
    azure: 4, oci: 3, aws: 4, gcp: 5,
    why: "OKE is solid and the control plane is inexpensive, but GKE is the benchmark.",
  },
  {
    category: "AI / ML & analytics",
    azure: 4, oci: 3, aws: 4, gcp: 5,
    why: "Strong GPU capacity and in-database ML, but fewer managed AI services than the other three.",
  },
  {
    category: "Microsoft / .NET ecosystem fit",
    azure: 5, oci: 3, aws: 3, gcp: 2,
    why: "Windows and SQL Server run fine, but Azure has the tightest Microsoft integration.",
  },
  {
    category: "Hybrid / multi-cloud",
    azure: 5, oci: 4, aws: 4, gcp: 4,
    why: "Dedicated Region and Cloud@Customer for on-prem, plus Oracle Database@Azure/AWS/Google for multi-cloud.",
  },
  {
    category: "Compliance & global footprint",
    azure: 5, oci: 4, aws: 5, gcp: 4,
    why: "Broad compliance coverage, with fewer regions than AWS or Azure.",
  },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const stars = (n) => "\u2605".repeat(n) + "\u2606".repeat(Math.max(0, 5 - n));

const CLOUD_KEYS = ["azure", "oci", "aws", "gcp"];

// Accepts whatever the API returned and gives back clean rows, or null.
function sanitizeRatings(raw) {
  if (!Array.isArray(raw)) return null;
  const clamp = (v) => {
    const n = Math.round(Number(v));
    return Number.isFinite(n) ? Math.min(5, Math.max(1, n)) : null;
  };
  const rows = [];
  for (const r of raw) {
    if (!r || !String(r.category || "").trim()) continue;
    const scores = CLOUD_KEYS.map((k) => clamp(r[k]));
    if (scores.some((s) => s === null)) continue;
    rows.push({
      category: String(r.category).trim(),
      azure: scores[0],
      oci: scores[1],
      aws: scores[2],
      gcp: scores[3],
      why: String(r.why || "").trim(),
    });
  }
  return rows.length >= 3 ? rows : null;
}

// Asks the backend (Gemini) for ratings tailored to this workload.
// Never throws: on any failure the built-in ratings are used instead.
async function fetchAiRatings(result, apiUrl) {
  if (!apiUrl) return null;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 45000);
  try {
    const res = await fetch(`${apiUrl}/api/ratings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result),
      signal: ctrl.signal,
    });
    if (!res.ok) return null;
    const data = await res.json();
    return sanitizeRatings(data.ratings);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const toNum = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return Number.isFinite(n) ? n : null;
};

const fmtVal = (v) =>
  v !== null && typeof v === "object" ? JSON.stringify(v) : String(v);

const joinPairs = (obj, skip = []) =>
  Object.entries(obj || {})
    .filter(([k]) => !skip.includes(k))
    .map(([k, v]) => `${k}: ${fmtVal(v)}`)
    .join(" \u00B7 ");

const moneyFmt = (currency) =>
  !currency || currency === "USD"
    ? "$#,##0.00"
    : `#,##0.00 "${currency}"`;

const argb = (hex) => ({ argb: `FF${hex}` });
const solid = (hex) => ({ type: "pattern", pattern: "solid", fgColor: argb(hex) });
const thin = { style: "thin", color: argb("E2E8F0") };
const border = { top: thin, left: thin, bottom: thin, right: thin };

// Turns each cloud's response shape into one common shape.
function normalize(key, data) {
  if (!data) return { missing: true, rows: [], total: null, currency: "USD", warnings: [] };
  if (data.error) {
    return { error: data.error, rows: [], total: null, currency: "USD", warnings: [] };
  }

  if (key === "azure") {
    const unitLabel = (p) =>
      p ? Object.keys(p).find((k) => k !== "Price Source" && k !== "Source") || "" : "";

    const rows = (data.breakdown || []).map((r) => {
      const source = r.pricing?.["Price Source"] || r.pricing?.["Source"];
      const details = [
        r.inputs && Object.keys(r.inputs).length ? `Inputs: ${joinPairs(r.inputs)}` : "",
        r.pricing ? `Pricing: ${joinPairs(r.pricing, ["Price Source", "Source"])}` : "",
        source ? `Price source: ${source}` : "",
        r.formula ? `Formula: ${r.formula}` : "",
        r.explanation ? `Notes: ${r.explanation}` : "",
      ]
        .filter(Boolean)
        .join("\n");

      return {
        product: r.service || "",
        category: r.sku || "",
        metric: unitLabel(r.pricing),
        calculation: r.calculation || "",
        details,
        monthly: toNum(r.monthly_cost),
      };
    });

    return {
      rows,
      total: toNum(data.total_monthly_cost),
      currency: data.currency || "USD",
      warnings: data.warnings || [],
    };
  }

  // oci / aws / gcp share the same shape
  const rows = (data.report || []).map((r) => ({
    product: r.product || "",
    category: r.category || "",
    metric: r.metric || "",
    calculation: r.calculation || "",
    details: "",
    monthly: toNum(r.cost),
  }));

  return {
    rows,
    total: toNum(data.total),
    currency: "USD",
    warnings: data.warnings || [],
  };
}

// ---------------------------------------------------------------------------
// Summary sheet
// ---------------------------------------------------------------------------
function buildSummary(wb, norm, sourceName, ratings, ratingsAreAi) {
  const ws = wb.addWorksheet("Summary", {
    views: [{ showGridLines: false }],
  });
  ws.columns = [
    { width: 44 },
    { width: 19 },
    { width: 19 },
    { width: 19 },
    { width: 19 },
    { width: 78 },
  ];

  let r = 1;

  // Title
  ws.mergeCells(r, 1, r, 6);
  ws.getCell(r, 1).value = "Cloud Cost Comparison";
  ws.getCell(r, 1).font = { size: 20, bold: true, color: argb("FFFFFF") };
  ws.getCell(r, 1).fill = solid("0F172A");
  ws.getCell(r, 1).alignment = { vertical: "middle", indent: 1 };
  ws.getRow(r).height = 36;
  r++;

  ws.mergeCells(r, 1, r, 6);
  ws.getCell(r, 1).value =
    `Source sheet: ${sourceName || "-"}   |   Generated: ${new Date().toLocaleString()}`;
  ws.getCell(r, 1).font = { size: 10, italic: true, color: argb("64748B") };
  ws.getCell(r, 1).alignment = { indent: 1 };
  r += 2;

  // ---- Section 1: cost table ------------------------------------------------
  const sectionTitle = (text) => {
    ws.mergeCells(r, 1, r, 6);
    const c = ws.getCell(r, 1);
    c.value = text;
    c.font = { size: 13, bold: true, color: argb("0F172A") };
    c.border = { bottom: { style: "medium", color: argb("0F172A") } };
    ws.getRow(r).height = 22;
    r++;
  };

  const headerRow = (labels) => {
    labels.forEach((text, i) => {
      const c = ws.getCell(r, i + 1);
      c.value = text;
      c.font = { bold: true, color: argb("FFFFFF") };
      c.fill = solid("334155");
      c.alignment = {
        vertical: "middle",
        horizontal: i === 0 || i === labels.length - 1 ? "left" : "center",
        wrapText: true,
      };
      c.border = border;
    });
    ws.getRow(r).height = 24;
    r++;
  };

  sectionTitle("1. Cost summary");
  headerRow(["Cloud provider", "Monthly cost", "Yearly cost (x12)", "Cost rank", "vs cheapest", "Status / notes"]);

  // Cheapest-first ranking among providers that returned a usable total.
  const valid = PROVIDERS.map((p) => ({ p, n: norm[p.key] })).filter(
    ({ n }) => !n.error && !n.missing && n.total !== null && n.total > 0
  );
  const sorted = [...valid].sort((a, b) => a.n.total - b.n.total);
  const rank = new Map(sorted.map(({ p }, i) => [p.key, i + 1]));
  const cheapest = sorted.length ? sorted[0].n.total : null;

  PROVIDERS.forEach((p) => {
    const n = norm[p.key];
    const isValid = rank.has(p.key);

    const nameCell = ws.getCell(r, 1);
    nameCell.value = p.label;
    nameCell.font = { bold: true, color: argb(p.color), size: 12 };

    const monthly = ws.getCell(r, 2);
    const yearly = ws.getCell(r, 3);
    const rankCell = ws.getCell(r, 4);
    const delta = ws.getCell(r, 5);
    const note = ws.getCell(r, 6);

    if (isValid) {
      monthly.value = n.total;
      monthly.numFmt = moneyFmt(n.currency);
      yearly.value = { formula: `B${r}*12`, result: n.total * 12 };
      yearly.numFmt = moneyFmt(n.currency);
      rankCell.value = rank.get(p.key);
      delta.value = rank.get(p.key) === 1 ? 0 : n.total / cheapest - 1;
      delta.numFmt = rank.get(p.key) === 1 ? '"Cheapest"' : "+0.0%";
      note.value = n.warnings.length
        ? `${n.warnings.length} warning(s) - see the ${p.label} sheet`
        : "OK";
    } else {
      [monthly, yearly, rankCell, delta].forEach((c) => (c.value = "N/A"));
      note.value = n.error
        ? `Error: ${n.error}`
        : n.missing
        ? "No data returned"
        : "No priced line items";
      note.font = { color: argb("B91C1C") };
    }

    [monthly, yearly, rankCell, delta].forEach((c) => {
      c.alignment = { horizontal: "center", vertical: "middle" };
      c.font = { ...(c.font || {}), bold: true };
    });
    note.alignment = { wrapText: true, vertical: "middle" };
    for (let col = 1; col <= 6; col++) ws.getCell(r, col).border = border;
    if (rank.get(p.key) === 1) {
      for (let col = 1; col <= 6; col++) ws.getCell(r, col).fill = solid("ECFDF5");
    }
    ws.getRow(r).height = 22;
    r++;
  });

  const mixed = PROVIDERS.some((p) => {
    const n = norm[p.key];
    return n && !n.error && !n.missing && n.currency && n.currency !== "USD";
  });
  if (mixed) {
    ws.mergeCells(r, 1, r, 6);
    ws.getCell(r, 1).value =
      "Note: at least one provider is reported in a non-USD currency; the rank and 'vs cheapest' columns compare raw numbers without conversion.";
    ws.getCell(r, 1).font = { size: 9, italic: true, color: argb("B45309") };
    r++;
  }
  r++;

  // ---- Section 2: star ratings ---------------------------------------------
  sectionTitle("2. Service comparison (star ratings)");
  ws.mergeCells(r, 1, r, 6);
  ws.getCell(r, 1).value = ratingsAreAi
    ? "Ratings generated by AI (Gemini) for this workload. They are qualitative opinion, not calculated from your sheet."
    : "AI ratings were unavailable, so default ratings are shown. They are qualitative opinion, not calculated from your sheet.";
  ws.getCell(r, 1).font = { size: 9, italic: true, color: argb("64748B") };
  r++;
  headerRow(["Capability", "Azure", "OCI", "AWS", "GCP", "Why OCI rates this way"]);

  const gold = argb("F59E0B");
  ratings.forEach((row, i) => {
    ws.getCell(r, 1).value = row.category;
    ws.getCell(r, 1).font = { bold: true };
    ws.getCell(r, 1).alignment = { vertical: "middle", wrapText: true };

    ["azure", "oci", "aws", "gcp"].forEach((k, idx) => {
      const c = ws.getCell(r, idx + 2);
      c.value = stars(row[k]);
      c.font = { color: gold, size: 13 };
      c.alignment = { horizontal: "center", vertical: "middle" };
      if (k === "oci") c.fill = solid("FEF2F2");
      // Highlight the best score(s) in each row
      if (row[k] === Math.max(row.azure, row.oci, row.aws, row.gcp)) {
        c.font = { color: gold, size: 13, bold: true };
      }
    });

    const why = ws.getCell(r, 6);
    why.value = row.why;
    why.alignment = { wrapText: true, vertical: "middle" };
    why.font = { size: 10, color: argb("475569") };

    for (let col = 1; col <= 6; col++) {
      ws.getCell(r, col).border = border;
      if (i % 2 === 1 && col !== 3) ws.getCell(r, col).fill = solid("F8FAFC");
    }
    ws.getRow(r).height = 32;
    r++;
  });

  // Overall average row
  const avg = (k) => ratings.reduce((s, x) => s + x[k], 0) / ratings.length;
  ws.getCell(r, 1).value = "Overall average";
  ws.getCell(r, 1).font = { bold: true };
  ["azure", "oci", "aws", "gcp"].forEach((k, idx) => {
    const c = ws.getCell(r, idx + 2);
    const a = avg(k);
    c.value = `${stars(Math.round(a))}  ${a.toFixed(1)}`;
    c.font = { bold: true, color: gold, size: 12 };
    c.alignment = { horizontal: "center", vertical: "middle" };
  });
  ws.getCell(r, 6).value = "Unweighted average of the ratings above.";
  ws.getCell(r, 6).font = { size: 10, color: argb("475569") };
  for (let col = 1; col <= 6; col++) {
    ws.getCell(r, col).border = border;
    ws.getCell(r, col).fill = solid("E2E8F0");
  }
  ws.getRow(r).height = 26;
  r += 2;

  // ---- Section 3: why prefer OCI -------------------------------------------
  sectionTitle("3. Why consider OCI");

  const bullets = [];
  const oci = norm.oci;
  const ociOk = rank.has("oci");
  if (ociOk) {
    const others = sorted.filter(({ p }) => p.key !== "oci");
    if (rank.get("oci") === 1 && others.length) {
      const parts = others.map(
        ({ p, n }) => `${(((n.total - oci.total) / n.total) * 100).toFixed(1)}% cheaper than ${p.label}`
      );
      bullets.push(`For this workload OCI is the lowest-cost option: ${parts.join(", ")}.`);
    } else if (others.length) {
      const top = sorted[0];
      bullets.push(
        `For this workload OCI is not the cheapest: it is ${(((oci.total / top.n.total) - 1) * 100).toFixed(1)}% above ${top.p.label}. The case for OCI rests on the capabilities below.`
      );
    }
  } else {
    bullets.push("OCI pricing was not available for this run, so no cost comparison could be made.");
  }
  const bestOf = (x) => Math.max(x.azure, x.oci, x.aws, x.gcp);
  const strengths = ratings
    .filter((x) => x.oci >= 4 && x.oci >= bestOf(x))
    .sort((a, b) => b.oci - a.oci);
  const gaps = ratings.filter((x) => x.oci <= 3 && bestOf(x) > x.oci);

  strengths.slice(0, 4).forEach((x) =>
    bullets.push(`${x.category} (${x.oci}\u2605 on OCI): ${x.why || "top-rated among the four clouds."}`)
  );
  if (!strengths.length) {
    bullets.push("OCI is not the top-rated cloud on any capability for this workload.");
  }
  if (gaps.length) {
    bullets.push(
      `Trade-offs to weigh: ${gaps
        .slice(0, 5)
        .map((x) => `${x.category} (OCI ${x.oci}\u2605 vs best ${bestOf(x)}\u2605)`)
        .join("; ")}.`
    );
  }
  bullets.forEach((b) => {
    ws.mergeCells(r, 1, r, 6);
    const c = ws.getCell(r, 1);
    c.value = `\u2022  ${b}`;
    c.alignment = { wrapText: true, vertical: "top", indent: 1 };
    ws.getRow(r).height = 32;
    r++;
  });
  r++;

  ws.mergeCells(r, 1, r, 6);
  ws.getCell(r, 1).value =
    "Costs come from the estimate engines for your uploaded sheet. Star ratings are qualitative opinion, not calculated values. Verify final pricing with each provider before committing.";
  ws.getCell(r, 1).font = { size: 9, italic: true, color: argb("64748B") };
  ws.getCell(r, 1).alignment = { wrapText: true, vertical: "top" };
  ws.getRow(r).height = 30;
}

// ---------------------------------------------------------------------------
// Per-cloud sheet
// ---------------------------------------------------------------------------
function buildProviderSheet(wb, p, n) {
  const ws = wb.addWorksheet(p.label, {
    properties: { tabColor: argb(p.color) },
    views: [{ showGridLines: false }],
  });
  ws.columns = [
    { width: 6 },   // A  #
    { width: 36 },  // B  product
    { width: 26 },  // C  category / sku
    { width: 26 },  // D  metric
    { width: 46 },  // E  calculation
    { width: 60 },  // F  details
    { width: 17 },  // G  monthly
    { width: 17 },  // H  yearly
  ];
  const cols = 8;
  const fmt = moneyFmt(n.currency);

  ws.mergeCells(1, 1, 1, cols);
  const title = ws.getCell(1, 1);
  title.value = `${p.label} - Cost Breakdown`;
  title.font = { size: 16, bold: true, color: argb("FFFFFF") };
  title.fill = solid(p.color);
  title.alignment = { vertical: "middle", indent: 1 };
  ws.getRow(1).height = 30;

  let r = 3;

  if (n.error || n.missing) {
    ws.mergeCells(r, 1, r, cols);
    ws.getCell(r, 1).value = n.error
      ? `${p.label} estimate failed: ${n.error}`
      : `No ${p.label} data was returned.`;
    ws.getCell(r, 1).font = { color: argb("B91C1C"), bold: true };
    ws.getCell(r, 1).alignment = { wrapText: true, vertical: "top" };
    ws.getRow(r).height = 40;
    return;
  }

  if (n.warnings.length) {
    n.warnings.forEach((w) => {
      ws.mergeCells(r, 1, r, cols);
      ws.getCell(r, 1).value = `\u26A0 ${w}`;
      ws.getCell(r, 1).font = { color: argb("B45309"), size: 10 };
      ws.getCell(r, 1).alignment = { wrapText: true, vertical: "top" };
      r++;
    });
    r++;
  }

  const headers = ["#", "Product / Service", "Category / SKU", "Metric", "Calculation", "Details", "Monthly cost", "Yearly cost"];
  headers.forEach((h, i) => {
    const c = ws.getCell(r, i + 1);
    c.value = h;
    c.font = { bold: true, color: argb("FFFFFF") };
    c.fill = solid("334155");
    c.alignment = {
      vertical: "middle",
      horizontal: i >= 6 ? "right" : "left",
      wrapText: true,
    };
    c.border = border;
  });
  ws.getRow(r).height = 24;
  const headerRowNum = r;
  r++;

  const first = r;
  if (!n.rows.length) {
    ws.mergeCells(r, 1, r, cols);
    ws.getCell(r, 1).value = "No line items";
    ws.getCell(r, 1).font = { italic: true, color: argb("94A3B8") };
    ws.getCell(r, 1).alignment = { horizontal: "center" };
    r++;
  }

  n.rows.forEach((row, i) => {
    const vals = [i + 1, row.product, row.category, row.metric, row.calculation, row.details];
    vals.forEach((v, idx) => {
      const c = ws.getCell(r, idx + 1);
      c.value = v;
      c.alignment = { wrapText: true, vertical: "top" };
    });
    ws.getCell(r, 2).font = { bold: true };
    ws.getCell(r, 5).font = { size: 10, color: argb("475569") };
    ws.getCell(r, 6).font = { size: 9, color: argb("64748B") };

    const m = ws.getCell(r, 7);
    const y = ws.getCell(r, 8);
    if (row.monthly !== null) {
      m.value = row.monthly;
      y.value = { formula: `G${r}*12`, result: row.monthly * 12 };
    }
    [m, y].forEach((c) => {
      c.numFmt = fmt;
      c.alignment = { horizontal: "right", vertical: "top" };
    });

    for (let col = 1; col <= cols; col++) {
      ws.getCell(r, col).border = border;
      if (i % 2 === 1) ws.getCell(r, col).fill = solid("F8FAFC");
    }
    r++;
  });
  const last = r - 1;

  // Total row (live SUM formulas)
  const sum = n.rows.reduce((s, x) => s + (x.monthly || 0), 0);
  ws.mergeCells(r, 1, r, 6);
  ws.getCell(r, 1).value = "TOTAL";
  ws.getCell(r, 1).alignment = { horizontal: "right" };
  const tm = ws.getCell(r, 7);
  const ty = ws.getCell(r, 8);
  if (n.rows.length) {
    tm.value = { formula: `SUM(G${first}:G${last})`, result: sum };
    ty.value = { formula: `SUM(H${first}:H${last})`, result: sum * 12 };
  } else {
    tm.value = n.total ?? 0;
    ty.value = (n.total ?? 0) * 12;
  }
  [tm, ty].forEach((c) => {
    c.numFmt = fmt;
    c.alignment = { horizontal: "right" };
  });
  for (let col = 1; col <= cols; col++) {
    const c = ws.getCell(r, col);
    c.font = { bold: true, size: 12 };
    c.fill = solid("E2E8F0");
    c.border = { ...border, top: { style: "medium", color: argb("0F172A") } };
  }
  ws.getRow(r).height = 24;

  // Reported total can differ slightly from the line-item sum (rounding, extra
  // charges). Show it when it does, so nothing is silently hidden.
  if (n.rows.length && n.total !== null && Math.abs(n.total - sum) > 0.01) {
    r += 2;
    ws.mergeCells(r, 1, r, cols);
    ws.getCell(r, 1).value =
      `Note: the engine reported a total of ${n.total.toFixed(2)} ${n.currency}, which differs from the line-item sum above.`;
    ws.getCell(r, 1).font = { size: 9, italic: true, color: argb("B45309") };
  }

  ws.views = [{ state: "frozen", ySplit: headerRowNum, showGridLines: false }];
  if (n.rows.length) {
    ws.autoFilter = { from: { row: headerRowNum, column: 1 }, to: { row: last, column: cols } };
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
export async function downloadEstimateExcel(result, sourceFileName, apiUrl) {
  if (!result) throw new Error("No estimate to export yet");

  // Loaded on demand so exceljs isn't in the initial page bundle.
  const ExcelJS = (await import("exceljs")).default;

  const wb = new ExcelJS.Workbook();
  wb.creator = "Cloud Cost Estimator";
  wb.created = new Date();

  const norm = {};
  PROVIDERS.forEach((p) => (norm[p.key] = normalize(p.key, result[p.key])));

  const aiRatings = await fetchAiRatings(result, apiUrl);
  buildSummary(wb, norm, sourceFileName, aiRatings || DEFAULT_RATINGS, !!aiRatings);
  PROVIDERS.forEach((p) => buildProviderSheet(wb, p, norm[p.key]));

  const buffer = await wb.xlsx.writeBuffer();
  const blob = new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });

  const base = (sourceFileName || "estimate").replace(/\.[^.]+$/, "");
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${base}_cloud-cost-comparison.xlsx`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
