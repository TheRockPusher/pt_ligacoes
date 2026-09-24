import cytoscape from "cytoscape";
import type { ElementDefinition } from "cytoscape";

type GraphNode = {
  data: { id: string; label: string; kind: string; url: string };
};
type GraphEdge = {
  data: { id: string; source: string; target: string; label: string; url: string };
};
type GraphPayload = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
};

function isText(value: unknown, limit = 1000): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= limit;
}

function publicUrl(value: unknown, kind: "entity" | "evidence"): string {
  if (!isText(value, 2048)) throw new Error("Invalid public URL");
  const url = new URL(value, window.location.origin);
  const prefix = kind === "entity" ? "/entidades/" : "/evidencias/";
  if (
    url.origin !== window.location.origin ||
    url.username ||
    url.password ||
    !url.pathname.startsWith(prefix)
  ) {
    throw new Error("Invalid public URL");
  }
  return url.href;
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
    value.nodes.length > 201 ||
    value.edges.length > 100 ||
    typeof value.truncated !== "boolean"
  ) {
    throw new Error("Invalid graph payload");
  }
  const ids = new Set<string>();
  const nodes: GraphNode[] = value.nodes.map((node: unknown) => {
    if (typeof node !== "object" || node === null || !("data" in node) || typeof node.data !== "object" || node.data === null) {
      throw new Error("Invalid node");
    }
    const data = node.data;
    if (
      !("id" in data) ||
      !isText(data.id, 100) ||
      ids.has(data.id) ||
      !("label" in data) ||
      !isText(data.label) ||
      !("kind" in data) ||
      !isText(data.kind, 100) ||
      !("url" in data)
    ) {
      throw new Error("Invalid node data");
    }
    ids.add(data.id);
    return {
      data: {
        id: data.id,
        label: data.label,
        kind: data.kind,
        url: publicUrl(data.url, "entity"),
      },
    };
  });
  const nodeIds = new Set(ids);
  const edges: GraphEdge[] = value.edges.map((edge: unknown) => {
    if (typeof edge !== "object" || edge === null || !("data" in edge) || typeof edge.data !== "object" || edge.data === null) {
      throw new Error("Invalid edge");
    }
    const data = edge.data;
    if (
      !("id" in data) ||
      !isText(data.id, 100) ||
      ids.has(data.id) ||
      !("source" in data) ||
      !isText(data.source, 100) ||
      !nodeIds.has(data.source) ||
      !("target" in data) ||
      !isText(data.target, 100) ||
      !nodeIds.has(data.target) ||
      !("label" in data) ||
      !isText(data.label) ||
      !("url" in data)
    ) {
      throw new Error("Invalid edge data");
    }
    ids.add(data.id);
    return {
      data: {
        id: data.id,
        source: data.source,
        target: data.target,
        label: data.label,
        url: publicUrl(data.url, "evidence"),
      },
    };
  });
  return { nodes, edges, truncated: value.truncated };
}

export async function mountGraph(container: HTMLElement): Promise<void> {
  const panel = container.closest<HTMLElement>(".graph-panel");
  const status = panel?.querySelector<HTMLElement>("[data-graph-status]");
  const selection = panel?.querySelector<HTMLAnchorElement>("[data-graph-selection]");
  if (!panel || !status || !selection) return;

  try {
    const endpoint = new URL(container.dataset.graphUrl ?? "", window.location.origin);
    if (
      endpoint.origin !== window.location.origin ||
      !/^\/entidades\/[^/]+\/grafo\/$/.test(endpoint.pathname)
    ) {
      throw new Error("Invalid graph endpoint");
    }
    const response = await fetch(endpoint, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      redirect: "error",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Graph request failed");
    const payload = parseGraph(await response.json());
    if (payload.edges.length === 0) {
      container.dataset.state = "empty";
      status.textContent =
        "Não há ligações públicas para desenhar nesta vista. A ausência de dados não demonstra a inexistência de relações.";
      return;
    }

    const elements: ElementDefinition[] = [...payload.nodes, ...payload.edges];
    const cy = cytoscape({
      container,
      elements,
      minZoom: 0.25,
      maxZoom: 3,
      wheelSensitivity: 0.2,
      userZoomingEnabled: false,
      boxSelectionEnabled: false,
      autounselectify: false,
      layout: {
        name: "concentric",
        animate: false,
        padding: 48,
        nodeDimensionsIncludeLabels: true,
        minNodeSpacing: 40,
        startAngle: container.clientWidth >= 600 ? 0 : Math.PI / 2,
      },
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            "background-color": "#176453",
            width: 28,
            height: 28,
            "border-width": 3,
            "border-color": "#ffffff",
            color: "#253b35",
            "font-size": 12,
            "font-family": "system-ui, sans-serif",
            "text-valign": "bottom",
            "text-margin-y": 8,
            "text-wrap": "wrap",
            "text-max-width": "140px",
            "text-background-color": "#f8faf6",
            "text-background-opacity": 0.95,
            "text-background-padding": "3px",
          },
        },
        {
          selector: "node[kind = 'person']",
          style: { "background-color": "#ac591e" },
        },
        {
          selector: "edge",
          style: {
            width: 2,
            "line-color": "#8eaaa0",
            "target-arrow-color": "#8eaaa0",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
          },
        },
        {
          selector: ":selected",
          style: {
            "background-color": "#1a3029",
            "line-color": "#ac591e",
            "target-arrow-color": "#ac591e",
            "border-color": "#ac591e",
            "border-width": 4,
          },
        },
      ],
    });
    const summary = `${payload.edges.length} ${payload.edges.length === 1 ? "ligação documentada" : "ligações documentadas"}.${payload.truncated ? " Limite de 100 ligações: esta vista não inclui todos os resultados." : ""}`;
    container.dataset.state = "ready";
    status.textContent = summary;
    cy.on("tap", "node, edge", (event) => {
      const element = event.target;
      selection.href = publicUrl(element.data("url"), element.isEdge() ? "evidence" : "entity");
      selection.textContent = element.isEdge()
        ? `Consultar evidência: ${element.data("label")}`
        : `Abrir perfil: ${element.data("label")}`;
      selection.hidden = false;
      status.textContent = `${summary} Seleção: ${element.data("label")}.`;
    });
    cy.on("tap", (event) => {
      if (event.target !== cy) return;
      selection.hidden = true;
      selection.removeAttribute("href");
      status.textContent = summary;
    });
    for (const button of panel.querySelectorAll<HTMLButtonElement>("[data-graph-action]")) {
      button.disabled = false;
      button.addEventListener("click", () => {
        const action = button.dataset.graphAction;
        if (action === "fit") {
          cy.fit(undefined, 48);
        } else {
          cy.zoom({
            level: cy.zoom() * (action === "zoom-in" ? 1.25 : 0.8),
            renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 },
          });
        }
      });
    }
    const resizeObserver = new ResizeObserver(() => cy.resize());
    resizeObserver.observe(container);
    window.addEventListener(
      "pagehide",
      (event) => {
        if (event.persisted) return;
        resizeObserver.disconnect();
        cy.destroy();
      },
      { once: true },
    );
  } catch {
    container.dataset.state = "error";
    status.textContent =
      "Não foi possível carregar o mapa. Consulte as mesmas ligações na lista abaixo.";
  }
}
