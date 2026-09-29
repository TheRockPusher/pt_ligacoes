import cytoscape from "cytoscape";
import type { Core, ElementDefinition, Position, StylesheetJson } from "cytoscape";
import "./graph.css";

const RELATIONSHIP_KINDS = [
  "public_office",
  "directorship",
  "shareholding",
  "employment",
  "professional_activity",
  "membership",
  "part_of",
  "succession",
  "education",
  "family",
] as const;
type RelationshipKind = (typeof RELATIONSHIP_KINDS)[number];

const KIND_FALLBACK_COLOURS: Record<RelationshipKind, string> = {
  public_office: "#46577D",
  directorship: "#542848",
  shareholding: "#7E8745",
  employment: "#7A5A12",
  professional_activity: "#373C09",
  membership: "#5C4749",
  part_of: "#2F5D62",
  succession: "#8A8578",
  education: "#6F8B9B",
  family: "#975B60",
};

/** Money and contact records, in the order their aggregated ties are grouped on the map. */
const EVENT_KINDS = [
  "contract",
  "subsidy",
  "eu_funding",
  "meeting",
  "hearing",
  "gift",
  "hospitality",
  "travel",
] as const;
type EventKind = (typeof EVENT_KINDS)[number];

const ENTITY_KINDS = {
  person: "Pessoa",
  company: "Empresa",
  organisation: "Organização",
  university: "Universidade",
} as const;
type EntityKind = keyof typeof ENTITY_KINDS;

/**
 * Organisations are drawn with a shape per family of classification (never a colour);
 * companies, universities and people keep the shape of their kind.
 */
const CLASSIFICATION_FAMILIES = {
  parliament: "parliament",
  parliamentary_group: "parliament",
  parliamentary_committee: "parliament",
  parliamentary_body: "parliament",
  parliamentary_delegation: "parliament",
  friendship_group: "parliament",
  government: "government",
  government_department: "government",
  government_office: "government",
  public_body: "public",
  regulator: "public",
  municipality: "public",
  parish: "public",
  eu_institution: "public",
  state_company: "other",
  company: "other",
  foundation: "other",
  association: "other",
  cooperative: "other",
  higher_education: "other",
  international_organisation: "other",
  other: "other",
} as const;
type Classification = keyof typeof CLASSIFICATION_FAMILIES;
type Family = (typeof CLASSIFICATION_FAMILIES)[Classification];

const DATE_PRECISIONS = ["day", "month", "year"] as const;
type DatePrecision = (typeof DATE_PRECISIONS)[number];

const TEMPORAL_STATUSES = {
  current: "Em curso",
  ended: "Terminada",
  unknown: "Desconhecida",
} as const;
type TemporalStatus = keyof typeof TEMPORAL_STATUSES;

type EntityNode = {
  id: string;
  label: string;
  kind: EntityKind;
  classification: Classification | "";
  classificationLabel: string;
  /** Shape family of an organisation; null for other kinds. */
  family: Family | null;
  url: string;
  connections: number;
};
/** "+N entidades": counterparts of one event kind beyond those drawn. */
type MoreNode = {
  id: string;
  label: string;
  kind: "more";
  eventKind: EventKind;
  eventKindLabel: string;
  count: number;
  url: string;
};
type NodeData = EntityNode | MoreNode;

type RelationshipEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: RelationshipKind;
  role: string;
  term: string | null;
  start: string | null;
  end: string | null;
  startPrecision: DatePrecision;
  endPrecision: DatePrecision;
  temporalStatus: TemporalStatus;
  url: string;
};
/** Published money or contact records between the centre and one counterpart, aggregated. */
type EventEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: "events";
  eventKind: EventKind;
  eventKindLabel: string;
  count: number;
  amount: string | null;
  first: string | null;
  last: string | null;
  entityRole: string;
  counterpartRole: string;
  url: string;
};
type MoreEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: "more";
  eventKind: EventKind;
  eventKindLabel: string;
  count: number;
  url: string;
};
type EdgeData = RelationshipEdge | EventEdge | MoreEdge;
type GraphPayload = {
  nodes: NodeData[];
  edges: EdgeData[];
  truncated: boolean;
};
type Palette = {
  ink: string;
  paper: string;
  surface: string;
  muted: string;
  rule: string;
  mark: string;
  kinds: Record<RelationshipKind, string>;
};

// Mirror graph_data.py: relationship edges, event edges and one "+N" node per event kind.
const RELATIONSHIP_EDGE_LIMIT = 100;
const EVENT_EDGE_LIMIT = 15;
const PAYLOAD_NODE_LIMIT = 2 * RELATIONSHIP_EDGE_LIMIT + 1 + EVENT_EDGE_LIMIT + EVENT_KINDS.length;
const PAYLOAD_EDGE_LIMIT = RELATIONSHIP_EDGE_LIMIT + EVENT_EDGE_LIMIT + EVENT_KINDS.length;
const CANVAS_NODE_LIMIT = 250;
const CANVAS_EDGE_LIMIT = 300;
const EXPANDABLE_LIMIT = 60;
const LABEL_ALL_LIMIT = 30;
const MAX_CONNECTIONS = 1_000_000;
const MAX_RECORDS = 100_000_000;
const FIT_PADDING = 32;
const FIT_PADDING_RATIO = 0.04;
/** Canvases narrower than this width/height ratio draw small maps as a tall ellipse. */
const NARROW_ASPECT = 1.1;
/** text-max-width of labels on narrow canvases (the wide default is 130px). */
const COMPACT_LABEL_WIDTH = 100;
/** Small maps would otherwise be blown up to maxZoom when fitted. */
const FIT_MAX_ZOOM = 1.25;
const FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const AMOUNT = /^\d{1,16}\.\d{2}$/;
const GRAPH_PATH = /^\/entidades\/[^/]+\/grafo\/$/;
const ENTITY_PATH = /^\/entidades\/[^/]+\/$/;
const EVENTS_PATH = /^\/entidades\/[^/]+\/eventos\/$/;

const EMPTY_MESSAGE =
  "Não há ligações públicas para desenhar nesta vista. A ausência de dados não demonstra a inexistência de relações.";
const ERROR_MESSAGE = "Não foi possível carregar o mapa. Consulte as mesmas ligações na lista acima.";
const TRUNCATED_NOTE = "Mapa limitado às primeiras 100 ligações desta vista; a lista completa está acima, com paginação.";
const CAPPED_NOTE = `O mapa atingiu o limite de ${CANVAS_NODE_LIMIT} entidades ou ${CANVAS_EDGE_LIMIT} ligações; algumas ligações não foram acrescentadas.`;
const TOO_MANY_NOTE = "Demasiadas ligações para expandir aqui — abra o perfil.";

const collator = new Intl.Collator("pt-PT", { sensitivity: "base", numeric: true });
const numberFormat = new Intl.NumberFormat("pt-PT");
const euroFormat = new Intl.NumberFormat("pt-PT", { style: "currency", currency: "EUR" });

// ---------------------------------------------------------------------------
// Payload validation

function isText(value: unknown, limit = 1000): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= limit;
}

function isEntityKind(value: unknown): value is EntityKind {
  return typeof value === "string" && Object.hasOwn(ENTITY_KINDS, value);
}

function isOneOf<T extends string>(values: readonly T[], value: unknown): value is T {
  return typeof value === "string" && (values as readonly string[]).includes(value);
}

function isClassification(value: unknown): value is Classification {
  return typeof value === "string" && Object.hasOwn(CLASSIFICATION_FAMILIES, value);
}

function isTemporalStatus(value: unknown): value is TemporalStatus {
  return typeof value === "string" && Object.hasOwn(TEMPORAL_STATUSES, value);
}

function isOptionalDate(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && ISO_DATE.test(value));
}

function isConnectionCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAX_CONNECTIONS;
}

function isRecordCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= MAX_RECORDS;
}

const PUBLIC_PATHS = {
  entity: ENTITY_PATH,
  evidence: /^\/evidencias\//,
  events: EVENTS_PATH,
} as const;

function publicUrl(value: unknown, page: keyof typeof PUBLIC_PATHS): string {
  if (!isText(value, 2048)) throw new Error("Invalid public URL");
  const url = new URL(value, window.location.origin);
  if (
    url.origin !== window.location.origin ||
    url.username ||
    url.password ||
    !PUBLIC_PATHS[page].test(url.pathname)
  ) {
    throw new Error("Invalid public URL");
  }
  return url.href;
}

function dataOf(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || !("data" in value) || typeof value.data !== "object" || value.data === null) {
    throw new Error("Invalid element");
  }
  return value.data as Record<string, unknown>;
}

function parseNode(value: unknown, ids: Set<string>): NodeData {
  const data = dataOf(value);
  const { id, label, kind, url } = data;
  if (!isText(id, 100) || ids.has(id) || !isText(label)) throw new Error("Invalid node data");
  ids.add(id);
  if (kind === "more") {
    const { event_kind: eventKind, event_kind_label: eventKindLabel, count } = data;
    if (!isOneOf(EVENT_KINDS, eventKind) || !isText(eventKindLabel, 80) || !isRecordCount(count)) {
      throw new Error("Invalid node data");
    }
    return { id, label, kind, eventKind, eventKindLabel, count, url: publicUrl(url, "events") };
  }
  const { classification, classification_label: classificationLabel, connections } = data;
  if (!isEntityKind(kind) || !isConnectionCount(connections)) throw new Error("Invalid node data");
  if (kind === "person") {
    // People carry no classification.
    if (classification !== "" || classificationLabel !== "") throw new Error("Invalid node data");
    return { id, label, kind, classification: "", classificationLabel: "", family: null, url: publicUrl(url, "entity"), connections };
  }
  if (!isClassification(classification) || !isText(classificationLabel, 160)) throw new Error("Invalid node data");
  return {
    id,
    label,
    kind,
    classification,
    classificationLabel,
    family: kind === "organisation" ? CLASSIFICATION_FAMILIES[classification] : null,
    url: publicUrl(url, "entity"),
    connections,
  };
}

function parseEdge(value: unknown, ids: Set<string>, nodes: Map<string, NodeData>): EdgeData {
  const data = dataOf(value);
  const { id, source, target, label, kind, url } = data;
  if (!isText(id, 100) || ids.has(id) || !isText(source, 100) || !isText(target, 100) || !isText(label)) {
    throw new Error("Invalid edge data");
  }
  const from = nodes.get(source);
  const to = nodes.get(target);
  // Only aggregate edges reach a "+N" node, and every edge leaves an entity.
  if (!from || !to || from.kind === "more" || (to.kind === "more") !== (kind === "more")) {
    throw new Error("Invalid edge data");
  }
  ids.add(id);
  if (kind === "more") {
    const { event_kind: eventKind, event_kind_label: eventKindLabel, count } = data;
    if (to.kind !== "more" || eventKind !== to.eventKind || !isText(eventKindLabel, 80) || !isRecordCount(count)) {
      throw new Error("Invalid edge data");
    }
    return { id, source, target, label, kind, eventKind: to.eventKind, eventKindLabel, count, url: publicUrl(url, "events") };
  }
  if (kind === "events") {
    const {
      event_kind: eventKind,
      event_kind_label: eventKindLabel,
      count,
      amount,
      first,
      last,
      entity_role: entityRole,
      counterpart_role: counterpartRole,
    } = data;
    if (
      !isOneOf(EVENT_KINDS, eventKind) ||
      !isText(eventKindLabel, 80) ||
      !isRecordCount(count) ||
      !(amount === null || (typeof amount === "string" && AMOUNT.test(amount))) ||
      !isOptionalDate(first) ||
      !isOptionalDate(last) ||
      !isText(entityRole, 80) ||
      !isText(counterpartRole, 80)
    ) {
      throw new Error("Invalid edge data");
    }
    return {
      id,
      source,
      target,
      label,
      kind,
      eventKind,
      eventKindLabel,
      count,
      amount,
      first,
      last,
      entityRole,
      counterpartRole,
      url: publicUrl(url, "events"),
    };
  }
  const {
    role,
    term,
    start,
    end,
    start_precision: startPrecision,
    end_precision: endPrecision,
    temporal_status: temporalStatus,
  } = data;
  if (
    !isOneOf(RELATIONSHIP_KINDS, kind) ||
    !(role === "" || isText(role, 240)) ||
    !(term === null || isText(term, 160)) ||
    !isOptionalDate(start) ||
    !isOptionalDate(end) ||
    !isOneOf(DATE_PRECISIONS, startPrecision) ||
    !isOneOf(DATE_PRECISIONS, endPrecision) ||
    !isTemporalStatus(temporalStatus)
  ) {
    throw new Error("Invalid edge data");
  }
  return {
    id,
    source,
    target,
    label,
    kind,
    role,
    term,
    start,
    end,
    startPrecision,
    endPrecision,
    temporalStatus,
    url: publicUrl(url, "evidence"),
  };
}

function parseGraph(value: unknown): GraphPayload {
  if (
    typeof value !== "object" ||
    value === null ||
    !("nodes" in value) ||
    !Array.isArray(value.nodes) ||
    !("edges" in value) ||
    !Array.isArray(value.edges) ||
    !("truncated" in value) ||
    value.nodes.length > PAYLOAD_NODE_LIMIT ||
    value.edges.length > PAYLOAD_EDGE_LIMIT ||
    typeof value.truncated !== "boolean"
  ) {
    throw new Error("Invalid graph payload");
  }
  const ids = new Set<string>();
  const nodes: NodeData[] = value.nodes.map((node: unknown) => parseNode(node, ids));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = value.edges.map((edge: unknown) => parseEdge(edge, ids, byId));
  return { nodes, edges, truncated: value.truncated };
}

function graphEndpoint(value: string, base = window.location.origin): URL {
  const endpoint = new URL(value, base);
  if (
    endpoint.origin !== window.location.origin ||
    endpoint.username ||
    endpoint.password ||
    !GRAPH_PATH.test(endpoint.pathname)
  ) {
    throw new Error("Invalid graph endpoint");
  }
  return endpoint;
}

/** The graph endpoint of an entity, carrying the same `?at=` as the page's own map. */
function entityGraphEndpoint(entityUrl: string, at: string | null): URL {
  const entity = new URL(entityUrl, window.location.origin);
  if (entity.origin !== window.location.origin || !ENTITY_PATH.test(entity.pathname)) {
    throw new Error("Invalid entity URL");
  }
  const endpoint = graphEndpoint(`${entity.pathname}grafo/`);
  if (at !== null) endpoint.searchParams.set("at", at);
  return endpoint;
}

async function fetchGraph(endpoint: URL): Promise<GraphPayload> {
  const response = await fetch(endpoint, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    redirect: "error",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("Graph request failed");
  return parseGraph(await response.json());
}

// ---------------------------------------------------------------------------
// Presentation helpers

function cssColour(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  const value = styles.getPropertyValue(name).trim();
  const hex = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;
  const rgb = /^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+)\s*)?\)$/i;
  return hex.test(value) || rgb.test(value) ? value : fallback;
}

function readPalette(): Palette {
  const styles = getComputedStyle(document.documentElement);
  const kinds = {} as Record<RelationshipKind, string>;
  for (const kind of RELATIONSHIP_KINDS) {
    kinds[kind] = cssColour(styles, `--kind-${kind}`, KIND_FALLBACK_COLOURS[kind]);
  }
  return {
    ink: cssColour(styles, "--ink", "#16181D"),
    paper: cssColour(styles, "--paper", "#F5F2EA"),
    surface: cssColour(styles, "--surface", "#FFFDF8"),
    muted: cssColour(styles, "--muted", "#595C62"),
    rule: cssColour(styles, "--rule", "#D8D2C3"),
    mark: cssColour(styles, "--mark", "#F2D14B"),
    kinds,
  };
}

function stylesheet(palette: Palette): StylesheetJson {
  return [
    {
      selector: "node",
      style: {
        shape: "ellipse",
        width: 18,
        height: 18,
        "background-color": palette.ink,
        "border-width": 0,
        label: "",
        color: palette.ink,
        "font-family": FONT_FAMILY,
        "font-size": 13,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 5,
        "text-wrap": "wrap",
        "text-max-width": "130px",
        "text-background-color": palette.paper,
        "text-background-opacity": 0.92,
        "text-background-padding": "2px",
      },
    },
    { selector: "node[kind = 'company']", style: { shape: "rectangle", width: 16, height: 16 } },
    { selector: "node[kind = 'organisation']", style: { shape: "diamond", width: 22, height: 22 } },
    { selector: "node[family = 'parliament']", style: { shape: "hexagon", width: 22, height: 20 } },
    { selector: "node[family = 'government']", style: { shape: "pentagon", width: 22, height: 22 } },
    { selector: "node[family = 'public']", style: { shape: "rhomboid", width: 24, height: 16 } },
    {
      selector: "node[kind = 'more']",
      style: {
        shape: "round-rectangle",
        width: 14,
        height: 14,
        "background-color": palette.paper,
        "border-width": 1.5,
        "border-style": "dashed",
        "border-color": palette.ink,
        label: "data(label)",
        "font-size": 12,
        "font-style": "italic",
      },
    },
    { selector: "node[kind = 'university']", style: { shape: "triangle", width: 21, height: 19 } },
    { selector: "node.label-above", style: { "text-valign": "top", "text-margin-y": -5 } },
    { selector: "node.compact", style: { "text-max-width": `${COMPACT_LABEL_WIDTH}px` } },
    {
      selector: "node.labelled, node.hover, node.chosen, node.centre",
      style: { label: "data(label)" },
    },
    {
      selector: "node.expanded",
      style: { "outline-width": 2, "outline-style": "dashed", "outline-color": palette.ink, "outline-offset": 3 },
    },
    {
      selector: "node.centre",
      style: {
        width: 34,
        height: 34,
        "border-width": 4,
        "border-color": palette.mark,
        "border-position": "outside",
        color: palette.paper,
        "font-size": 14,
        "font-weight": 600,
        "text-margin-y": 9,
        "text-max-width": "180px",
        "text-background-color": palette.ink,
        "text-background-opacity": 1,
        "text-background-padding": "4px",
        "z-index": 10,
      },
    },
    { selector: "node.centre[kind = 'company']", style: { width: 30, height: 30 } },
    { selector: "node.centre[kind = 'university']", style: { width: 38, height: 34 } },
    { selector: "node.centre[kind = 'organisation']", style: { width: 40, height: 40 } },
    {
      selector: "edge",
      style: {
        width: 2,
        "curve-style": "bezier",
        "control-point-step-size": 16,
        "line-color": palette.muted,
        "target-arrow-shape": "triangle",
        "target-arrow-color": palette.muted,
        "arrow-scale": 0.8,
        label: "",
        color: palette.ink,
        "font-family": FONT_FAMILY,
        "font-size": 12,
        "text-rotation": "autorotate",
        "text-background-color": palette.paper,
        "text-background-opacity": 0.92,
        "text-background-padding": "2px",
      },
    },
    ...RELATIONSHIP_KINDS.map((kind) => ({
      selector: `edge[kind = '${kind}']`,
      style: { "line-color": palette.kinds[kind], "target-arrow-color": palette.kinds[kind] },
    })),
    {
      selector: "edge.undocumented",
      style: { "line-style": "dashed", "line-dash-pattern": [6, 4], opacity: 0.75 },
    },
    {
      // Money and contact totals: neutral ink, dotted, always labelled; no direction.
      selector: "edge[kind = 'events']",
      style: {
        width: 1.5,
        "line-color": palette.ink,
        "line-style": "dotted",
        "target-arrow-shape": "none",
        label: "data(caption)",
        "font-size": 11,
      },
    },
    {
      selector: "edge[kind = 'more']",
      style: { width: 1, "line-color": palette.muted, "line-style": "dotted", "target-arrow-shape": "none" },
    },
    { selector: "edge.hover, edge.chosen", style: { label: "data(caption)" } },
    { selector: "edge.hover", style: { width: 3 } },
    { selector: ".faded", style: { opacity: 0.25 } },
    {
      selector: "node.chosen",
      style: {
        "z-index": 20,
        "underlay-color": palette.mark,
        "underlay-padding": 7,
        "underlay-opacity": 1,
        "underlay-shape": "ellipse",
      },
    },
    {
      selector: "edge.chosen",
      style: {
        width: 4,
        opacity: 1,
        "underlay-color": palette.mark,
        "underlay-padding": 4,
        "underlay-opacity": 1,
        "z-index": 20,
      },
    },
  ];
}

const DATE_PATTERNS: Record<DatePrecision, string> = { day: "$3/$2/$1", month: "$2/$1", year: "$1" };

/** ISO YYYY-MM-DD (validated) → DD/MM/YYYY, MM/YYYY or YYYY, as precise as the source. */
function formatDay(value: string, precision: DatePrecision = "day"): string {
  return value.replace(/^(\d{4})-(\d{2})-(\d{2})$/, DATE_PATTERNS[precision]);
}

function formatDates(edge: RelationshipEdge): string {
  if (edge.start === null && edge.end === null) return "datas não documentadas";
  const start = edge.start === null ? "início não documentado" : formatDay(edge.start, edge.startPrecision);
  const end = edge.end === null ? "fim não documentado" : formatDay(edge.end, edge.endPrecision);
  return `${start} — ${end}`;
}

function formatEventDates(edge: EventEdge): string {
  if (edge.first === null || edge.last === null) return "datas não indicadas na fonte";
  const first = formatDay(edge.first);
  return edge.first === edge.last ? first : `${first} — ${formatDay(edge.last)}`;
}

/** What an edge says on the map: role and term for relationships, totals for records. */
function caption(edge: EdgeData): string {
  if (edge.kind === "more") return "";
  if (edge.kind === "events") return edge.label;
  return [edge.role || edge.label, edge.term].filter(Boolean).join(" · ");
}

function plural(count: number, one: string, many: string): string {
  return `${numberFormat.format(count)} ${count === 1 ? one : many}`;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function link(href: string, text: string, className?: string): HTMLAnchorElement {
  const anchor = document.createElement("a");
  if (className) anchor.className = className;
  anchor.href = href;
  anchor.textContent = text;
  return anchor;
}

// ---------------------------------------------------------------------------
// Deterministic layout

type RingOptions = {
  /** Angle of the first slot (full circle) or of the arc's middle (partial arc), in radians. */
  angle: number;
  /** Angular extent: 2π for a full ring, less for an arc. */
  spread: number;
  innerRadius: number;
  /** Minimum distance between neighbouring nodes on the same ring. */
  spacing: number;
  ringGap: number;
  maxRings: number;
  /** Leave one empty slot between groups so sectors read apart (only worth it on dense maps). */
  groupGaps: boolean;
  /** Stretch the rings vertically to at least this radius (an ellipse for tall canvases). */
  radiusY?: number;
  /** Full rings only: turn by half a slot if that keeps nodes off the centre's horizontal. */
  avoidHorizontal?: boolean;
};

/**
 * Groups counterparts of `hub` by the first relationship kind (in the shared kind order)
 * that links them to it, then by event kind (each "+N" node after its kind), each group
 * sorted by label, so same-kind neighbours share a sector.
 */
function groupAround(hub: string, candidates: NodeData[], edges: EdgeData[]): string[][] {
  const rank = new Map<string, number>();
  for (const edge of edges) {
    const other = edge.source === hub ? edge.target : edge.target === hub ? edge.source : null;
    if (other === null || other === hub) continue;
    const order =
      edge.kind === "events" || edge.kind === "more"
        ? RELATIONSHIP_KINDS.length + 2 * EVENT_KINDS.indexOf(edge.eventKind) + (edge.kind === "more" ? 1 : 0)
        : RELATIONSHIP_KINDS.indexOf(edge.kind);
    rank.set(other, Math.min(rank.get(other) ?? order, order));
  }
  const ranked = candidates
    .map((node) => ({ node, rank: rank.get(node.id) ?? RELATIONSHIP_KINDS.length + 2 * EVENT_KINDS.length }))
    .sort(
      (a, b) =>
        a.rank - b.rank ||
        collator.compare(a.node.label, b.node.label) ||
        (a.node.id < b.node.id ? -1 : a.node.id > b.node.id ? 1 : 0),
    );
  const groups: string[][] = [];
  let previous = -1;
  for (const { node, rank: order } of ranked) {
    if (order !== previous) groups.push([]);
    groups[groups.length - 1].push(node.id);
    previous = order;
  }
  return groups;
}

/**
 * Places grouped ids on concentric rings around `origin`. Angles are assigned in order
 * (optionally one empty slot between groups), so each group occupies a contiguous sector;
 * consecutive slots are staggered across rings, so capacity grows with the number of rings
 * while the sectors stay intact.
 */
function ringPositions(origin: Position, groups: string[][], options: RingOptions): Map<string, Position> {
  const positions = new Map<string, Position>();
  const count = groups.reduce((total, group) => total + group.length, 0);
  if (count === 0) return positions;
  const fullCircle = options.spread >= 2 * Math.PI - 1e-6;
  const gaps =
    options.groupGaps && groups.length > 1 ? (fullCircle ? groups.length : groups.length - 1) : 0;
  let slots = count + gaps;
  const stepFor = (total: number) =>
    fullCircle ? options.spread / total : total > 1 ? options.spread / (total - 1) : 0;
  let step = stepFor(slots);
  let rings = 1;
  if (step > 0) {
    rings = Math.min(options.maxRings, Math.max(1, Math.ceil(options.spacing / (step * options.innerRadius))));
    if (fullCircle && rings > 1) {
      // Round up to a whole number of turns so the last and first slots never share a ring.
      slots = Math.ceil(slots / rings) * rings;
      step = stepFor(slots);
    }
  }
  const radius =
    step > 0 ? Math.max(options.innerRadius, options.spacing / (step * rings)) : options.innerRadius;
  let first = fullCircle ? options.angle : options.angle - options.spread / 2;
  if (fullCircle && options.avoidHorizontal && step > 0) {
    // A node level with the centre puts its label beside the centre's own label: turn the
    // ring by half a slot when that keeps every slot further from the horizontal.
    const clearance = (offset: number) =>
      Math.min(...Array.from({ length: slots }, (_, index) => Math.abs(Math.sin(first + offset + index * step))));
    if (clearance(step / 2) > clearance(0) + 1e-9) first += step / 2;
  }
  const stretchY = options.radiusY === undefined ? 1 : Math.max(1, options.radiusY / radius);
  let slot = 0;
  for (const group of groups) {
    for (const id of group) {
      const angle = first + slot * step;
      const distance = radius + (slot % rings) * options.ringGap;
      positions.set(id, {
        x: origin.x + distance * Math.cos(angle),
        y: origin.y + distance * Math.sin(angle) * stretchY,
      });
      slot += 1;
    }
    if (options.groupGaps) slot += 1;
  }
  return positions;
}

/**
 * Rendered room the fitted map may use around a canvas edge: 32px, tighter on narrow
 * (mobile) canvases so labels keep their size.
 */
function fitPadding(width: number): number {
  return Math.max(12, Math.min(FIT_PADDING, width * FIT_PADDING_RATIO));
}

/**
 * `frame` is the canvas size when a small map is drawn on a tall, narrow canvas: the ring
 * becomes an ellipse filling the canvas (at zoom ≈ 1), with room left for padding, half a
 * compact label at the sides and a label block at the top and bottom.
 */
function centreLayout(
  centreId: string,
  payload: GraphPayload,
  frame: { width: number; height: number } | null,
): Map<string, Position> {
  const counterparts = payload.nodes.filter((node) => node.id !== centreId);
  const groups = groupAround(centreId, counterparts, payload.edges);
  const common = { angle: -Math.PI / 2, spread: 2 * Math.PI };
  let options: RingOptions;
  if (payload.nodes.length > LABEL_ALL_LIMIT) {
    options = { ...common, innerRadius: 170, spacing: 44, ringGap: 46, maxRings: 8, groupGaps: true };
  } else if (frame) {
    const padding = fitPadding(frame.width);
    options = {
      ...common,
      innerRadius: Math.max(80, frame.width / 2 - padding - COMPACT_LABEL_WIDTH / 2),
      spacing: COMPACT_LABEL_WIDTH + 12,
      ringGap: 56,
      maxRings: counterparts.length > 12 ? 2 : 1,
      groupGaps: false,
      avoidHorizontal: true,
      radiusY: frame.height / 2 - padding - 60,
    };
  } else {
    // Small maps label every node: keep the ring tight so labels stay legible at fit, and let
    // relationship colour (not empty slots) separate the sectors. Spacing exceeds
    // text-max-width so side-by-side labels (e.g. the bottom pair) never touch.
    options = {
      ...common,
      innerRadius: 110,
      spacing: 150,
      ringGap: 70,
      maxRings: counterparts.length > 12 ? 2 : 1,
      groupGaps: false,
      avoidHorizontal: true,
    };
  }
  const positions = ringPositions({ x: 0, y: 0 }, groups, options);
  positions.set(centreId, { x: 0, y: 0 });
  return positions;
}

/** Nudges new positions outward from `origin` until they clear every occupied position. */
function avoidCollisions(origin: Position, placed: Map<string, Position>, occupied: Position[]): void {
  const clearance = 34;
  const taken = [...occupied];
  for (const position of placed.values()) {
    let dx = position.x - origin.x;
    let dy = position.y - origin.y;
    const length = Math.hypot(dx, dy) || 1;
    dx /= length;
    dy /= length;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const clash = taken.some((other) => Math.hypot(other.x - position.x, other.y - position.y) < clearance);
      if (!clash) break;
      position.x += dx * 30;
      position.y += dy * 30;
    }
    taken.push(position);
  }
}

// ---------------------------------------------------------------------------
// Map

export async function mountGraph(container: HTMLElement): Promise<void> {
  const panel = container.closest<HTMLElement>("[data-graph-panel]");
  const statusLine = panel?.querySelector<HTMLElement>("[data-graph-status]");
  if (!panel || !statusLine) return;
  const status: HTMLElement = statusLine;
  const detail = panel.querySelector<HTMLElement>("[data-graph-detail]");
  const emptyDetail = detail?.querySelector<HTMLElement>("[data-graph-empty]") ?? null;
  const buttons = [...panel.querySelectorAll<HTMLButtonElement>("[data-graph-action]")];
  const resetButton = buttons.find((button) => button.dataset.graphAction === "reset") ?? null;

  let cy: Core;
  let base: GraphPayload;
  let centreId: string;
  let at: string | null;
  try {
    const endpoint = graphEndpoint(container.dataset.graphUrl ?? "");
    at = endpoint.searchParams.get("at");
    base = await fetchGraph(endpoint);
    if (base.edges.length === 0) {
      container.dataset.state = "empty";
      status.textContent = EMPTY_MESSAGE;
      return;
    }
    const requested = container.dataset.entityId;
    centreId = base.nodes.some((node) => node.id === requested) ? (requested as string) : base.nodes[0].id;
    cy = cytoscape({
      container,
      elements: [],
      minZoom: 0.2,
      maxZoom: 3,
      userZoomingEnabled: false,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      autounselectify: true,
      style: stylesheet(readPalette()),
    });
  } catch {
    container.dataset.state = "error";
    status.textContent = ERROR_MESSAGE;
    return;
  }

  const nodes = new Map<string, NodeData>();
  const edges = new Map<string, EdgeData>();
  const expanded = new Set<string>();
  let chosen: string | null = null;
  /** Small map on a narrow canvas: ellipse layout and compact labels (decided in `load`). */
  let compact = false;
  let expanding = false;
  let capped = false;
  let notice = "";
  let generation = 0;
  let destroyed = false;

  const nodeDefinition = (data: NodeData, position: Position): ElementDefinition => ({
    group: "nodes",
    data: { ...data },
    position: { ...position },
    // Labels sit on the side facing away from the centre (at the origin), clear of its edges.
    classes: [
      data.id === centreId ? "centre" : position.y < -1 ? "label-above" : "",
      compact ? "compact" : "",
    ].join(" "),
  });
  const edgeDefinition = (data: EdgeData): ElementDefinition => ({
    group: "edges",
    data: { ...data, caption: caption(data) },
    classes:
      data.kind !== "events" && data.kind !== "more" && (data.start === null || data.end === null) ? "undocumented" : "",
  });

  function fitView(): void {
    cy.fit(undefined, fitPadding(container.clientWidth));
    if (cy.zoom() > FIT_MAX_ZOOM) {
      cy.zoom(FIT_MAX_ZOOM);
      cy.center();
    }
  }

  function updateStatus(): void {
    const drawn = [...edges.values()];
    const ties = drawn.filter((edge) => edge.kind !== "events" && edge.kind !== "more").length;
    const totals = drawn.filter((edge) => edge.kind === "events").length;
    const entities = [...nodes.values()].filter((node) => node.kind !== "more").length;
    const counted = [plural(ties, "ligação", "ligações")];
    if (totals > 0) counted.push(plural(totals, "conjunto de registos oficiais", "conjuntos de registos oficiais"));
    const parts = [`${counted.join(", ")} e ${plural(entities, "entidade", "entidades")} no mapa.`];
    if (base.truncated) parts.push(TRUNCATED_NOTE);
    if (capped) parts.push(CAPPED_NOTE);
    if (notice) parts.push(notice);
    status.textContent = parts.join(" ");
  }

  function refreshLabels(): void {
    const all = cy.nodes();
    all.removeClass("labelled");
    if (all.length <= LABEL_ALL_LIMIT) {
      all.addClass("labelled");
    } else if (chosen !== null) {
      const selected = cy.getElementById(chosen);
      (edges.has(chosen) ? selected.connectedNodes() : selected.closedNeighborhood().nodes()).addClass("labelled");
    }
  }

  function counterpartOf(edge: EdgeData): { counterpart: NodeData; other: NodeData } | null {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return null;
    if (edge.source === centreId) return { counterpart: target, other: source };
    if (edge.target === centreId) return { counterpart: source, other: target };
    if (expanded.has(edge.source) && !expanded.has(edge.target)) return { counterpart: target, other: source };
    if (expanded.has(edge.target) && !expanded.has(edge.source)) return { counterpart: source, other: target };
    return { counterpart: target, other: source };
  }

  function kindLabel(text: string, kind: RelationshipKind | null): HTMLElement {
    const label = element("p", "kind-label");
    const swatch = element("span", kind === null ? "swatch swatch-events" : "swatch");
    swatch.setAttribute("aria-hidden", "true");
    if (kind !== null) label.dataset.kind = kind;
    label.append(swatch, document.createTextNode(text));
    return label;
  }

  function facts(rows: [string, string][]): HTMLDListElement {
    const list = element("dl", "map-detail-facts");
    for (const [term, value] of rows) list.append(element("dt", "", term), element("dd", "", value));
    return list;
  }

  function moreDetail(more: MoreNode | MoreEdge): HTMLElement {
    const body = element("div", "map-detail-body");
    body.append(
      kindLabel(more.eventKindLabel, null),
      element("h3", "map-detail-title", more.label),
      element(
        "p",
        "map-detail-meta",
        `Mais ${plural(more.count, "entidade tem", "entidades têm")} registos deste tipo com este perfil; não estão desenhadas no mapa.`,
      ),
    );
    const actions = element("div", "map-detail-actions");
    actions.append(link(more.url, "Consultar todos os registos deste tipo", "text-link"));
    body.append(actions);
    return body;
  }

  function eventDetail(edge: EventEdge, counterpart: NodeData): HTMLElement {
    const body = element("div", "map-detail-body");
    const title = element("h3", "map-detail-title");
    title.append(link(counterpart.url, counterpart.label));
    body.append(kindLabel(edge.eventKindLabel, null), title, element("p", "map-detail-dates", edge.label));
    const rows: [string, string][] = [];
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (source && target) rows.push([source.label, edge.entityRole], [target.label, edge.counterpartRole]);
    rows.push(["Registos", plural(edge.count, "registo publicado", "registos publicados")]);
    if (edge.amount !== null) rows.push(["Montante (soma em euros)", euroFormat.format(Number(edge.amount))]);
    rows.push(["Datas", formatEventDates(edge)]);
    body.append(
      facts(rows),
      element(
        "p",
        "map-detail-note",
        "Os registos mostram que o contrato, apoio ou contacto ocorreu; não indicam influência nem quem decidiu.",
      ),
    );
    const actions = element("div", "map-detail-actions");
    actions.append(
      link(edge.url, "Consultar os registos e as fontes", "text-link"),
      link(counterpart.url, "Abrir perfil", "text-link"),
    );
    body.append(actions);
    return body;
  }

  function edgeDetail(edge: EdgeData): HTMLElement | null {
    if (edge.kind === "more") return moreDetail(edge);
    const ends = counterpartOf(edge);
    if (!ends) return null;
    if (edge.kind === "events") return eventDetail(edge, ends.counterpart);
    const body = element("div", "map-detail-body");
    const title = element("h3", "map-detail-title");
    title.append(link(ends.counterpart.url, ends.counterpart.label));
    body.append(kindLabel(edge.label, edge.kind), title);
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (source && target) {
      // Direction is part of the claim (who holds shares in whom), so it is always stated.
      body.append(element("p", "map-detail-meta", `${source.label} → ${target.label}`));
    }
    body.append(element("p", "map-detail-dates", formatDates(edge)));
    const rows: [string, string][] = [];
    if (edge.role) rows.push(["Cargo ou função (fonte)", edge.role]);
    if (edge.term) rows.push(["Mandato", edge.term]);
    rows.push(["Estado", TEMPORAL_STATUSES[edge.temporalStatus]]);
    body.append(facts(rows));
    const actions = element("div", "map-detail-actions");
    actions.append(
      link(edge.url, "Consultar evidência", "text-link"),
      link(ends.counterpart.url, "Abrir perfil", "text-link"),
    );
    body.append(actions);
    return body;
  }

  function nodeDetail(node: EntityNode): HTMLElement {
    const body = element("div", "map-detail-body");
    const kind = element("p", "entity-label");
    kind.dataset.entityKind = node.kind;
    if (node.family !== null) kind.dataset.entityFamily = node.family;
    const shape = element("span", "shape");
    shape.setAttribute("aria-hidden", "true");
    kind.append(shape, document.createTextNode(node.classificationLabel || ENTITY_KINDS[node.kind]));
    const title = element("h3", "map-detail-title");
    title.append(link(node.url, node.label));
    body.append(
      kind,
      title,
      element("p", "map-detail-meta", plural(node.connections, "ligação publicada", "ligações publicadas")),
    );
    const actions = element("div", "map-detail-actions");
    actions.append(link(node.url, "Abrir perfil", "text-link"));
    if (node.id !== centreId) {
      if (expanded.has(node.id)) {
        body.append(element("p", "map-detail-meta", "As ligações desta entidade já estão no mapa."));
      } else if (node.connections > EXPANDABLE_LIMIT) {
        body.append(element("p", "map-detail-meta", TOO_MANY_NOTE));
      } else if (node.connections > 1) {
        const button = element("button", "button button-quiet", `Mostrar as ligações de ${node.label}`);
        button.type = "button";
        button.dataset.graphExpand = "";
        button.disabled = expanding;
        button.addEventListener("click", () => void expand(node.id));
        actions.append(button);
      }
    }
    body.append(actions);
    return body;
  }

  function renderDetail(): void {
    if (!detail) return;
    detail.querySelector(".map-detail-body")?.remove();
    const edge = chosen === null ? undefined : edges.get(chosen);
    const node = chosen === null ? undefined : nodes.get(chosen);
    const body = edge
      ? edgeDetail(edge)
      : node
        ? node.kind === "more"
          ? moreDetail(node)
          : nodeDetail(node)
        : null;
    if (emptyDetail) emptyDetail.hidden = body !== null;
    if (body) detail.append(body);
  }

  function select(id: string | null): void {
    chosen = id !== null && cy.getElementById(id).nonempty() ? id : null;
    cy.batch(() => {
      cy.elements().removeClass("chosen faded");
      if (chosen !== null) {
        const selected = cy.getElementById(chosen);
        selected.addClass("chosen");
        const keep = edges.has(chosen) ? selected.union(selected.connectedNodes()) : selected.closedNeighborhood();
        cy.elements().difference(keep).addClass("faded");
      }
      refreshLabels();
    });
    renderDetail();
  }

  function load(payload: GraphPayload): void {
    const width = container.clientWidth;
    const height = container.clientHeight;
    compact =
      payload.nodes.length <= LABEL_ALL_LIMIT && width > 0 && height > 0 && width / height < NARROW_ASPECT;
    const positions = centreLayout(centreId, payload, compact ? { width, height } : null);
    nodes.clear();
    edges.clear();
    for (const node of payload.nodes) nodes.set(node.id, node);
    for (const edge of payload.edges) edges.set(edge.id, edge);
    cy.batch(() => {
      cy.elements().remove();
      cy.add([
        ...payload.nodes.map((node) => nodeDefinition(node, positions.get(node.id) ?? { x: 0, y: 0 })),
        ...payload.edges.map(edgeDefinition),
      ]);
    });
    refreshLabels();
  }

  function merge(hubId: string, payload: GraphPayload): number {
    let nodeCount = cy.nodes().length;
    let edgeCount = cy.edges().length;
    const accepted: EdgeData[] = [];
    const incoming = new Map<string, NodeData>();
    const payloadNodes = new Map(payload.nodes.map((node) => [node.id, node]));
    for (const edge of payload.edges) {
      // Expanding adds a neighbour's relationships; money and contact totals stay the centre's.
      if (edge.kind === "events" || edge.kind === "more" || edges.has(edge.id)) continue;
      const missing = [edge.source, edge.target].filter((id) => !nodes.has(id) && !incoming.has(id));
      if (edgeCount + 1 > CANVAS_EDGE_LIMIT || nodeCount + missing.length > CANVAS_NODE_LIMIT) {
        capped = true;
        continue;
      }
      for (const id of missing) {
        const node = payloadNodes.get(id);
        if (node) incoming.set(id, node);
      }
      nodeCount += missing.length;
      edgeCount += 1;
      accepted.push(edge);
    }
    if (accepted.length === 0) return 0;
    const hub = cy.getElementById(hubId);
    const origin = hub.position();
    const centre = cy.getElementById(centreId).position();
    const distance = Math.hypot(origin.x - centre.x, origin.y - centre.y);
    const outward = distance > 1 ? Math.atan2(origin.y - centre.y, origin.x - centre.x) : -Math.PI / 2;
    const newNodes = [...incoming.values()];
    const groups = groupAround(hubId, newNodes, accepted);
    const slots = newNodes.length + Math.max(0, groups.length - 1);
    const labelled = nodes.size + newNodes.length <= LABEL_ALL_LIMIT;
    const placed = ringPositions({ ...origin }, groups, {
      angle: outward,
      spread: Math.min(Math.PI * 0.9, Math.max(0, slots - 1) * (labelled ? 0.9 : 0.35)),
      innerRadius: 130,
      spacing: labelled ? (compact ? COMPACT_LABEL_WIDTH + 12 : 150) : 60,
      ringGap: 44,
      maxRings: labelled ? 1 : 4,
      groupGaps: true,
    });
    avoidCollisions(
      origin,
      placed,
      cy.nodes().map((node) => node.position()),
    );
    for (const node of newNodes) nodes.set(node.id, node);
    for (const edge of accepted) edges.set(edge.id, edge);
    cy.batch(() => {
      cy.add([
        ...newNodes.map((node) => nodeDefinition(node, placed.get(node.id) ?? { ...origin })),
        ...accepted.map(edgeDefinition),
      ]);
    });
    return accepted.length;
  }

  async function expand(id: string): Promise<void> {
    const node = nodes.get(id);
    if (expanding || destroyed || !node || expanded.has(id) || id === centreId) return;
    const started = generation;
    expanding = true;
    notice = `A carregar as ligações de ${node.label}…`;
    updateStatus();
    renderDetail();
    let refit = false;
    try {
      const payload = await fetchGraph(entityGraphEndpoint(node.url, at));
      if (destroyed || started !== generation) return;
      if (!payload.nodes.some((candidate) => candidate.id === id)) throw new Error("Unexpected graph");
      const added = merge(id, payload);
      expanded.add(id);
      cy.getElementById(id).addClass("expanded");
      notice =
        added > 0
          ? `Ligações de ${node.label} acrescentadas ao mapa: ${numberFormat.format(added)}.`
          : `Não há ligações novas de ${node.label} para acrescentar.`;
      if (resetButton) resetButton.hidden = false;
      refit = added > 0;
    } catch {
      if (destroyed || started !== generation) return;
      notice = `Não foi possível mostrar as ligações de ${node.label} — abra o perfil para as consultar.`;
    } finally {
      expanding = false;
      if (!destroyed && started === generation) {
        updateStatus();
        select(chosen);
        if (refit) fitView();
      }
    }
  }

  function reset(): void {
    generation += 1;
    expanded.clear();
    capped = false;
    notice = "";
    chosen = null;
    load(base);
    select(null);
    fitView();
    updateStatus();
    if (resetButton) resetButton.hidden = true;
  }

  load(base);
  select(null);
  fitView();
  updateStatus();
  container.dataset.state = "ready";

  cy.on("tap", "node, edge", (event) => select(event.target.id()));
  cy.on("tap", (event) => {
    if (event.target === cy) select(null);
  });
  cy.on("mouseover", "node, edge", (event) => {
    event.target.addClass("hover");
    container.style.cursor = "pointer";
  });
  cy.on("mouseout", "node, edge", (event) => {
    event.target.removeClass("hover");
    container.style.cursor = "";
  });

  for (const button of buttons) {
    const action = button.dataset.graphAction;
    if (action !== "reset") button.disabled = false;
    button.addEventListener("click", () => {
      if (destroyed) return;
      if (action === "fit") {
        fitView();
      } else if (action === "reset") {
        reset();
      } else if (action === "zoom-in" || action === "zoom-out") {
        cy.zoom({
          level: cy.zoom() * (action === "zoom-in" ? 1.25 : 0.8),
          renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 },
        });
      }
    });
  }

  let hadSize = container.clientWidth > 0 && container.clientHeight > 0;
  const resizeObserver = new ResizeObserver(() => {
    if (destroyed) return;
    cy.resize();
    const hasSize = container.clientWidth > 0 && container.clientHeight > 0;
    if (hasSize && !hadSize) fitView();
    hadSize = hasSize;
  });
  resizeObserver.observe(container);
  window.addEventListener(
    "pagehide",
    (event) => {
      if (event.persisted) return;
      destroyed = true;
      resizeObserver.disconnect();
      cy.destroy();
    },
    { once: true },
  );
}
