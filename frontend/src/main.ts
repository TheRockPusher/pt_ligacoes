import htmx from "htmx.org";
import "./styles.css";

htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;
htmx.config.includeIndicatorStyles = false;
htmx.config.selfRequestsOnly = true;
htmx.config.historyCacheSize = 0;

document.body.addEventListener("htmx:responseError", showSearchError);
document.body.addEventListener("htmx:sendError", showSearchError);

function showSearchError(): void {
  const results = document.querySelector<HTMLElement>("#profile-results");
  if (!results || results.querySelector("[data-search-error]")) return;
  const message = document.createElement("p");
  message.className = "notice form-error";
  message.dataset.searchError = "true";
  message.setAttribute("role", "alert");
  message.textContent =
    "A pesquisa não foi concluída. Os resultados abaixo não foram atualizados. Tente novamente ou recarregue a página.";
  results.prepend(message);
}

const graph = document.querySelector<HTMLElement>("[data-relationship-graph]");
if (graph) {
  import("./graph")
    .then(({ mountGraph }) => mountGraph(graph))
    .catch(() => {
      graph.dataset.state = "error";
      const status = document.querySelector<HTMLElement>("[data-graph-status]");
      if (status) {
        status.textContent =
          "Não foi possível carregar o mapa. Consulte as mesmas ligações na lista abaixo.";
      }
    });
}
